from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_roles
from app.db import get_db
from app.errors import NotFound
from app.auth.principal import principal_for
from app.learner.service import LearnerService
from app.learner.views import gap_map, passport
from app.models import EvidenceEvent, User

router = APIRouter(prefix="/v1", tags=["learner"])


def _out(rows) -> dict:
    return {"items": [{"id": str(e.id), "evidence_type": e.evidence_type, "weight": e.weight,
                       "topic_id": str(e.topic_id) if e.topic_id else None, "source_run_id": str(e.source_run_id),
                       "provenance": e.provenance, "created_at": e.created_at} for e in rows],
            "note": "Self-reported evidence (understood / confused / check me) carries zero mastery weight."}


@router.get("/learners/me/evidence")
def my_evidence(user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    rows = db.scalars(select(EvidenceEvent).where(EvidenceEvent.student_id == user.id)
                      .order_by(EvidenceEvent.created_at).limit(200))
    return _out(rows)


@router.get("/admin/learners/{student_id}/evidence")
def student_evidence(student_id: str, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise NotFound("Student")
    rows = db.scalars(select(EvidenceEvent).where(EvidenceEvent.student_id == sid)
                      .order_by(EvidenceEvent.created_at).limit(200))
    return _out(rows)


@router.get("/learners/me/progress")
def my_progress(request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    """Per-topic mastery from the ledger, with suspected gaps (hypotheses) kept separate from confirmed gaps."""
    return {"topics": LearnerService(db, request.app.state.settings).progress(user.id),
            "note": "Mastery is computed only from graded practice and teacher assessments. Acknowledgments carry no weight. "
                    "A suspected gap is a hypothesis until it is confirmed."}


@router.get("/learners/me/history")
def my_history(request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    return {"items": LearnerService(db, request.app.state.settings).history(user.id)}


@router.get("/admin/learners/{student_id}/progress")
def student_progress(student_id: str, request: Request, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise NotFound("Student")
    return {"topics": LearnerService(db, request.app.state.settings).progress(sid)}


def _course(db: Session, user: User, course_id: str | None) -> uuid.UUID:
    """The student's own enrolled course (the requested one, or their first). Anything else is a 404, not a hint."""
    allowed = principal_for(db, user).allowed_course_ids
    try:
        cid = uuid.UUID(course_id) if course_id else (sorted(allowed, key=str)[0] if allowed else None)
    except ValueError:
        cid = None
    if cid is None or cid not in allowed:
        raise NotFound("Course")
    return cid


@router.get("/learners/me/gap-map")
def my_gap_map(request: Request, course_id: str | None = None, user: User = Depends(require_roles("student")),
               db: Session = Depends(get_db)):
    return gap_map(db, request.app.state.settings, user.id, _course(db, user, course_id))


@router.get("/learners/me/passport")
def my_passport(request: Request, course_id: str | None = None, user: User = Depends(require_roles("student")),
                db: Session = Depends(get_db)):
    return passport(db, request.app.state.settings, user, _course(db, user, course_id))
