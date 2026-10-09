"""Document lifecycle: replace, re-index, failure isolation, deletion, audit trail, embeddings cleanup."""
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.auth.principal import principal_for
from app.db import make_session_factory
from app.knowledge import vectors
from app.knowledge.embeddings import get_embedding_provider
from app.knowledge.ingestion import Ingestion
from app.knowledge.retrieval import build_retriever
from app.main import create_app
from app.models import Chunk, Course, Document, DocumentEvent, IngestionJob, User
from app.worker import run_once
from tests.conftest import login, make_pdf


@pytest.fixture
def lsettings(settings):
    return settings.model_copy(update={"ingestion_mode": "async", "redis_url": None, "job_retry_backoff_seconds": 0.0,
                                       "parser_isolation": "inline", "ocr_engine": "none"})


@pytest.fixture
def factory(database):
    return make_session_factory(database)


@pytest.fixture
def env(lsettings, factory, cn_course_id):
    c = TestClient(create_app(lsettings), raise_server_exceptions=False)
    return type("Env", (), {"c": c, "s": login(c, "student1@demo.local"), "s2": login(c, "student2@demo.local"),
                            "admin": login(c, "admin@demo.local"), "teacher": login(c, "teacher1@demo.local"),
                            "cid": cn_course_id, "settings": lsettings, "factory": factory})


def pdf(tag, extra=""):
    return make_pdf([f"Lifecycle document {tag}. The {tag} protocol frames unique tokens such as {tag}zork and {tag}quux. {extra}"])


def up(env, headers, data, title="Doc"):
    return env.c.post("/v1/documents", headers=headers, data={"course_id": env.cid, "title": title},
                      files={"file": ("a.pdf", io.BytesIO(data), "application/pdf")})


def put_file(env, headers, doc_id, data):
    return env.c.put(f"/v1/documents/{doc_id}/file", headers=headers, files={"file": ("b.pdf", io.BytesIO(data), "application/pdf")})


def search_text(env, headers, q):
    r = env.c.get("/v1/search", headers=headers, params={"q": q, "course_id": env.cid, "top_k": 20}).json()["results"]
    return [h["text"] for h in r]


def ready(env, data, tag="x", headers=None):
    d = up(env, headers or env.s, data).json()
    run_once(env.factory, env.settings, "w")
    st = env.c.get(f"/v1/documents/{d['document_id']}", headers=headers or env.s).json()
    assert st["status"] == "READY", st
    return d["document_id"]


def chunk_ids(factory, doc_id):
    with factory() as db:
        return set(db.scalars(select(Chunk.id).where(Chunk.document_id == uuid.UUID(doc_id))))


def events(factory, doc_id):
    with factory() as db:
        return [e.event for e in db.scalars(select(DocumentEvent).where(DocumentEvent.document_id == uuid.UUID(doc_id))
                                            .order_by(DocumentEvent.created_at))]


# ------------------------------------------------------------------------------------------------ replace
def test_replacement_swaps_versions_atomically_and_leaves_no_obsolete_chunks(env, storage_dir):
    doc = ready(env, pdf("alphaone"))
    old_chunks = chunk_ids(env.factory, doc)
    old_file = storage_dir / f"{doc}.pdf"
    assert old_file.exists()
    r = put_file(env, env.s, doc, pdf("betatwo"))
    assert r.status_code == 202 and r.json()["job"]["status"] == "QUEUED"
    # while the replacement is queued the CURRENT version stays live
    assert any("alphaonezork" in t for t in search_text(env, env.s, "alphaonezork"))
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["status"] == "READY"
    run_once(env.factory, env.settings, "w")
    st = env.c.get(f"/v1/documents/{doc}", headers=env.s).json()
    assert st["status"] == "READY" and st["version"] == 2 and st["job"]["status"] == "DONE"
    assert search_text(env, env.s, "alphaonezork") == []                       # obsolete content not searchable
    assert any("betatwozork" in t for t in search_text(env, env.s, "betatwozork"))
    new_chunks = chunk_ids(env.factory, doc)
    assert new_chunks and not (new_chunks & old_chunks)                        # nothing left from version 1
    with env.factory() as db:
        assert {v for (v,) in db.execute(select(Chunk.ingestion_version).where(Chunk.document_id == uuid.UUID(doc)))} == {2}
        assert db.get(Document, uuid.UUID(doc)).sha256 != ""
    assert not old_file.exists() and (storage_dir / f"{doc}.v2.pdf").exists()    # old file removed, new file kept
    assert events(env.factory, doc) == ["upload_accepted", "ingested", "replace_requested", "replaced"]


