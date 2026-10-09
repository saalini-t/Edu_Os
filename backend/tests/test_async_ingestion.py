"""Asynchronous ingestion: durable jobs in PostgreSQL, Redis as a wake-up channel, a separate worker process."""
import io
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db import make_session_factory
from app.knowledge.ingestion import Ingestion
from app.main import create_app
from app.models import Chunk, Document, IngestionJob
from app.queue import QUEUE_KEY, WakeupQueue
from app.worker import run_once, sweep
from tests.conftest import TEST_DB, blank_pdf, login, make_pdf

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:63790/0")
DEAD_REDIS = "redis://127.0.0.1:1/0"
BACKEND = Path(__file__).resolve().parent.parent


def redis_up() -> bool:
    return WakeupQueue(REDIS_URL).ping()


needs_redis = pytest.mark.skipif(not redis_up(), reason=f"no Redis at {REDIS_URL} (docker compose -f docker-compose.test.yml up -d)")


@pytest.fixture
def asettings(settings):
    return settings.model_copy(update={"ingestion_mode": "async", "redis_url": None, "job_retry_backoff_seconds": 0.0})


@pytest.fixture
def factory(database):
    return make_session_factory(database)


def aclient(s):
    return TestClient(create_app(s), raise_server_exceptions=False)


def up(client, headers, course_id, data, title="Doc"):
    return client.post("/v1/documents", headers=headers, data={"course_id": course_id, "title": title},
                       files={"file": ("n.pdf", io.BytesIO(data), "application/pdf")})


def pdf(tag="alpha"):
    return make_pdf([f"Asynchronous ingestion test {tag}. The {tag} protocol frames unique tokens such as {tag}zork and {tag}quux."])


def search(client, headers, course_id, q):
    return client.get("/v1/search", headers=headers, params={"q": q, "course_id": course_id}).json()["results"]


def job_of(factory, doc_id):
    with factory() as db:
        return db.scalar(select(IngestionJob).where(IngestionJob.document_id == uuid.UUID(doc_id)))


# ------------------------------------------------------------------------------------------- lifecycle
def test_upload_is_accepted_but_not_indexed_until_the_worker_runs(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    r = up(c, s, cn_course_id, pdf("lifecycle"))
    assert r.status_code == 202
    d = r.json()
    assert d["status"] == "QUEUED" and d["indexed"] is False and d["chunk_count"] is None
    assert d["job"]["status"] == "QUEUED" and d["job"]["attempts"] == 0 and d["job"]["queue"] == "deferred"
    assert search(c, s, cn_course_id, "lifecyclezork") == []                  # accepted != searchable
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "QUEUED" and st["indexed"] is False

    assert run_once(factory, asettings, "t1") == 1
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "READY" and st["indexed"] is True and st["chunk_count"] >= 1
    assert st["job"]["status"] == "DONE" and st["job"]["attempts"] == 1 and st["job"]["finished_at"]
    assert any("lifecyclezork" in h["text"] for h in search(c, s, cn_course_id, "lifecyclezork"))


def test_duplicate_upload_does_not_create_a_second_document_or_job(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    data = pdf("dup")
    a = up(c, s, cn_course_id, data)
    b = up(c, s, cn_course_id, data)
    assert a.status_code == 202 and b.status_code == 200
    assert a.json()["document_id"] == b.json()["document_id"] and a.json()["job"]["job_id"] == b.json()["job"]["job_id"]
    run_once(factory, asettings, "t")
    third = up(c, s, cn_course_id, data)
    assert third.status_code == 200 and third.json()["status"] == "READY"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(IngestionJob).where(
            IngestionJob.document_id == uuid.UUID(a.json()["document_id"]))) == 1


def test_database_prevents_two_active_jobs_for_one_document(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    doc_id = uuid.UUID(up(c, s, cn_course_id, pdf("one")).json()["document_id"])
    with factory() as db:
        db.add(IngestionJob(document_id=doc_id, status="QUEUED", max_attempts=3))
        with pytest.raises(IntegrityError):
            db.commit()


def test_permanent_failure_fails_fast_without_retries(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, blank_pdf()).json()       # valid PDF without text: admitted, then fails deterministically
    assert d["status"] == "QUEUED"
    run_once(factory, asettings, "t")
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "FAILED" and st["error_code"] == "NO_EXTRACTABLE_TEXT" and st["indexed"] is False
    assert st["job"]["status"] == "FAILED" and st["job"]["attempts"] == 1       # not retried: deterministic
    assert run_once(factory, asettings, "t") == 0


def test_transient_failure_retries_with_backoff_and_then_succeeds_without_duplicate_chunks(asettings, factory, cn_course_id, monkeypatch):
    s2 = asettings.model_copy(update={"job_retry_backoff_seconds": 30.0})
    c = aclient(s2)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("retry")).json()
    import app.knowledge.ingestion as ing_mod
    real, calls = ing_mod.parse_document, {"n": 0}

    def flaky(data, settings, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(data, settings, **kw)
    monkeypatch.setattr(ing_mod, "parse_document", flaky)
    assert run_once(factory, s2, "t") == 1
    j = job_of(factory, d["document_id"])
    assert j.status == "QUEUED" and j.attempts == 1 and "OSError" in j.last_error
    assert j.available_at > datetime.now(timezone.utc) + timedelta(seconds=20)         # backoff applied
    assert run_once(factory, s2, "t") == 0                                              # not due yet
    with factory() as db:                                                               # make it due
        db.execute(text("update know.ingestion_jobs set available_at = now() where id = :i"), {"i": j.id})
        db.commit()
    assert run_once(factory, s2, "t") == 1
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "READY" and st["job"]["status"] == "DONE" and st["job"]["attempts"] == 2
    with factory() as db:
        n = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == uuid.UUID(d["document_id"])))
        assert n == st["chunk_count"]


