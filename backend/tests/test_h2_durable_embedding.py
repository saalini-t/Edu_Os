"""H2 steps 2-3: embedding is a durable, resumable, time-sliced stage; READY means fully embedded; the job lease is
owned (heartbeat, owner-checked writes), recovery is bounded, and a reconcile sweep repairs incomplete documents."""
from __future__ import annotations

import io
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db import make_session_factory
from app.knowledge import vectors
from app.knowledge.embeddings import EmbeddingUnavailable, get_embedding_provider
from app.knowledge.ingestion import Ingestion, LeaseLost
from app.main import create_app
from app.models import Chunk, Document, IngestionJob
from app.worker import run_once
from tests.conftest import login, make_pdf


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture
def emb(db, settings):
    """Async settings with the deterministic hash embedder ACTIVE, tiny batches and a ~zero time slice (one batch per claim)."""
    if not vectors.ensure_vector_support(db):
        pytest.skip("pgvector is not available on this PostgreSQL server")
    db.execute(text("DELETE FROM know.embedding_models"))
    db.commit()
    s = settings.model_copy(update=dict(ingestion_mode="async", redis_url=None, embedding_provider="hash",
                                        embedding_batch_size=2, embed_slice_seconds=0.001, job_retry_backoff_seconds=0.0,
                                        job_max_attempts=3, chunk_words=30, chunk_overlap_words=5))
    p = get_embedding_provider(s)
    m = vectors.register_model(db, p.model, p.dims)
    vectors.activate_model(db, m)
    vectors.embed_missing(db, p, m, batch_size=16)       # steady state: documents that already exist have their vectors
    return s, p, m


@pytest.fixture
def factory(database):
    return make_session_factory(database)


def book(tag: str, paragraphs: int = 10) -> bytes:
    """A PDF that produces many chunks (30-word chunks) with a unique searchable token `<tag>zork`."""
    return make_pdf([f"Paragraph {i} of the {tag} handbook explains how the {tag}zork mechanism schedules frames, "
                     f"acknowledges segments, retransmits losses and shares the link fairly between competing flows number {i}."
                     for i in range(paragraphs)])


def client_for(s):
    c = TestClient(create_app(s), raise_server_exceptions=False)
    return c, login(c, "student1@demo.local")


def upload(c, h, course, data, title="H2 doc"):
    return c.post("/v1/documents", headers=h, data={"course_id": course, "title": title},
                  files={"file": ("b.pdf", io.BytesIO(data), "application/pdf")})


def search(c, h, course, q):
    return c.get("/v1/search", headers=h, params={"q": q, "course_id": course}).json()["results"]


def found(c, h, course, q, doc_id) -> bool:
    """Is any returned passage from THIS document? (dense search always returns neighbours from other documents)"""
    return any(r["document_id"] == doc_id for r in search(c, h, course, q))


def state(factory, doc_id, model_id):
    with factory() as db:
        d = db.get(Document, uuid.UUID(doc_id))
        j = db.scalar(select(IngestionJob).where(IngestionJob.document_id == d.id).order_by(IngestionJob.created_at.desc()))
        total = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == d.id))
        missing = db.execute(text("SELECT count(*) FROM know.chunks c WHERE c.document_id = :d AND NOT EXISTS ("
                                  "SELECT 1 FROM know.chunk_embeddings e WHERE e.chunk_id = c.id AND e.model_id = :m)"),
                             {"d": d.id, "m": model_id}).scalar()
        return {"doc": d.status, "version": d.ingestion_version, "job": j.status, "stage": j.stage, "total": total,
                "embedded": total - missing, "error": d.error_code, "payload": dict(j.payload or {}), "attempts": j.attempts}


def step(factory, s, worker="w"):
    """Claim one job and run one slice. Returns (job_id, result) or None if nothing is claimable."""
    with factory() as db:
        ing = Ingestion(db, s)
        job = ing.claim_next(worker)
        return None if job is None else (job.id, ing.process_job(job.id, slice_s=s.embed_slice_seconds))


def expire_lease(factory, doc_id):
    with factory() as db:
        db.execute(text("UPDATE know.ingestion_jobs SET lease_expires_at = now() - interval '1 second' "
                        "WHERE document_id = :d AND status = 'PROCESSING'"), {"d": uuid.UUID(doc_id)})
        db.commit()


