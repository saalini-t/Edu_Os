"""Crash recovery for workflow runs. A node that calls a model commits (status RUNNING) and then waits with no lock held. If the
process dies in that window the run would stay RUNNING forever, so the worker sweep re-drives runs whose lease has expired.
`advance` itself refuses to touch a RUNNING run with a fresh lease, so a healthy executor is never run twice."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import principal_for
from app.config import Settings
from app.llm.base import LLMProvider
from app.models import User, WorkflowRun
from app.workflow.engine import WorkflowEngine

log = logging.getLogger("eduos.workflow.recovery")


def stuck_run_ids(db: Session, settings: Settings, limit: int = 20) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.workflow_lease_seconds)
    return list(db.scalars(select(WorkflowRun.id).where(WorkflowRun.status == "RUNNING", WorkflowRun.updated_at < cutoff)
                           .order_by(WorkflowRun.updated_at).limit(limit)))


def recover_stuck_runs(db: Session, settings: Settings, provider: LLMProvider) -> dict:
    """Resume every RUNNING run whose lease expired. Safe to call concurrently and repeatedly."""
    ids = stuck_run_ids(db, settings)
    resumed = 0
    for run_id in ids:
        run = db.get(WorkflowRun, run_id)
        student = db.get(User, run.student_id) if run else None
        if run is None or student is None:
            continue
        try:
            WorkflowEngine(db, settings, provider, principal_for(db, student)).advance(run_id)
            resumed += 1
        except Exception:                                   # one broken run must not stop the sweep
            db.rollback()
            log.exception("could not recover run", extra={"run_id": str(run_id)})
    return {"stuck": len(ids), "resumed": resumed}
