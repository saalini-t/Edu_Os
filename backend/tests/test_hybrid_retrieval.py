"""Hybrid retrieval (pgvector + full-text + RRF). Uses the deterministic 'hash' embedding stand-in so the tests exercise
storage, access control, fusion, fallbacks and model-mismatch handling without downloading a model."""
import uuid

import pytest
from sqlalchemy import select, text

from app.auth.principal import principal_for
from app.knowledge import vectors
from app.knowledge.embeddings import HashEmbeddingProvider, get_embedding_provider
from app.knowledge.reindex import main as reindex_main
from app.knowledge.retrieval import HybridRetriever, RetrievalUnavailable, build_retriever, rrf
from app.knowledge.service import KnowledgeService
from app.models import Chunk, Course, EmbeddingModel, User
from tests.conftest import SLOW_START_Q, ask, login, make_pdf
from tests.test_ingestion import upload
from tests.test_workflow import trace

Q = "How does TCP slow start grow the congestion window?"


@pytest.fixture
def vec_db(db):
    if not vectors.ensure_vector_support(db):
        pytest.skip("pgvector is not available on this PostgreSQL server")
    db.execute(text("DELETE FROM know.embedding_models"))
    db.commit()
    return db


@pytest.fixture
def hsettings(settings):
    return settings.model_copy(update={"embedding_provider": "hash", "retrieval_mode": "hybrid"})


@pytest.fixture
def indexed(vec_db, hsettings):
    """Seed document embedded with the hash model and activated."""
    p = get_embedding_provider(hsettings)
    m = vectors.register_model(vec_db, p.model, p.dims)
    vectors.embed_missing(vec_db, p, m, batch_size=4)
    vectors.activate_model(vec_db, m)
    return m


def principal(db, email="student1@demo.local"):
    return principal_for(db, db.scalar(select(User).where(User.email == email)))


def cn_id(db):
    return db.scalar(select(Course.id).where(Course.code == "CN101"))


