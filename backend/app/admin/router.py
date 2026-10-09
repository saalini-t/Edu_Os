from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_roles
from app.db import get_db
from app.errors import NotFound
from sqlalchemy import func

from app.errors import AppError
from app.learner.anonymize import anonymize_student
from app.models import (
    Document, DecisionRecord, Escalation, IngestionJob, User, WorkflowRun, WorkflowStep,
)
from app.ops import retrieval_info

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/runs")
def list_runs(user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(50))
    return {"items": [{"run_id": str(r.id), "session_id": str(r.session_id), "status": r.status,
                       "student_ref": r.state.get("student_ref"), "created_at": r.created_at} for r in rows]}


@router.get("/runs/{run_id}/trace")
def run_trace(run_id: str, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise NotFound("Run")
    run = db.get(WorkflowRun, rid)
    if run is None:
        raise NotFound("Run")
    steps = list(db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == rid).order_by(WorkflowStep.seq)))
    decisions = list(db.scalars(select(DecisionRecord).where(DecisionRecord.run_id == rid)
                                .order_by(DecisionRecord.created_at)))
    st = run.state
    retrieval = [{"seq": s.seq, "query": s.output.get("query"), "terms": s.output.get("terms"),
                  "mode": s.output.get("retrieval"),
                  "chunk_ids": [r["chunk_id"] for r in s.output.get("results", [])],
                  "results": s.output.get("results", [])} for s in steps if s.node == "load_context"]
    citation_validation = [{"seq": s.seq, "fallback": s.output.get("fallback"),
                            "checks": s.output.get("citation_checks", []),
                            "n_verified": s.output.get("n_verified"), "n_stripped": s.output.get("n_stripped")}
                           for s in steps if s.node == "explain"]
    final = decisions[-1] if decisions else None
    return {
        "run": {"run_id": str(run.id), "session_id": str(run.session_id), "status": run.status,
                "current_node": run.current_node, "outcome": st.get("outcome"), "version": run.version,
                "student_id": str(run.student_id), "student_ref": st.get("student_ref"),
                "created_at": run.created_at, "updated_at": run.updated_at},
        "summary": {
            "rules_fired": [d.rule_id for d in decisions],
            "final_rule_id": final.rule_id if final else None,
            "final_action": final.action if final else None,
            "providers_used": sorted({s.provider for s in steps if s.provider}),
            "counters": st.get("counters"), "budgets": st.get("budgets"), "flags": st.get("flags"),
        },
        "steps": [{"seq": s.seq, "node": s.node, "started_at": s.started_at, "latency_ms": s.latency_ms,
                   "provider": s.provider, "model": s.model, "prompt_version": s.prompt_version,
                   "input_hash": s.input_hash, "error": s.error, "output": s.output} for s in steps],
        "decisions": [{"decision_id": str(d.id), "evidence_refs": d.evidence_refs, "context": d.context, "rule_id": d.rule_id, "action": d.action, "outcome": d.outcome, "reasons": d.reasons,
                       "inputs_snapshot": d.inputs_snapshot, "advisor": d.advisor, "overridden": d.overridden,
                       "created_at": d.created_at} for d in decisions],
        "retrieval": retrieval,
        "citation_validation": citation_validation,
    }


@router.get("/ingestion/jobs")
def ingestion_jobs(status: str | None = None, limit: int = 50, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    q = select(IngestionJob, Document.title, Document.status).join(Document, Document.id == IngestionJob.document_id).order_by(
        IngestionJob.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        q = q.where(IngestionJob.status == status)
    return {"items": [{"job_id": str(j.id), "document_id": str(j.document_id), "title": t, "document_status": ds, "kind": j.kind,
                       "status": j.status, "stage": j.stage, "attempts": j.attempts, "max_attempts": j.max_attempts,
                       "error_code": j.error_code, "last_error": j.last_error, "queue": "deferred" if j.enqueue_error else "notified",
                       "created_at": j.created_at, "finished_at": j.finished_at} for j, t, ds in db.execute(q)]}


@router.get("/system")
def system_overview(request: Request, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    """Operational snapshot: retrieval mode actually in effect, model provider reachability, and queue / workflow counters."""
    def counts(col):
        return {k: v for k, v in db.execute(select(col, func.count()).group_by(col))}
    try:
        llm = request.app.state.llm_provider.status()
    except Exception as e:
        llm = {"provider": request.app.state.llm_provider.name, "reachable": False, "error": type(e).__name__}
    return {"retrieval": retrieval_info(request.app), "llm": llm,
            "documents": counts(Document.status), "ingestion_jobs": counts(IngestionJob.status),
            "workflow_runs": counts(WorkflowRun.status), "escalations": counts(Escalation.status),
            "pending_resumes": db.scalar(select(func.count()).select_from(Escalation).where(Escalation.resume_status == "pending"))}


class AnonymizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


@router.post("/students/{student_id}/anonymize")
def anonymize(student_id: str, body: AnonymizeIn, request: Request, user: User = Depends(require_roles("admin")),
              db: Session = Depends(get_db)):
    """Irreversible. Keeps de-identified learning evidence, deletes everything personal. See ADR-013."""
    if not body.confirm:
        raise AppError(422, "VALIDATION_ERROR", "Set confirm=true: this operation cannot be undone")
    try:
        sid = uuid.UUID(student_id)
    except ValueError:
        raise NotFound("Student")
    return {"anonymized": True, "summary": anonymize_student(db, request.app.state.settings, sid, user.id)}