def test_retries_are_bounded_and_the_error_stays_visible(asettings, factory, cn_course_id, monkeypatch):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("bounded")).json()
    import app.knowledge.ingestion as ing_mod
    monkeypatch.setattr(ing_mod, "parse_document", lambda *a, **k: (_ for _ in ()).throw(OSError("always broken")))
    for _ in range(10):
        run_once(factory, asettings, "t")
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "FAILED" and st["error_code"] == "MAX_ATTEMPTS_EXCEEDED"
    assert st["job"]["attempts"] == asettings.job_max_attempts and "OSError" in st["job"]["last_error"]
    assert run_once(factory, asettings, "t") == 0                                       # no further attempts


def test_a_failure_halfway_leaves_no_partial_chunks(asettings, factory, cn_course_id, monkeypatch):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("atomic")).json()
    import app.knowledge.ingestion as ing_mod
    from app.knowledge.chunker import ChunkSpec
    monkeypatch.setattr(ing_mod, "chunk_pages", lambda *a, **k: [ChunkSpec(1, 0, "good chunk text"),
                                                                  ChunkSpec(1, 1, None)])   # 2nd insert violates NOT NULL
    run_once(factory, asettings, "t")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == uuid.UUID(d["document_id"]))) == 0
        assert db.get(Document, uuid.UUID(d["document_id"])).status != "READY"
    assert search(c, s, cn_course_id, "good chunk text") == []


def test_hard_crash_while_writing_never_leaves_a_ready_document_without_chunks(asettings, factory, cn_course_id, monkeypatch):
    """SystemExit is not caught by the worker's error handling, so this simulates the process dying mid-write."""
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("hardcrash")).json()
    import app.knowledge.ingestion as ing_mod
    from app.knowledge.chunker import ChunkSpec

    class DiesMidway(list):
        def __iter__(self):
            yield self[0]
            raise SystemExit("killed")
    monkeypatch.setattr(ing_mod, "chunk_pages", lambda *a, **k: DiesMidway([ChunkSpec(1, 0, "first"), ChunkSpec(1, 1, "second")]))
    with pytest.raises(SystemExit):
        run_once(factory, asettings, "doomed")
    with factory() as db:        # what any other process sees after the crash
        doc = db.get(Document, uuid.UUID(d["document_id"]))
        n = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id))
        assert n == 0 and doc.status != "READY"
    monkeypatch.undo()
    with factory() as db:
        db.execute(text("update know.ingestion_jobs set lease_expires_at = now() - interval '1 second' "
                        "where status = 'PROCESSING' and document_id = :d"), {"d": d["document_id"]})
        db.commit()
        Ingestion(db, asettings).reap_expired()
    assert run_once(factory, asettings, "recovery") == 1
    assert c.get(f"/v1/documents/{d['document_id']}", headers=s).json()["status"] == "READY"