def test_rrf_math_and_ordering():
    s = rrf([["a", "b", "c"], ["c", "a"]], k=60)
    assert s["a"] == pytest.approx(1 / 61 + 1 / 62) and s["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert s["a"] > s["c"] > s["b"]
    assert rrf([], 60) == {} and rrf([["x"]], 1) == {"x": 0.5}


def test_hybrid_returns_fused_results_and_reports_mode(vec_db, hsettings, indexed):
    res = build_retriever(vec_db, hsettings, "hybrid").search(Q, principal(vec_db), course_id=cn_id(vec_db), top_k=6)
    assert res.mode_requested == "hybrid" and res.mode_used == "hybrid" and res.fallback_reason is None
    assert not res.degraded and res.missing_embeddings == 0 and res.embedding_model == "hash-64"
    assert res.hits and all(h.fused_score and h.fused_score > 0 for h in res.hits)
    assert any("fts" in h.sources for h in res.hits) and any("dense" in h.sources for h in res.hits)
    assert [h.fused_score for h in res.hits] == sorted([h.fused_score for h in res.hits], reverse=True)
    assert all(h.dense_similarity is None or -1.0 <= h.dense_similarity <= 1.0 for h in res.hits)
    assert any("slow start" in h.text.lower() for h in res.hits)


def test_dense_mode_and_fts_mode_are_selectable(vec_db, hsettings, indexed):
    p, cid = principal(vec_db), cn_id(vec_db)
    d = build_retriever(vec_db, hsettings, "dense").search(Q, p, course_id=cid)
    assert d.mode_used == "dense" and d.hits and all(h.sources == ["dense"] for h in d.hits)
    assert build_retriever(vec_db, hsettings, "fts").search(Q, p, course_id=cid).mode_used == "fts"
    with pytest.raises(ValueError):
        build_retriever(vec_db, hsettings, "bogus")


def test_falls_back_to_fts_when_embeddings_not_configured(vec_db, settings):
    s = settings.model_copy(update={"embedding_provider": "none", "retrieval_mode": "hybrid"})
    res = build_retriever(vec_db, s, "hybrid").search(Q, principal(vec_db), course_id=cn_id(vec_db))
    assert res.mode_used == "fts" and res.fallback_reason == "embeddings_not_configured" and res.hits


def test_falls_back_when_no_active_model_or_no_pgvector(vec_db, hsettings, monkeypatch):
    p, cid = principal(vec_db), cn_id(vec_db)
    res = build_retriever(vec_db, hsettings, "hybrid").search(Q, p, course_id=cid)
    assert res.mode_used == "fts" and "no_active_embedding_model" in res.fallback_reason
    monkeypatch.setattr(vectors, "vector_available", lambda db: False)
    res = build_retriever(vec_db, hsettings, "hybrid").search(Q, p, course_id=cid)
    assert res.mode_used == "fts" and res.fallback_reason == "pgvector_unavailable" and res.hits
    with pytest.raises(RetrievalUnavailable):
        build_retriever(vec_db, hsettings, "dense").search(Q, p, course_id=cid)


def test_model_mismatch_never_mixes_vectors(vec_db, hsettings, indexed):
    other = HashEmbeddingProvider(dims=32)           # different model name AND dimension than the active one
    r = HybridRetriever(vec_db, hsettings, other, "hybrid")
    res = r.search(Q, principal(vec_db), course_id=cn_id(vec_db))
    assert res.mode_used == "fts" and "embedding_model_mismatch" in res.fallback_reason and res.hits
    with pytest.raises(RetrievalUnavailable):
        HybridRetriever(vec_db, hsettings, other, "dense").search(Q, principal(vec_db), course_id=cn_id(vec_db))
    with pytest.raises(ValueError):                   # storing vectors with a mismatching provider is refused
        vectors.embed_missing(vec_db, other, indexed)


def test_empty_and_stopword_queries(vec_db, hsettings, indexed):
    r = build_retriever(vec_db, hsettings, "hybrid")
    e = r.search("   ", principal(vec_db), course_id=cn_id(vec_db))
    assert e.hits == [] and e.mode_used == "none" and e.fallback_reason == "empty_query"
    assert r.search("the of and", principal(vec_db), course_id=cn_id(vec_db)).mode_used == "hybrid"   # runs; dense still answers


def test_embedding_runtime_error_degrades_to_fts(vec_db, hsettings, indexed):
    class Boom(HashEmbeddingProvider):
        def embed_query(self, text):
            raise RuntimeError("model crashed")
    res = HybridRetriever(vec_db, hsettings, Boom(), "hybrid").search(Q, principal(vec_db), course_id=cn_id(vec_db))
    assert res.mode_used == "fts" and res.fallback_reason.startswith("dense_error") and res.hits


def test_dense_search_enforces_access_control(client, student, student2, student3, vec_db, hsettings, indexed, cn_course_id):
    secret = "Zebracorn quokka frobnication protocol handshake window congestion."
    svc = KnowledgeService(vec_db, hsettings)
    owner = vec_db.scalar(select(User).where(User.email == "student1@demo.local"))
    doc = svc.ingest_pdf(owner_id=owner.id, course_id=uuid.UUID(cn_course_id), visibility="private", title="Private",
                         filename="p.pdf", data=make_pdf([secret]))
    assert doc.status == "READY"
    chunk_ids = {c.id for c in vec_db.scalars(select(Chunk).where(Chunk.document_id == doc.id))}
    assert vectors.missing_count(vec_db, indexed.id) == 0     # the new document was embedded on ingest (active model matches)
    r = build_retriever(vec_db, hsettings, "dense")
    mine = r.search(secret, principal(vec_db, "student1@demo.local"), course_id=uuid.UUID(cn_course_id), top_k=20)
    assert {uuid.UUID(h.chunk_id) for h in mine.hits} & chunk_ids
    for who in ("student2@demo.local", "student3@demo.local"):
        res = r.search(secret, principal(vec_db, who), top_k=20)
        assert not ({uuid.UUID(h.chunk_id) for h in res.hits} & chunk_ids), who
    # the dense SQL itself must filter (the re-fetch through get_chunks is a second layer; test the first layer directly)
    raw = HybridRetriever(vec_db, hsettings, get_embedding_provider(hsettings), "dense")
    leaked = {uuid.UUID(cid) for cid, _ in raw._dense(secret, principal(vec_db, "student2@demo.local"), None, indexed, 50)}
    assert not (leaked & chunk_ids)
    assert {uuid.UUID(cid) for cid, _ in raw._dense(secret, principal(vec_db, "student1@demo.local"), None, indexed, 50)} & chunk_ids
    assert raw._dense(Q, principal(vec_db, "student3@demo.local"), uuid.UUID(cn_course_id), indexed, 50) == []
    # student3 (OS only) gets nothing at all from the CN course even by asking for it
    assert r.search(Q, principal(vec_db, "student3@demo.local"), course_id=uuid.UUID(cn_course_id)).hits == []


def test_deleting_a_document_removes_its_vectors(client, student, vec_db, hsettings, indexed, cn_course_id):
    svc = KnowledgeService(vec_db, hsettings)
    owner = vec_db.scalar(select(User).where(User.email == "student1@demo.local"))
    doc = svc.ingest_pdf(owner_id=owner.id, course_id=uuid.UUID(cn_course_id), visibility="private", title="Temp",
                         filename="t.pdf", data=make_pdf(["Wibblewobble handshake frames are unique to this document."]))
    n = vec_db.execute(text("select count(*) from know.chunk_embeddings e join know.chunks c on c.id=e.chunk_id "
                            "where c.document_id = :d"), {"d": doc.id}).scalar()
    assert n > 0
    svc.delete_document(doc)
    assert vec_db.execute(text("select count(*) from know.chunk_embeddings where chunk_id not in (select id from know.chunks)")).scalar() == 0
    res = build_retriever(vec_db, hsettings, "dense").search("Wibblewobble handshake frames", principal(vec_db),
                                                              course_id=uuid.UUID(cn_course_id), top_k=20)
    assert all("Wibblewobble" not in h.text for h in res.hits)


def test_missing_embeddings_are_reported_and_reindex_is_idempotent(vec_db, hsettings, indexed, cn_course_id, monkeypatch):
    # ingest with embeddings unavailable -> document is READY (searchable by text) but has no vectors yet
    svc = KnowledgeService(vec_db, hsettings.model_copy(update={"embedding_provider": "none"}))
    owner = vec_db.scalar(select(User).where(User.email == "student1@demo.local"))
    svc.ingest_pdf(owner_id=owner.id, course_id=uuid.UUID(cn_course_id), visibility="private", title="Late",
                   filename="l.pdf", data=make_pdf(["Tardigrade routing tables converge slowly."]))
    res = build_retriever(vec_db, hsettings, "hybrid").search("tardigrade routing", principal(vec_db), course_id=uuid.UUID(cn_course_id))
    assert res.degraded and res.missing_embeddings > 0 and "no embedding yet" in res.fallback_reason
    assert any("Tardigrade" in h.text for h in res.hits)                 # still found through full text
    # the reindex command embeds exactly what is missing, then does nothing the second time
    monkeypatch.setattr("app.knowledge.reindex.get_settings", lambda: hsettings)
    assert reindex_main([]) == 0
    assert vectors.missing_count(vec_db, indexed.id) == 0
    before = vec_db.execute(text("select count(*) from know.chunk_embeddings")).scalar()
    assert reindex_main([]) == 0
    assert vec_db.execute(text("select count(*) from know.chunk_embeddings")).scalar() == before   # no duplicates


def test_switching_models_requires_explicit_activation(vec_db, hsettings, indexed, monkeypatch, capsys):
    """A different configured model must not silently replace the active one."""
    other_settings = hsettings.model_copy(update={"embedding_model": "ignored-for-hash"})
    monkeypatch.setattr("app.knowledge.reindex.get_settings", lambda: other_settings)
    monkeypatch.setattr("app.knowledge.reindex.get_embedding_provider", lambda s: HashEmbeddingProvider(dims=32))
    assert reindex_main([]) == 5 and "--activate" in capsys.readouterr().err
    active = vectors.active_model(vec_db)
    assert active.name == "hash-64"                                       # unchanged
    assert reindex_main(["--activate"]) == 0
    vec_db.expire_all()
    new_active = vectors.active_model(vec_db)
    assert new_active.name == "hash-32" and new_active.dims == 32
    retired = vec_db.scalar(select(EmbeddingModel).where(EmbeddingModel.name == "hash-64"))
    assert retired.status == "retired"
    # vectors of the retired model remain stored but are NOT eligible for search
    assert vec_db.execute(text("select count(*) from know.chunk_embeddings where model_id = :m"), {"m": retired.id}).scalar() > 0
    p32 = HashEmbeddingProvider(dims=32)
    res = HybridRetriever(vec_db, hsettings, p32, "dense").search(Q, principal(vec_db), course_id=cn_id(vec_db))
    assert res.embedding_model == "hash-32"
    # old provider is now the mismatching one -> full-text fallback
    old = HybridRetriever(vec_db, hsettings, HashEmbeddingProvider(dims=64), "hybrid").search(Q, principal(vec_db), course_id=cn_id(vec_db))
    assert old.mode_used == "fts" and "embedding_model_mismatch" in old.fallback_reason
    monkeypatch.setattr("app.knowledge.reindex.get_embedding_provider", lambda s: p32)
    assert reindex_main(["--purge-retired"]) == 0
    assert vec_db.execute(text("select count(*) from know.chunk_embeddings where model_id = :m"), {"m": retired.id}).scalar() == 0


def test_workflow_uses_hybrid_and_keeps_citation_integrity(app, client, student, admin, vec_db, hsettings, indexed, cn_course_id, monkeypatch):
    app.state.settings = hsettings
    r = ask(client, student, cn_course_id, SLOW_START_Q)
    assert r.json()["status"] == "AWAITING_STUDENT"
    t = trace(client, admin, r.json()["run_id"])
    mode = t["retrieval"][0]["mode"]
    assert mode["used"] == "hybrid" and mode["requested"] == "hybrid" and mode["embedding_model"] == "hash-64"
    assert any(x["sources"] for x in t["retrieval"][0]["results"])
    cv = t["citation_validation"][0]
    assert cv["n_verified"] >= 1 and cv["n_stripped"] == 0 and all(c["verified"] for c in cv["checks"])
    retrieved = set(t["retrieval"][0]["chunk_ids"])
    view = client.get(f"/v1/doubts/{r.json()['session_id']}", headers=student).json()
    for c in view["latest_intervention"]["explanation"]["citations"]:
        assert c["chunk_id"] in retrieved
        text_ = vec_db.execute(text("select text from know.chunks where id = :i"), {"i": c["chunk_id"]}).scalar()
        assert c["quote"] in text_


def test_readyz_reports_effective_retrieval_mode(app, client, vec_db, hsettings, indexed):
    assert client.get("/readyz").json()["retrieval"]["effective_mode"] == "fts"        # default test settings: no provider
    app.state.settings = hsettings
    info = client.get("/readyz").json()["retrieval"]
    assert info["effective_mode"] == "hybrid" and info["embedding_model"] == "hash-64" and info["missing_embeddings"] == 0


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_ties_in_dense_and_fused_scores_are_broken_deterministically(vec_db, hsettings, indexed, cn_course_id, mode):
    svc = KnowledgeService(vec_db, hsettings)
    owner = vec_db.scalar(select(User).where(User.email == "student1@demo.local"))
    text_ = "Tiebreaker protocol zebrafish handshakes are described only in this paragraph about tiebreakerzork."
    for title in ("Tie B", "Tie A", "Tie C"):
        svc.ingest_pdf(owner_id=owner.id, course_id=uuid.UUID(cn_course_id), visibility="private", title=title,
                       filename="t.pdf", data=make_pdf([text_]))
    r = build_retriever(vec_db, hsettings, mode)
    for _ in range(3):
        res = r.search("tiebreakerzork zebrafish", principal(vec_db), course_id=uuid.UUID(cn_course_id), top_k=20)
        assert [h.document_title for h in res.hits if "tiebreakerzork" in h.text] == ["Tie A", "Tie B", "Tie C"]
