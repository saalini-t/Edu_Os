"""Student-facing views of practice and escalation state. The rule that matters: an answer key (or rubric) is revealed ONLY
for items the student has already submitted and that were scored; unanswered items expose just the question."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attempt, Escalation, PracticeItem, User

SCORED = ("GRADED", "UNCERTAIN")


def attempt_view(a: Attempt, it: PracticeItem) -> dict:
    scored = a.status in SCORED
    out = {"attempt_id": str(a.id), "status": a.status, "answer": a.answer, "scored": scored,
           "correct": a.correct if a.status == "GRADED" else None, "partial_credit": a.partial_credit,
           "feedback": a.feedback, "error_tags": a.error_tags, "uncertainty": a.uncertainty, "grader": a.grader,
           "counted_as_evidence": a.evidence_event_id is not None}
    if scored:     # reveal only after submission
        out["reveal"] = {"correct_answer": it.answer_key, "rubric": it.rubric if it.kind == "short_text" else None}
    return out


def practice_sets(db: Session, session_id: uuid.UUID) -> list[dict]:
    items = db.scalars(select(PracticeItem).where(PracticeItem.session_id == session_id).order_by(
        PracticeItem.set_index, PracticeItem.position)).all()
    by_item: dict[uuid.UUID, Attempt] = {}
    for a in db.scalars(select(Attempt).where(Attempt.session_id == session_id).order_by(Attempt.created_at)):
        if a.item_id not in by_item or a.status in SCORED:      # prefer the scored attempt; otherwise the latest
            by_item[a.item_id] = a
    sets: dict[int, list[dict]] = {}
    for it in items:
        a = by_item.get(it.id)
        sets.setdefault(it.set_index, []).append({
            "item_id": str(it.id), "position": it.position, "kind": it.kind, "prompt": it.prompt, "options": it.options,
            "difficulty": it.difficulty, "targets_suspected_gap": it.targets_hypothesis_id is not None,
            "source": it.source, "attempt": attempt_view(a, it) if a else None})
    return [{"set": k, "items": v} for k, v in sorted(sets.items())]


def escalation_info(db: Session, session_id: uuid.UUID) -> dict | None:
    esc = db.scalar(select(Escalation).where(Escalation.session_id == session_id).order_by(Escalation.created_at.desc()).limit(1))
    if esc is None:
        return None
    teacher = db.get(User, esc.assigned_teacher_id) if esc.assigned_teacher_id else None
    return {"id": str(esc.id), "status": esc.status, "assigned_teacher": teacher.display_name if teacher else None,
            "expires_at": esc.expires_at, "outcome": esc.resume_outcome,
            "waiting_for": "a teacher to accept" if esc.status == "OPEN" else ("the teacher to reply" if esc.status == "ACCEPTED" else None)}