def test_failed_replacement_does_not_corrupt_the_live_version(env, storage_dir):
    doc = ready(env, pdf("livever"))
    before = chunk_ids(env.factory, doc)
    assert put_file(env, env.s, doc, b"%PDF-1.4\nthis is garbage, not a pdf").status_code == 202
    run_once(env.factory, env.settings, "w")
    st = env.c.get(f"/v1/documents/{doc}", headers=env.s).json()
    assert st["status"] == "READY" and st["version"] == 1 and st["error_code"] is None      # live doc untouched
    assert st["job"]["status"] == "FAILED" and st["job"]["error_code"] == "UNREADABLE_PDF"
    assert chunk_ids(env.factory, doc) == before
    assert any("liveverzork" in t for t in search_text(env, env.s, "liveverzork"))
    assert not (storage_dir / f"{doc}.v2.pdf").exists()                                      # failed upload cleaned up
    assert events(env.factory, doc)[-2:] == ["replace_requested", "replace_failed"]
    # and a later, valid replacement still works
    assert put_file(env, env.s, doc, pdf("afterfail")).status_code == 202
    run_once(env.factory, env.settings, "w")
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["version"] == 2


def test_transient_failure_during_replacement_keeps_serving_the_old_version(env, monkeypatch):
    doc = ready(env, pdf("transient"))
    put_file(env, env.s, doc, pdf("transientnew"))
    import app.knowledge.ingestion as ing
    real, n = ing.parse_document, {"n": 0}

    def flaky(data, settings):
        n["n"] += 1
        if n["n"] == 1:
            raise OSError("disk hiccup")
        return real(data, settings)
    monkeypatch.setattr(ing, "parse_document", flaky)
    slow = env.settings.model_copy(update={"job_retry_backoff_seconds": 30.0})
    run_once(env.factory, slow, "w")                               # attempt 1 fails, job re-queued with a 30 s backoff
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["status"] == "READY"
    assert any("transientzork" in t for t in search_text(env, env.s, "transientzork"))      # old version still served
    with env.factory() as db:
        db.execute(text("update know.ingestion_jobs set available_at = now() where document_id = :d"), {"d": doc})
        db.commit()
    run_once(env.factory, slow, "w")                               # attempt 2 succeeds
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["version"] == 2


def test_hard_crash_during_swap_leaves_the_old_version_intact(env, monkeypatch):
    doc = ready(env, pdf("crashswap"))
    before = chunk_ids(env.factory, doc)
    put_file(env, env.s, doc, pdf("crashswapnew"))
    import app.knowledge.ingestion as ing
    from app.knowledge.chunker import ChunkSpec

    class Dies(list):
        def __iter__(self):
            yield self[0]
            raise SystemExit("killed")
    real = ing.chunk_pages
    monkeypatch.setattr(ing, "chunk_pages", lambda *a, **k: Dies([ChunkSpec(1, 0, "new one"), ChunkSpec(1, 1, "new two")]))
    with pytest.raises(SystemExit):
        run_once(env.factory, env.settings, "doomed")
    assert chunk_ids(env.factory, doc) == before                    # the DELETE of old chunks was rolled back with the rest
    assert any("crashswapzork" in t for t in search_text(env, env.s, "crashswapzork"))
    monkeypatch.setattr(ing, "chunk_pages", real)
    with env.factory() as db:
        db.execute(text("update know.ingestion_jobs set lease_expires_at = now() - interval '1 second' "
                        "where status = 'PROCESSING' and document_id = :d"), {"d": doc})
        db.commit()
        Ingestion(db, env.settings).reap_expired()
    run_once(env.factory, env.settings, "w2")
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["version"] == 2


