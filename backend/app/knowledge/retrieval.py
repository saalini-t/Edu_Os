"""Single retrieval entry point. The workflow only sees the `Retriever` interface (knowledge.service); this module decides
whether a query runs as full-text only, dense only (evaluation), or hybrid (full-text + pgvector fused with RRF),
and ALWAYS reports what actually ran (mode_used, fallback_reason, degraded) so traces and logs are truthful."""
from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.knowledge import vectors
from app.knowledge.embeddings import EmbeddingProvider, EmbeddingUnavailable, get_embedding_provider
from app.knowledge.service import (
    PostgresFtsRetriever, Principal, Retriever, SearchHit, SearchResult, hit_supported, query_terms,
)

log = logging.getLogger("eduos.retrieval")


class RetrievalUnavailable(Exception):
    """An explicitly requested mode (dense/hybrid for evaluation) cannot run."""


def rrf(rankings: list[list[str]], k: int) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = sum over rankers of 1 / (k + rank). Ranks are 1-based."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


class HybridRetriever:
    def __init__(self, db: Session, settings: Settings, provider: EmbeddingProvider | None, mode: str = "hybrid"):
        self.db, self.s, self.provider, self.mode = db, settings, provider, mode
        self.fts = PostgresFtsRetriever(db, settings.ret_min_terms, settings.ret_min_sim)

    # -- plan: decide whether dense search can run; never silently mix embedding models
    def _dense_plan(self):
        if self.provider is None:
            return None, "embeddings_not_configured"
        if not vectors.vector_available(self.db):
            return None, "pgvector_unavailable"
        model = vectors.active_model(self.db)
        if model is None:
            return None, "no_active_embedding_model (run: python -m app.knowledge.reindex)"
        try:
            configured_dims = self.provider.dims
        except EmbeddingUnavailable as e:
            return None, f"embedding_model_unavailable: {e}"
        if model.name != self.provider.model or model.dims != configured_dims:
            return None, (f"embedding_model_mismatch: active={model.name}/{model.dims}, "
                          f"configured={self.provider.model}/{configured_dims} (controlled re-embedding required)")
        return model, None

    def _dense(self, query: str, principal: Principal, course_id, model, pool: int) -> list[tuple[str, float]]:
        qv = vectors.vec_literal(self.provider.embed_query(query))
        courses = list(principal.allowed_course_ids if course_id is None else principal.allowed_course_ids & {course_id})
        if not courses:
            return []
        d = int(model.dims)
        stmt = text(
            f"SELECT c.id AS id, 1 - (e.embedding::vector({d}) <=> CAST(:q AS vector({d}))) AS sim "
            "FROM know.chunk_embeddings e JOIN know.chunks c ON c.id = e.chunk_id "
            "JOIN know.documents doc ON doc.id = c.document_id "
            "WHERE e.model_id = :m AND doc.status = 'READY' AND c.ingestion_version = doc.ingestion_version "
            "AND c.course_id IN :courses "
            "AND (c.visibility = 'course' OR c.owner_id = :uid) "
            f"ORDER BY e.embedding::vector({d}) <=> CAST(:q AS vector({d})), doc.title, c.page, c.chunk_index, c.id LIMIT :pool"
        ).bindparams(bindparam("courses", expanding=True))
        rows = self.db.execute(stmt, {"q": qv, "m": model.id, "courses": courses, "uid": principal.user_id,
                                      "pool": pool}).all()
        return [(str(r.id), float(r.sim)) for r in rows]

    def search(self, query: str, principal: Principal, *, course_id: uuid.UUID | None = None,
               top_k: int = 6, need_from: str | None = None) -> SearchResult:
        if not query or not query.strip():
            return SearchResult(query=query or "", terms=[], mode_requested=self.mode, mode_used="none",
                                fallback_reason="empty_query", method="none")
        pool = max(self.s.candidate_pool, top_k)
        model, reason = self._dense_plan()
        if model is None:
            if self.mode == "dense":
                raise RetrievalUnavailable(reason)
            res = self.fts.search(query, principal, course_id=course_id, top_k=top_k, need_from=need_from)
            res.mode_requested, res.mode_used, res.fallback_reason = self.mode, "fts", reason
            if reason and reason != "embeddings_not_configured":
                log.warning("hybrid retrieval degraded to full-text only", extra={"reason": reason})
            return res
        try:
            dense = self._dense(query, principal, course_id, model, pool)
        except Exception as e:   # embedding runtime failure or SQL error: fall back, loudly
            self.db.rollback()
            if self.mode == "dense":
                raise RetrievalUnavailable(f"dense_error: {type(e).__name__}") from e
            log.warning("dense retrieval failed; using full-text only", extra={"error": type(e).__name__})
            res = self.fts.search(query, principal, course_id=course_id, top_k=top_k, need_from=need_from)
            res.mode_requested, res.mode_used, res.fallback_reason = self.mode, "fts", f"dense_error: {type(e).__name__}"
            return res

        terms = query_terms(query)
        basis = query_terms(need_from) if need_from else terms          # see PostgresFtsRetriever.search
        need = max(1, min(self.s.ret_min_terms, len(basis) or len(terms))) if terms else 1
        fts_res = self.fts.search(query, principal, course_id=course_id, top_k=pool, need_from=need_from) if self.mode == "hybrid" else None
        fts_hits = {h.chunk_id: h for h in (fts_res.hits if fts_res else [])}
        sims = dict(dense)
        rankings = [[cid for cid, _ in dense]]
        if fts_res:
            rankings.insert(0, [h.chunk_id for h in fts_res.hits])
        fused = rrf(rankings, self.s.rrf_k)
        # fetch text/title for dense-only hits (ACL re-checked); then order deterministically: by fused score, with ties
        # broken by stable content keys (title, page, chunk index), never by random UUIDs
        missing = [uuid.UUID(c) for c in fused if c not in fts_hits]
        fetched = {h.chunk_id: h for h in self.fts.get_chunks(missing, principal)}
        pool_hits = {c: (fts_hits.get(c) or fetched.get(c)) for c in fused}
        order = sorted((c for c, h in pool_hits.items() if h is not None),   # None: deleted meanwhile / no longer authorised
                       key=lambda c: (-fused[c], pool_hits[c].document_title, pool_hits[c].page, pool_hits[c].chunk_index, c))[:top_k]
        hits: list[SearchHit] = []
        for cid in order:
            h = pool_hits[cid]
            hits.append(replace(h, dense_similarity=sims.get(cid), fused_score=round(fused[cid], 6),
                                sources=[s for s, present in (("fts", cid in fts_hits), ("dense", cid in sims)) if present]))
        need_ids = [uuid.UUID(h.chunk_id) for h in hits if h.chunk_id not in fts_hits]
        for cid, m in self.fts.matched_terms(need_ids, terms).items():
            for h in hits:
                if h.chunk_id == cid:
                    h.matched_terms = m
        n_missing = vectors.missing_count(self.db, model.id)
        res = SearchResult(query=query, terms=terms, hits=hits, min_terms=need, min_sim=self.s.ret_min_sim,
                           method="hybrid_rrf" if self.mode == "hybrid" else "dense", mode_requested=self.mode,
                           mode_used=self.mode, degraded=n_missing > 0, missing_embeddings=n_missing,
                           embedding_model=model.name)
        if n_missing:
            res.fallback_reason = f"{n_missing} chunk(s) have no embedding yet; dense candidates are incomplete"
        res.n_above_threshold = sum(1 for h in hits if hit_supported(h, need, self.s.ret_min_sim))
        return res

    def get_chunks(self, chunk_ids, principal):
        return self.fts.get_chunks(chunk_ids, principal)


def build_retriever(db: Session, settings: Settings, mode: str) -> Retriever:
    if mode == "fts":
        return PostgresFtsRetriever(db, settings.ret_min_terms, settings.ret_min_sim)
    if mode not in ("hybrid", "dense"):
        raise ValueError(f"unknown retrieval mode {mode!r}")
    return HybridRetriever(db, settings, get_embedding_provider(settings), mode)


def eval_index_hook(db: Session, settings: Settings) -> None:
    """Evaluation setup: create pgvector support, register + embed + activate the configured model."""
    provider = get_embedding_provider(settings)
    if provider is None:
        raise RuntimeError("evaluation of dense/hybrid retrieval needs --embedding-provider")
    if not vectors.ensure_vector_support(db):
        raise RuntimeError("pgvector is not available in the evaluation database")
    model = vectors.register_model(db, provider.model, provider.dims)
    vectors.embed_missing(db, provider, model, batch_size=settings.embedding_batch_size)
    vectors.activate_model(db, model)
