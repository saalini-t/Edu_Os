from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_idempotency import run_idempotent
from app.auth.deps import current_user, require_roles
from app.db import get_db
from app.errors import AppError, NotFound
from app.models import AvailabilitySlot, Escalation, TeacherCourse, TeacherProfile, TeacherTopic, Topic, User
from app.teaching.resume import reconcile, resume_escalation
from app.teaching.service import TeachingService

router = APIRouter(prefix="/v1", tags=["teaching"])


def _key(key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128)) -> str:
    if not key:
        raise AppError(400, "BAD_REQUEST", "Idempotency-Key header is required")
    return key


def _svc(request: Request, db: Session) -> TeachingService:
    return TeachingService(db, request.app.state.settings)


def _uuid(v: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise NotFound(what)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: uuid.UUID | None = None


class AssessmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic_id: uuid.UUID
    level: Literal["struggling", "emerging", "solid"]


class HypothesisDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: uuid.UUID
    decision: Literal["confirmed", "refuted"]


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field(min_length=1, max_length=4000)
    topic_assessments: list[AssessmentIn] = Field(default_factory=list, max_length=10)
    hypothesis_decisions: list[HypothesisDecisionIn] = Field(default_factory=list, max_length=10)


class SlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: AwareDatetime
    end_at: AwareDatetime


class AvailabilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: list[SlotIn] = Field(max_length=50)


class RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    helpful: bool


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teacher_id: uuid.UUID


# ----------------------------------------------------------------------------------------------- shared (role-aware)
@router.get("/escalations/{escalation_id}")
def get_escalation(escalation_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    svc = _svc(request, db)
    esc = svc.get_for(user, _uuid(escalation_id, "Escalation"))
    return {"student": svc.student_view, "teacher": svc.teacher_view, "admin": svc.admin_view}[user.role](esc)


@router.post("/escalations/{escalation_id}/messages", status_code=201)
def post_message(escalation_id: str, body: MessageIn, request: Request, key: str = Depends(_key),
                 user: User = Depends(require_roles("student", "teacher")), db: Session = Depends(get_db)):
    svc, eid = _svc(request, db), _uuid(escalation_id, "Escalation")

    def go():
        m = svc.add_message(user, eid, body.content)
        return {"id": str(m.id), "role": m.author_role, "content": m.content, "at": m.created_at}
    return run_idempotent(db, scope=f"msg:{user.id}:{escalation_id}", key=key, body=body.model_dump(), fn=go)


# ----------------------------------------------------------------------------------------------- student
@router.get("/escalations")
def my_escalations(request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    svc = _svc(request, db)
    rows = db.scalars(select(Escalation).where(Escalation.student_id == user.id).order_by(Escalation.created_at.desc()).limit(50))
    return {"items": [svc.summary_row(e) for e in rows]}


@router.post("/escalations/{escalation_id}/rate")
def rate(escalation_id: str, body: RateIn, request: Request, user: User = Depends(require_roles("student")),
         db: Session = Depends(get_db)):
    esc = _svc(request, db).rate(user, _uuid(escalation_id, "Escalation"), body.helpful)
    return {"id": str(esc.id), "helpful": esc.student_helpful}


# ----------------------------------------------------------------------------------------------- teacher
@router.get("/teacher/profile")
def teacher_profile(user: User = Depends(require_roles("teacher")), db: Session = Depends(get_db)):
    prof = db.get(TeacherProfile, user.id)
    topics = db.execute(select(Topic.name, TeacherTopic.proficiency).join(TeacherTopic, TeacherTopic.topic_id == Topic.id).where(
        TeacherTopic.teacher_id == user.id).order_by(Topic.sort)).all()
    courses = list(db.scalars(select(TeacherCourse.course_id).where(TeacherCourse.teacher_id == user.id)))
    return {"display_name": user.display_name, "bio": prof.bio if prof else "", "languages": prof.languages if prof else [],
            "active": bool(prof and prof.active), "courses": [str(c) for c in courses],
            "topics": [{"name": n, "proficiency": p} for n, p in topics]}


@router.get("/teacher/escalations")
def teacher_inbox(request: Request, status: Literal["OPEN", "ACCEPTED", "RESOLVED", "EXPIRED", "CANCELLED"] | None = None,
                  user: User = Depends(require_roles("teacher")), db: Session = Depends(get_db)):
    svc = _svc(request, db)
    return {"items": [svc.summary_row(e) for e in svc.list_for_teacher(user, status)]}


@router.post("/teacher/escalations/{escalation_id}/accept")
def accept(escalation_id: str, request: Request, body: AcceptIn | None = None, user: User = Depends(require_roles("teacher")),
           db: Session = Depends(get_db)):
    esc = _svc(request, db).accept(user, _uuid(escalation_id, "Escalation"), body.slot_id if body else None)
    return {"id": str(esc.id), "status": esc.status}


@router.post("/teacher/escalations/{escalation_id}/release")
def release(escalation_id: str, request: Request, user: User = Depends(require_roles("teacher", "admin")),
            db: Session = Depends(get_db)):
    esc = _svc(request, db).release(user, _uuid(escalation_id, "Escalation"))
    return {"id": str(esc.id), "status": esc.status}


@router.post("/teacher/escalations/{escalation_id}/resolve")
def resolve(escalation_id: str, body: ResolveIn, request: Request, key: str = Depends(_key),
            user: User = Depends(require_roles("teacher")), db: Session = Depends(get_db)):
    svc, eid = _svc(request, db), _uuid(escalation_id, "Escalation")
    s = request.app.state

    def go():
        svc.resolve(user, eid, notes=body.notes, topic_assessments=[a.model_dump() for a in body.topic_assessments],
                    hypothesis_decisions=[d.model_dump() for d in body.hypothesis_decisions])
        resumed = resume_escalation(db, s.settings, s.llm_provider, eid)       # a failure leaves it pending for the sweeper
        return {"id": escalation_id, "status": "RESOLVED", "workflow_resumed": resumed}
    return run_idempotent(db, scope=f"resolve:{user.id}:{escalation_id}", key=key,
                          body=body.model_dump(mode="json"), fn=go)


@router.get("/teacher/availability")
def get_availability(user: User = Depends(require_roles("teacher")), db: Session = Depends(get_db)):
    rows = db.scalars(select(AvailabilitySlot).where(AvailabilitySlot.teacher_id == user.id).order_by(AvailabilitySlot.start_at))
    return {"slots": [{"id": str(r.id), "start_at": r.start_at, "end_at": r.end_at, "booked": r.booked_by_escalation_id is not None}
                      for r in rows]}


@router.put("/teacher/availability")
def put_availability(body: AvailabilityIn, request: Request, key: str = Depends(_key),
                     user: User = Depends(require_roles("teacher")), db: Session = Depends(get_db)):
    svc = _svc(request, db)

    def go():
        slots = svc.replace_availability(user, [{"start_at": s.start_at, "end_at": s.end_at} for s in body.slots])
        return {"slots": [{"id": str(s.id), "start_at": s.start_at, "end_at": s.end_at} for s in slots]}
    return run_idempotent(db, scope=f"avail:{user.id}", key=key, body=body.model_dump(mode="json"), fn=go)


# ----------------------------------------------------------------------------------------------- admin
@router.get("/admin/escalations")
def admin_list(request: Request, status: str | None = None, user: User = Depends(require_roles("admin")),
               db: Session = Depends(get_db)):
    svc = _svc(request, db)
    q = select(Escalation).order_by(Escalation.created_at.desc()).limit(200)
    if status:
        q = q.where(Escalation.status == status)
    return {"items": [svc.summary_row(e) for e in db.scalars(q)]}


@router.get("/admin/teachers")
def admin_teachers(user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    rows = db.execute(select(User, TeacherProfile).join(TeacherProfile, TeacherProfile.user_id == User.id).order_by(User.display_name)).all()
    out = []
    for u, p in rows:
        load = db.scalar(select(func.count()).select_from(Escalation).where(Escalation.assigned_teacher_id == u.id, Escalation.status == "ACCEPTED"))
        out.append({"id": str(u.id), "display_name": u.display_name, "languages": p.languages, "active": p.active, "open_load": load,
                    "courses": [str(c) for c in db.scalars(select(TeacherCourse.course_id).where(TeacherCourse.teacher_id == u.id))]})
    return {"items": out}


@router.post("/admin/escalations/{escalation_id}/assign")
def admin_assign(escalation_id: str, body: AssignIn, request: Request, key: str = Depends(_key),
                 user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    svc, eid = _svc(request, db), _uuid(escalation_id, "Escalation")

    def go():
        esc = svc.admin_assign(user, eid, body.teacher_id)
        return {"id": str(esc.id), "status": esc.status, "assigned_teacher_id": str(esc.assigned_teacher_id)}
    return run_idempotent(db, scope=f"assign:{user.id}:{escalation_id}", key=key, body=body.model_dump(mode="json"), fn=go)


@router.post("/admin/escalations/reconcile")
def admin_reconcile(request: Request, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    """Expire overdue escalations and apply every pending workflow resume (also done periodically by the worker)."""
    s = request.app.state
    return reconcile(db, s.settings, s.llm_provider)
