"""Resuming the workflow after an escalation ends. The teaching service marks `resume_status = pending` in the same transaction
that records the outcome; this module applies it to the run. It is idempotent and retried by the worker sweep and by an admin
endpoint, so a crash between "teacher resolved" and "run resumed" can never strand a student."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.knowledge.service import Principal
from app.llm.base import LLMProvider
from app.models import Escalation
from app.teaching.service import TeachingService

log = logging.getLogger("eduos.teaching")


def resume_escalation(db: Session, settings: Settings, provider: LLMProvider, esc_id: uuid.UUID) -> bool:
    """Apply a pending resume. Returns True if the escalation is (now) fully resumed."""
    from app.workflow.engine import WorkflowEngine          # imported here: the engine imports the teaching service
    esc = db.get(Escalation, esc_id)
    if esc is None or esc.resume_status != "pending":
        return esc is not None and esc.resume_status == "done"
    run_id, outcome, message = esc.run_id, esc.resume_outcome, esc.resume_message or ""
    engine = WorkflowEngine(db, settings, provider, Principal(esc.student_id, "student", frozenset()))
    try:
        engine.resume_after_escalation(run_id, esc_id, outcome, message)
    except Exception:
        db.rollback()
        log.exception("resume failed; it stays pending and will be retried", extra={"escalation_id": str(esc_id)})
        return False
    esc = db.get(Escalation, esc_id)
    esc.resume_status = "done"
    TeachingService(db, settings).event(esc, "resume_applied", None, outcome=outcome)
    db.commit()
    return True


def reconcile(db: Session, settings: Settings, provider: LLMProvider, *, expire: bool = True) -> dict:
    """Expire overdue escalations, then apply every pending resume. Safe to run concurrently and repeatedly."""
    expired = TeachingService(db, settings).expire_due() if expire else []
    pending = list(db.scalars(select(Escalation.id).where(Escalation.resume_status == "pending")))
    done = sum(1 for eid in pending if resume_escalation(db, settings, provider, eid))
    return {"expired": len(expired), "pending": len(pending), "resumed": done}
