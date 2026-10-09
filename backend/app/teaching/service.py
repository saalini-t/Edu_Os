"""Teaching service (core-api, module `teaching`): deterministic matching, escalation lifecycle, asynchronous student-teacher
thread, resolution feedback, evidence from teacher assessments, expiry, and the audit trail. A model never decides anything
here: matching is arithmetic, access is explicit rules, and every transition writes an `escalation_events` row."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, Forbidden, NotFound
from app.learner.ledger import EvidenceIn, EvidenceRejected, append_evidence
from app.learner.service import LearnerService
from app.models import (
    Attempt, AvailabilitySlot, Course, Escalation, EscalationEvent, EscalationMessage, GapHypothesis, Intervention, Message,
    PracticeItem, TeacherCourse, TeacherFeedback, TeacherProfile, TeacherTopic, Topic, User,
)
from app.teaching import matching as mt

ACTIVE = ("OPEN", "ACCEPTED")
LEVELS = ("struggling", "emerging", "solid")
LEVEL_TO_TYPE = {"solid": "teacher_assessment_solid", "struggling": "teacher_assessment_struggling",
                 "emerging": "teacher_assessment_emerging"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TeachingService:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.s = db, settings
        self.learner = LearnerService(db, settings)

    # ------------------------------------------------------------------ audit
    def event(self, esc: Escalation, event: str, actor_id: uuid.UUID | None, **meta) -> None:
        self.db.add(EscalationEvent(escalation_id=esc.id, event=event, actor_id=actor_id, meta=meta))

    # ------------------------------------------------------------------ matching
    def _facts(self, course_id: uuid.UUID, topic_id: uuid.UUID | None, now: datetime) -> list[mt.TeacherFacts]:
        teachers = self.db.execute(select(User, TeacherProfile).join(TeacherProfile, TeacherProfile.user_id == User.id).join(
            TeacherCourse, TeacherCourse.teacher_id == User.id).where(TeacherCourse.course_id == course_id, User.role == "teacher",
                                                                      User.active.is_(True))).all()
        out = []
        for u, prof in teachers:
            prof_topic = self.db.scalar(select(TeacherTopic.proficiency).where(TeacherTopic.teacher_id == u.id,
                                                                                TeacherTopic.topic_id == topic_id)) if topic_id else None
            best = self.db.scalar(select(func.max(TeacherTopic.proficiency)).join(Topic, Topic.id == TeacherTopic.topic_id).where(
                TeacherTopic.teacher_id == u.id, Topic.course_id == course_id)) or 0.0
            slots = [(a, b) for a, b in self.db.execute(select(AvailabilitySlot.start_at, AvailabilitySlot.end_at).where(
                AvailabilitySlot.teacher_id == u.id, AvailabilitySlot.booked_by_escalation_id.is_(None),
                AvailabilitySlot.end_at > now))]
            rated = self.db.scalar(select(func.count()).select_from(Escalation).where(
                Escalation.assigned_teacher_id == u.id, Escalation.status == "RESOLVED", Escalation.student_helpful.is_not(None)))
            helpful = self.db.scalar(select(func.count()).select_from(Escalation).where(
                Escalation.assigned_teacher_id == u.id, Escalation.status == "RESOLVED", Escalation.student_helpful.is_(True)))
            load = self.db.scalar(select(func.count()).select_from(Escalation).where(
                Escalation.assigned_teacher_id == u.id, Escalation.status == "ACCEPTED"))
            out.append(mt.TeacherFacts(str(u.id), u.display_name, prof.active, list(prof.languages or []), prof_topic,
                                       float(best), slots, helpful, rated, load))
        return out

    def match(self, student: User, course_id: uuid.UUID, topic_id: uuid.UUID | None, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        w = mt.Weights(self.s.match_w_topic, self.s.match_w_availability, self.s.match_w_language, self.s.match_w_feedback,
                       self.s.match_w_load)
        ctx = mt.Context(topic_known=topic_id is not None, student_language=student.language, now=now,
                         window_hours=self.s.match_window_hours, max_open=self.s.teacher_max_open)
        return mt.rank(self._facts(course_id, topic_id, now), ctx, w)

    # ------------------------------------------------------------------ creation (called by the workflow)
    def build_brief(self, state, rule_id: str, reasons: list[str]) -> dict:
        sid, topic_id = uuid.UUID(state.student_id), (uuid.UUID(state.analysis.topic_id) if state.analysis and state.analysis.topic_id else None)
        attempts = []
        for a, it in self.db.execute(select(Attempt, PracticeItem).join(PracticeItem, PracticeItem.id == Attempt.item_id).where(
                Attempt.session_id == uuid.UUID(state.session_id)).order_by(Attempt.created_at).limit(12)):
            attempts.append({"prompt": it.prompt[:200], "kind": it.kind, "difficulty": it.difficulty, "answer": a.answer[:300],
                             "correct": a.correct, "scored": a.status in ("GRADED", "UNCERTAIN"), "error_tags": a.error_tags,
                             "feedback": (a.feedback or "")[:200]})
        last_expl = self.db.scalar(select(Intervention.payload).where(
            Intervention.session_id == uuid.UUID(state.session_id), Intervention.action == "GENERATE_EXPLANATION").order_by(
            Intervention.created_at.desc()).limit(1))
        hyps = []
        mastery = None
        if topic_id:
            hyps = [{"id": str(h.id), "description": h.description, "status": h.status} for h in self.db.scalars(
                select(GapHypothesis).where(GapHypothesis.student_id == sid, GapHypothesis.topic_id == topic_id))]
            v = self.learner.view(sid, topic_id)
            mastery = {k: v[k] for k in ("status", "mean", "evidence_count", "distinct_sources")}
        topic = self.db.get(Topic, topic_id) if topic_id else None
        return {"doubt": state.doubt_text, "follow_ups": state.history[-5:],
                "reason": {"rule_id": rule_id, "reasons": reasons},
                "topic": {"id": str(topic.id), "name": topic.name} if topic else None,
                "explanation_given": state.explained,
                "last_explanation": (last_expl or {}).get("explanation", {}).get("text", "")[:700] if last_expl else "",
                "attempts": attempts, "hypotheses": hyps, "mastery": mastery,
                "retrieved_chunk_ids": state.context.chunk_ids,
                "counters": state.counters.model_dump()}

    def create_escalation(self, *, run_id: uuid.UUID, state, rule_id: str, reasons: list[str]) -> Escalation:
        existing = self.db.scalar(select(Escalation).where(Escalation.run_id == run_id, Escalation.status.in_(ACTIVE)))
        if existing is not None:
            return existing                                    # re-entering the node must not create a second escalation
        student = self.db.get(User, uuid.UUID(state.student_id))
        topic_id = uuid.UUID(state.analysis.topic_id) if state.analysis and state.analysis.topic_id else None
        now = _now()
        cands = self.match(student, uuid.UUID(state.course_id), topic_id, now)
        esc = Escalation(session_id=uuid.UUID(state.session_id), run_id=run_id, student_id=student.id,
                         course_id=uuid.UUID(state.course_id), topic_id=topic_id, status="OPEN", reason_rule_id=rule_id,
                         brief=self.build_brief(state, rule_id, reasons), candidates=cands,
                         expires_at=now + timedelta(hours=self.s.escalation_ttl_hours))
        self.db.add(esc)
        self.db.flush()
        self.event(esc, "created", student.id, rule_id=rule_id, topic_id=str(topic_id) if topic_id else None)
        self.event(esc, "matched", None, n=len(cands), top=[{"teacher_id": c["teacher_id"], "score": c["score"]} for c in cands[:3]],
                   unmatched_policy="stays OPEN and visible to all teachers of the course until it expires")
        return esc

    # ------------------------------------------------------------------ access rules (one place)
    def teacher_covers(self, teacher_id: uuid.UUID, course_id: uuid.UUID) -> bool:
        return self.db.get(TeacherCourse, (teacher_id, course_id)) is not None

    def teacher_can_view(self, esc: Escalation, teacher_id: uuid.UUID) -> bool:
        """Assigned teacher always; otherwise only while the escalation is OPEN and the teacher covers its course and is either
        a ranked candidate or no candidate exists at all (unmatched escalations stay visible to the course's teachers)."""
        if esc.assigned_teacher_id == teacher_id:
            return True
        if esc.status != "OPEN" or not self.teacher_covers(teacher_id, esc.course_id):
            return False
        ids = {c["teacher_id"] for c in (esc.candidates or [])}
        return not ids or str(teacher_id) in ids

    def get_for(self, user: User, esc_id: uuid.UUID, *, lock: bool = False) -> Escalation:
        q = select(Escalation).where(Escalation.id == esc_id)
        esc = self.db.scalar(q.with_for_update() if lock else q)
        ok = esc is not None and (user.role == "admin" or (user.role == "student" and esc.student_id == user.id)
                                  or (user.role == "teacher" and self.teacher_can_view(esc, user.id)))
        if not ok:
            raise NotFound("Escalation")                      # no existence leak across students / teachers
        return esc

    # ------------------------------------------------------------------ teacher actions
    def accept(self, teacher: User, esc_id: uuid.UUID, slot_id: uuid.UUID | None = None) -> Escalation:
        esc = self.get_for(teacher, esc_id, lock=True)
        if esc.status != "OPEN":
            raise AppError(409, "ESCALATION_ALREADY_ACCEPTED" if esc.status == "ACCEPTED" else "ESCALATION_NOT_OPEN",
                           f"Escalation is {esc.status}")
        if slot_id is not None:
            slot = self.db.get(AvailabilitySlot, slot_id)
            if slot is None or slot.teacher_id != teacher.id or slot.booked_by_escalation_id is not None:
                raise AppError(422, "VALIDATION_ERROR", "Slot is not one of your free slots")
            slot.booked_by_escalation_id = esc.id
        esc.status, esc.assigned_teacher_id, esc.accepted_at = "ACCEPTED", teacher.id, _now()
        self.event(esc, "accepted", teacher.id, slot_id=str(slot_id) if slot_id else None)
        self.db.commit()
        return esc

    def release(self, user: User, esc_id: uuid.UUID) -> Escalation:
        esc = self.get_for(user, esc_id, lock=True)
        if esc.status != "ACCEPTED" or not (user.role == "admin" or esc.assigned_teacher_id == user.id):
            raise AppError(409, "INVALID_ESCALATION_STATE", "Only the assigned teacher (or an admin) can release an accepted escalation")
        prev = esc.assigned_teacher_id
        esc.status, esc.assigned_teacher_id, esc.accepted_at = "OPEN", None, None
        self.event(esc, "released", user.id, previous_teacher_id=str(prev))
        self.db.commit()
        return esc

    def admin_assign(self, admin: User, esc_id: uuid.UUID, teacher_id: uuid.UUID) -> Escalation:
        esc = self.get_for(admin, esc_id, lock=True)
        if esc.status not in ACTIVE:
            raise AppError(409, "INVALID_ESCALATION_STATE", f"Escalation is {esc.status}")
        teacher = self.db.get(User, teacher_id)
        if teacher is None or teacher.role != "teacher" or not teacher.active or not self.teacher_covers(teacher_id, esc.course_id):
            raise AppError(422, "VALIDATION_ERROR", "Teacher must be an active teacher of this escalation's course")
        previous = esc.assigned_teacher_id
        in_candidates = str(teacher_id) in {c["teacher_id"] for c in (esc.candidates or [])}
        esc.status, esc.assigned_teacher_id, esc.accepted_at = "ACCEPTED", teacher_id, _now()
        self.event(esc, "reassigned" if previous else "assigned", admin.id, teacher_id=str(teacher_id),
                   previous_teacher_id=str(previous) if previous else None)
        if not in_candidates:
            self.event(esc, "overridden", admin.id, reason="teacher was not among the matcher's candidates", teacher_id=str(teacher_id))
        self.db.commit()
        return esc

    # ------------------------------------------------------------------ thread
    def add_message(self, user: User, esc_id: uuid.UUID, content: str) -> EscalationMessage:
        esc = self.get_for(user, esc_id, lock=True)
        if esc.status not in ACTIVE:
            raise AppError(409, "INVALID_ESCALATION_STATE", f"Escalation is {esc.status}; the thread is read-only")
        if user.role == "teacher" and not (esc.status == "ACCEPTED" and esc.assigned_teacher_id == user.id):
            raise AppError(409, "INVALID_ESCALATION_STATE", "Accept the escalation before replying")
        if user.role == "admin":
            raise Forbidden("Admins do not take part in the student-teacher thread")
        msg = EscalationMessage(escalation_id=esc.id, author_id=user.id, author_role=user.role, content=content.strip())
        self.db.add(msg)
        self.event(esc, "message", user.id, role=user.role, chars=len(content))
        self.db.commit()
        return msg

    def thread(self, esc_id: uuid.UUID) -> list[dict]:
        rows = self.db.execute(select(EscalationMessage, User.display_name).join(User, User.id == EscalationMessage.author_id).where(
            EscalationMessage.escalation_id == esc_id).order_by(EscalationMessage.created_at)).all()
        return [{"id": str(m.id), "role": m.author_role, "author": name, "content": m.content, "at": m.created_at} for m, name in rows]

    # ------------------------------------------------------------------ resolution
    def resolve(self, teacher: User, esc_id: uuid.UUID, *, notes: str, topic_assessments: list[dict],
                hypothesis_decisions: list[dict]) -> Escalation:
        """Store the feedback, write teacher evidence to the ledger, apply hypothesis decisions, close the escalation and mark the
        workflow resume as PENDING. The resume itself happens afterwards (resume.py) and can be retried safely."""
        esc = self.get_for(teacher, esc_id, lock=True)
        if esc.status != "ACCEPTED" or esc.assigned_teacher_id != teacher.id:
            raise AppError(409, "INVALID_ESCALATION_STATE", "Only the assigned teacher can resolve an accepted escalation")
        course_topics = {t.id: t for t in self.db.scalars(select(Topic).where(Topic.course_id == esc.course_id))}
        clean_assess = []
        for a in topic_assessments:
            tid = uuid.UUID(str(a["topic_id"]))
            if tid not in course_topics or a["level"] not in LEVELS:
                raise AppError(422, "VALIDATION_ERROR", "Assessment needs a topic of this course and a valid level")
            clean_assess.append({"topic_id": str(tid), "level": a["level"]})
        if len({a["topic_id"] for a in clean_assess}) != len(clean_assess):
            raise AppError(422, "VALIDATION_ERROR", "Give at most one assessment per topic")
        decisions = []
        for d in hypothesis_decisions:
            h = self.db.get(GapHypothesis, uuid.UUID(str(d["hypothesis_id"])))
            if h is None or h.student_id != esc.student_id or h.topic_id not in course_topics or d["decision"] not in ("confirmed", "refuted"):
                raise AppError(422, "VALIDATION_ERROR", "Hypothesis decision must reference one of this student's hypotheses")
            decisions.append({"hypothesis_id": str(h.id), "decision": d["decision"]})
        self.db.add(TeacherFeedback(escalation_id=esc.id, teacher_id=teacher.id, notes=notes.strip(),
                                    topic_assessments=clean_assess, hypothesis_decisions=decisions))
        for a in clean_assess:                                   # teacher evidence: bounded weight, full provenance
            etype = LEVEL_TO_TYPE[a["level"]]
            try:
                row = append_evidence(self.db, EvidenceIn(
                    student_id=esc.student_id, course_id=esc.course_id, topic_id=uuid.UUID(a["topic_id"]), evidence_type=etype,
                    weight=self.s.w_teacher if a["level"] != "emerging" else 0.0, source_run_id=esc.run_id,
                    source_ref=f"teacher:{esc.id}:{a['topic_id']}",
                    provenance={"source": "teacher_feedback", "schema_version": 1, "session_id": str(esc.session_id),
                                "run_id": str(esc.run_id), "escalation_id": str(esc.id), "teacher_id": str(teacher.id),
                                "level": a["level"]}))
            except EvidenceRejected as e:
                raise AppError(422, "EVIDENCE_REJECTED", str(e))
            self.db.flush()
            self.learner.apply_row(row)
        for d in decisions:
            self.learner.teacher_decide(uuid.UUID(d["hypothesis_id"]), d["decision"], teacher.id, esc.id)
        self.db.add(EscalationMessage(escalation_id=esc.id, author_id=teacher.id, author_role="teacher",
                                      content=f"Resolution: {notes.strip()}"))
        esc.status, esc.resolved_at = "RESOLVED", _now()
        esc.resume_status, esc.resume_outcome = "pending", "RESOLVED"
        esc.resume_message = f"A teacher resolved this doubt: {notes.strip()[:500]}"
        self.event(esc, "resolved", teacher.id, assessments=clean_assess, hypothesis_decisions=decisions)
        self.event(esc, "resume_requested", None, outcome="RESOLVED")
        self.db.commit()
        return esc

    def rate(self, student: User, esc_id: uuid.UUID, helpful: bool) -> Escalation:
        esc = self.get_for(student, esc_id, lock=True)
        if esc.status != "RESOLVED":
            raise AppError(409, "INVALID_ESCALATION_STATE", "You can rate a resolved escalation only")
        if esc.student_helpful is not None:
            raise AppError(409, "ALREADY_RATED", "This escalation was already rated")
        esc.student_helpful = helpful
        self.event(esc, "rated", student.id, helpful=helpful)
        self.db.commit()
        return esc

    # ------------------------------------------------------------------ expiry
    def expire_due(self, now: datetime | None = None) -> list[uuid.UUID]:
        """OPEN/ACCEPTED escalations past their expiry are closed; their runs end UNRESOLVED (via the pending resume)."""
        now = now or _now()
        rows = self.db.scalars(select(Escalation).where(Escalation.status.in_(ACTIVE), Escalation.expires_at < now).with_for_update(skip_locked=True)).all()
        ids = []
        for esc in rows:
            esc.status, esc.resolved_at = "EXPIRED", now
            esc.resume_status, esc.resume_outcome = "pending", "EXPIRED"
            esc.resume_message = ("No teacher was able to resolve this doubt in time, so it stays unresolved. You can ask again "
                                  "or submit a new doubt.")
            self.event(esc, "expired", None, expires_at=esc.expires_at.isoformat())
            self.event(esc, "resume_requested", None, outcome="EXPIRED")
            ids.append(esc.id)
        self.db.commit()
        return ids

    # ------------------------------------------------------------------ availability
    def replace_availability(self, teacher: User, slots: list[dict]) -> list[AvailabilitySlot]:
        now = _now()
        if len(slots) > 50:
            raise AppError(422, "VALIDATION_ERROR", "At most 50 slots")
        parsed = []
        for s in slots:
            a, b = s["start_at"], s["end_at"]
            if b <= a or a < now - timedelta(minutes=5) or b > now + timedelta(days=120) or (b - a) > timedelta(hours=12):
                raise AppError(422, "VALIDATION_ERROR", "Slots must be in the next 120 days, end after they start, and be at most 12 hours")
            parsed.append((a, b))
        for old in self.db.scalars(select(AvailabilitySlot).where(AvailabilitySlot.teacher_id == teacher.id,
                                                                  AvailabilitySlot.end_at > now,
                                                                  AvailabilitySlot.booked_by_escalation_id.is_(None))):
            self.db.delete(old)                                  # booked slots are kept: they back an accepted escalation
        out = []
        for a, b in parsed:
            slot = AvailabilitySlot(teacher_id=teacher.id, start_at=a, end_at=b)
            self.db.add(slot)
            out.append(slot)
        self.db.commit()
        return out

    # ------------------------------------------------------------------ views
    def _base(self, esc: Escalation) -> dict:
        topic = self.db.get(Topic, esc.topic_id) if esc.topic_id else None
        course = self.db.get(Course, esc.course_id)
        return {"id": str(esc.id), "status": esc.status, "session_id": str(esc.session_id),
                "course": {"id": str(course.id), "name": course.name},
                "topic": {"id": str(topic.id), "name": topic.name} if topic else None,
                "created_at": esc.created_at, "expires_at": esc.expires_at, "resolved_at": esc.resolved_at,
                "messages": self.thread(esc.id)}

    def student_view(self, esc: Escalation) -> dict:
        v = self._base(esc)
        teacher = self.db.get(User, esc.assigned_teacher_id) if esc.assigned_teacher_id else None
        fb = self.db.scalar(select(TeacherFeedback).where(TeacherFeedback.escalation_id == esc.id))
        v.update(assigned_teacher=teacher.display_name if teacher else None,
                 waiting_for="a teacher to accept" if esc.status == "OPEN" else ("the teacher to reply" if esc.status == "ACCEPTED" else None),
                 can_message=esc.status in ACTIVE, resolution={"notes": fb.notes} if fb else None,
                 rated=esc.student_helpful, outcome=esc.resume_outcome)
        return v

    def sources_for(self, esc: Escalation) -> list[dict]:
        """The course passages the explanation was built from. Only COURSE-visible material of the escalation's own course:
        a student's private documents are never shown to a teacher."""
        from app.models import Chunk, Document
        ids = []
        for c in (esc.brief or {}).get("retrieved_chunk_ids", []):
            try:
                ids.append(uuid.UUID(c))
            except ValueError:
                pass
        if not ids:
            return []
        rows = self.db.execute(select(Chunk, Document.title).join(Document, Document.id == Chunk.document_id).where(
            Chunk.id.in_(ids), Chunk.course_id == esc.course_id, Chunk.visibility == "course").order_by(Chunk.page, Chunk.chunk_index)).all()
        return [{"chunk_id": str(c.id), "document_title": t, "page": c.page, "text": c.text[:900]} for c, t in rows]

    def teacher_view(self, esc: Escalation) -> dict:
        v = self._base(esc)
        student = self.db.get(User, esc.student_id)
        msgs = self.db.scalars(select(Message).where(Message.session_id == esc.session_id).order_by(Message.created_at)).all()
        live = None
        if esc.topic_id:
            live = {"mastery": {k: v2 for k, v2 in self.learner.view(esc.student_id, esc.topic_id).items() if k != "last_evidence_at"},
                    "hypotheses": [{"id": str(h.id), "description": h.description, "status": h.status} for h in self.db.scalars(
                        select(GapHypothesis).where(GapHypothesis.student_id == esc.student_id, GapHypothesis.topic_id == esc.topic_id))]}
        v.update(sources=self.sources_for(esc),
                 student={"display_name": student.display_name, "language": student.language}, brief=esc.brief,
                 doubt_conversation=[{"role": m.role, "content": m.content[:1200], "at": m.created_at} for m in msgs],
                 live=live, assigned_to_me=esc.assigned_teacher_id is not None, can_accept=esc.status == "OPEN",
                 can_resolve=esc.status == "ACCEPTED")
        return v

    def admin_view(self, esc: Escalation) -> dict:
        v = self.teacher_view(esc)
        events = self.db.scalars(select(EscalationEvent).where(EscalationEvent.escalation_id == esc.id).order_by(EscalationEvent.created_at)).all()
        v.update(candidates=esc.candidates, assigned_teacher_id=str(esc.assigned_teacher_id) if esc.assigned_teacher_id else None,
                 student_id=str(esc.student_id), run_id=str(esc.run_id), reason_rule_id=esc.reason_rule_id,
                 resume={"status": esc.resume_status, "outcome": esc.resume_outcome},
                 events=[{"event": e.event, "actor_id": str(e.actor_id) if e.actor_id else None, "meta": e.meta, "at": e.created_at} for e in events])
        return v

    def summary_row(self, esc: Escalation) -> dict:
        topic = self.db.get(Topic, esc.topic_id) if esc.topic_id else None
        return {"id": str(esc.id), "status": esc.status, "topic": topic.name if topic else None, "reason_rule_id": esc.reason_rule_id,
                "created_at": esc.created_at, "expires_at": esc.expires_at,
                "assigned_teacher_id": str(esc.assigned_teacher_id) if esc.assigned_teacher_id else None,
                "doubt": (esc.brief or {}).get("doubt", "")[:160], "matched_candidates": len(esc.candidates or [])}

    def list_for_teacher(self, teacher: User, status: str | None) -> list[Escalation]:
        q = select(Escalation).where(Escalation.course_id.in_(select(TeacherCourse.course_id).where(TeacherCourse.teacher_id == teacher.id)))
        if status:
            q = q.where(Escalation.status == status)
        return [e for e in self.db.scalars(q.order_by(Escalation.created_at.desc()).limit(200)) if self.teacher_can_view(e, teacher.id)]
