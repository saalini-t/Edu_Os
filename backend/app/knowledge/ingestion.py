"""Durable, retry-safe document ingestion and lifecycle (ingest / replace / re-index).

* Jobs live in PostgreSQL (`know.ingestion_jobs`); `claim_next` leases one atomically (`FOR UPDATE SKIP LOCKED`).
* A document has an ACTIVE version (`documents.ingestion_version`). Every job builds the NEW version's chunks and swaps
  them in with ONE transaction (delete all old chunks + insert new + bump version + mark READY). A crash, a retry or a
  failed replacement therefore can never leave partial or duplicate chunks, and never touches the live version.
* Old chunks are deleted in that same transaction; their embeddings go with them (ON DELETE CASCADE).
* Permanent failures (unreadable/encrypted/empty PDF, parser timeout) fail immediately. Transient failures retry with
  exponential backoff up to `job_max_attempts`. Jobs whose worker died are re-queued by `reap_expired`.
* Every lifecycle step is appended to `know.document_events` (metadata only, survives deletion)."""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.knowledge.chunker import chunk_pages
from app.knowledge.pdf import PdfError, parse_document
from app.models import Chunk, Document, DocumentEvent, IngestionJob

log = logging.getLogger("eduos.ingestion")
ACTIVE = ("QUEUED", "PROCESSING")
_FILE_RE = re.compile(r"^[0-9a-f-]{36}(\.v\d+)?\.pdf$")


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
        if existing is not None:
            if existing.status != "FAILED":
                return existing, self.active_job(existing.id) or self.latest_job(existing.id), False
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
                       filename=safe_name, sha256=digest, size_bytes=len(data), status="QUEUED", storage_path=name)
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
        target = doc.ingestion_version + 1
        name = f"{doc.id}.v{target}.pdf"
        self.path(name).write_bytes(data)
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).name)[:200] or "upload.pdf"
        job = IngestionJob(document_id=doc.id, kind="replace", status="QUEUED", max_attempts=self.s.job_max_attempts,
                           payload={"new_file": name, "sha256": digest, "size_bytes": len(data), "filename": safe_name})
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

    # ------------------------------------------------------------------ worker side
    _CLAIM = ("UPDATE know.ingestion_jobs SET status = 'PROCESSING', worker_id = :w, attempts = attempts + 1, "
              "lease_expires_at = now() + make_interval(secs => :lease), stage = 'parsing', updated_at = now() ")

    def claim_next(self, worker_id: str) -> IngestionJob | None:
        row = self.db.execute(text(
            self._CLAIM + "WHERE id = (SELECT id FROM know.ingestion_jobs WHERE status = 'QUEUED' AND available_at <= now() "
            "ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id"),
            {"w": worker_id, "lease": self.s.job_lease_seconds}).first()
        self.db.commit()
        if not row:
            return None
        job = self.db.get(IngestionJob, row.id)
        self.db.refresh(job)
        return job

    def claim_job(self, job_id: uuid.UUID, worker_id: str) -> bool:
        """Claim one specific QUEUED job (used by synchronous ingestion)."""
        row = self.db.execute(text(self._CLAIM + "WHERE id = :id AND status = 'QUEUED' RETURNING id"),
                              {"w": worker_id, "lease": self.s.job_lease_seconds, "id": job_id}).first()
        self.db.commit()
        return row is not None

    def reap_expired(self) -> int:
        """Jobs whose worker vanished (lease expired): requeue, or fail once attempts are exhausted."""
        n = 0
        expired = list(self.db.scalars(select(IngestionJob).where(
            IngestionJob.status == "PROCESSING", IngestionJob.lease_expires_at < _now()).with_for_update(skip_locked=True)))
        for job in expired:
            doc = self.db.get(Document, job.document_id)
            if job.attempts >= job.max_attempts:
                self._fail(job, doc, "WORKER_LOST", "worker stopped responding and no attempts remain")
            else:
                job.status, job.worker_id, job.lease_expires_at, job.available_at = "QUEUED", None, None, _now()
                job.last_error = "lease expired (worker lost); requeued"
                if doc is not None and job.kind == "ingest" and doc.status != "READY":
                    doc.status = "QUEUED"
                record_event(self.db, job.document_id, "job_requeued_after_worker_loss", None, attempt=job.attempts)
            n += 1
        self.db.commit()
        if n:
            log.warning("reaped expired ingestion jobs", extra={"count": n})
        return n

    def _extend_lease(self, job: IngestionJob, stage: str) -> None:
        job.stage = stage
        job.lease_expires_at = _now() + timedelta(seconds=self.s.job_lease_seconds)
        self.db.commit()

    def _fail(self, job: IngestionJob, doc: Document | None, code: str, message: str) -> None:
        """Mark the job FAILED. For an initial ingest the document becomes FAILED; for replace/re-index the LIVE document
        is left exactly as it was (still READY, still serving the old version)."""
        job.status, job.error_code, job.last_error, job.finished_at = "FAILED", code, message[:300], _now()
        job.lease_expires_at = None
        if doc is not None:
            if job.kind == "ingest":
                doc.status, doc.error_code = "FAILED", code
            if job.kind == "replace":
                new = (job.payload or {}).get("new_file")
                if new:
                    self.path(new).unlink(missing_ok=True)
            record_event(self.db, doc.id, f"{job.kind}_failed", None, error_code=code, attempts=job.attempts)
        self.db.commit()

    def process_job(self, job_id: uuid.UUID) -> str:
        """Run one claimed job. Returns the resulting job status (DONE | QUEUED | FAILED). Never raises on ordinary errors."""
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
        kind, payload = job.kind, dict(job.payload or {})
        old_file = doc.storage_path
        try:
            if kind == "ingest":
                doc.status = "PROCESSING"
            self._extend_lease(job, "parsing")
            src = self.path(payload["new_file"] if kind == "replace" else doc.storage_path)
            if not src.exists():
                raise PdfError("FILE_MISSING", "stored file is missing")
            extraction = parse_document(src.read_bytes(), self.s)       # sandboxed child process by default
            self._extend_lease(job, "chunking")
            specs = chunk_pages(extraction.texts(), self.s.chunk_words, self.s.chunk_overlap_words)
            if not specs:
                raise PdfError("NO_EXTRACTABLE_TEXT", "No text chunks produced")
            target = 1 if kind == "ingest" else doc.ingestion_version + 1
            # ---- ONE transaction: replace ALL chunks (their embeddings cascade) + bump the active version + finish the job
            self.db.query(Chunk).filter(Chunk.document_id == doc.id).delete(synchronize_session=False)
            for sp in specs:
                self.db.add(Chunk(document_id=doc.id, course_id=doc.course_id, owner_id=doc.owner_id,
                                  visibility=doc.visibility, page=sp.page, chunk_index=sp.chunk_index, text=sp.text,
                                  ingestion_version=target, content_hash=hashlib.sha256(sp.text.encode()).hexdigest()))
            doc.status, doc.error_code, doc.ingestion_version = "READY", None, target
            doc.page_count, doc.chunk_count = len(extraction.pages), len(specs)
            doc.extraction_report = extraction.report()
            if kind == "replace":
                doc.sha256, doc.size_bytes = payload["sha256"], payload["size_bytes"]
                doc.filename, doc.storage_path = payload["filename"], payload["new_file"]
            job.status, job.stage, job.error_code, job.last_error = "DONE", "done", None, None
            job.finished_at, job.lease_expires_at = _now(), None
            rep = extraction.report()
            record_event(self.db, doc.id, {"ingest": "ingested", "replace": "replaced", "reindex": "reindexed"}[kind], None,
                         version=target, chunks=len(specs), ocr_pages=rep["ocr_pages"],
                         failed_pages=[f["page"] for f in rep["failed_pages"]])
            self.db.commit()
            if kind == "replace" and old_file != doc.storage_path:
                self.path(old_file).unlink(missing_ok=True)
        except PdfError as e:     # deterministic: retrying cannot help
            self.db.rollback()
            self._fail_after_rollback(job_id, e.code, str(e))
            return "FAILED"
        except IntegrityError:    # e.g. a concurrent upload of the same content: permanent
            self.db.rollback()
            self._fail_after_rollback(job_id, "DUPLICATE_CONTENT", "same content already stored for this owner and course")
            return "FAILED"
        except Exception as e:    # transient (database hiccup, I/O): retry with backoff, bounded
            self.db.rollback()
            return self._retry_or_fail(job_id, type(e).__name__)
        log.info("document processed", extra={"document_id": str(doc.id), "kind": kind, "chunks": doc.chunk_count,
                                              "version": doc.ingestion_version})
        from app.knowledge.service import KnowledgeService   # best-effort embeddings; READY does not depend on them
        KnowledgeService(self.db, self.s)._embed_document(doc.id)
        return "DONE"

    def _fail_after_rollback(self, job_id: uuid.UUID, code: str, message: str) -> None:
        job = self.db.get(IngestionJob, job_id)
        self._fail(job, self.db.get(Document, job.document_id), code, message)

    def _retry_or_fail(self, job_id: uuid.UUID, error_name: str) -> str:
        job = self.db.get(IngestionJob, job_id)
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
