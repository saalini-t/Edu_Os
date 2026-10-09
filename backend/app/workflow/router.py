from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import require_roles
from app.auth.principal import principal_for
from app.db import get_db
from app.errors import AppError, Forbidden, NotFound
from app.agents.evaluation import resolve_mcq_choice
from app.api_idempotency import run_idempotent
from app.learner.service import LearnerService
from app.models import Attempt, DoubtSession, IdempotencyKey, PracticeItem, Topic, User, WorkflowRun
from app.workflow.engine import WorkflowEngine
from app.workflow.explain import decision_explanations
from app.workflow.views import SCORED, attempt_view, escalation_info, practice_sets

router = APIRouter(prefix="/v1", tags=["doubts"])


class DoubtIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: uuid.UUID
    text: str = Field(min_length=3, max_length=2000)
    client_ref: str | None = Field(default=None, max_length=128)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8,
                                             max_length=128)) -> str:
    if not key:
        raise AppError(400, "BAD_REQUEST", "Idempotency-Key header is required")
    return key


def _engine(request: Request, db: Session, user: User) -> WorkflowEngine:
    return WorkflowEngine(db, request.app.state.settings, request.app.state.llm_provider, principal_for(db, user))


def _load_session(db: Session, user: User, session_id: str) -> DoubtSession:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise NotFound("Session")
    s = db.get(DoubtSession, sid)
    # students only ever see their own sessions; admins may read any; others get 404 (no existence leak)
    if s is None or not (s.student_id == user.id or user.role == "admin"):
        raise NotFound("Session")
    return s


def session_view(db: Session, s: DoubtSession, *, admin: bool) -> dict:
    topic = db.get(Topic, s.topic_id) if s.topic_id else None
    latest = s.interventions[-1] if s.interventions else None
    li = None
    if latest is not None:
        li = {"id": str(latest.id), "action": latest.action, "created_at": latest.created_at, **latest.payload}
        if admin:
            li["rule_id"] = latest.rule_id
    return {
        "session_id": str(s.id), "run_id": str(s.run_id) if s.run_id else None, "status": s.status,
        "course_id": str(s.course_id),
        "topic": {"id": str(topic.id), "name": topic.name} if topic else None,
        "latest_intervention": li,
        "practice": practice_sets(db, s.id),
        "escalation": escalation_info(db, s.id),
        "messages": [{"id": str(m.id), "role": m.role, "content": m.content, "created_at": m.created_at}
                     for m in s.messages],
        "created_at": s.created_at,
    }


