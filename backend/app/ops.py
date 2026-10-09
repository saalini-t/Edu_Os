"""Operational read models shared by /readyz and the admin API."""
from __future__ import annotations

from fastapi import FastAPI


def retrieval_info(app: FastAPI) -> dict:
    """Informational only (does not affect readiness): which retrieval mode would actually run."""
    s = app.state.settings
    info: dict = {"configured_mode": s.retrieval_mode, "embedding_provider": s.embedding_provider}
    try:
        from app.knowledge import vectors
        from app.knowledge.retrieval import HybridRetriever
        from app.knowledge.embeddings import get_embedding_provider
        with app.state.session_factory() as db:
            if s.retrieval_mode == "fts":
                info["effective_mode"] = "fts"
                return info
            info["pgvector"] = vectors.vector_available(db)
            model, reason = HybridRetriever(db, s, get_embedding_provider(s))._dense_plan()
            info["effective_mode"] = "hybrid" if model else "fts"
            info["fallback_reason"] = reason
            if model:
                info.update(embedding_model=model.name, dims=model.dims, missing_embeddings=vectors.missing_count(db, model.id))
    except Exception as e:
        info["effective_mode"] = "fts"
        info["error"] = type(e).__name__
    return info
