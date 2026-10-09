"""Ingestion worker (a separate process of the same application package):

    python -m app.worker

Loop: wait for a Redis wake-up (or poll), claim jobs from PostgreSQL, process them, periodically reap jobs whose
worker died and re-announce jobs that were never announced (Redis down at upload time). Safe to run several workers."""
from __future__ import annotations

import logging
import os
import signal
import socket
import time

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.knowledge.ingestion import Ingestion
from app.logging_setup import setup_logging
from app.queue import WakeupQueue

log = logging.getLogger("eduos.worker")


def run_once(factory: sessionmaker[Session], settings: Settings, worker_id: str, *, max_jobs: int = 100) -> int:
    """Drain every currently claimable job. Returns the number processed (used by the loop and by tests)."""
    done = 0
    while done < max_jobs:
        with factory() as db:
            ing = Ingestion(db, settings)
            job = ing.claim_next(worker_id)
            if job is None:
                return done
            result = ing.process_job(job.id, slice_s=settings.embed_slice_seconds)
            log.info("job processed", extra={"job_id": str(job.id), "result": result, "worker": worker_id})
        done += 1
    return done


def sweep(factory: sessionmaker[Session], settings: Settings, queue: WakeupQueue) -> dict:
    """Maintenance: requeue jobs whose worker died; re-announce QUEUED jobs (covers a lost or unavailable Redis)."""
    with factory() as db:
        ing = Ingestion(db, settings)
        reaped = ing.reap_expired()
        repaired = ing.reconcile_embeddings()      # documents with missing vectors get a resumable repair job
        from sqlalchemy import text
        due = db.execute(text("SELECT id FROM know.ingestion_jobs WHERE status = 'QUEUED' AND available_at <= now() "
                              "ORDER BY created_at LIMIT 50")).scalars().all()
    announced = sum(1 for j in due if queue.notify(str(j)) is None)
    out = {"reaped": reaped, "repair_queued": repaired, "queued_due": len(due), "announced": announced}
    try:                       # teaching + learner maintenance: expire escalations, resume runs, expire stale hypotheses
        from app.learner.service import LearnerService
        from app.llm.factory import build_provider
        from app.teaching.resume import reconcile
        with factory() as db:
            provider = build_provider(settings)
            out["escalations"] = reconcile(db, settings, provider)
            from app.workflow.recovery import recover_stuck_runs
            out["runs"] = recover_stuck_runs(db, settings, provider)       # runs whose executor died mid model call
            ls = LearnerService(db, settings)
            out["hypotheses_expired"] = ls.expire_stale()
            db.commit()
    except Exception:
        log.exception("maintenance sweep failed; continuing")
    return out


def main() -> int:
    setup_logging()
    s = get_settings()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    factory = make_session_factory(make_engine(s.database_url))
    queue = WakeupQueue(s.redis_url)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    try:                       # load the embedding model before the first job instead of during it (6-9 s)
        from app.knowledge.embeddings import get_embedding_provider
        p = get_embedding_provider(s)
        if p is not None:
            p.embed_query("warm up")
    except Exception as e:
        log.warning("embedding warm-up failed; it will be retried when a job needs it", extra={"error": type(e).__name__})
    log.info("worker started", extra={"worker": worker_id, "redis": "configured" if s.redis_url else "not configured (polling)"})
    next_sweep = 0.0
    while not stop["flag"]:
        try:
            if time.monotonic() >= next_sweep:        # also runs immediately at startup => recovery after a restart
                log.info("sweep", extra=sweep(factory, s, queue))
                next_sweep = time.monotonic() + s.worker_sweep_seconds
            run_once(factory, s, worker_id)
            t0 = time.monotonic()
            if not queue.wait(s.worker_poll_seconds):
                spent = time.monotonic() - t0     # Redis down/unset returns at once: avoid a busy loop
                time.sleep(max(0.0, s.worker_poll_seconds - spent))
        except Exception:
            log.exception("worker loop error; continuing")
            time.sleep(s.worker_poll_seconds)
    log.info("worker stopped", extra={"worker": worker_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
