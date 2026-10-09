"""Durable, retry-safe document ingestion and lifecycle (ingest / replace / re-index).

* Jobs live in PostgreSQL (`know.ingestion_jobs`); `claim_next` leases one atomically (`FOR UPDATE SKIP LOCKED`).
* A document has an ACTIVE version (`documents.ingestion_version`). A job STAGES the new version's chunks (version
  `target`, invisible to retrieval), embeds them in bounded batches, and only then swaps them in with ONE transaction
  (verify every chunk has its vector + delete the old version + bump the version + mark READY). READY therefore means
  "chunks AND (when embeddings are configured) all their vectors exist"; a crash, retry or failed replacement can never
  leave a partial document searchable, and never touches the live version.
* Document status: QUEUED -> PROCESSING (parse, chunk) -> INDEXING (embedding) -> READY | FAILED. Only READY is searchable.
  Replace / re-index keep the old version READY and searchable the whole time.
* Embedding is resumable and time-sliced: progress is the data itself (chunks without a vector). One claim embeds for at
  most `embed_slice_seconds`, then the job goes back to QUEUED (stage `embedding`) so newer uploads are claimed first.
* Lease: every write by a running job first matches `worker_id` (a per-claim token) and renews the lease in the same
  transaction, so a worker that lost its lease can neither write a batch nor finish the job (`LeaseLost`). A heartbeat
  thread renews the lease while the (blocking) parse runs. Crash recovery is bounded: `reap_expired` + attempt counters.
* Permanent failures (unreadable/encrypted/empty PDF, parser timeout) fail immediately. Transient failures retry with
  exponential backoff up to `job_max_attempts`. Embedding failures count separately (`payload.embed_failures`, reset by
  progress); a document whose embedding keeps failing ends in the explicit terminal state FAILED / EMBEDDING_FAILED.
* `reconcile_embeddings` repairs documents that lack vectors (legacy partial documents, embeddings enabled later).
* Every lifecycle step is appended to `know.document_events` (metadata only, survives deletion)."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.knowledge import vectors
from app.knowledge.chunker import chunk_pages
from app.knowledge.pdf import PdfError, parse_document, probe_pdf
from app.models import Chunk, Document, DocumentEvent, IngestionJob

log = logging.getLogger("eduos.ingestion")
ACTIVE = ("QUEUED", "PROCESSING")
# terminal failures a user may retry (transient causes); the others (encrypted, unreadable, no text...) need a different file
RETRYABLE = frozenset({"EMBEDDING_FAILED", "WORKER_LOST", "MAX_ATTEMPTS_EXCEEDED", "PARSER_TIMEOUT", "PARSER_CRASHED",
                       "PARSER_RESOURCE_LIMIT"})
_FILE_RE = re.compile(r"^[0-9a-f-]{36}(\.v\d+)?\.pdf$")


class LeaseLost(Exception):
    """This worker no longer owns the job (its lease expired and the job was re-queued or re-claimed). Nothing may be written."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_event(db: Session, document_id: uuid.UUID, event: str, actor_id: uuid.UUID | None = None, **meta) -> None:
    """Append a lifecycle audit event (the caller commits). Metadata only: never document text."""
    db.add(DocumentEvent(document_id=document_id, event=event, actor_id=actor_id, meta=meta))


