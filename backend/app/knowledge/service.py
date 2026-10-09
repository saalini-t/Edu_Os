"""Knowledge module (future `knowledge-svc`). It never reads `core` tables: callers pass a `Principal`
that already carries the enrollment-derived `allowed_course_ids`."""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, case, delete, func, literal, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, NotFound
from app.models import Chunk, Document

log = logging.getLogger("eduos.knowledge")

STOPWORDS = frozenset("""a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my of on or so than that the their then there these they this to was we were what when where which who why will with would
you your about after again all also any because been before being both between during each few more most other over same some
such through under until up very while should us not no yes""".split())


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: str
    allowed_course_ids: frozenset[uuid.UUID]


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    document_title: str
    course_id: str
    page: int
    chunk_index: int
    text: str
    rank: float
    matched_terms: int = 0
    dense_similarity: float | None = None      # cosine similarity to the query (None if not computed)
    fused_score: float | None = None           # reciprocal-rank-fusion score (hybrid only)
    sources: list[str] = field(default_factory=list)   # which rankers returned it: "fts", "dense"


@dataclass
class SearchResult:
    query: str
    terms: list[str]
    hits: list[SearchHit] = field(default_factory=list)
    n_above_threshold: int = 0
    min_terms: int = 1
    min_sim: float | None = None
    method: str = "postgres_fts"
    mode_requested: str = "fts"
    mode_used: str = "fts"                     # what actually ran: fts | hybrid | dense | none
    fallback_reason: str | None = None         # why a requested mode was not used
    degraded: bool = False                     # e.g. some chunks have no embedding yet
    missing_embeddings: int | None = None
    embedding_model: str | None = None


class Retriever(Protocol):
    """Interface kept stable so Phase 2 can add pgvector + RRF without touching the workflow."""
    def search(self, query: str, principal: Principal, *, course_id: uuid.UUID | None = None,
               top_k: int = 6) -> SearchResult: ...
    def get_chunks(self, chunk_ids: list[uuid.UUID], principal: Principal) -> list[SearchHit]: ...


def hit_supported(h: "SearchHit", need: int, min_sim: float | None) -> bool:
    """A passage supports an answer if it matches enough query terms OR (when enabled) is semantically close."""
    return h.matched_terms >= need or (min_sim is not None and h.dense_similarity is not None
                                       and h.dense_similarity >= min_sim)


def query_terms(text: str) -> list[str]:
    seen: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if len(tok) >= 2 and tok not in STOPWORDS and tok not in seen:
            seen.append(tok)
    return seen[:24]


def _acl(principal: Principal, course_id: uuid.UUID | None):
    courses = principal.allowed_course_ids
    if course_id is not None:
        courses = courses & {course_id}
    return and_(Chunk.course_id.in_(list(courses)) if courses else Chunk.course_id.is_(None),
                or_(Chunk.visibility == "course", Chunk.owner_id == principal.user_id))


