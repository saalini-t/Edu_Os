"""Learning Gap Map and Learning Passport: deterministic read models derived only from the append-only evidence ledger,
the gap hypotheses and the workflow records. Same inputs -> same output (the passport carries a digest to prove it)."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.learner.service import LearnerService
from app.models import (
    Attempt, DoubtSession, Escalation, EvidenceEvent, GapHypothesis, Intervention, PracticeItem, TeacherFeedback, Topic, User,
)

STATUSES = ("not_assessed", "suspected_gap", "practising", "improving", "demonstrated")
IMPROVING_MEAN = 0.6      # documented, untuned: emerging mastery at/above this mean is shown as "improving"
LABEL = {"not_assessed": "Not assessed yet", "suspected_gap": "Suspected gap", "practising": "Practising",
         "improving": "Improving", "demonstrated": "Understanding demonstrated"}


def _status(view: dict, hyps: list[GapHypothesis]) -> tuple[str, str, str | None]:
    """-> (status, reason, gap_kind). Precedence: an open/confirmed hypothesis is shown as a gap until it is refuted or expires."""
    live = [h for h in hyps if h.status in ("proposed", "confirmed")]
    if live:
        kind = "confirmed" if any(h.status == "confirmed" for h in live) else "suspected"
        why = ("A teacher, or two failed attempts on distinct targeted questions, confirmed this gap." if kind == "confirmed"
               else "This is only a hypothesis from your question; it is not confirmed.")
        return "suspected_gap", why, kind
    n = view["evidence_count"]
    if n == 0:
        return "not_assessed", "No graded practice or teacher assessment yet. Saying you understood does not count.", None
    if view["status"] == "demonstrated":
        return "demonstrated", f"Correct work on {view['distinct_sources']} different items: estimate {view['mean']:.2f}.", None
    if view["mean"] >= IMPROVING_MEAN:
        return "improving", f"Estimate {view['mean']:.2f} from {n} piece(s) of evidence; more distinct correct items are needed.", None
    return "practising", f"Estimate {view['mean']:.2f} from {n} piece(s) of evidence.", None


def _evidence(db: Session, student_id: uuid.UUID, topic_id: uuid.UUID, limit: int = 6) -> list[dict]:
    rows = db.scalars(select(EvidenceEvent).where(EvidenceEvent.student_id == student_id, EvidenceEvent.topic_id == topic_id)
                      .order_by(EvidenceEvent.created_at.desc(), EvidenceEvent.id).limit(limit))
    return [{"id": str(e.id), "type": e.evidence_type, "weight": e.weight, "at": e.created_at,
             "attempt_id": (e.provenance or {}).get("attempt_id"), "escalation_id": (e.provenance or {}).get("escalation_id")}
            for e in rows]


def gap_map(db: Session, settings: Settings, student_id: uuid.UUID, course_id: uuid.UUID) -> dict:
    svc = LearnerService(db, settings)
    topics = list(db.scalars(select(Topic).where(Topic.course_id == course_id).order_by(Topic.sort)))
    by_slug = {t.slug: t for t in topics}
    nodes = {}
    for t in topics:
        v = svc.view(student_id, t.id)
        hyps = list(db.scalars(select(GapHypothesis).where(GapHypothesis.student_id == student_id, GapHypothesis.topic_id == t.id)
                               .order_by(GapHypothesis.created_at)))
        status, why, kind = _status(v, hyps)
        nodes[t.slug] = {
            "topic_id": str(t.id), "slug": t.slug, "name": t.name, "status": status, "label": LABEL[status], "gap_kind": kind,
            "reason": why, "mean": v["mean"] if v["evidence_count"] else None, "evidence_count": v["evidence_count"],
            "last_evidence_at": v["last_evidence_at"], "evidence": _evidence(db, student_id, t.id),
            "prerequisites": [str(by_slug[s].id) for s in (t.prerequisites or []) if s in by_slug],
            "hypotheses": [{"id": str(h.id), "description": h.description, "status": h.status, "created_at": h.created_at,
                            "resolution": h.resolution} for h in hyps]}
    for t in topics:        # a possible root cause is a prerequisite that is itself a gap: a SUGGESTION to look there, not a diagnosis
        n = nodes[t.slug]
        n["check_prerequisites"] = [{"topic_id": nodes[s]["topic_id"], "name": nodes[s]["name"], "status": nodes[s]["status"]}
                                   for s in (t.prerequisites or []) if s in nodes and n["status"] in ("suspected_gap", "practising")
                                   and nodes[s]["status"] in ("suspected_gap", "not_assessed")]
    counts = {s: sum(1 for n in nodes.values() if n["status"] == s) for s in STATUSES}
    return {"course_id": str(course_id), "topics": list(nodes.values()), "counts": counts, "statuses": list(STATUSES),
            "note": ("Statuses come from graded practice and teacher assessments only. Acknowledgements (understood / still confused / "
                     "check me) carry zero weight. This describes your work on these topics, not your ability.")}


def _iso(o):
    return o.isoformat() if isinstance(o, datetime) else str(o)


def passport(db: Session, settings: Settings, student: User, course_id: uuid.UUID) -> dict:
    svc = LearnerService(db, settings)
    gm = gap_map(db, settings, student.id, course_id)
    attempts = db.execute(select(PracticeItem.topic_id, func.count(Attempt.id), func.count(Attempt.id).filter(Attempt.correct.is_(True)))
                          .join(PracticeItem, PracticeItem.id == Attempt.item_id)
                          .where(Attempt.student_id == student.id, Attempt.status.in_(("GRADED", "UNCERTAIN")))
                          .group_by(PracticeItem.topic_id)).all()
    att = {str(t): {"attempts": n, "correct": c} for t, n, c in attempts}
    for n in gm["topics"]:
        n["attempts"] = att.get(n["topic_id"], {"attempts": 0, "correct": 0})
    sessions = list(db.scalars(select(DoubtSession).where(DoubtSession.student_id == student.id, DoubtSession.course_id == course_id)
                               .order_by(DoubtSession.created_at, DoubtSession.id)))
    doubts = []
    for s in sessions:
        last = db.scalar(select(Intervention).where(Intervention.session_id == s.id).order_by(Intervention.created_at.desc()).limit(1))
        outcome = (last.payload or {}).get("outcome") if last else None
        first = s.messages[0].content if s.messages else ""
        doubts.append({"session_id": str(s.id), "created_at": s.created_at, "text": first[:200], "status": s.status, "outcome": outcome,
                       "unresolved": s.status != "COMPLETED" or outcome in ("UNRESOLVED", "UNVERIFIED"),
                       "last_action": last.action if last else None, "last_rule": last.rule_id if last else None})
    feedback = db.execute(select(TeacherFeedback, Escalation).join(Escalation, Escalation.id == TeacherFeedback.escalation_id)
                          .where(Escalation.student_id == student.id).order_by(TeacherFeedback.created_at)).all()
    fb = [{"escalation_id": str(e.id), "at": f.created_at, "notes": f.notes, "assessments": f.topic_assessments} for f, e in feedback]
    body = {"student": {"display_name": student.display_name}, "course_id": str(course_id),
            "topics": gm["topics"], "counts": gm["counts"], "doubts": doubts, "teacher_feedback": fb,
            "history": svc.history(student.id, limit=100)}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=_iso).encode()).hexdigest()
    return {**body, "digest": digest, "totals": {"doubts": len(doubts), "unresolved": sum(d["unresolved"] for d in doubts),
                                                 "graded_attempts": sum(a["attempts"] for a in att.values())},
            "retention": ("Evidence is append-only and kept while your account exists. An administrator can anonymize your account: "
                          "personal content is deleted and evidence stays only under a pseudonym. Nothing is silently rewritten."),
            "note": "Derived deterministically from your evidence ledger. It never labels ability or intelligence."}
