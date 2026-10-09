"""pgvector storage. The `know.chunk_embeddings` table is created with raw SQL (the `vector` type is optional
infrastructure), so the application and migrations keep working on a PostgreSQL without pgvector (full-text only)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.models import EmbeddingModel

log = logging.getLogger("eduos.vectors")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS know.chunk_embeddings (
    chunk_id uuid NOT NULL REFERENCES know.chunks(id) ON DELETE CASCADE,
    model_id uuid NOT NULL REFERENCES know.embedding_models(id) ON DELETE CASCADE,
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model_id)
)"""


def ensure_vector_support(db: Session) -> bool:
    """Create the extension and table if the server offers pgvector. Idempotent. Returns availability."""
    try:
        with db.begin_nested():
            db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            db.execute(text(CREATE_TABLE))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        log.warning("pgvector unavailable, retrieval stays full-text only", extra={"error": type(e).__name__})
        return False


def vector_available(db: Session) -> bool:
    return bool(db.execute(text(
        "SELECT (SELECT count(*) FROM pg_extension WHERE extname = 'vector') > 0 "
        "AND to_regclass('know.chunk_embeddings') IS NOT NULL")).scalar())


def active_model(db: Session) -> EmbeddingModel | None:
    return db.scalar(select(EmbeddingModel).where(EmbeddingModel.status == "active"))


def register_model(db: Session, name: str, dims: int) -> EmbeddingModel:
    m = db.scalar(select(EmbeddingModel).where(EmbeddingModel.name == name, EmbeddingModel.dims == dims))
    if m is None:
        m = EmbeddingModel(name=name, dims=dims, status="building")
        db.add(m)
        db.commit()
    return m


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def missing_count(db: Session, model_id: uuid.UUID) -> int:
    return db.execute(text(
        "SELECT count(*) FROM know.chunks c JOIN know.documents d ON d.id = c.document_id "
        "WHERE d.status = 'READY' AND c.ingestion_version = d.ingestion_version AND NOT EXISTS ("
        "SELECT 1 FROM know.chunk_embeddings e "
        "WHERE e.chunk_id = c.id AND e.model_id = :m)"), {"m": model_id}).scalar() or 0


def embed_missing(db: Session, provider, model: EmbeddingModel, *, batch_size: int = 16,
                  document_id: uuid.UUID | None = None) -> int:
    """Embed chunks that lack a vector for `model`. Idempotent and resumable (ON CONFLICT DO NOTHING)."""
    if provider.model != model.name or provider.dims != model.dims:
        raise ValueError("embedding provider does not match the model record (refusing to mix vectors)")
    total = 0
    while True:
        rows = db.execute(text(
            "SELECT c.id, c.text FROM know.chunks c JOIN know.documents d ON d.id = c.document_id "
            "WHERE d.status = 'READY' AND c.ingestion_version = d.ingestion_version "
            "AND (:doc IS NULL OR c.document_id = :doc) AND NOT EXISTS ("
            "SELECT 1 FROM know.chunk_embeddings e WHERE e.chunk_id = c.id AND e.model_id = :m) "
            "ORDER BY c.document_id, c.chunk_index LIMIT :n"),
            {"doc": document_id, "m": model.id, "n": batch_size}).all()
        if not rows:
            return total
        vecs = provider.embed_documents([r.text for r in rows])
        for r, v in zip(rows, vecs):
            if len(v) != model.dims:
                raise ValueError(f"embedding has {len(v)} dims, model record says {model.dims}")
            db.execute(text("INSERT INTO know.chunk_embeddings (chunk_id, model_id, embedding) "
                            "VALUES (:c, :m, CAST(:v AS vector)) ON CONFLICT DO NOTHING"),
                       {"c": r.id, "m": model.id, "v": vec_literal(v)})
        db.commit()
        total += len(rows)


def activate_model(db: Session, model: EmbeddingModel) -> None:
    """Make `model` the only active model; older models become 'retired' (their vectors stay ineligible for search)."""
    db.execute(update(EmbeddingModel).where(EmbeddingModel.status == "active", EmbeddingModel.id != model.id)
               .values(status="retired"))
    db.execute(update(EmbeddingModel).where(EmbeddingModel.id == model.id)
               .values(status="active", activated_at=datetime.now(timezone.utc)))
    db.commit()
    create_ann_index(db, model)


def create_ann_index(db: Session, model: EmbeddingModel) -> None:
    """Partial HNSW index per model using an expression cast to the model's fixed dimension. At the current corpus
    size the planner will still choose an exact scan; the index exists for when the table grows."""
    dims = int(model.dims)
    name = "ix_chunk_emb_hnsw_" + re.sub(r"[^a-f0-9]", "", model.id.hex)[:12]
    try:
        with db.begin_nested():
            db.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON know.chunk_embeddings "
                            f"USING hnsw ((embedding::vector({dims})) vector_cosine_ops) "
                            f"WHERE model_id = '{model.id}'"))
        db.commit()
    except Exception as e:  # index is an optimisation only
        db.rollback()
        log.warning("could not create HNSW index", extra={"error": type(e).__name__})


def purge_retired(db: Session) -> int:
    r = db.execute(text("DELETE FROM know.chunk_embeddings WHERE model_id IN "
                        "(SELECT id FROM know.embedding_models WHERE status = 'retired')"))
    db.commit()
    return r.rowcount or 0