class Ingestion:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.s = db, settings
        self.storage = Path(settings.storage_dir).resolve()

    # ------------------------------------------------------------------ files
    def path(self, name: str) -> Path:
        """Resolve a stored file name. Names are generated server-side; this re-validates them (defense in depth)."""
        if not _FILE_RE.match(name):
            raise AppError(500, "INTERNAL_ERROR", "Invalid storage name")
        p = (self.storage / name).resolve()
        if self.storage not in p.parents:
            raise AppError(500, "INTERNAL_ERROR", "Invalid storage path")
        return p

    def _check_upload(self, data: bytes) -> str:
        if len(data) > self.s.max_upload_bytes:
            raise AppError(413, "FILE_TOO_LARGE", f"File exceeds {self.s.max_upload_bytes} bytes")
        if not data.startswith(b"%PDF-"):
            raise AppError(415, "UNSUPPORTED_MEDIA_TYPE", "Only PDF files are accepted")
        return hashlib.sha256(data).hexdigest()

    def _probe(self, data: bytes) -> int:
        """Admission: valid, unencrypted, within the page budget. Rejected BEFORE anything is stored or queued."""
        try:
            return probe_pdf(data, self.s)
        except PdfError as e:
            raise AppError(422, e.code, str(e), {"max_pages": self.s.max_pdf_pages})

    def active_job(self, doc_id: uuid.UUID) -> IngestionJob | None:
        return self.db.scalar(select(IngestionJob).where(IngestionJob.document_id == doc_id,
                                                         IngestionJob.status.in_(ACTIVE)))

    def latest_job(self, doc_id: uuid.UUID) -> IngestionJob | None:
        return self.db.scalar(select(IngestionJob).where(IngestionJob.document_id == doc_id)
                              .order_by(IngestionJob.created_at.desc()).limit(1))

    # ------------------------------------------------------------------ registration (request path)
    def register_upload(self, *, owner_id: uuid.UUID, course_id: uuid.UUID, visibility: str, title: str,
                        filename: str, data: bytes) -> tuple[Document, IngestionJob | None, bool]:
        """Returns (document, job, created). The same bytes by the same owner in the same course never create a second
        document or a second active job."""
        digest = self._check_upload(data)
        existing = self.db.scalar(select(Document).where(
            Document.owner_id == owner_id, Document.course_id == course_id, Document.sha256 == digest))
        if existing is not None and existing.status != "FAILED":
            return existing, self.active_job(existing.id) or self.latest_job(existing.id), False
        pages = self._probe(data)
        if existing is not None:
            for f in self.storage.glob(f"{existing.id}*.pdf"):       # re-upload of a failed file starts over
                f.unlink(missing_ok=True)
            self.db.delete(existing)
            self.db.flush()
        doc_id = uuid.uuid4()
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).name)[:200] or "upload.pdf"
        name = f"{doc_id}.pdf"
        self.storage.mkdir(parents=True, exist_ok=True)
        self.path(name).write_bytes(data)
        doc = Document(id=doc_id, owner_id=owner_id, course_id=course_id, visibility=visibility, title=title[:200],
                       filename=safe_name, sha256=digest, size_bytes=len(data), status="QUEUED", storage_path=name,
                       page_count=pages)
        job = IngestionJob(document_id=doc_id, kind="ingest", status="QUEUED", max_attempts=self.s.job_max_attempts)
        self.db.add_all([doc, job])
        record_event(self.db, doc_id, "upload_accepted", owner_id, size_bytes=len(data), sha256=digest[:12])
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            self.path(name).unlink(missing_ok=True)
            again = self.db.scalar(select(Document).where(Document.owner_id == owner_id, Document.course_id == course_id,
                                                          Document.sha256 == digest))
            if again is None:
                raise
            return again, self.active_job(again.id) or self.latest_job(again.id), False
        return doc, job, True

    def _require_idle_ready(self, doc: Document) -> None:
        if doc.status != "READY":
            raise AppError(409, "DOCUMENT_NOT_READY", f"Document is {doc.status}; only READY documents can be changed")
        if self.active_job(doc.id) is not None:
            raise AppError(409, "JOB_IN_PROGRESS", "Another ingestion job for this document is still running")

    def register_replacement(self, doc: Document, *, actor_id: uuid.UUID, filename: str, data: bytes) -> IngestionJob:
        """Queue a replacement. The current version stays live and searchable until the new one is fully built."""
        digest = self._check_upload(data)
        self._require_idle_ready(doc)
        if digest == doc.sha256:
            raise AppError(409, "UNCHANGED_CONTENT", "The new file is identical to the current version")
        clash = self.db.scalar(select(Document.id).where(Document.owner_id == doc.owner_id, Document.course_id == doc.course_id,
                                                         Document.sha256 == digest, Document.id != doc.id))
        if clash is not None:
            raise AppError(409, "DUPLICATE_CONTENT", "The same content is already stored as another document")
        pages = self._probe(data)
        target = doc.ingestion_version + 1
        name = f"{doc.id}.v{target}.pdf"
        self.path(name).write_bytes(data)
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).name)[:200] or "upload.pdf"
        job = IngestionJob(document_id=doc.id, kind="replace", status="QUEUED", max_attempts=self.s.job_max_attempts,
                           payload={"new_file": name, "sha256": digest, "size_bytes": len(data), "filename": safe_name,
                                    "pages": pages})
        self.db.add(job)
        record_event(self.db, doc.id, "replace_requested", actor_id, target_version=target, sha256=digest[:12])
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            self.path(name).unlink(missing_ok=True)
            raise AppError(409, "JOB_IN_PROGRESS", "Another ingestion job for this document is still running")
        return job

    def register_reindex(self, doc: Document, *, actor_id: uuid.UUID) -> IngestionJob:
        """Queue a re-index of the stored file (new chunking settings / OCR now enabled). Same atomic swap."""
        self._require_idle_ready(doc)
        job = IngestionJob(document_id=doc.id, kind="reindex", status="QUEUED", max_attempts=self.s.job_max_attempts)
        self.db.add(job)
        record_event(self.db, doc.id, "reindex_requested", actor_id, from_version=doc.ingestion_version)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise AppError(409, "JOB_IN_PROGRESS", "Another ingestion job for this document is still running")
        return job

    def progress(self, doc: Document, job: IngestionJob | None) -> dict:
        """User-facing progress, derived from persisted state only (document, job, and the chunk / vector counts).
        Counts and stage names only: no text, no internal error detail."""
        active = job is not None and job.status in ACTIVE
        staged = (job.payload or {}).get("staged") if job is not None else None
        if doc.status == "FAILED":
            phase = "failed"
        elif active and job.stage == "embedding":
            phase = "embedding"
        elif active and job.status == "PROCESSING":
            phase = job.stage if job.stage in ("parsing", "chunking") else "parsing"
        elif active:
            phase = "queued"
        else:
            phase = "ready" if doc.status == "READY" else {"QUEUED": "queued", "PROCESSING": "parsing", "INDEXING": "embedding"}.get(doc.status, "queued")
        out = {"phase": phase, "pages": doc.page_count, "chunks_total": None, "chunks_embedded": None, "percent": None,
               "reprocessing": bool(active and doc.status == "READY")}
        if phase == "embedding":
            total = staged["chunks"] if staged else doc.chunk_count
            plan = self.embedding_plan()
            if total and plan is not None:
                version = staged["version"] if staged else doc.ingestion_version
                done = total - vectors.missing_for(self.db, plan[1].id, doc.id, version)
                out.update(chunks_total=total, chunks_embedded=done, percent=int(100 * done / total))
        elif phase == "ready":
            out.update(chunks_total=doc.chunk_count, chunks_embedded=doc.chunk_count, percent=100)
        return out

    def set_visibility(self, doc: Document, visibility: str, *, actor_id: uuid.UUID) -> None:
        """private (owner only) <-> course (every enrolled user). Chunks carry a copy of the flag (the ACL filter needs no
        join), so ALL versions of the document change in the same transaction."""
        if doc.visibility == visibility:
            return
        doc.visibility = visibility
        self.db.query(Chunk).filter(Chunk.document_id == doc.id).update({"visibility": visibility}, synchronize_session=False)
        record_event(self.db, doc.id, "visibility_changed", actor_id, to=visibility)
        self.db.commit()

    def register_retry(self, doc: Document, *, actor_id: uuid.UUID) -> IngestionJob:
        """Re-run ingestion of the stored file of a FAILED document (e.g. embedding failed, worker lost)."""
        if doc.status != "FAILED":
            raise AppError(409, "DOCUMENT_NOT_FAILED", f"Document is {doc.status}; only FAILED documents can be retried")
        if doc.error_code not in RETRYABLE:
            raise AppError(409, "NOT_RETRYABLE", f"{doc.error_code} cannot be fixed by retrying; upload a different file")
        if not self.path(doc.storage_path).exists():
            raise AppError(409, "FILE_MISSING", "The stored file is gone; upload the document again")
        job = IngestionJob(document_id=doc.id, kind="ingest", status="QUEUED", max_attempts=self.s.job_max_attempts)
        record_event(self.db, doc.id, "retry_requested", actor_id, previous_error=doc.error_code)
        doc.status, doc.error_code = "QUEUED", None
        self.db.add(job)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise AppError(409, "JOB_IN_PROGRESS", "Another ingestion job for this document is still running")
        return job

    # ------------------------------------------------------------------ worker side
    # a per-claim token makes every claim a distinct lease owner, even for the same worker process
    _CLAIM = ("UPDATE know.ingestion_jobs SET status = 'PROCESSING', worker_id = :w, "
              "attempts = attempts + CASE WHEN stage = 'embedding' THEN 0 ELSE 1 END, "      # embedding slices are not attempts
              "lease_expires_at = now() + make_interval(secs => :lease), "
              "stage = CASE WHEN stage = 'embedding' THEN 'embedding' ELSE 'parsing' END, updated_at = now() ")

    @staticmethod
    def _owner(worker_id: str) -> str:
        return f"{worker_id[:50]}#{uuid.uuid4().hex[:8]}"

    def claim_next(self, worker_id: str) -> IngestionJob | None:
        row = self.db.execute(text(
            self._CLAIM + "WHERE id = (SELECT id FROM know.ingestion_jobs WHERE status = 'QUEUED' AND available_at <= now() "
            "ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id"),
            {"w": self._owner(worker_id), "lease": self.s.job_lease_seconds}).first()
        self.db.commit()
        if not row:
            return None
        job = self.db.get(IngestionJob, row.id)
        self.db.refresh(job)
        return job

    def claim_job(self, job_id: uuid.UUID, worker_id: str) -> bool:
        """Claim one specific QUEUED job (used by synchronous ingestion)."""
        row = self.db.execute(text(self._CLAIM + "WHERE id = :id AND status = 'QUEUED' RETURNING id"),
                              {"w": self._owner(worker_id), "lease": self.s.job_lease_seconds, "id": job_id}).first()
        self.db.commit()
        return row is not None

    # ------------------------------------------------------------------ lease ownership
    def _renew(self, db: Session, job_id: uuid.UUID, me: str, stage: str | None = None) -> None:
        """Ownership check AND lease renewal in one UPDATE: only the current owner matches. Run inside the transaction
        that does the write, it also row-locks the job, so the reaper (SKIP LOCKED) cannot re-queue it mid-write."""
        row = db.execute(text("UPDATE know.ingestion_jobs SET lease_expires_at = now() + make_interval(secs => :l), "
                              "updated_at = now()" + (", stage = :st" if stage else "") +
                              " WHERE id = :id AND worker_id = :w AND status = 'PROCESSING' RETURNING id"),
                         {"l": self.s.job_lease_seconds, "id": job_id, "w": me, **({"st": stage} if stage else {})}).first()
        if row is None:
            raise LeaseLost(str(job_id))

    def _enter_stage(self, job_id: uuid.UUID, me: str, stage: str) -> None:
        self._renew(self.db, job_id, me, stage)
        self.db.commit()

    def _mine(self, job: IngestionJob | None, me: str) -> bool:
        return job is not None and job.status == "PROCESSING" and job.worker_id == me

    @contextmanager
    def _heartbeat(self, job_id: uuid.UUID, me: str):
        """Renew the lease from a second connection while a long blocking step (the parse) runs."""
        stop, engine = threading.Event(), self.db.get_bind()

        def beat() -> None:
            while not stop.wait(max(1.0, self.s.job_lease_seconds / 3)):
                try:
                    with Session(engine) as s2:
                        s2.execute(text("UPDATE know.ingestion_jobs SET lease_expires_at = now() + make_interval(secs => :l) "
                                        "WHERE id = :id AND worker_id = :w AND status = 'PROCESSING'"),
                                   {"l": self.s.job_lease_seconds, "id": job_id, "w": me})
                        s2.commit()
                except Exception:
                    log.warning("lease heartbeat failed", extra={"job_id": str(job_id)})

        t = threading.Thread(target=beat, daemon=True, name="job-heartbeat")
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(5)

    # ------------------------------------------------------------------ recovery
    def reap_expired(self) -> int:
        """Jobs whose worker vanished (lease expired): requeue, or fail once the bounded counter is exhausted. A crash
        during embedding counts in `payload.crashes` (embedding claims are not attempts) and keeps its progress."""
        n = 0
        expired = list(self.db.scalars(select(IngestionJob).where(
            IngestionJob.status == "PROCESSING", IngestionJob.lease_expires_at < _now()).with_for_update(skip_locked=True)))
        for job in expired:
            doc = self.db.get(Document, job.document_id)
            embedding = job.stage == "embedding"
            payload = dict(job.payload or {})
            if embedding:
                payload["crashes"] = payload.get("crashes", 0) + 1
                job.payload = payload
            used = payload["crashes"] if embedding else job.attempts
            if used >= job.max_attempts:
                self._fail(job, doc, "WORKER_LOST", "worker stopped responding and no attempts remain")
            else:
                job.status, job.worker_id, job.lease_expires_at, job.available_at = "QUEUED", None, None, _now()
                job.last_error = "lease expired (worker lost); requeued"
                if doc is not None and job.kind == "ingest" and not embedding and doc.status != "READY":
                    doc.status = "QUEUED"
                record_event(self.db, job.document_id, "job_requeued_after_worker_loss", None, attempt=job.attempts)
                self.db.commit()
            n += 1
        self.db.commit()
        if n:
            log.warning("reaped expired ingestion jobs", extra={"count": n})
        return n

    def embedding_plan(self):
        """(provider, model) when new documents must be embedded before they count as searchable; None when embeddings
        are not configured / not available (the document is then searchable by full text only, as before)."""
        from app.knowledge.embeddings import get_embedding_provider
        provider = get_embedding_provider(self.s)
        if provider is None or not vectors.vector_available(self.db):
            return None
        model = vectors.active_model(self.db)
        if model is None:
            return None
        if model.name != provider.model:
            log.warning("embedding model mismatch; documents will not be embedded",
                        extra={"active": model.name, "configured": provider.model})
            return None
        return provider, model

    def reconcile_embeddings(self, limit: int = 10) -> int:
        """Find documents whose live chunks lack vectors and no job is working on them (legacy partial documents, embeddings
        enabled after upload, a lost job) and queue a resumable repair. The document leaves READY until repaired."""
        plan = self.embedding_plan()
        if plan is None:
            return 0
        ids = self.db.execute(text(
            "SELECT d.id FROM know.documents d WHERE d.status IN ('READY', 'INDEXING') "
            "AND NOT EXISTS (SELECT 1 FROM know.ingestion_jobs j WHERE j.document_id = d.id AND j.status IN ('QUEUED', 'PROCESSING')) "
            "AND EXISTS (SELECT 1 FROM know.chunks c WHERE c.document_id = d.id AND c.ingestion_version = d.ingestion_version "
            "AND NOT EXISTS (SELECT 1 FROM know.chunk_embeddings e WHERE e.chunk_id = c.id AND e.model_id = :m)) "
            "ORDER BY d.updated_at LIMIT :n"), {"m": plan[1].id, "n": limit}).scalars().all()
        queued = 0
        for did in ids:
            try:
                moved = self.db.execute(text("UPDATE know.documents SET status = 'INDEXING', updated_at = now() "
                                             "WHERE id = :d AND status IN ('READY', 'INDEXING') RETURNING id"), {"d": did}).first()
                if moved is None:
                    self.db.rollback()
                    continue
                self.db.add(IngestionJob(document_id=did, kind="embed", status="QUEUED", stage="embedding",
                                         max_attempts=self.s.job_max_attempts))
                record_event(self.db, did, "embedding_repair_queued", None)
                self.db.commit()
                queued += 1
            except IntegrityError:                      # another sweeper queued it first
                self.db.rollback()
        return queued

    # ------------------------------------------------------------------ failure paths
    def _fail(self, job: IngestionJob, doc: Document | None, code: str, message: str) -> None:
        """Mark the job FAILED. For an initial ingest (or a repair) the document becomes FAILED; for replace / re-index the
        LIVE document is left exactly as it was (still READY, still serving the old version). Staged chunks of a failed
        build are removed so nothing partial remains."""
        job.status, job.error_code, job.last_error, job.finished_at = "FAILED", code, message[:300], _now()
        job.lease_expires_at = None
        if doc is not None:
            staged = (job.payload or {}).get("staged")
            if job.kind in ("ingest", "embed"):
                doc.status, doc.error_code = "FAILED", code
            if job.kind == "ingest":
                self.db.query(Chunk).filter(Chunk.document_id == doc.id).delete(synchronize_session=False)
                doc.chunk_count = None
            elif job.kind in ("replace", "reindex") and staged:
                self.db.query(Chunk).filter(Chunk.document_id == doc.id,
                                            Chunk.ingestion_version == staged["version"]).delete(synchronize_session=False)
            if job.kind == "replace":
                new = (job.payload or {}).get("new_file")
                if new:
                    self.path(new).unlink(missing_ok=True)
            record_event(self.db, doc.id, f"{job.kind}_failed", None, error_code=code, attempts=job.attempts)
        self.db.commit()

    def _terminal(self, job_id: uuid.UUID, me: str, code: str, message: str) -> str:
        job = self.db.get(IngestionJob, job_id)
        self.db.refresh(job)
        if not self._mine(job, me):
            return "LOST"
        self._fail(job, self.db.get(Document, job.document_id), code, message)
        return "FAILED"

    def _retry_or_fail(self, job_id: uuid.UUID, me: str, error_name: str) -> str:
        job = self.db.get(IngestionJob, job_id)
        self.db.refresh(job)
        if not self._mine(job, me):
            return "LOST"
        doc = self.db.get(Document, job.document_id)
        if job.attempts >= job.max_attempts:
            self._fail(job, doc, "MAX_ATTEMPTS_EXCEEDED", f"gave up after {job.attempts} attempts; last error: {error_name}")
            log.error("ingestion gave up", extra={"job_id": str(job.id), "error": error_name})
            return "FAILED"
        delay = self.s.job_retry_backoff_seconds * (2 ** (job.attempts - 1))
        job.status, job.worker_id, job.lease_expires_at = "QUEUED", None, None
        job.available_at = _now() + timedelta(seconds=delay)
        job.last_error = f"attempt {job.attempts} failed: {error_name}"
        if doc is not None and job.kind == "ingest":
            doc.status = "QUEUED"
        self.db.commit()
        log.warning("ingestion will retry", extra={"job_id": str(job.id), "attempt": job.attempts, "delay_s": delay})
        return "QUEUED"

    # ------------------------------------------------------------------ the job
    def process_job(self, job_id: uuid.UUID, *, slice_s: float | None = None) -> str:
        """Run one claimed job: build (parse + stage chunks) unless the job is already in its embedding stage, then embed
        for at most `slice_s` seconds (None = to completion, used by synchronous ingestion). Returns DONE | QUEUED |
        FAILED | LOST (lease lost: nothing was written). Never raises on ordinary errors."""
        job = self.db.get(IngestionJob, job_id)
        if job is not None:
            self.db.refresh(job)       # sessions do not expire on commit: re-read what the claim UPDATE wrote
        doc = self.db.get(Document, job.document_id) if job else None
        if doc is not None:
            self.db.refresh(doc)
        if job is None or job.status != "PROCESSING":
            return job.status if job else "MISSING"
        if doc is None:
            self._fail(job, None, "DOCUMENT_MISSING", "document was deleted")
            return "FAILED"
        me = job.worker_id
        try:
            if job.stage != "embedding" and self._build(job, doc, me):
                return "DONE"
        except LeaseLost:
            self.db.rollback()
            return "LOST"
        except PdfError as e:     # deterministic: retrying cannot help
            self.db.rollback()
            return self._terminal(job_id, me, e.code, str(e))
        except IntegrityError:    # e.g. a concurrent upload of the same content: permanent
            self.db.rollback()
            return self._terminal(job_id, me, "DUPLICATE_CONTENT", "same content already stored for this owner and course")
        except Exception as e:    # transient (database hiccup, I/O): retry with backoff, bounded
            self.db.rollback()
            return self._retry_or_fail(job_id, me, type(e).__name__)
        return self._embed_stage(job_id, me, slice_s)

    def _build(self, job: IngestionJob, doc: Document, me: str) -> bool:
        """Parse, chunk and STAGE the new version's chunks (invisible to retrieval until the swap). Returns True when the
        job finished here because no embedding is required."""
        kind, payload = job.kind, dict(job.payload or {})
        if kind == "ingest":
            doc.status = "PROCESSING"
        self._enter_stage(job.id, me, "parsing")
        src = self.path(payload["new_file"] if kind == "replace" else doc.storage_path)
        if not src.exists():
            raise PdfError("FILE_MISSING", "stored file is missing")
        with self._heartbeat(job.id, me):
            extraction = parse_document(src.read_bytes(), self.s, pages=payload.get("pages") or doc.page_count)
        self._enter_stage(job.id, me, "chunking")
        specs = chunk_pages(extraction.texts(), self.s.chunk_words, self.s.chunk_overlap_words)
        if not specs:
            raise PdfError("NO_EXTRACTABLE_TEXT", "No text chunks produced")
        target = doc.ingestion_version if kind == "ingest" else doc.ingestion_version + 1
        # ---- ONE transaction: owner check, drop leftovers of an earlier attempt at this version, stage the new chunks
        self._renew(self.db, job.id, me)
        old = self.db.query(Chunk).filter(Chunk.document_id == doc.id)
        (old if kind == "ingest" else old.filter(Chunk.ingestion_version == target)).delete(synchronize_session=False)
        for sp in specs:
            self.db.add(Chunk(document_id=doc.id, course_id=doc.course_id, owner_id=doc.owner_id,
                              visibility=doc.visibility, page=sp.page, chunk_index=sp.chunk_index, text=sp.text,
                              ingestion_version=target, content_hash=hashlib.sha256(sp.text.encode()).hexdigest()))
        payload["staged"] = {"version": target, "chunks": len(specs), "pages": len(extraction.pages),
                             "report": extraction.report()}
        job.payload = payload
        if self.embedding_plan() is None:           # nothing to wait for: swap in right away (full-text-only deployment)
            self.db.flush()
            self._finalize(job, doc, me)
            return True
        job.stage = "embedding"
        if kind == "ingest":
            doc.status, doc.chunk_count = "INDEXING", len(specs)
        self.db.commit()
        return False

    def _embed_stage(self, job_id: uuid.UUID, me: str, slice_s: float | None) -> str:
        job = self.db.get(IngestionJob, job_id)
        self.db.refresh(job)
        if not self._mine(job, me):
            return "LOST"
        doc = self.db.get(Document, job.document_id)
        if doc is None:
            self._fail(job, None, "DOCUMENT_MISSING", "document was deleted")
            return "FAILED"
        self.db.refresh(doc)
        staged = (job.payload or {}).get("staged")
        version = staged["version"] if staged else doc.ingestion_version
        try:
            plan = self.embedding_plan()
            progressed = False
            if plan is not None:
                provider, model = plan
                progressed = vectors.embed_missing(self.db, provider, model, batch_size=self.s.embedding_batch_size,
                                                   document_id=doc.id, version=version, max_seconds=slice_s,
                                                   guard=lambda db: self._renew(db, job_id, me)) > 0
                if vectors.missing_for(self.db, model.id, doc.id, version) > 0:
                    return self._yield(job_id, me, progressed)
            self.db.refresh(job)
            if self._finalize(job, doc, me):
                return "DONE"
            return self._yield(job_id, me, progressed)
        except LeaseLost:
            self.db.rollback()
            return "LOST"
        except Exception as e:
            self.db.rollback()
            return self._embed_failed(job_id, me, e)

    def _yield(self, job_id: uuid.UUID, me: str, progressed: bool) -> str:
        """End this slice: back to the queue (behind jobs that became available earlier), progress kept in the data."""
        row = self.db.execute(text(
            "UPDATE know.ingestion_jobs SET status = 'QUEUED', worker_id = NULL, lease_expires_at = NULL, available_at = now(), "
            "stage = 'embedding', updated_at = now(), "
            "payload = CASE WHEN :p THEN jsonb_set(payload, '{embed_failures}', '0') ELSE payload END "
            "WHERE id = :id AND worker_id = :w AND status = 'PROCESSING' RETURNING id"),
            {"p": progressed, "id": job_id, "w": me}).first()
        self.db.commit()
        return "QUEUED" if row else "LOST"

    def _embed_failed(self, job_id: uuid.UUID, me: str, exc: Exception) -> str:
        job = self.db.get(IngestionJob, job_id)
        self.db.refresh(job)
        if not self._mine(job, me):
            return "LOST"
        doc = self.db.get(Document, job.document_id)
        payload = dict(job.payload or {})
        fails = payload.get("embed_failures", 0) + 1
        name = type(exc).__name__
        if isinstance(exc, ValueError) or fails >= job.max_attempts:       # ValueError = vector/model mismatch: not transient
            self._fail(job, doc, "EMBEDDING_FAILED", f"embedding failed after {fails} attempt(s): {name}")
            log.error("embedding gave up", extra={"job_id": str(job.id), "error": name})
            return "FAILED"
        payload["embed_failures"] = fails
        job.payload = payload
        job.status, job.worker_id, job.lease_expires_at, job.stage = "QUEUED", None, None, "embedding"
        job.available_at = _now() + timedelta(seconds=self.s.job_retry_backoff_seconds * (2 ** (fails - 1)))
        job.last_error = f"embedding attempt {fails} failed: {name}"
        self.db.commit()
        log.warning("embedding will retry", extra={"job_id": str(job.id), "failures": fails, "error": name})
        return "QUEUED"

    def _finalize(self, job: IngestionJob, doc: Document, me: str) -> bool:
        """The swap, ONE transaction: owner check + row lock, verify every staged chunk has its vector (when embeddings are
        required), delete the old version, bump the version, mark READY, finish the job. False = vectors still missing."""
        kind, payload, old_file = job.kind, dict(job.payload or {}), doc.storage_path
        staged = payload.pop("staged", None)
        version = staged["version"] if staged else doc.ingestion_version
        self._renew(self.db, job.id, me)
        plan = self.embedding_plan()
        if plan is not None and vectors.missing_for(self.db, plan[1].id, doc.id, version) > 0:
            self.db.rollback()
            return False
        if staged:
            self.db.query(Chunk).filter(Chunk.document_id == doc.id,
                                        Chunk.ingestion_version != version).delete(synchronize_session=False)
            doc.ingestion_version = version
            doc.page_count, doc.chunk_count, doc.extraction_report = staged["pages"], staged["chunks"], staged["report"]
        doc.status, doc.error_code = "READY", None
        self.db.query(Chunk).filter(Chunk.document_id == doc.id, Chunk.ingestion_version == version,
                                    Chunk.visibility != doc.visibility).update(
            {"visibility": doc.visibility}, synchronize_session=False)       # sharing may have changed while this job ran
        if kind == "replace":
            doc.sha256, doc.size_bytes = payload["sha256"], payload["size_bytes"]
            doc.filename, doc.storage_path = payload["filename"], payload["new_file"]
        job.payload = payload
        job.status, job.stage, job.error_code, job.last_error = "DONE", "done", None, None
        job.finished_at, job.lease_expires_at = _now(), None
        if kind == "embed":
            record_event(self.db, doc.id, "embeddings_repaired", None, version=version, chunks=doc.chunk_count)
        else:
            rep = staged["report"]
            record_event(self.db, doc.id, {"ingest": "ingested", "replace": "replaced", "reindex": "reindexed"}[kind], None,
                         version=version, chunks=staged["chunks"], ocr_pages=rep["ocr_pages"],
                         failed_pages=[f["page"] for f in rep["failed_pages"]])
        self.db.commit()
        if kind == "replace" and old_file != doc.storage_path:
            self.path(old_file).unlink(missing_ok=True)
        log.info("document processed", extra={"document_id": str(doc.id), "kind": kind, "chunks": doc.chunk_count,
                                              "version": doc.ingestion_version})
        return True
