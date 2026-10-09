"""The workflow's only door into `core` data (a future `core-api` internal API: topics, learner state, practice items,
attempts, interventions, escalations). Keeps the engine free of direct core-table access."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.evaluation import EvaluationOutcome, ItemForGrading
from app.agents.practice import prompt_hash
from app.config import Settings
from app.learner.service import LearnerService
from app.llm.schemas import PracticeItemDraft, TopicRef
from app.models import AuditEvent, Attempt, DoubtSession, EvidenceEvent, Intervention, Message, PracticeItem, Topic
from app.workflow.policy import MasterySummary


class CoreGateway:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.s = db, settings
        self.learner = LearnerService(db, settings)

    # ---- catalog / learner
    def topics(self, course_id: uuid.UUID) -> list[TopicRef]:
        rows = self.db.scalars(select(Topic).where(Topic.course_id == course_id).order_by(Topic.sort, Topic.slug))
        return [TopicRef(id=str(t.id), slug=t.slug, name=t.name,
                         keywords=[k.strip() for k in t.keywords.split(",") if k.strip()]) for t in rows]

    def topic(self, topic_id: uuid.UUID) -> Topic:
        return self.db.get(Topic, topic_id)

    def learner_summary(self, student_id: uuid.UUID, topic_id: uuid.UUID | None) -> MasterySummary:
        return self.learner.summary(student_id, topic_id)

    # ---- session / messages
    def set_session(self, session_id: uuid.UUID, *, status: str | None = None, topic_id: str | None = None,
                    run_id: uuid.UUID | None = None) -> None:
        s = self.db.get(DoubtSession, session_id)
        if status is not None:
            s.status = status
        if topic_id is not None:
            s.topic_id = uuid.UUID(topic_id)
        if run_id is not None:
            s.run_id = run_id

    def add_message(self, session_id: uuid.UUID, role: str, content: str) -> None:
        self.db.add(Message(session_id=session_id, role=role, content=content))

    def add_intervention(self, session_id: uuid.UUID, run_id: uuid.UUID, action: str, rule_id: str, payload: dict) -> None:
        self.db.add(Intervention(session_id=session_id, run_id=run_id, action=action, rule_id=rule_id, payload=payload))

    def append_evidence(self, ev) -> None:
        """Validated, append-only ledger write in the CURRENT transaction (committed together with the run state)."""
        from app.learner.ledger import append_evidence
        append_evidence(self.db, ev)

    def evidence_refs(self, student_id: uuid.UUID, topic_id: uuid.UUID | None, limit: int = 12) -> list[str]:
        if topic_id is None:
            return []
        rows = self.db.scalars(select(EvidenceEvent.id).where(EvidenceEvent.student_id == student_id, EvidenceEvent.topic_id == topic_id)
                               .order_by(EvidenceEvent.created_at.desc()).limit(limit))
        return [str(r) for r in rows]

    def audit(self, actor_id: uuid.UUID | None, action: str, entity: str, entity_id: str, meta: dict) -> None:
        self.db.add(AuditEvent(actor_id=actor_id, action=action, entity=entity, entity_id=entity_id, meta=meta))

    # ---- practice
    def recent_prompt_hashes(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> set[str]:
        return set(self.db.scalars(select(PracticeItem.prompt_hash).where(
            PracticeItem.student_id == student_id, PracticeItem.topic_id == topic_id)))

    def save_practice_items(self, *, student_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID, topic_id: uuid.UUID,
                            set_index: int, drafts: list[PracticeItemDraft], hypothesis_id: uuid.UUID | None, source: str,
                            provider: str | None, model: str | None) -> list[PracticeItem]:
        items = []
        for pos, d in enumerate(drafts, start=1):
            it = PracticeItem(student_id=student_id, session_id=session_id, run_id=run_id, topic_id=topic_id,
                              set_index=set_index, position=pos, kind=d.kind, prompt=d.prompt, options=d.options,
                              answer_key=d.answer_key, numeric_tolerance=d.numeric_tolerance, rubric=d.rubric,
                              difficulty=d.difficulty, source_chunk_ids=d.source_chunk_ids,
                              distractor_tags={k: v[:40] for k, v in d.distractor_tags.items() if d.options and k in d.options},
                              targets_hypothesis_id=hypothesis_id, source=source, prompt_hash=prompt_hash(d.prompt),
                              provider=provider, model=model)
            self.db.add(it)
            items.append(it)
        self.db.flush()
        return items

    @staticmethod
    def public_item(it: PracticeItem) -> dict:
        """What a student may see BEFORE answering: never the key, rubric or distractor tags."""
        return {"item_id": str(it.id), "position": it.position, "kind": it.kind, "prompt": it.prompt,
                "options": it.options, "difficulty": it.difficulty, "topic_id": str(it.topic_id)}

    def get_item(self, item_id: uuid.UUID) -> PracticeItem:
        return self.db.get(PracticeItem, item_id)

    def get_attempt(self, attempt_id: uuid.UUID) -> Attempt:
        return self.db.get(Attempt, attempt_id)

    @staticmethod
    def for_grading(it: PracticeItem) -> ItemForGrading:
        return ItemForGrading(kind=it.kind, prompt=it.prompt, options=it.options, answer_key=it.answer_key,
                              numeric_tolerance=it.numeric_tolerance, rubric=it.rubric,
                              distractor_tags=dict(it.distractor_tags or {}))

    def save_grading(self, attempt: Attempt, o: EvaluationOutcome) -> None:
        attempt.status = ("GRADED" if o.grader_status == "ok" else "UNCERTAIN" if o.grader_status == "uncertain" else "UNGRADED")
        attempt.correct = o.correct if o.grader_status == "ok" else None
        attempt.partial_credit, attempt.feedback, attempt.evidence_text = o.partial_credit, o.feedback, o.evidence
        attempt.error_tags, attempt.uncertainty = list(o.error_tags), o.uncertainty
        attempt.grader, attempt.grader_status = o.grader, o.grader_status
        attempt.graded_at = datetime.now(timezone.utc)
        self.db.flush()

    # ---- teaching
    def create_escalation(self, *, run_id: uuid.UUID, state, rule_id: str, reasons: list[str]):
        from app.teaching.service import TeachingService
        return TeachingService(self.db, self.s).create_escalation(run_id=run_id, state=state, rule_id=rule_id, reasons=reasons)