@router.post("/doubts", status_code=202)
def create_doubt(body: DoubtIn, request: Request, key: str = Depends(idempotency_key),
                 user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    principal = principal_for(db, user)
    if body.course_id not in principal.allowed_course_ids:
        raise Forbidden("Not enrolled in this course")
    scope = f"POST /v1/doubts:{user.id}"
    req_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    prior = db.get(IdempotencyKey, (scope, key))
    if prior is not None:
        if prior.request_hash != req_hash:
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was used with a different request")
        return prior.response
    session = DoubtSession(student_id=user.id, course_id=body.course_id, status="CREATED")
    db.add(session)
    db.flush()
    eng = _engine(request, db, user)
    eng.core.add_message(session.id, "student", body.text)
    run = eng.create_run(session_id=session.id, student_id=user.id, course_id=body.course_id,
                         doubt_text=body.text, trigger_id=key)
    run = eng.advance(run.id)
    response = {"session_id": str(session.id), "run_id": str(run.id), "status": run.status,
                "created_at": session.created_at.isoformat()}
    db.add(IdempotencyKey(scope=scope, key=key, request_hash=req_hash, response=response))
    try:
        db.commit()
    except IntegrityError:  # concurrent duplicate request: return the winner's response
        db.rollback()
        prior = db.get(IdempotencyKey, (scope, key))
        if prior is None:
            raise
        return prior.response
    return response


@router.get("/doubts")
def list_doubts(user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    rows = db.scalars(select(DoubtSession).where(DoubtSession.student_id == user.id)
                      .order_by(DoubtSession.created_at.desc()).limit(50))
    items = []
    for s in rows:
        t = db.get(Topic, s.topic_id) if s.topic_id else None
        items.append({"session_id": str(s.id), "status": s.status, "course_id": str(s.course_id), "created_at": s.created_at,
                      "topic": t.name if t else None, "text": (s.messages[0].content[:160] if s.messages else "")})
    return {"items": items, "next_cursor": None}


@router.get("/doubts/{session_id}")
def get_doubt(session_id: str, user: User = Depends(require_roles("student", "admin")),
              db: Session = Depends(get_db)):
    s = _load_session(db, user, session_id)
    return session_view(db, s, admin=user.role == "admin")


@router.get("/doubts/{session_id}/decisions")
def doubt_decisions(session_id: str, user: User = Depends(require_roles("student", "admin")), db: Session = Depends(get_db)):
    """Why the system chose each next step: the rule that fired, its recorded reasons and the evidence it used."""
    s = _load_session(db, user, session_id)
    items = decision_explanations(db, s.run_id, admin=user.role == "admin") if s.run_id else []
    return {"items": items, "note": "Built from recorded rules and evidence only. A language model never makes these decisions."}


@router.post("/doubts/{session_id}/messages", status_code=202)
def post_message(session_id: str, body: MessageIn, request: Request,
                 user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    s = _load_session(db, user, session_id)
    run = _engine(request, db, user).apply_event(s.run_id, "student_message", {"text": body.text})
    return {"run_id": str(run.id), "status": run.status}


class AckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ack: Literal["understood", "still_confused", "check_me"]


@router.post("/doubts/{session_id}/ack", status_code=202)
def acknowledge(session_id: str, body: AckIn, request: Request, key: str = Depends(idempotency_key),
                user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    """The student says whether the explanation helped. This is recorded in the evidence ledger as a SELF-REPORT with
    ZERO mastery weight and it never marks a topic as mastered or a gap as confirmed."""
    s = _load_session(db, user, session_id)               # 404 for anyone but the owner (and admins, who then cannot write: role check)
    if s.run_id is None:
        raise AppError(409, "INVALID_RUN_STATE", "Session has no workflow run")
    scope = f"POST /v1/doubts/ack:{user.id}:{session_id}"
    req_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    prior = db.get(IdempotencyKey, (scope, key))
    if prior is not None:
        if prior.request_hash != req_hash:
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was used with a different request")
        return prior.response                              # duplicate delivery: replay, apply nothing
    last = s.interventions[-1] if s.interventions else None
    try:
        run = _engine(request, db, user).apply_event(s.run_id, "student_ack", {
            "ack": body.ack, "source_ref": f"ack:{s.run_id}:{key}", "intervention_id": str(last.id) if last else None})
    except IntegrityError:
        db.rollback()
        raise AppError(409, "DUPLICATE_ACK", "This acknowledgment was already processed")
    response = {"run_id": str(run.id), "status": run.status, "ack": body.ack, "mastery_credit": 0.0}
    db.add(IdempotencyKey(scope=scope, key=key, request_hash=req_hash, response=response))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.get(IdempotencyKey, (scope, key))
        if prior is None:
            raise
        return prior.response
    return response


@router.post("/doubts/{session_id}/request-teacher", status_code=202)
def request_teacher(session_id: str, request: Request, user: User = Depends(require_roles("student")),
                    db: Session = Depends(get_db)):
    s = _load_session(db, user, session_id)
    run = _engine(request, db, user).apply_event(s.run_id, "student_requests_teacher", {})
    return {"run_id": str(run.id), "status": run.status}


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: uuid.UUID
    answer: str = Field(min_length=1, max_length=2000)
    hints_used: int = Field(default=0, ge=0, le=5)


@router.post("/doubts/{session_id}/answers", status_code=200)
def submit_answer(session_id: str, body: AnswerIn, request: Request, key: str = Depends(idempotency_key),
                  user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    """Submit ONE practice answer. The answer is graded (objective items deterministically), evidence is written to the ledger
    only if the grade counts, and the correct answer is revealed only now. One scored attempt per item; a repeated delivery with
    the same Idempotency-Key replays the first response."""
    s = _load_session(db, user, session_id)
    if s.run_id is None:
        raise AppError(409, "INVALID_RUN_STATE", "Session has no workflow run")
    item = db.get(PracticeItem, body.item_id)
    if item is None or item.student_id != user.id or item.session_id != s.id:
        raise NotFound("Question")                           # another student's question: no existence leak
    if item.kind == "mcq" and resolve_mcq_choice(body.answer, item.options or []) is None:
        raise AppError(422, "VALIDATION_ERROR", "Choose one of the listed options")

    def go() -> dict:
        if db.scalar(select(Attempt.id).where(Attempt.item_id == item.id, Attempt.status.in_(SCORED))) is not None:
            raise AppError(409, "ITEM_ALREADY_ANSWERED", "This question has already been answered")
        att = Attempt(item_id=item.id, student_id=user.id, session_id=s.id, run_id=s.run_id, answer=body.answer.strip(),
                      hints_used=body.hints_used, idempotency_key=key, status="SUBMITTED")
        db.add(att)
        db.flush()
        try:
            run = _engine(request, db, user).apply_event(s.run_id, "practice_answer", {"attempt_id": str(att.id)})
        except IntegrityError:                               # lost a race with a concurrent scored attempt on the same item
            db.rollback()
            raise AppError(409, "ITEM_ALREADY_ANSWERED", "This question has already been answered")
        db.refresh(att)
        st = run.state
        remaining = [i for i in st["practice"]["item_ids"] if i not in st["practice"]["answered"]]
        view = LearnerService(db, request.app.state.settings).view(user.id, item.topic_id)
        return {**attempt_view(att, item), "item_id": str(item.id), "run_status": run.status, "remaining": len(remaining),
                "mastery": {k: view[k] for k in ("status", "mean", "evidence_count", "distinct_sources")}}
    return run_idempotent(db, scope=f"answer:{user.id}:{session_id}", key=key,
                          body={"item_id": str(body.item_id), "answer": body.answer, "hints_used": body.hints_used}, fn=go)
