"""Learner service (core-api, module `learner`): applies ledger evidence to the mastery model, keeps the progress history,
and manages suspected-gap hypotheses. The ledger is the source of truth; `learner_topic_state` is a rebuildable cache."""
from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.agents.evaluation import EvaluationOutcome
from app.config import Settings
from app.learner import mastery as m
from app.learner.ledger import NEGATIVE, POSITIVE, EvidenceIn, append_evidence
from app.models import Attempt, EvidenceEvent, GapHypothesis, LearnerTopicState, MasteryHistory, PracticeItem, Topic
from app.workflow.policy import MasterySummary

OPEN = ("proposed",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hypothesis_key(description: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", description.strip().lower()).encode()).hexdigest()[:16]


def source_key(ev: EvidenceEvent) -> str:
    prov = ev.provenance or {}
    return str(prov["item_id"]) if ev.evidence_type.startswith("attempt_") else f"teacher:{prov.get('escalation_id')}"


class LearnerService:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.s = db, settings
        self.p = m.MasteryParams.from_settings(settings)

    # ------------------------------------------------------------------ state
    def _row(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> LearnerTopicState | None:
        return self.db.get(LearnerTopicState, (student_id, topic_id))

    @staticmethod
    def _posterior(row: LearnerTopicState | None, p: m.MasteryParams) -> m.Posterior:
        if row is None:
            return m.Posterior.prior(p)
        return m.Posterior(row.alpha, row.beta, row.evidence_count, list(row.sources), row.last_evidence_at)

    def has_open_hypothesis(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> bool:
        return self.db.scalar(select(func.count()).select_from(GapHypothesis).where(
            GapHypothesis.student_id == student_id, GapHypothesis.topic_id == topic_id,
            GapHypothesis.status.in_(OPEN))) > 0

    def trusted_positive(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> int:
        """Positive ledger rows that do NOT depend on a model's judgement: objectively graded attempts and teacher assessments."""
        return self.db.scalar(select(func.count()).select_from(EvidenceEvent).where(
            EvidenceEvent.student_id == student_id, EvidenceEvent.topic_id == topic_id,
            or_(EvidenceEvent.evidence_type == "teacher_assessment_solid",
                and_(EvidenceEvent.evidence_type == "attempt_correct", EvidenceEvent.provenance["grader"].astext == "exact")))) or 0

    def apply_row(self, ev: EvidenceEvent) -> dict | None:
        """Apply ONE ledger row to the cached posterior and append a history row. Idempotent per evidence id. Rows that carry
        no weight (acknowledgments, informational teacher notes) are ignored by construction."""
        if ev.evidence_type not in POSITIVE and ev.evidence_type not in NEGATIVE:
            return None
        if ev.topic_id is None:
            return None
        if self.db.scalar(select(MasteryHistory.id).where(MasteryHistory.evidence_id == ev.id)) is not None:
            return None
        row = self._row(ev.student_id, ev.topic_id)
        post = m.apply(self._posterior(row, self.p),
                       m.Event(str(ev.id), ev.evidence_type, ev.weight, source_key(ev), ev.created_at), self.p)
        if row is None:
            row = LearnerTopicState(student_id=ev.student_id, topic_id=ev.topic_id, alpha=post.alpha, beta=post.beta,
                                    evidence_count=post.evidence_count, sources=post.sources, last_evidence_at=post.last_at)
            self.db.add(row)
        else:
            row.alpha, row.beta, row.evidence_count = post.alpha, post.beta, post.evidence_count
            row.sources, row.last_evidence_at = post.sources, post.last_at
        status = m.status(post, ev.created_at, self.p, self.has_open_hypothesis(ev.student_id, ev.topic_id),
                          self.trusted_positive(ev.student_id, ev.topic_id))
        self.db.add(MasteryHistory(student_id=ev.student_id, topic_id=ev.topic_id, evidence_id=ev.id, alpha=post.alpha,
                                   beta=post.beta, mean=m.mean(post.alpha, post.beta), status=status,
                                   evidence_count=post.evidence_count, distinct_sources=len(post.sources)))
        self.db.flush()
        return {"status": status, "mean": m.mean(post.alpha, post.beta)}

    def rebuild(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> m.Posterior:
        """Replay the ledger from scratch. Used by tests and repair; must equal the incrementally maintained cache."""
        rows = self.db.scalars(select(EvidenceEvent).where(EvidenceEvent.student_id == student_id,
                                                           EvidenceEvent.topic_id == topic_id)).all()
        events = [m.Event(str(r.id), r.evidence_type, r.weight, source_key(r), r.created_at) for r in rows
                  if r.evidence_type in POSITIVE or r.evidence_type in NEGATIVE]
        return m.replay(events, self.p)

    def view(self, student_id: uuid.UUID, topic_id: uuid.UUID, now: datetime | None = None) -> dict:
        now = now or _now()
        post = self._posterior(self._row(student_id, topic_id), self.p)
        a, b = m.decayed(post, now, self.p)
        return {"status": m.status(post, now, self.p, self.has_open_hypothesis(student_id, topic_id),
                                    self.trusted_positive(student_id, topic_id)),
                "mean": round(m.mean(a, b), 4), "alpha": round(a, 4), "beta": round(b, 4),
                "evidence_count": post.evidence_count, "distinct_sources": len(post.sources),
                "last_evidence_at": post.last_at}

    def error_tag_counts(self, student_id: uuid.UUID, topic_id: uuid.UUID, now: datetime | None = None) -> Counter:
        since = (now or _now()) - timedelta(days=self.s.error_tag_window_days)
        rows = self.db.execute(select(Attempt.error_tags).join(PracticeItem, PracticeItem.id == Attempt.item_id).where(
            Attempt.student_id == student_id, PracticeItem.topic_id == topic_id, Attempt.correct.is_(False),
            Attempt.evidence_event_id.is_not(None), Attempt.created_at >= since)).scalars().all()
        c: Counter = Counter()
        for tags in rows:
            c.update(set(tags or []))
        return c

    def summary(self, student_id: uuid.UUID, topic_id: uuid.UUID | None, now: datetime | None = None) -> MasterySummary:
        if topic_id is None:
            return MasterySummary(status="unknown")
        v = self.view(student_id, topic_id, now)
        tags = self.error_tag_counts(student_id, topic_id, now)
        return MasterySummary(status=v["status"], mean=v["mean"] if v["evidence_count"] else None,
                              evidence_count=v["evidence_count"], distinct_items=v["distinct_sources"],
                              repeated_error_tag_max=max(tags.values(), default=0))

    # ------------------------------------------------------------------ attempts -> evidence -> hypotheses
    def record_graded_attempt(self, *, attempt: Attempt, item: PracticeItem, outcome: EvaluationOutcome,
                              course_id: uuid.UUID, at: datetime | None = None) -> EvidenceEvent | None:
        """Turn a graded attempt into ledger evidence (only if the grade counts as evidence), update mastery and hypotheses.
        A second call for the same attempt writes nothing (unique source_ref + history uniqueness)."""
        if attempt.evidence_event_id is not None:          # already recorded: repeated processing never duplicates evidence
            return self.db.get(EvidenceEvent, attempt.evidence_event_id)
        if not outcome.counts_as_evidence:
            return None
        grader = outcome.grader
        ev = EvidenceIn(
            student_id=attempt.student_id, course_id=course_id, topic_id=item.topic_id,
            evidence_type="attempt_correct" if outcome.correct else "attempt_incorrect",
            weight=m.attempt_weight(grader, item.difficulty, attempt.hints_used, self.p),
            source_run_id=attempt.run_id, source_ref=f"attempt:{attempt.id}",
            provenance={"source": "practice_attempt", "schema_version": 1, "session_id": str(attempt.session_id),
                        "run_id": str(attempt.run_id), "attempt_id": str(attempt.id), "item_id": str(item.id),
                        "grader": grader, "uncertainty": outcome.uncertainty, "difficulty": item.difficulty,
                        "hints_used": attempt.hints_used, "error_tags": outcome.error_tags,
                        "item_source": item.source, "source_chunk_ids": item.source_chunk_ids},
            created_at=at)
        row = append_evidence(self.db, ev)
        self.db.flush()
        attempt.evidence_event_id = row.id
        self.apply_row(row)
        self._update_hypothesis(attempt, item)
        return row

    def _update_hypothesis(self, attempt: Attempt, item: PracticeItem) -> None:
        if item.targets_hypothesis_id is None:
            return
        h = self.db.get(GapHypothesis, item.targets_hypothesis_id)
        if h is None or h.status != "proposed":
            return
        base = select(func.count(func.distinct(Attempt.item_id))).join(PracticeItem, PracticeItem.id == Attempt.item_id).where(
            PracticeItem.targets_hypothesis_id == h.id, Attempt.evidence_event_id.is_not(None))
        failed = self.db.scalar(base.where(Attempt.correct.is_(False)))
        solid = self.db.scalar(base.where(Attempt.correct.is_(True), Attempt.hints_used == 0))
        ids = [str(i) for i in self.db.scalars(select(Attempt.evidence_event_id).join(
            PracticeItem, PracticeItem.id == Attempt.item_id).where(PracticeItem.targets_hypothesis_id == h.id,
                                                                    Attempt.evidence_event_id.is_not(None))).all()]
        if failed >= self.s.hypothesis_confirm_failures:
            h.status, h.resolved_at = "confirmed", _now()
            h.resolution = {"source": "attempts", "rule": f">={self.s.hypothesis_confirm_failures} failed distinct targeted items",
                            "evidence_ids": ids}
        elif solid >= self.s.hypothesis_refute_successes:
            h.status, h.resolved_at = "refuted", _now()
            h.resolution = {"source": "attempts", "rule": f">={self.s.hypothesis_refute_successes} correct distinct targeted items without hints",
                            "evidence_ids": ids}

    # ------------------------------------------------------------------ hypotheses
    def propose_hypotheses(self, student_id: uuid.UUID, run_id: uuid.UUID, drafts) -> list[GapHypothesis]:
        out = []
        for g in drafts:
            key = hypothesis_key(g.description)
            tid = uuid.UUID(g.topic_id)
            existing = self.db.scalar(select(GapHypothesis).where(
                GapHypothesis.student_id == student_id, GapHypothesis.topic_id == tid, GapHypothesis.key == key))
            if existing is None:       # never re-propose something already confirmed, refuted or expired
                existing = GapHypothesis(student_id=student_id, topic_id=tid, description=g.description[:300], key=key,
                                         source_run_id=run_id)
                self.db.add(existing)
                self.db.flush()
            out.append(existing)
        return out

    def open_hypothesis(self, student_id: uuid.UUID, topic_id: uuid.UUID) -> GapHypothesis | None:
        return self.db.scalar(select(GapHypothesis).where(
            GapHypothesis.student_id == student_id, GapHypothesis.topic_id == topic_id,
            GapHypothesis.status == "proposed").order_by(GapHypothesis.created_at).limit(1))

    def teacher_decide(self, hypothesis_id: uuid.UUID, decision: str, teacher_id: uuid.UUID, escalation_id: uuid.UUID) -> GapHypothesis | None:
        if decision not in ("confirmed", "refuted"):
            raise ValueError("decision must be confirmed or refuted")
        h = self.db.get(GapHypothesis, hypothesis_id)
        if h is None:
            return None
        history = list((h.resolution or {}).get("history", []))
        history.append({"source": "teacher", "decision": decision, "teacher_id": str(teacher_id),
                        "escalation_id": str(escalation_id), "at": _now().isoformat(), "previous_status": h.status})
        h.status, h.resolved_at = decision, _now()
        h.resolution = {**(h.resolution or {}), "source": "teacher", "history": history}
        return h

    def expire_stale(self, now: datetime | None = None) -> int:
        now = now or _now()
        cutoff = now - timedelta(days=self.s.hypothesis_expiry_days)
        rows = self.db.scalars(select(GapHypothesis).where(GapHypothesis.status == "proposed", GapHypothesis.created_at < cutoff)).all()
        n = 0
        for h in rows:
            touched = self.db.scalar(select(func.count()).select_from(Attempt).join(PracticeItem, PracticeItem.id == Attempt.item_id).where(
                PracticeItem.targets_hypothesis_id == h.id, Attempt.created_at >= cutoff))
            if not touched:
                h.status, h.resolved_at = "expired", now
                h.resolution = {"source": "expiry", "rule": f"no evidence for {self.s.hypothesis_expiry_days} days"}
                n += 1
        return n

    # ------------------------------------------------------------------ student-facing progress
    def progress(self, student_id: uuid.UUID, now: datetime | None = None) -> list[dict]:
        topic_ids = set(self.db.scalars(select(LearnerTopicState.topic_id).where(LearnerTopicState.student_id == student_id)))
        topic_ids |= set(self.db.scalars(select(GapHypothesis.topic_id).where(GapHypothesis.student_id == student_id)))
        out = []
        for t in self.db.scalars(select(Topic).where(Topic.id.in_(topic_ids)).order_by(Topic.sort)) if topic_ids else []:
            v = self.view(student_id, t.id, now)
            hyps = self.db.scalars(select(GapHypothesis).where(GapHypothesis.student_id == student_id,
                                                               GapHypothesis.topic_id == t.id).order_by(GapHypothesis.created_at)).all()
            out.append({"topic_id": str(t.id), "topic": t.name, **v,
                        "hypotheses": [{"id": str(h.id), "description": h.description, "status": h.status,
                                        "kind": {"proposed": "suspected", "confirmed": "confirmed"}.get(h.status, h.status),
                                        "resolution": h.resolution} for h in hyps]})
        return out

    def history(self, student_id: uuid.UUID, limit: int = 100) -> list[dict]:
        rows = self.db.execute(select(MasteryHistory, EvidenceEvent, Topic).join(
            EvidenceEvent, EvidenceEvent.id == MasteryHistory.evidence_id).join(Topic, Topic.id == MasteryHistory.topic_id).where(
            MasteryHistory.student_id == student_id).order_by(MasteryHistory.created_at.desc()).limit(limit)).all()
        return [{"at": h.created_at, "topic": t.name, "topic_id": str(t.id), "status": h.status, "mean": round(h.mean, 4),
                 "evidence_count": h.evidence_count, "distinct_sources": h.distinct_sources,
                 "evidence": {"id": str(e.id), "type": e.evidence_type, "weight": e.weight,
                              "attempt_id": (e.provenance or {}).get("attempt_id"),
                              "item_id": (e.provenance or {}).get("item_id"),
                              "escalation_id": (e.provenance or {}).get("escalation_id"),
                              "grader": (e.provenance or {}).get("grader")}} for h, e, t in rows]