def test_worker_crash_is_recovered_by_lease_expiry(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("crash")).json()
    with factory() as db:                      # a worker claims the job ... and dies without finishing
        job = Ingestion(db, asettings).claim_next("dead-worker")
        assert job.status == "PROCESSING" and job.attempts == 1
    assert run_once(factory, asettings, "t") == 0                                       # still leased: nobody steals it
    with factory() as db:                      # lease expires
        db.execute(text("update know.ingestion_jobs set lease_expires_at = now() - interval '1 second' "
                        "where status = 'PROCESSING' and document_id = :d"), {"d": d["document_id"]})
        db.commit()
    with factory() as db:
        assert Ingestion(db, asettings).reap_expired() == 1
    assert run_once(factory, asettings, "t2") == 1
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "READY" and st["job"]["attempts"] == 2


def test_lost_worker_with_no_attempts_left_fails_visibly(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("lost")).json()
    with factory() as db:
        db.execute(text("update know.ingestion_jobs set status='PROCESSING', attempts = max_attempts, "
                        "lease_expires_at = now() - interval '1 second' where document_id = :d"), {"d": d["document_id"]})
        db.commit()
    with factory() as db:
        Ingestion(db, asettings).reap_expired()
    st = c.get(f"/v1/documents/{d['document_id']}", headers=s).json()
    assert st["status"] == "FAILED" and st["error_code"] == "WORKER_LOST"


