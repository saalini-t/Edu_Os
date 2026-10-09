"""Human-readable explanation of each policy decision, built ONLY from recorded rules, reasons, evidence references and step
records. No model text and no hidden reasoning is involved."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DecisionRecord, EvidenceEvent, WorkflowStep

TITLE = {
    "R1_explicit_teacher_request": "You asked for a teacher",
    "R1b_safety_flag": "Your message needs a person to look at it",
    "R2_budget_exhausted": "Automatic help limit reached, so a teacher is needed",
    "R3_needs_clarification": "The question needs a little more detail",
    "R4_no_grounding": "No supporting course material was found",
    "R5_repeated_failure": "Repeated unsuccessful checks, so a teacher is needed",
    "R6_low_evidence_explain": "Little evidence yet, so an explanation comes first",
    "R7_check_after_explanation": "A short practice check follows the explanation",
    "R8_mastery_demonstrated": "Your practice demonstrated understanding",
    "R9_retry_explain_new_angle": "The check was not passed, so the idea is explained from a new angle",
    "R10_default_clarify": "Not enough information, so a clarifying question is asked",
    "RESUME_TEACHER_RESOLVED": "A teacher resolved the doubt",
    "RESUME_ESCALATION_EXPIRED": "No teacher replied in time",
    "RESUME_ESCALATION_CANCELLED": "The teacher request was cancelled",
}
HARD = ("R1_explicit_teacher_request", "R1b_safety_flag", "R2_budget_exhausted")


def decision_explanations(db: Session, run_id: uuid.UUID, *, admin: bool) -> list[dict]:
    decisions = list(db.scalars(select(DecisionRecord).where(DecisionRecord.run_id == run_id).order_by(DecisionRecord.created_at)))
    steps = list(db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run_id).order_by(WorkflowStep.seq)))
    notes = [{"node": s.node, "provider": s.provider, "model": s.model, "prompt_version": s.prompt_version,
              "fallback": bool((s.output.get("agent") or {}).get("fallback") or s.output.get("fallback")),
              "error": s.error or (s.output.get("agent") or {}).get("error"), "at": s.created_at}
             for s in steps if s.provider or s.error]
    ids = {uuid.UUID(r) for d in decisions for r in (d.evidence_refs or [])}
    ev = {e.id: e for e in db.scalars(select(EvidenceEvent).where(EvidenceEvent.id.in_(ids)))} if ids else {}
    out = []
    for d in decisions:
        snap = d.inputs_snapshot or {}
        before = [n for n in notes if n["at"] <= d.created_at]
        out.append({
            "decision_id": str(d.id), "run_id": str(run_id), "at": d.created_at, "rule_id": d.rule_id, "action": d.action,
            "title": TITLE.get(d.rule_id, d.rule_id), "reasons": d.reasons, "hard_rule": d.rule_id in HARD,
            "precedence": ("A hard rule: it is checked before every adaptive rule and no model can override it."
                           if d.rule_id in HARD else "Adaptive rule, applied because no hard rule (R1, R1b, R2) fired first."),
            "outcome": d.outcome, "overridden": d.overridden,
            "mastery": snap.get("mastery") if isinstance(snap, dict) else None,
            "evidence": [{"id": r, "type": ev[uuid.UUID(r)].evidence_type, "weight": ev[uuid.UUID(r)].weight,
                          "at": ev[uuid.UUID(r)].created_at} for r in (d.evidence_refs or []) if uuid.UUID(r) in ev],
            "provider": (d.context or {}).get("provider"), "model": (d.context or {}).get("model"),
            "fallbacks": sorted({n["node"] for n in before if n["fallback"]}),
            "model_calls": [{k: n[k] for k in ("node", "provider", "model", "prompt_version", "fallback")} for n in before][-4:] if admin else None,
            "errors": [{"node": n["node"], "error": n["error"]} for n in before if n["error"]][-3:] if admin else None})
    return out