class PostgresFtsRetriever:
    def __init__(self, db: Session, min_terms: int, min_sim: float | None = None):
        self.db, self.min_terms, self.min_sim = db, min_terms, min_sim

    def _hit(self, chunk: Chunk, title: str, rank: float, matched: int = 0) -> SearchHit:
        return SearchHit(str(chunk.id), str(chunk.document_id), title, str(chunk.course_id), chunk.page,
                         chunk.chunk_index, chunk.text, float(rank), int(matched))

    def search(self, query: str, principal: Principal, *, course_id: uuid.UUID | None = None,
               top_k: int = 6) -> SearchResult:
        terms = query_terms(query)
        need = max(1, min(self.min_terms, len(terms)))
        result = SearchResult(query=query, terms=terms, min_terms=need, min_sim=self.min_sim)
        if not terms or not principal.allowed_course_ids:
            return result
        tsq = func.to_tsquery("english", " | ".join(terms))  # terms are [a-z0-9]+ only
        rank = func.ts_rank(Chunk.tsv, tsq, 32).label("rank")
        # distinct query terms present in the passage: a far better support signal than the FTS rank
        matched = sum((case((Chunk.tsv.op("@@")(func.to_tsquery("english", t)), 1), else_=0) for t in terms),
                      literal(0)).label("matched")
        stmt = (select(Chunk, Document.title, rank, matched)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.tsv.op("@@")(tsq), _acl(principal, course_id), Document.status == "READY",
                       Chunk.ingestion_version == Document.ingestion_version)
                .order_by(rank.desc(), Document.title, Chunk.page, Chunk.chunk_index, Chunk.id)   # stable tie-break: never random UUIDs
                .limit(top_k))
        for chunk, title, r, m in self.db.execute(stmt):
            h = self._hit(chunk, title, r, m)
            h.sources = ["fts"]
            result.hits.append(h)
        result.n_above_threshold = sum(1 for h in result.hits if hit_supported(h, need, self.min_sim))
        return result

    def matched_terms(self, chunk_ids: list[uuid.UUID], terms: list[str]) -> dict[str, int]:
        """Distinct query terms present in each given chunk (used to annotate dense-only hits)."""
        if not chunk_ids or not terms:
            return {str(c): 0 for c in chunk_ids}
        expr = sum((case((Chunk.tsv.op("@@")(func.to_tsquery("english", t)), 1), else_=0) for t in terms), literal(0))
        rows = self.db.execute(select(Chunk.id, expr).where(Chunk.id.in_(chunk_ids)))
        return {str(cid): int(m) for cid, m in rows}

    def get_chunks(self, chunk_ids: list[uuid.UUID], principal: Principal) -> list[SearchHit]:
        if not chunk_ids:
            return []
        stmt = (select(Chunk, Document.title).join(Document, Document.id == Chunk.document_id)
                .where(Chunk.id.in_(chunk_ids), _acl(principal, None), Document.status == "READY",
                       Chunk.ingestion_version == Document.ingestion_version))
        by_id = {c.id: self._hit(c, t, 0.0) for c, t in self.db.execute(stmt)}
        return [by_id[i] for i in chunk_ids if i in by_id]


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.s = db, settings
        self.storage = Path(settings.storage_dir).resolve()

    # ---------------------------------------------------------------- ingestion
    def _store_path(self, doc_id: uuid.UUID) -> Path:
        p = (self.storage / f"{doc_id}.pdf").resolve()
        if self.storage not in p.parents:  # defense in depth; name is derived from a UUID only
            raise AppError(500, "INTERNAL_ERROR", "Invalid storage path")
        return p

    def ingest_pdf(self, *, owner_id: uuid.UUID, course_id: uuid.UUID, visibility: str, title: str,
                   filename: str, data: bytes) -> Document:
        """Synchronous ingestion: registers the upload and runs its job inline (seed, evaluation, tests, and
        INGESTION_MODE=sync). The asynchronous path uses Ingestion.register_upload + a worker."""
        from app.knowledge.ingestion import Ingestion
        ing = Ingestion(self.db, self.s)
        doc, job, created = ing.register_upload(owner_id=owner_id, course_id=course_id, visibility=visibility,
                                                title=title, filename=filename, data=data)
        if job is not None and job.status == "QUEUED" and ing.claim_job(job.id, "inline"):
            ing.process_job(job.id)
            self.db.refresh(doc)
        return doc

    def _embed_document(self, doc_id: uuid.UUID) -> None:
        """Best effort: a document is searchable by full text as soon as it is READY. If embeddings are not
        configured/available the chunks simply have no vector yet (visible via `missing_embeddings`)."""
        from app.knowledge import vectors
        from app.knowledge.embeddings import get_embedding_provider
        try:
            provider = get_embedding_provider(self.s)
            model = vectors.active_model(self.db) if provider and vectors.vector_available(self.db) else None
            if provider is None or model is None:
                return
            if model.name != provider.model:
                log.warning("embedding model mismatch; not embedding new chunks",
                            extra={"active": model.name, "configured": provider.model})
                return
            vectors.embed_missing(self.db, provider, model, batch_size=self.s.embedding_batch_size, document_id=doc_id)
        except Exception as e:
            self.db.rollback()
            log.warning("embedding skipped", extra={"document_id": str(doc_id), "error": type(e).__name__})

    def delete_document(self, doc: Document, actor_id: uuid.UUID | None = None) -> None:
        """Immediately removes the document, its chunks, embeddings and jobs in ONE transaction (so it can never be
        retrieved afterwards), records a metadata-only audit event, then deletes every stored file of every version."""
        from app.knowledge.ingestion import record_event
        doc_id, version, nchunks = doc.id, doc.ingestion_version, doc.chunk_count
        record_event(self.db, doc_id, "deleted", actor_id, version=version, chunks=nchunks)
        self.db.execute(delete(Document).where(Document.id == doc_id))     # chunks, embeddings, jobs cascade
        self.db.commit()
        for f in self.storage.glob(f"{doc_id}*.pdf"):
            f.unlink(missing_ok=True)

    # ---------------------------------------------------------------- reads
    def get_document(self, doc_id: uuid.UUID) -> Document | None:
        return self.db.get(Document, doc_id)

    def visible_to(self, doc: Document, principal: Principal) -> bool:
        if doc.course_id not in principal.allowed_course_ids:
            return False
        return doc.visibility == "course" or doc.owner_id == principal.user_id

    def list_documents(self, principal: Principal) -> list[Document]:
        if not principal.allowed_course_ids:
            return []
        stmt = (select(Document).where(Document.course_id.in_(list(principal.allowed_course_ids)),
                                       or_(Document.visibility == "course", Document.owner_id == principal.user_id))
                .order_by(Document.created_at.desc()))
        return list(self.db.scalars(stmt))

    def retriever(self) -> Retriever:
        from app.knowledge.retrieval import build_retriever   # single entry point; the workflow never picks a strategy
        return build_retriever(self.db, self.s, self.s.retrieval_mode)