def test_concurrent_workers_never_process_the_same_job_twice(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    for i in range(3):
        up(c, s, cn_course_id, pdf(f"conc{i}"))
    claimed, barrier = [], threading.Barrier(6)

    def worker(n):
        with factory() as db:
            barrier.wait()
            j = Ingestion(db, asettings).claim_next(f"w{n}")
            if j:
                claimed.append(j.id)
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(claimed) == 3 and len(set(claimed)) == 3


# ------------------------------------------------------------------------------------------- Redis
def test_upload_succeeds_and_is_processed_when_redis_is_unavailable(asettings, factory, cn_course_id):
    s2 = asettings.model_copy(update={"redis_url": DEAD_REDIS})
    c = aclient(s2)
    s = login(c, "student1@demo.local")
    r = up(c, s, cn_course_id, pdf("noredis"))
    assert r.status_code == 202 and r.json()["job"]["queue"] == "deferred"
    assert job_of(factory, r.json()["document_id"]).enqueue_error.startswith("redis_unavailable")
    info = sweep(factory, s2, WakeupQueue(DEAD_REDIS))                                  # must not crash
    assert info["announced"] == 0 and info["queued_due"] >= 1
    assert run_once(factory, s2, "t") == 1                                              # DB polling needs no Redis
    assert c.get(f"/v1/documents/{r.json()['document_id']}", headers=s).json()["status"] == "READY"
    assert WakeupQueue(DEAD_REDIS).wait(1) is False


@needs_redis
def test_upload_notifies_redis_and_sweep_reannounces_lost_wakeups(asettings, factory, cn_course_id):
    import redis
    r0 = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r0.delete(QUEUE_KEY)
    s2 = asettings.model_copy(update={"redis_url": REDIS_URL})
    c = aclient(s2)
    s = login(c, "student1@demo.local")
    r = up(c, s, cn_course_id, pdf("withredis"))
    assert r.json()["job"]["queue"] == "notified" and r0.llen(QUEUE_KEY) == 1
    assert r0.lpop(QUEUE_KEY) == r.json()["job"]["job_id"]
    assert sweep(factory, s2, WakeupQueue(REDIS_URL))["announced"] >= 1                 # lost wake-up is re-announced
    assert WakeupQueue(REDIS_URL).wait(2) is True
    r0.delete(QUEUE_KEY)
    run_once(factory, s2, "t")


@needs_redis
def test_a_real_worker_process_indexes_documents_and_recovers_after_a_restart(asettings, factory, cn_course_id, storage_dir):
    s2 = asettings.model_copy(update={"redis_url": REDIS_URL})
    c = aclient(s2)
    s = login(c, "student1@demo.local")
    env = {**os.environ, "APP_ENV": "test", "DATABASE_URL": TEST_DB, "JWT_SECRET": s2.jwt_secret, "REDIS_URL": REDIS_URL,
           "STORAGE_DIR": str(storage_dir), "WORKER_POLL_SECONDS": "1", "WORKER_SWEEP_SECONDS": "2",
           "JOB_RETRY_BACKOFF_SECONDS": "0", "PYTHONPATH": str(BACKEND)}

    def wait_ready(doc_id, secs=40):
        end = time.time() + secs
        while time.time() < end:
            st = c.get(f"/v1/documents/{doc_id}", headers=s).json()
            if st["status"] in ("READY", "FAILED"):
                return st
            time.sleep(0.5)
        return c.get(f"/v1/documents/{doc_id}", headers=s).json()

    # restart scenario: a job is stuck in PROCESSING from a dead worker *before* the new worker starts
    d1 = up(c, s, cn_course_id, pdf("procone")).json()
    with factory() as db:
        Ingestion(db, s2).claim_next("dead")
        db.execute(text("update know.ingestion_jobs set lease_expires_at = now() - interval '1 second' "
                        "where status = 'PROCESSING' and document_id = :d"), {"d": d1["document_id"]})
        db.commit()
    w = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        st1 = wait_ready(d1["document_id"])
        assert st1["status"] == "READY" and st1["job"]["attempts"] == 2                 # recovered at worker start-up
        d2 = up(c, s, cn_course_id, pdf("proctwo")).json()                              # normal path via Redis wake-up
        assert wait_ready(d2["document_id"])["status"] == "READY"
        assert any("proctwozork" in h["text"] for h in search(c, s, cn_course_id, "proctwozork"))
    finally:
        w.terminate()
        w.wait(timeout=15)


# ------------------------------------------------------------------------------------------- authorization
def test_authorization_on_upload_status_and_search(asettings, factory, cn_course_id):
    c = aclient(asettings)
    s1, s2, s3 = (login(c, e) for e in ("student1@demo.local", "student2@demo.local", "student3@demo.local"))
    t = login(c, "teacher1@demo.local")
    assert up(c, {}, cn_course_id, pdf("anon")).status_code == 401
    assert up(c, t, cn_course_id, pdf("teach")).status_code == 403
    assert up(c, s3, cn_course_id, pdf("unenrolled")).status_code == 403
    d = up(c, s1, cn_course_id, pdf("private")).json()
    path = f"/v1/documents/{d['document_id']}"
    assert c.get(path).status_code == 401
    assert c.get(path, headers=s2).status_code == 404 and c.get(path, headers=s3).status_code == 404   # no existence leak
    assert c.delete(path, headers=s2).status_code == 404
    run_once(factory, asettings, "t")
    assert search(c, s2, cn_course_id, "privatezork") == []
    assert any("privatezork" in h["text"] for h in search(c, s1, cn_course_id, "privatezork"))
    assert c.get(path, headers=s1).json()["indexed"] is True


def test_deleting_a_queued_document_cancels_its_job(asettings, factory, cn_course_id, storage_dir):
    c = aclient(asettings)
    s = login(c, "student1@demo.local")
    d = up(c, s, cn_course_id, pdf("gone")).json()
    f = storage_dir / f"{d['document_id']}.pdf"
    assert f.exists()
    assert c.delete(f"/v1/documents/{d['document_id']}", headers=s).status_code == 204
    assert not f.exists() and job_of(factory, d["document_id"]) is None
    assert run_once(factory, asettings, "t") == 0
    assert c.get(f"/v1/documents/{d['document_id']}", headers=s).status_code == 404
