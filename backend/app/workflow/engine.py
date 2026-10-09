"""Persisted, bounded workflow state machine (docs/ARCHITECTURE.md section 7).

    understand -> load_context -> decide -> clarify | explain | practice | escalate | complete
    practice -> [student answers each item] -> evaluate -> update_evidence -> (when the set is done) decide again
    escalate -> WAITING_HUMAN -> resume (teacher resolved / expired) -> complete

Every node commits its step row and the new state together, so a run can always be resumed from the database. Policy rules
R1-R10 live in policy.decide() and are not modified here; the engine only supplies their inputs from persisted facts."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import evaluation as evaluation_agent
from app.agents import explanation as explanation_agent
from app.agents import practice as practice_agent
from app.agents import understanding as understanding_agent
from app.agents.common import AgentRun
from app.config import Settings
from app.errors import AppError
from app.knowledge.service import KnowledgeService, Principal
from app.llm.base import LLMProvider
from app.llm.schemas import DoubtAnalysis
from app.models import DecisionRecord, WorkflowRun, WorkflowStep
from app.workflow.citations import injection_flags
from app.workflow.gateway import CoreGateway
from app.workflow.policy import (
    Budgets, CheckResult, Counters, Decision, Flags, MasterySummary, PolicyInput, RetrievalSupport, Thresholds, decide,
)

log = logging.getLogger("eduos.workflow")

RunStatus = Literal["CREATED", "RUNNING", "AWAITING_STUDENT", "AWAITING_ANSWER", "WAITING_HUMAN",
                    "COMPLETED", "FAILED", "CANCELLED"]
HARD_RULES = {"R1_explicit_teacher_request", "R1b_safety_flag", "R2_budget_exhausted"}   # precede every adaptive rule
MAX_NODE_EXECUTIONS = 25  # hard guard per advance() call, independent of the policy budgets
MAX_DOUBT_CHARS = 2000
EASIER = {"hard": "medium", "medium": "easy", "easy": "easy"}


def student_ref(student_id: uuid.UUID) -> str:
    return "sr_" + hashlib.sha256(f"student:{student_id}".encode()).hexdigest()[:10]


class Pending(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["student_message", "student_ack", "practice_answer", "teacher", "none"] = "none"
    escalation_id: str | None = None


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    chunk_ids: list[str] = []
    retrieval: RetrievalSupport = RetrievalSupport(available=False)
    mastery: MasterySummary = MasterySummary()


class PracticeState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = []
    answered: list[str] = []
    results: dict[str, dict] = {}      # item_id -> {"correct": bool | None, "evidence": bool}


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    run_id: str
    session_id: str
    student_id: str
    student_ref: str
    course_id: str
    doubt_text: str
    history: list[str] = []            # later student messages (clarification replies)
    counters: Counters = Counters()
    budgets: Budgets
    thresholds: Thresholds
    analysis: DoubtAnalysis | None = None
    context: RunContext = Field(default_factory=RunContext)
    explained: bool = False
    acked: Literal["none", "understood", "still_confused", "check_me"] = "none"
    practice: PracticeState = Field(default_factory=PracticeState)
    last_check: CheckResult | None = None          # outcome of the latest completed practice set (reset by a new explanation)
    last_decision: Decision | None = None
    pending: Pending = Pending()
    flags: Flags = Flags()
    escalation_id: str | None = None
    outcome: Literal["RESOLVED", "UNRESOLVED", "UNVERIFIED"] | None = None   # UNVERIFIED: acknowledged, mastery not verified
    step_seq: int = 0


def _hash(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


class WorkflowEngine:
    def __init__(self, db: Session, settings: Settings, provider: LLMProvider, principal: Principal):
        self.db, self.s, self.provider, self.principal = db, settings, provider, principal
        self.knowledge = KnowledgeService(db, settings)
        self.retriever = self.knowledge.retriever()
        self.core = CoreGateway(db, settings)

    # ------------------------------------------------------------------ lifecycle
    def create_run(self, *, session_id: uuid.UUID, student_id: uuid.UUID, course_id: uuid.UUID,
                   doubt_text: str, trigger_id: str) -> WorkflowRun:
        run_id = uuid.uuid4()
        s = self.s
        state = WorkflowState(
            run_id=str(run_id), session_id=str(session_id), student_id=str(student_id),
            student_ref=student_ref(student_id), course_id=str(course_id), doubt_text=doubt_text[:MAX_DOUBT_CHARS],
            budgets=Budgets(max_actions=s.max_actions, max_clarify_rounds=s.max_clarify_rounds,
                            max_explain_attempts=s.max_explain_attempts, max_practice_sets=s.max_practice_sets),
            thresholds=Thresholds(t_clarify=s.t_clarify, ret_min_chunks=s.ret_min_chunks, t_master=s.t_master,
                                  n_min=s.n_min))
        run = WorkflowRun(id=run_id, session_id=session_id, student_id=student_id, trigger_id=trigger_id,
                          status="CREATED", current_node="understand", state=state.model_dump(mode="json"),
                          version=0)
        self.db.add(run)
        self.core.set_session(session_id, status="CREATED", run_id=run_id)
        self.db.commit()
        return run

    def _lock(self, run_id: uuid.UUID) -> WorkflowRun:
        run = self.db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
        if run is None:
            raise AppError(404, "NOT_FOUND", "Run not found")
        return run

    def _state(self, run: WorkflowRun) -> WorkflowState:
        return WorkflowState.model_validate(run.state)

    def _save(self, run: WorkflowRun, st: WorkflowState, *, status: str | None = None,
              node: str | None = None) -> None:
        if status:
            run.status = status
        if node:
            run.current_node = node
        run.state = st.model_dump(mode="json")
        run.version += 1
        self.core.set_session(uuid.UUID(st.session_id), status=run.status)
        self.db.commit()

    def _step(self, run: WorkflowRun, st: WorkflowState, node: str, inp, out: dict, started: float, *,
              agent: AgentRun | None = None, provider: str | None = None, model: str | None = None,
              prompt_version: str | None = None, error: str | None = None) -> None:
        st.step_seq += 1
        if agent is not None:
            provider, model, prompt_version, error = agent.provider, agent.model, agent.prompt_version, error or agent.error
            out = {**out, "agent": agent.trace()}
        self.db.add(WorkflowStep(
            run_id=run.id, seq=st.step_seq, node=node, input_hash=_hash(inp), output=out, provider=provider,
            model=model, prompt_version=prompt_version, latency_ms=int((time.perf_counter() - started) * 1000),
            error=error, started_at=datetime.now(timezone.utc)))

    def advance(self, run_id: uuid.UUID) -> WorkflowRun:
        run = self._lock(run_id)
        guard = 0
        while run.status in ("CREATED", "RUNNING"):
            guard += 1
            st = self._state(run)
            if guard > MAX_NODE_EXECUTIONS:
                self._fail(run, st, "node execution guard exceeded")
                break
            run.status = "RUNNING"
            node = run.current_node
            handler = getattr(self, f"_node_{node}", None)
            if handler is None:
                self._fail(run, st, f"unknown node {node}")
                break
            try:
                handler(run, st)
            except Exception as e:  # unrecoverable: make the failure visible and auditable
                log.exception("workflow node crashed", extra={"run_id": str(run_id), "node": node})
                self.db.rollback()
                run = self._lock(run_id)
                self._fail(run, self._state(run), f"{type(e).__name__} in node {node}")
        return run

    def _fail(self, run: WorkflowRun, st: WorkflowState, message: str) -> None:
        started = time.perf_counter()
        self._step(run, st, "fail", {"node": run.current_node}, {"reason": message}, started, error=message)
        self.core.add_message(uuid.UUID(st.session_id), "system",
                              "Something went wrong while processing your doubt. You can ask again or request a teacher.")
        self.core.audit(None, "workflow_failed", "workflow_run", str(run.id), {"reason": message})
        self._save(run, st, status="FAILED", node="fail")

    # ------------------------------------------------------------------ events
    def apply_event(self, run_id: uuid.UUID, event_type: str, payload: dict) -> WorkflowRun:
        run = self._lock(run_id)
        st = self._state(run)
        if event_type == "student_message":
            if run.status != "AWAITING_STUDENT" or st.pending.kind != "student_message":
                raise AppError(409, "INVALID_RUN_STATE", f"Run is {run.status}; it is not waiting for a reply")
            text = payload["text"][:MAX_DOUBT_CHARS]
            st.history.append(text)
            st.pending = Pending()
            self.core.add_message(uuid.UUID(st.session_id), "student", text)
            self._save(run, st, status="RUNNING", node="understand")
        elif event_type == "student_ack":
            if run.status != "AWAITING_STUDENT" or st.pending.kind != "student_ack":
                raise AppError(409, "INVALID_RUN_STATE", f"Run is {run.status}; it is not waiting for an acknowledgment")
            from app.learner.ledger import ACK_TO_TYPE, EvidenceIn, EvidenceRejected
            ack = payload["ack"]
            try:
                self.core.append_evidence(EvidenceIn(
                    student_id=uuid.UUID(st.student_id), course_id=uuid.UUID(st.course_id),
                    topic_id=uuid.UUID(st.analysis.topic_id) if st.analysis and st.analysis.topic_id else None,
                    evidence_type=ACK_TO_TYPE[ack], weight=0.0,       # self-reports NEVER carry mastery weight
                    source_run_id=run.id, source_ref=payload["source_ref"],
                    provenance={"source": "student_ack", "schema_version": 1, "session_id": st.session_id,
                                "run_id": str(run.id), "intervention_id": payload.get("intervention_id"), "ack": ack}))
            except EvidenceRejected as e:
                raise AppError(422, "EVIDENCE_REJECTED", str(e))
            st.acked, st.pending = ack, Pending()
            self._save(run, st, status="RUNNING", node="decide")
        elif event_type == "student_requests_teacher":
            if run.status not in ("AWAITING_STUDENT", "AWAITING_ANSWER"):
                raise AppError(409, "INVALID_RUN_STATE", f"Run is {run.status}")
            st.flags.explicit_teacher_request = True
            st.pending = Pending()
            if st.analysis is None:
                st.analysis = DoubtAnalysis()
            self._save(run, st, status="RUNNING", node="decide")
        elif event_type == "practice_answer":
            self._on_practice_answer(run, st, payload)
        else:
            raise AppError(409, "INVALID_RUN_STATE", f"Event {event_type} is not supported")
        return self.advance(run_id)

    # ------------------------------------------------------------------ nodes
    def _node_understand(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        topics = self.core.topics(uuid.UUID(st.course_id))
        out = understanding_agent.understand(self.provider, self.s, doubt_text=st.doubt_text, history=st.history, topics=topics)
        st.counters.llm_calls += out.run.attempts
        analysis = out.analysis
        st.analysis = analysis
        st.flags.explicit_teacher_request = st.flags.explicit_teacher_request or analysis.explicit_teacher_request
        st.flags.safety_flag = st.flags.safety_flag or analysis.safety_flag
        if analysis.topic_id:
            self.core.set_session(uuid.UUID(st.session_id), topic_id=analysis.topic_id)
        proposed = self.core.learner.propose_hypotheses(uuid.UUID(st.student_id), run.id, analysis.gap_hypotheses)
        self._step(run, st, "understand", {"doubt_text": st.doubt_text, "history": st.history},
                   {"analysis": analysis.model_dump(mode="json"), "fallback": out.run.fallback, "attempts": out.run.attempts,
                    "hypotheses_proposed": [str(h.id) for h in proposed]},
                   started, agent=out.run)
        self._save(run, st, node="load_context")

    def _node_load_context(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        topic_name = ""
        if st.analysis and st.analysis.topic_id:
            topic_name = next((t.name for t in self.core.topics(uuid.UUID(st.course_id))
                               if t.id == st.analysis.topic_id), "")
        query = " ".join([st.doubt_text, *st.history, topic_name]).strip()
        error = None
        try:
            res = self.retriever.search(query, self.principal, course_id=uuid.UUID(st.course_id), top_k=6)
            hits = res.hits
            support = RetrievalSupport(available=True, n_chunks_above_threshold=res.n_above_threshold,
                                       max_matched_terms=max((h.matched_terms for h in hits), default=0),
                                       max_dense_similarity=max((h.dense_similarity or 0.0 for h in hits), default=0.0),
                                       lexical_hits=len(hits))
            terms = res.terms
            meta = {"requested": res.mode_requested, "used": res.mode_used, "fallback_reason": res.fallback_reason,
                    "degraded": res.degraded, "missing_embeddings": res.missing_embeddings,
                    "embedding_model": res.embedding_model, "min_sim": res.min_sim}
            log.info("retrieval", extra={"run_id": str(run.id), "mode_used": res.mode_used,
                                         "fallback_reason": res.fallback_reason, "hits": len(hits)})
        except Exception as e:  # retrieval failure is explicit: policy R4 treats it as "no grounding"
            self.db.rollback()
            hits, terms, error = [], [], f"retrieval_error:{type(e).__name__}"
            meta = {"requested": self.s.retrieval_mode, "used": "none", "fallback_reason": error}
            support = RetrievalSupport(available=False)
        mastery = self._mastery(st)
        st.context = RunContext(query=query, chunk_ids=[h.chunk_id for h in hits], retrieval=support, mastery=mastery)
        self._step(run, st, "load_context", {"query": query},
                   {"query": query, "terms": terms, "retrieval": meta, "min_terms": res.min_terms if not error else None,
                    "results": [{"chunk_id": h.chunk_id, "document_id": h.document_id, "page": h.page,
                                 "rank": round(h.rank, 4), "matched_terms": h.matched_terms,
                                 "dense_similarity": None if h.dense_similarity is None else round(h.dense_similarity, 4),
                                 "fused_score": h.fused_score, "sources": h.sources} for h in hits],
                    "retrieval_support": support.model_dump(), "mastery": mastery.model_dump(),
                    "injection_flagged_chunks": injection_flags(hits)},
                   started, error=error)
        self._save(run, st, node="decide")

    def _mastery(self, st: WorkflowState) -> MasterySummary:
        topic = uuid.UUID(st.analysis.topic_id) if st.analysis and st.analysis.topic_id else None
        return self.core.learner_summary(uuid.UUID(st.student_id), topic)

    def _policy_input(self, st: WorkflowState) -> PolicyInput:
        a = st.analysis or DoubtAnalysis()
        lc = st.last_check
        if lc is not None and lc.passed and st.context.mastery.status != "demonstrated":
            lc = None          # a pass that does not demonstrate mastery is inconclusive: another check may follow (R7)
        return PolicyInput(clarity=a.clarity, classification_confidence=a.classification_confidence,
                           retrieval=st.context.retrieval, mastery=st.context.mastery, counters=st.counters,
                           budgets=st.budgets, flags=st.flags, explained=st.explained, acked=st.acked,
                           last_check=lc, thresholds=st.thresholds)

    def _node_decide(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        st.context.mastery = self._mastery(st)          # always decide on the CURRENT learner state, never a stale snapshot
        inp = self._policy_input(st)
        d = decide(inp)
        st.last_decision = d
        topic = uuid.UUID(st.analysis.topic_id) if st.analysis and st.analysis.topic_id else None
        self.db.add(DecisionRecord(run_id=run.id, rule_id=d.rule_id, action=d.action, outcome=d.outcome,
                                   inputs_snapshot=inp.model_dump(mode="json"), reasons=d.reasons,
                                   advisor=None, overridden=False,
                                   evidence_refs=self.core.evidence_refs(uuid.UUID(st.student_id), topic),
                                   context={"provider": self.provider.name, "model": getattr(self.provider, "model", None),
                                            "hard_rule": d.rule_id in HARD_RULES}))
        self._step(run, st, "decide", inp.model_dump(mode="json"),
                   {"rule_id": d.rule_id, "action": d.action, "outcome": d.outcome, "reasons": d.reasons,
                    "advisor": None}, started)
        nxt = {"ASK_CLARIFICATION": "clarify", "GENERATE_EXPLANATION": "explain",
               "ESCALATE_TO_TEACHER": "escalate", "COMPLETE": "complete", "GENERATE_PRACTICE": "practice"}[d.action]
        self._save(run, st, node=nxt)

    def _intervention(self, run: WorkflowRun, st: WorkflowState, payload: dict) -> None:
        d = st.last_decision
        self.core.add_intervention(uuid.UUID(st.session_id), run.id, d.action, d.rule_id, payload)

    def _node_clarify(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        q = (st.analysis.clarification_question if st.analysis else None) or understanding_agent.GENERIC_CLARIFICATION
        st.counters.clarify_rounds += 1
        st.counters.actions_used += 1
        st.pending = Pending(kind="student_message")
        self.core.add_message(uuid.UUID(st.session_id), "system", q)
        self._intervention(run, st, {"clarification_question": q})
        self._step(run, st, "clarify", {"round": st.counters.clarify_rounds}, {"question": q}, started)
        self._save(run, st, status="AWAITING_STUDENT", node="await_student")

    def _node_explain(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        st.counters.explain_attempts += 1
        st.counters.actions_used += 1
        hits = self.retriever.get_chunks([uuid.UUID(c) for c in st.context.chunk_ids], self.principal)
        out = explanation_agent.explain(self.provider, self.s, doubt_text=" ".join([st.doubt_text, *st.history]),
                                        analysis=st.analysis or DoubtAnalysis(), hits=hits, attempt=st.counters.explain_attempts)
        st.counters.llm_calls += out.provider_calls
        payload = {"explanation": {"text": out.text, "citations": out.citations, "follow_up_check_offered": out.follow_up_offered},
                   "provider": self.provider.name, "model": self.provider.model, "provider_note": out.provider_note,
                   "fallback": out.fallback}
        st.explained = True
        st.acked = "none"
        st.last_check = None              # a new explanation starts a new round: the next check is evaluated afresh
        st.practice = PracticeState()
        st.pending = Pending(kind="student_ack")
        self.core.add_message(uuid.UUID(st.session_id), "system", out.text)
        self._intervention(run, st, payload)
        self._step(run, st, "explain",
                   {"chunk_ids": [h.chunk_id for h in hits], "attempt": st.counters.explain_attempts},
                   {"fallback": out.fallback, "regenerated": out.regenerated, "citation_checks": out.checks,
                    "n_verified": out.n_verified, "n_stripped": out.n_stripped, "provider_calls": out.provider_calls,
                    "answer_text": out.text}, started, agent=out.run)
        self._save(run, st, status="AWAITING_STUDENT", node="await_student")

    # ---- practice ------------------------------------------------------------------------------------------------
    def _practice_unavailable(self, run: WorkflowRun, st: WorkflowState, started: float, reason: str, agent=None, extra=None) -> None:
        msg = ("I could not prepare practice questions for this topic, so your understanding cannot be verified "
               "automatically. Your acknowledgment was recorded but carries no mastery credit.")
        st.outcome = "UNVERIFIED"
        self.core.add_message(uuid.UUID(st.session_id), "system", msg)
        self._intervention(run, st, {"practice": {"available": False, "reason": reason}, "message": msg, "outcome": st.outcome})
        self._step(run, st, "practice", {"rule_id": st.last_decision.rule_id},
                   {"available": False, "reason": reason, "outcome": st.outcome, **(extra or {})}, started, agent=agent)
        self._save(run, st, status="COMPLETED", node="done")

    def _node_practice(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        st.counters.actions_used += 1
        st.counters.practice_sets += 1
        topic_id = st.analysis.topic_id if st.analysis else None
        if topic_id is None:
            return self._practice_unavailable(run, st, started, "the doubt was not mapped to a course topic")
        sid, tid = uuid.UUID(st.student_id), uuid.UUID(topic_id)
        topic = self.core.topic(tid)
        hits = self.retriever.get_chunks([uuid.UUID(c) for c in st.context.chunk_ids], self.principal)
        hyp = self.core.learner.open_hypothesis(sid, tid)
        difficulty = st.analysis.difficulty_estimate
        if st.counters.failed_checks > 0:
            difficulty = EASIER[difficulty]                 # after a failed check, ask easier questions
        tags = sorted(self.core.learner.error_tag_counts(sid, tid))
        out = practice_agent.generate_practice(
            self.provider, self.s, topic_id=topic_id, topic_slug=topic.slug, topic_name=topic.name,
            doubt_text=" ".join([st.doubt_text, *st.history]), hits=hits, count=self.s.practice_set_size,
            difficulty=difficulty, hypothesis=hyp.description if hyp else None, error_tags=tags,
            seen_prompt_hashes=self.core.recent_prompt_hashes(sid, tid))
        st.counters.llm_calls += out.provider_calls
        if not out.items:
            return self._practice_unavailable(run, st, started, "no valid practice items could be generated",
                                              agent=out.run, extra={"rejected": out.rejected[:10]})
        items = self.core.save_practice_items(
            student_id=sid, session_id=uuid.UUID(st.session_id), run_id=run.id, topic_id=tid, set_index=st.counters.practice_sets,
            drafts=out.items, hypothesis_id=hyp.id if hyp else None, source=out.source,
            provider=self.provider.name if out.source == "model" else "seed_bank",
            model=self.provider.model if out.source == "model" else None)
        st.practice = PracticeState(item_ids=[str(i.id) for i in items])
        st.pending = Pending(kind="practice_answer")
        payload = {"practice": {"available": True, "set": st.counters.practice_sets, "source": out.source,
                                "targets_suspected_gap": hyp.description if hyp else None,
                                "items": [self.core.public_item(i) for i in items]}}
        self.core.add_message(uuid.UUID(st.session_id), "system",
                              f"Here are {len(items)} practice questions to check your understanding.")
        self._intervention(run, st, payload)
        self._step(run, st, "practice", {"topic_id": topic_id, "difficulty": difficulty, "count": self.s.practice_set_size},
                   {"available": True, "source": out.source, "n_items": len(items), "item_ids": st.practice.item_ids,
                    "rejected": out.rejected[:10], "targets_hypothesis_id": str(hyp.id) if hyp else None,
                    "difficulty": difficulty}, started, agent=out.run)
        self._save(run, st, status="AWAITING_ANSWER", node="await_answer")

    def _on_practice_answer(self, run: WorkflowRun, st: WorkflowState, payload: dict) -> None:
        """Grade ONE submitted answer, write evidence if (and only if) the grade counts, and either wait for the remaining
        items or finish the check and re-enter the policy. Runs inside the run lock, in one transaction."""
        if run.status != "AWAITING_ANSWER" or st.pending.kind != "practice_answer":
            raise AppError(409, "INVALID_RUN_STATE", f"Run is {run.status}; it is not waiting for a practice answer")
        attempt = self.core.get_attempt(uuid.UUID(payload["attempt_id"]))
        item_id = str(attempt.item_id)
        if item_id not in st.practice.item_ids:
            raise AppError(409, "INVALID_RUN_STATE", "That question is not part of the current practice set")
        if item_id in st.practice.answered:
            raise AppError(409, "ITEM_ALREADY_ANSWERED", "This question has already been answered")
        item = self.core.get_item(attempt.item_id)
        started = time.perf_counter()
        outcome = evaluation_agent.evaluate_answer(self.provider, self.s, self.core.for_grading(item), attempt.answer)
        if outcome.run is not None:
            st.counters.llm_calls += outcome.run.attempts
        self.core.save_grading(attempt, outcome)
        self._step(run, st, "evaluate", {"item_id": item_id, "kind": item.kind},
                   {"item_id": item_id, "grader": outcome.grader, "grader_status": outcome.grader_status,
                    "correct": outcome.correct, "partial_credit": outcome.partial_credit, "uncertainty": outcome.uncertainty,
                    "counts_as_evidence": outcome.counts_as_evidence, "error_tags": outcome.error_tags},
                   started, agent=outcome.run)
        ev = None
        if outcome.counts_as_evidence:
            t0 = time.perf_counter()
            ev = self.core.learner.record_graded_attempt(attempt=attempt, item=item, outcome=outcome,
                                                         course_id=uuid.UUID(st.course_id))
            view = self.core.learner.view(attempt.student_id, item.topic_id)
            self._step(run, st, "update_evidence", {"attempt_id": str(attempt.id)},
                       {"evidence_id": str(ev.id), "evidence_type": ev.evidence_type, "weight": ev.weight,
                        "mastery_after": {k: v for k, v in view.items() if k != "last_evidence_at"},
                        "hypothesis_ids": [str(item.targets_hypothesis_id)] if item.targets_hypothesis_id else []}, t0)
        if attempt.status == "UNGRADED":       # grader unavailable: the student may submit this question again
            self._save(run, st)
            return
        st.practice.answered.append(item_id)
        st.practice.results[item_id] = {"correct": outcome.correct if outcome.counts_as_evidence else None,
                                        "evidence": bool(ev)}
        if len(st.practice.answered) < len(st.practice.item_ids):
            self._save(run, st)                # still AWAITING_ANSWER
            return
        graded = [r for r in st.practice.results.values() if r["evidence"]]
        if graded:
            passed = sum(1 for r in graded if r["correct"]) / len(graded) >= self.s.check_pass_fraction
            st.last_check = CheckResult(passed=passed)
            if not passed:
                st.counters.failed_checks += 1
        else:
            st.last_check = None               # nothing countable: inconclusive
        st.pending = Pending()
        self._step(run, st, "check", {"set": st.counters.practice_sets},
                   {"graded_with_evidence": len(graded), "passed": st.last_check.passed if st.last_check else None,
                    "failed_checks": st.counters.failed_checks}, time.perf_counter())
        self._save(run, st, status="RUNNING", node="decide")

    # ---- escalation ----------------------------------------------------------------------------------------------
    def _node_escalate(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        st.counters.actions_used += 1
        d = st.last_decision
        esc = self.core.create_escalation(run_id=run.id, state=st, rule_id=d.rule_id, reasons=d.reasons)
        st.pending = Pending(kind="teacher", escalation_id=str(esc.id))
        st.escalation_id = str(esc.id)
        n = len(esc.candidates or [])
        msg = (f"I have asked a teacher to help ({n} matching teacher{'s' if n != 1 else ''} notified). You can follow the "
               "status and message them here; this doubt stays open until a teacher resolves it."
               if n else
               "No teacher is available for this topic right now. Your request stays open and any teacher of this course can "
               f"pick it up until {esc.expires_at:%Y-%m-%d %H:%M} UTC.")
        self.core.add_message(uuid.UUID(st.session_id), "system", msg)
        self._intervention(run, st, {"message": msg, "escalation": {"id": str(esc.id), "status": esc.status,
                                                                    "matched_teachers": n, "expires_at": esc.expires_at.isoformat()}})
        self.core.audit(uuid.UUID(st.student_id), "escalation_created", "escalation", str(esc.id), {"rule_id": d.rule_id})
        self._step(run, st, "escalate", {"rule_id": d.rule_id},
                   {"escalation_id": str(esc.id), "matched_teachers": n,
                    "candidates": [{"teacher_id": c["teacher_id"], "score": c["score"]} for c in (esc.candidates or [])],
                    "expires_at": str(esc.expires_at)}, started)
        self._save(run, st, status="WAITING_HUMAN", node="await_teacher")

    def resume_after_escalation(self, run_id: uuid.UUID, escalation_id: uuid.UUID, outcome: str, message: str) -> WorkflowRun:
        """Called by the teaching service AFTER it has committed the teacher's feedback, evidence and hypothesis decisions.
        Idempotent: a run that is no longer waiting for this escalation is returned unchanged."""
        run = self._lock(run_id)
        st = self._state(run)
        if run.status != "WAITING_HUMAN" or st.pending.escalation_id != str(escalation_id):
            self.db.rollback()
            return run
        started = time.perf_counter()
        resolved = outcome == "RESOLVED"
        st.outcome = "RESOLVED" if resolved else "UNRESOLVED"
        rule = "RESUME_TEACHER_RESOLVED" if resolved else f"RESUME_ESCALATION_{outcome}"
        d = Decision(action="COMPLETE", rule_id=rule, outcome=st.outcome,
                     reasons=[f"escalation {escalation_id} ended: {outcome}"])
        st.last_decision = d
        st.pending = Pending()
        self.db.add(DecisionRecord(run_id=run.id, rule_id=rule, action="COMPLETE", outcome=st.outcome,
                                   inputs_snapshot={"escalation_id": str(escalation_id), "outcome": outcome},
                                   reasons=d.reasons, advisor=None, overridden=False))
        self.core.add_message(uuid.UUID(st.session_id), "system", message)
        self._intervention(run, st, {"outcome": st.outcome, "message": message,
                                     "escalation": {"id": str(escalation_id), "status": outcome}})
        self._step(run, st, "resume", {"escalation_id": str(escalation_id)}, {"outcome": outcome, "run_outcome": st.outcome}, started)
        self._save(run, st, status="COMPLETED", node="done")
        return run

    def _node_complete(self, run: WorkflowRun, st: WorkflowState) -> None:
        started = time.perf_counter()
        st.outcome = st.last_decision.outcome or "RESOLVED"
        msg = ("Marked as resolved: your answers to the practice questions demonstrated understanding of this topic."
               if st.outcome == "RESOLVED" else "This doubt is unresolved and no teacher was available.")
        self.core.add_message(uuid.UUID(st.session_id), "system", msg)
        self._intervention(run, st, {"outcome": st.outcome, "message": msg})
        self._step(run, st, "complete", {"rule_id": st.last_decision.rule_id}, {"outcome": st.outcome}, started)
        self._save(run, st, status="COMPLETED", node="done")