def test_replace_and_reindex_conflicts(env):
    same, other_bytes = pdf("conflict"), pdf("otherdoc")      # PDFs embed a timestamp: reuse the exact bytes
    doc = ready(env, same)
    other = ready(env, other_bytes)
    assert put_file(env, env.s, doc, same).json()["error"]["code"] == "UNCHANGED_CONTENT"      # identical content
    assert put_file(env, env.s, doc, other_bytes).json()["error"]["code"] == "DUPLICATE_CONTENT"
    assert put_file(env, env.s, doc, b"MZ not a pdf").status_code == 415
    assert put_file(env, env.s, doc, pdf("conflictnew")).status_code == 202
    r = put_file(env, env.s, doc, pdf("conflictnewer"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "JOB_IN_PROGRESS"
    assert env.c.post(f"/v1/documents/{doc}/reindex", headers=env.s).json()["error"]["code"] == "JOB_IN_PROGRESS"
    queued = up(env, env.s, pdf("stillqueued")).json()["document_id"]                          # not READY yet
    assert env.c.post(f"/v1/documents/{queued}/reindex", headers=env.s).json()["error"]["code"] == "DOCUMENT_NOT_READY"
    assert other


def test_lifecycle_authorization(env):
    doc = ready(env, pdf("authz"))
    assert env.c.post(f"/v1/documents/{doc}/reindex").status_code == 401
    assert env.c.post(f"/v1/documents/{doc}/reindex", headers=env.s2).status_code == 404       # private doc: no existence leak
    assert put_file(env, env.s2, doc, pdf("hijack")).status_code == 404
    assert env.c.post(f"/v1/documents/{doc}/reindex", headers=env.teacher).status_code == 403
    assert env.c.get(f"/v1/admin/documents/{doc}/events", headers=env.s).status_code == 403
    assert env.c.get(f"/v1/admin/documents/{doc}/events", headers=env.admin).status_code == 200


# ------------------------------------------------------------------------------------------------ reindex
def test_reindex_does_not_duplicate_chunks(env):
    doc = ready(env, make_pdf([f"Sentence number {i} talks about routers and switches in networks." for i in range(40)]))
    first = chunk_ids(env.factory, doc)
    n = len(first)
    for expected_version in (2, 3):
        assert env.c.post(f"/v1/documents/{doc}/reindex", headers=env.s).status_code == 202
        run_once(env.factory, env.settings, "w")
        st = env.c.get(f"/v1/documents/{doc}", headers=env.s).json()
        assert st["version"] == expected_version and st["chunk_count"] == n
        ids = chunk_ids(env.factory, doc)
        assert len(ids) == n and not (ids & first)
    texts = search_text(env, env.s, "routers switches networks")
    assert len(texts) == len(set(texts))                                  # no duplicate passages in search results
    assert events(env.factory, doc).count("reindexed") == 2


def test_reindex_picks_up_new_chunking_settings(env):
    doc = ready(env, make_pdf([f"Sentence number {i} talks about routers and switches in networks." for i in range(40)]))
    n1 = env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["chunk_count"]
    small = env.settings.model_copy(update={"chunk_words": 40, "chunk_overlap_words": 5})
    env.c.post(f"/v1/documents/{doc}/reindex", headers=env.s)
    run_once(env.factory, small, "w")
    st = env.c.get(f"/v1/documents/{doc}", headers=env.s).json()
    assert st["chunk_count"] > n1 and len(chunk_ids(env.factory, doc)) == st["chunk_count"]


# ------------------------------------------------------------------------------------------------ delete
def test_delete_is_immediate_complete_and_audited(env, storage_dir):
    doc = ready(env, pdf("doomeddoc"))
    put_file(env, env.s, doc, pdf("doomeddocnew"))                         # a pending replacement file exists too
    assert (storage_dir / f"{doc}.v2.pdf").exists()
    assert any("doomeddoczork" in t for t in search_text(env, env.s, "doomeddoczork"))
    assert env.c.delete(f"/v1/documents/{doc}", headers=env.s).status_code == 204
    assert search_text(env, env.s, "doomeddoczork") == []                 # excluded immediately
    with env.factory() as db:
        assert db.get(Document, uuid.UUID(doc)) is None
        assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == uuid.UUID(doc))) == 0
        assert db.scalar(select(func.count()).select_from(IngestionJob).where(IngestionJob.document_id == uuid.UUID(doc))) == 0
    assert not list(storage_dir.glob(f"{doc}*.pdf"))                      # every stored version removed
    evs = env.c.get(f"/v1/admin/documents/{doc}/events", headers=env.admin).json()["items"]
    assert [e["event"] for e in evs][-1] == "deleted" and "replace_requested" in [e["event"] for e in evs]
    assert "doomeddoc" not in str(evs)                                    # metadata only, never document text
    assert env.c.get(f"/v1/documents/{doc}", headers=env.s).status_code == 404