# ------------------------------------------------------------------ READY means fully embedded
def test_document_is_not_searchable_until_every_chunk_has_its_vector(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("alpha")).json()
    assert d["status"] == "QUEUED" and d["searchable"] is False
    seen_indexing, guard = False, 0
    while True:
        guard += 1
        assert guard < 100
        r = step(factory, s)
        if r is None:
            break
        st = state(factory, d["document_id"], m.id)
        if st["doc"] != "READY":
            seen_indexing |= st["doc"] == "INDEXING" and 0 < st["embedded"] < st["total"]
            assert not found(c, h, cn_course_id, "alphazork", d["document_id"])      # partially embedded => never searchable
            assert st["job"] == "QUEUED" and st["stage"] == "embedding"
    st = state(factory, d["document_id"], m.id)
    assert seen_indexing, "the document should have been observed partially embedded (INDEXING)"
    assert st["doc"] == "READY" and st["total"] > 4 and st["embedded"] == st["total"] and st["job"] == "DONE"
    assert found(c, h, cn_course_id, "alphazork", d["document_id"])


def test_embedding_slices_do_not_block_other_documents(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    big = upload(c, h, cn_course_id, book("slowdoc", 14), "big").json()["document_id"]
    first = step(factory, s)                                             # parse + one embedding batch, then yields
    assert first[1] == "QUEUED" and state(factory, big, m.id)["doc"] == "INDEXING"
    small = upload(c, h, cn_course_id, make_pdf(["A short unrelated note about quickdoc zork tokens only."]), "small").json()["document_id"]
    # Round-robin, not head-of-line blocking: big's next slice was queued before the upload, so it may take ONE more slice;
    # the new upload is then claimed ahead of big's following slice. Without slicing it would wait for the whole embedding.
    steps = 0
    while state(factory, small, m.id)["doc"] != "READY":
        steps += 1
        assert steps <= 2 and step(factory, s) is not None
    assert state(factory, big, m.id)["doc"] == "INDEXING"                # the small document finished first
    while step(factory, s):
        pass
    assert state(factory, big, m.id)["doc"] == "READY"


def test_each_chunk_is_embedded_exactly_once_across_slices(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    calls = []
    real = p.embed_documents
    monkeypatch.setattr(p, "embed_documents", lambda texts: (calls.append(len(texts)), real(texts))[1])
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("once")).json()["document_id"]
    while step(factory, s):
        pass
    st = state(factory, d, m.id)
    assert sum(calls) == st["total"] and max(calls) <= 2 and st["doc"] == "READY"       # bounded batches, no repeats


# ------------------------------------------------------------------ crash, resume, bounded recovery
def test_crash_during_embedding_resumes_missing_batches_only(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("crash")).json()["document_id"]
    step(factory, s)                                                    # parse + first batch
    step(factory, s)                                                    # second batch
    before = state(factory, d, m.id)
    assert before["doc"] == "INDEXING" and 0 < before["embedded"] < before["total"]
    with factory() as db:                                               # a worker claims the job and is killed
        assert Ingestion(db, s).claim_next("doomed") is not None
    assert state(factory, d, m.id)["job"] == "PROCESSING"
    expire_lease(factory, d)
    with factory() as db:
        assert Ingestion(db, s).reap_expired() == 1
    mid = state(factory, d, m.id)
    assert mid["job"] == "QUEUED" and mid["stage"] == "embedding" and mid["embedded"] == before["embedded"]   # progress kept
    assert mid["payload"].get("crashes") == 1 and mid["attempts"] == before["attempts"]
    calls = []
    real = p.embed_documents
    monkeypatch.setattr(p, "embed_documents", lambda texts: (calls.append(len(texts)), real(texts))[1])
    while step(factory, s):
        pass
    after = state(factory, d, m.id)
    assert after["doc"] == "READY" and after["embedded"] == after["total"]
    assert sum(calls) == before["total"] - before["embedded"]                           # only the missing chunks were embedded


def test_crash_loop_is_bounded_and_ends_in_an_explicit_failure(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("loop")).json()["document_id"]
    step(factory, s)
    for _ in range(s.job_max_attempts):
        with factory() as db:
            if Ingestion(db, s).claim_next("doomed") is None:
                break
        expire_lease(factory, d)
        with factory() as db:
            Ingestion(db, s).reap_expired()
    st = state(factory, d, m.id)
    assert st["job"] == "FAILED" and st["doc"] == "FAILED" and st["error"] == "WORKER_LOST"
    assert st["total"] == 0                                                              # nothing partial is left behind
    assert step(factory, s) is None


def test_parse_stage_crash_is_requeued_with_attempts(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("parsecrash")).json()["document_id"]
    with factory() as db:
        assert Ingestion(db, s).claim_next("doomed").stage == "parsing"
    expire_lease(factory, d)
    with factory() as db:
        assert Ingestion(db, s).reap_expired() == 1
    assert state(factory, d, m.id)["job"] == "QUEUED" and state(factory, d, m.id)["doc"] == "QUEUED"
    while step(factory, s):
        pass
    assert state(factory, d, m.id)["doc"] == "READY"


# ------------------------------------------------------------------ transient and permanent embedding failures
def test_transient_embedding_failures_retry_with_backoff_then_complete(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    s2 = s.model_copy(update={"job_retry_backoff_seconds": 30.0})
    c, h = client_for(s2)
    d = upload(c, h, cn_course_id, book("flaky")).json()["document_id"]
    real, n = p.embed_documents, {"n": 0}

    def flaky(texts):
        n["n"] += 1
        if n["n"] <= 2:
            raise EmbeddingUnavailable("model not loaded yet")
        return real(texts)
    monkeypatch.setattr(p, "embed_documents", flaky)
    assert step(factory, s2)[1] == "QUEUED"                                              # build ok, first batch failed
    st = state(factory, d, m.id)
    assert st["doc"] == "INDEXING" and st["payload"]["embed_failures"] == 1 and st["embedded"] == 0
    assert step(factory, s2) is None                                                     # backoff: not due yet
    with factory() as db:
        db.execute(text("UPDATE know.ingestion_jobs SET available_at = now() WHERE document_id = :d"), {"d": uuid.UUID(d)})
        db.commit()
    assert step(factory, s2)[1] == "QUEUED"                                              # second failure
    assert state(factory, d, m.id)["payload"]["embed_failures"] == 2
    s3 = s2.model_copy(update={"job_retry_backoff_seconds": 0.0})
    with factory() as db:
        db.execute(text("UPDATE know.ingestion_jobs SET available_at = now() WHERE document_id = :d"), {"d": uuid.UUID(d)})
        db.commit()
    while step(factory, s3):
        pass
    done = state(factory, d, m.id)
    assert done["doc"] == "READY" and done["embedded"] == done["total"] and done["payload"].get("embed_failures") == 0


def test_persistent_embedding_failure_is_terminal_never_ready_and_retryable(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("doomedvec")).json()["document_id"]
    real = p.embed_documents
    monkeypatch.setattr(p, "embed_documents", lambda texts: (_ for _ in ()).throw(EmbeddingUnavailable("down")))
    while step(factory, s):
        pass
    st = state(factory, d, m.id)
    assert st["doc"] == "FAILED" and st["error"] == "EMBEDDING_FAILED" and st["job"] == "FAILED" and st["total"] == 0
    assert not found(c, h, cn_course_id, "doomedveczork", d)
    api = c.get(f"/v1/documents/{d}", headers=h).json()
    assert api["status"] == "FAILED" and api["searchable"] is False and api["retryable"] is True
    monkeypatch.setattr(p, "embed_documents", real)                                      # the provider recovers
    r = c.post(f"/v1/documents/{d}/retry", headers=h)
    assert r.status_code == 202
    while step(factory, s):
        pass
    assert state(factory, d, m.id)["doc"] == "READY" and found(c, h, cn_course_id, "doomedveczork", d)


def test_dimension_mismatch_is_permanent_not_retried(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("dims")).json()["document_id"]
    monkeypatch.setattr(p, "embed_documents", lambda texts: [[0.1, 0.2] for _ in texts])
    while step(factory, s):
        pass
    st = state(factory, d, m.id)
    assert st["doc"] == "FAILED" and st["error"] == "EMBEDDING_FAILED" and st["payload"].get("embed_failures") is None


def test_retry_rules(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("retryrules")).json()["document_id"]
    assert c.post(f"/v1/documents/{d}/retry", headers=h).status_code == 409           # not failed
    ok = upload(c, h, cn_course_id, make_pdf(["blank page doc"]), "other").json()["document_id"]
    with factory() as db:                                                              # a failure a retry cannot fix
        db.execute(text("UPDATE know.documents SET status='FAILED', error_code='ENCRYPTED_PDF' WHERE id = :d"), {"d": uuid.UUID(ok)})
        db.execute(text("UPDATE know.ingestion_jobs SET status='FAILED' WHERE document_id = :d"), {"d": uuid.UUID(ok)})
        db.commit()
    r = c.post(f"/v1/documents/{ok}/retry", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_RETRYABLE"
    other = login(c, "student2@demo.local")
    assert c.post(f"/v1/documents/{d}/retry", headers=other).status_code in (403, 404)


# ------------------------------------------------------------------ lease ownership
def test_only_the_current_lease_owner_can_write_or_finish(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("owner")).json()["document_id"]
    with factory() as db:
        j1 = Ingestion(db, s).claim_next("w1")
        old_owner, jid = j1.worker_id, j1.id
        Ingestion(db, s).process_job(jid, slice_s=0.001)                                # w1 builds + embeds a batch, yields
    with factory() as db:
        j2 = Ingestion(db, s).claim_next("w1")                                          # same process, NEW claim => new owner token
        assert j2.worker_id != old_owner
        new_owner = j2.worker_id
    before = state(factory, d, m.id)["embedded"]
    with factory() as db:
        ing = Ingestion(db, s)
        with pytest.raises(LeaseLost):
            ing._renew(db, jid, old_owner)                                              # stale owner: rejected
        db.rollback()
        assert ing._embed_stage(jid, old_owner, None) == "LOST"                         # and cannot embed or finish
    st = state(factory, d, m.id)
    assert st["embedded"] == before and st["doc"] == "INDEXING" and st["job"] == "PROCESSING"
    with factory() as db:
        assert Ingestion(db, s)._embed_stage(jid, new_owner, 0.001) == "QUEUED"        # the real owner still progresses
    assert state(factory, d, m.id)["embedded"] > before


def test_expired_lease_stale_worker_cannot_finalize_after_takeover(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("takeover", 3)).json()["document_id"]
    with factory() as db:
        j = Ingestion(db, s).claim_next("slow")
        slow_owner, jid = j.worker_id, j.id
    expire_lease(factory, d)
    with factory() as db:
        Ingestion(db, s).reap_expired()
    while step(factory, s, "fast"):                                                     # another worker finishes the job
        pass
    assert state(factory, d, m.id)["doc"] == "READY"
    with factory() as db:
        assert Ingestion(db, s).process_job(jid, slice_s=None) in ("DONE", "MISSING")     # late duplicate delivery: harmless
        assert Ingestion(db, s)._embed_stage(jid, slow_owner, None) == "LOST"
    st = state(factory, d, m.id)
    assert st["doc"] == "READY" and st["embedded"] == st["total"]


def test_heartbeat_renews_the_lease_while_the_parser_blocks(emb, factory, cn_course_id, monkeypatch):
    s, _, _ = emb
    s = s.model_copy(update={"job_lease_seconds": 5})
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("beat", 3)).json()["document_id"]
    import app.knowledge.ingestion as ing_mod
    real, seen = ing_mod.parse_document, {}

    def slow_parse(data, settings, **kw):
        seen["first"] = _lease(factory, d)
        time.sleep(3.6)                                                                 # longer than two heartbeats (5 s / 3)
        seen["last"] = _lease(factory, d)
        return real(data, settings, **kw)
    monkeypatch.setattr(ing_mod, "parse_document", slow_parse)
    step(factory, s)
    assert seen["last"] > seen["first"], "the lease must be extended while the parse is still running"


def _lease(factory, doc_id):
    with factory() as db:
        return db.scalar(text("SELECT lease_expires_at FROM know.ingestion_jobs WHERE document_id = :d"), {"d": uuid.UUID(doc_id)})


def test_a_claim_gets_a_unique_owner_token_and_duplicate_claims_are_impossible(emb, factory, cn_course_id):
    s, _, _ = emb
    c, h = client_for(s)
    upload(c, h, cn_course_id, book("claims"))
    with factory() as a, factory() as b:
        first = Ingestion(a, s).claim_next("w")
        assert first is not None and Ingestion(b, s).claim_next("w") is None            # one job, one owner
        assert first.worker_id.startswith("w#")


# ------------------------------------------------------------------ the swap invariant
def test_finalize_refuses_to_mark_ready_while_vectors_are_missing(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("invariant")).json()["document_id"]
    step(factory, s)                                                                    # staged + one batch embedded
    with factory() as db:
        ing = Ingestion(db, s)
        job = ing.claim_next("w")
        doc = db.get(Document, job.document_id)
        db.refresh(doc)
        assert ing._finalize(job, doc, job.worker_id) is False
        db.refresh(doc)
        assert doc.status == "INDEXING"


# ------------------------------------------------------------------ reindex / replace keep the live version
def test_reindex_keeps_the_old_version_searchable_until_the_new_one_is_fully_embedded(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("reidx")).json()["document_id"]
    while step(factory, s):
        pass
    with factory() as db:
        old_ids = set(db.scalars(select(Chunk.id).where(Chunk.document_id == uuid.UUID(d))))
    assert c.post(f"/v1/documents/{d}/reindex", headers=h).status_code == 202
    step(factory, s)                                                                    # stage v2 + first batch
    mid = state(factory, d, m.id)
    assert mid["doc"] == "READY" and mid["version"] == 1 and mid["job"] == "QUEUED"     # still the old, complete version
    mine = {x["chunk_id"] for x in search(c, h, cn_course_id, "reidxzork") if x["document_id"] == d}
    assert mine and mine <= {str(i) for i in old_ids}                                   # only the complete OLD version is served
    while step(factory, s):
        pass
    end = state(factory, d, m.id)
    assert end["doc"] == "READY" and end["version"] == 2 and end["embedded"] == end["total"]
    with factory() as db:
        assert not (old_ids & set(db.scalars(select(Chunk.id).where(Chunk.document_id == uuid.UUID(d)))))
        orphans = db.execute(text("SELECT count(*) FROM know.chunk_embeddings e LEFT JOIN know.chunks c ON c.id = e.chunk_id "
                                  "WHERE c.id IS NULL")).scalar()
        assert orphans == 0


def test_failed_embedding_of_a_replacement_leaves_the_live_version_untouched(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("liveone")).json()["document_id"]
    while step(factory, s):
        pass
    live = state(factory, d, m.id)
    real = p.embed_documents
    monkeypatch.setattr(p, "embed_documents", lambda texts: (_ for _ in ()).throw(EmbeddingUnavailable("down")))
    r = c.put(f"/v1/documents/{d}/file", headers=h, files={"file": ("n.pdf", io.BytesIO(book("livetwo")), "application/pdf")})
    assert r.status_code == 202
    while step(factory, s):
        pass
    after = state(factory, d, m.id)
    assert after["job"] == "FAILED" and after["doc"] == "READY" and after["version"] == 1
    assert after["total"] == live["total"] and after["embedded"] == live["embedded"]   # staged v2 chunks removed, v1 intact
    assert found(c, h, cn_course_id, "liveonezork", d)
    monkeypatch.setattr(p, "embed_documents", real)


# ------------------------------------------------------------------ reconciliation / backfill
def test_reconcile_repairs_a_ready_document_that_has_missing_vectors(emb, factory, cn_course_id):
    s, _, m = emb
    sync_no_embed = s.model_copy(update={"embedding_provider": "none"})      # how a legacy document was created: READY, no vectors
    c, h = client_for(sync_no_embed)
    d = upload(c, h, cn_course_id, book("legacy")).json()["document_id"]
    while step(factory, sync_no_embed):
        pass
    legacy = state(factory, d, m.id)
    assert legacy["doc"] == "READY" and legacy["embedded"] == 0 and legacy["total"] > 4
    with factory() as db:
        assert Ingestion(db, s).reconcile_embeddings() == 1                      # embeddings are now configured: repair queued
    q = state(factory, d, m.id)
    assert q["doc"] == "INDEXING" and q["job"] == "QUEUED" and q["stage"] == "embedding"
    assert not found(c, h, cn_course_id, "legacyzork", d)                         # not "fully searchable" while incomplete
    with factory() as db:
        assert Ingestion(db, s).reconcile_embeddings() == 0                      # idempotent: an active job exists
    while step(factory, s):
        pass
    fixed = state(factory, d, m.id)
    assert fixed["doc"] == "READY" and fixed["embedded"] == fixed["total"] and fixed["version"] == legacy["version"]
    assert found(c, h, cn_course_id, "legacyzork", d)
    with factory() as db:
        assert Ingestion(db, s).reconcile_embeddings() == 0                      # nothing left to repair


def test_reconcile_partial_document_resumes_instead_of_restarting(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("partial")).json()["document_id"]
    while step(factory, s):
        pass
    with factory() as db:                                                         # simulate the baseline defect: 50 % of vectors lost
        db.execute(text("DELETE FROM know.chunk_embeddings WHERE chunk_id IN (SELECT id FROM know.chunks "
                        "WHERE document_id = :d ORDER BY chunk_index LIMIT (SELECT count(*)/2 FROM know.chunks WHERE document_id = :d))"),
                   {"d": uuid.UUID(d)})
        db.commit()
    broken = state(factory, d, m.id)
    assert broken["doc"] == "READY" and 0 < broken["embedded"] < broken["total"]
    calls = []
    real = p.embed_documents
    monkeypatch.setattr(p, "embed_documents", lambda texts: (calls.append(len(texts)), real(texts))[1])
    with factory() as db:
        assert Ingestion(db, s).reconcile_embeddings() == 1
    while step(factory, s):
        pass
    end = state(factory, d, m.id)
    assert end["doc"] == "READY" and end["embedded"] == end["total"] and sum(calls) == broken["total"] - broken["embedded"]


def test_worker_sweep_runs_the_reconciliation(emb, factory, cn_course_id):
    from app.queue import WakeupQueue
    from app.worker import sweep
    s, _, m = emb
    legacy_s = s.model_copy(update={"embedding_provider": "none"})
    c, h = client_for(legacy_s)
    d = upload(c, h, cn_course_id, book("sweep", 4)).json()["document_id"]
    while step(factory, legacy_s):
        pass
    out = sweep(factory, s, WakeupQueue(None))
    assert out["repair_queued"] == 1
    run_once(factory, s, "w")
    assert state(factory, d, m.id)["doc"] == "READY"


def test_without_embeddings_configured_documents_are_ready_immediately(settings, factory, cn_course_id):
    s = settings.model_copy(update={"ingestion_mode": "async", "redis_url": None, "embedding_provider": "none"})
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("ftsonly")).json()["document_id"]
    assert step(factory, s)[1] == "DONE"
    assert c.get(f"/v1/documents/{d}", headers=h).json()["status"] == "READY"
    assert found(c, h, cn_course_id, "ftsonlyzork", d)


# ------------------------------------------------------------------ progress API (what the UI shows) and its permissions
def test_progress_endpoint_reflects_persisted_state_and_hides_other_users_documents(emb, factory, cn_course_id):
    s, _, m = emb
    c, h = client_for(s)
    other = login(c, "student2@demo.local")
    d = upload(c, h, cn_course_id, book("progress")).json()
    assert d["progress"]["phase"] == "queued" and d["searchable"] is False
    step(factory, s)                                                          # parse + first batch
    api = c.get(f"/v1/documents/{d['document_id']}", headers=h).json()
    p = api["progress"]
    assert api["status"] == "INDEXING" and p["phase"] == "embedding" and api["searchable"] is False
    st = state(factory, d["document_id"], m.id)
    assert (p["chunks_total"], p["chunks_embedded"]) == (st["total"], st["embedded"]) and 0 < p["percent"] < 100
    # the owner's private document is invisible to another student: no progress, not in their list
    assert c.get(f"/v1/documents/{d['document_id']}", headers=other).status_code == 404
    assert d["document_id"] not in [x["document_id"] for x in c.get("/v1/documents", headers=other).json()["items"]]
    listed = {x["document_id"]: x for x in c.get("/v1/documents", headers=h).json()["items"]}
    assert listed[d["document_id"]]["progress"]["phase"] == "embedding"
    while step(factory, s):
        pass
    done = c.get(f"/v1/documents/{d['document_id']}", headers=h).json()
    assert done["status"] == "READY" and done["searchable"] is True and done["progress"]["percent"] == 100


def test_progress_never_exposes_internal_detail(emb, factory, cn_course_id, monkeypatch):
    s, p, m = emb
    c, h = client_for(s)
    d = upload(c, h, cn_course_id, book("secrets")).json()["document_id"]
    monkeypatch.setattr(p, "embed_documents", lambda texts: (_ for _ in ()).throw(RuntimeError("password=hunter2 /srv/private/path")))
    while step(factory, s):
        pass
    body = c.get(f"/v1/documents/{d}", headers=h).text
    assert "hunter2" not in body and "/srv/private" not in body and "Traceback" not in body
    assert '"error_code":"EMBEDDING_FAILED"' in body.replace(" ", "")


def test_upload_config_lists_only_the_callers_courses_and_the_limits(emb, cn_course_id):
    s, _, _ = emb
    c, h = client_for(s)
    cfg = c.get("/v1/documents/upload-config", headers=h).json()
    assert cfg["max_upload_bytes"] == s.max_upload_bytes and cfg["max_pdf_pages"] == s.max_pdf_pages and cfg["can_upload"]
    assert [x["course_id"] for x in cfg["courses"]] == [cn_course_id]
    teacher = login(c, "teacher1@demo.local")
    assert c.get("/v1/documents/upload-config", headers=teacher).json()["can_upload"] is False
    assert c.get("/v1/documents/upload-config").status_code == 401