def test_deleting_while_a_job_is_processing_never_resurrects_chunks(env):
    d = up(env, env.s, pdf("midflight")).json()["document_id"]
    with env.factory() as db:
        job = Ingestion(db, env.settings).claim_next("w")                  # job is PROCESSING
        job_id = job.id
    assert env.c.delete(f"/v1/documents/{d}", headers=env.s).status_code == 204
    with env.factory() as db:
        assert Ingestion(db, env.settings).process_job(job_id) in ("MISSING", "FAILED")
    assert chunk_ids(env.factory, d) == set()
    assert search_text(env, env.s, "midflightzork") == []


def test_stale_chunks_of_an_inactive_version_are_never_returned(env):
    """Defense in depth: even if a chunk from a non-active version existed, retrieval filters on the active version."""
    doc = ready(env, pdf("stalever"))
    with env.factory() as db:
        d = db.get(Document, uuid.UUID(doc))
        db.add(Chunk(document_id=d.id, course_id=d.course_id, owner_id=d.owner_id, visibility=d.visibility, page=1,
                     chunk_index=99, text="Ghostly obsolete passage mentioning staleversionzork.", content_hash="x",
                     ingestion_version=d.ingestion_version + 5))
        db.commit()
    assert search_text(env, env.s, "staleversionzork") == []


# ------------------------------------------------------------------------------------------------ embeddings
def test_replacement_and_deletion_remove_old_embeddings(env, factory):
    hs = env.settings.model_copy(update={"embedding_provider": "hash", "retrieval_mode": "hybrid"})
    with factory() as db:
        if not vectors.ensure_vector_support(db):
            pytest.skip("pgvector is not available")
        db.execute(text("DELETE FROM know.embedding_models"))
        db.commit()
        p = get_embedding_provider(hs)
        m = vectors.register_model(db, p.model, p.dims)
        vectors.activate_model(db, m)
    env.settings = hs
    doc = ready(env, pdf("embedlife"))                                      # embedded on ingest (active model matches)
    with factory() as db:
        old = set(db.execute(text("select e.chunk_id from know.chunk_embeddings e join know.chunks c on c.id = e.chunk_id "
                                  "where c.document_id = :d"), {"d": doc}).scalars())
        assert old
    put_file(env, env.s, doc, pdf("embedlifenew"))
    run_once(factory, hs, "w")
    with factory() as db:
        gone = db.execute(text("select count(*) from know.chunk_embeddings where chunk_id = any(:ids)"), {"ids": list(old)}).scalar()
        new = db.execute(text("select count(*) from know.chunk_embeddings e join know.chunks c on c.id = e.chunk_id "
                              "where c.document_id = :d"), {"d": doc}).scalar()
        assert gone == 0 and new == env.c.get(f"/v1/documents/{doc}", headers=env.s).json()["chunk_count"]
        user = db.scalar(select(User).where(User.email == "student1@demo.local"))
        cid = db.scalar(select(Course.id).where(Course.code == "CN101"))
        res = build_retriever(db, hs, "dense").search("embedlife zork", principal_for(db, user), course_id=cid, top_k=20)
        assert all("embedlifezork" not in h.text and "embedlifenew" not in h.text or "embedlifenewzork" in h.text for h in res.hits)
        assert not ({uuid.UUID(h.chunk_id) for h in res.hits} & old)        # old vectors are not eligible for search
    env.c.delete(f"/v1/documents/{doc}", headers=env.s)
    with factory() as db:
        assert db.execute(text("select count(*) from know.chunk_embeddings e where not exists "
                               "(select 1 from know.chunks c where c.id = e.chunk_id)")).scalar() == 0
