"""ORM models. Three schemas mirror the service-ownership boundaries in docs/ARCHITECTURE.md:
`core` (core-api), `orch` (orchestrator), `know` (knowledge-svc).
Cross-schema references are by value (no foreign keys)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, Float, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------- core
class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # student | teacher | admin
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en", server_default="en")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[uuid.UUID] = _uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")  # comma separated
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prerequisites: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")   # curated: slugs of prerequisite topics
    __table_args__ = (UniqueConstraint("course_id", "slug"), {"schema": "core"})


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = {"schema": "core"}
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), primary_key=True)


class DoubtSession(Base):
    __tablename__ = "doubt_sessions"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), nullable=False)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.topics.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # by value
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    messages: Mapped[list["Message"]] = relationship(order_by="Message.created_at", cascade="all, delete-orphan")
    interventions: Mapped[list["Intervention"]] = relationship(order_by="Intervention.created_at",
                                                               cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.doubt_sessions.id", ondelete="CASCADE"),
                                                  nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # student | system | teacher
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)


class Intervention(Base):
    __tablename__ = "interventions"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.doubt_sessions.id", ondelete="CASCADE"),
                                                  nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)


class EvidenceEvent(Base):
    """Append-only learner evidence ledger (a database trigger rejects UPDATE and DELETE). Phase 2 stores ONLY
    self-reports, which carry ZERO mastery weight (CHECK constraints). Mastery evidence from graded attempts and
    teacher assessments arrives in Phase 4 together with the model that consumes it."""
    __tablename__ = "evidence_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), nullable=False)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.topics.id"), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    source_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)   # by value (orch schema)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)                   # idempotency key of the source
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)
    __table_args__ = (
        UniqueConstraint("evidence_type", "source_ref", name="uq_evidence_type_source_ref"),
        CheckConstraint("evidence_type IN ('self_report_understood', 'self_report_confused', 'check_requested', "
                        "'attempt_correct', 'attempt_incorrect', 'teacher_assessment_solid', "
                        "'teacher_assessment_struggling', 'teacher_assessment_emerging')", name="ck_evidence_type_known"),
        CheckConstraint("weight >= 0 AND weight <= 3", name="ck_evidence_weight_range"),
        CheckConstraint("evidence_type NOT LIKE 'self_report%' OR weight = 0", name="ck_self_report_zero_weight"),
        CheckConstraint("evidence_type <> 'check_requested' OR weight = 0", name="ck_check_requested_zero_weight"),
        CheckConstraint("evidence_type NOT LIKE 'attempt_%' OR (weight > 0 AND weight <= 1)", name="ck_attempt_weight"),
        CheckConstraint("evidence_type NOT IN ('teacher_assessment_solid', 'teacher_assessment_struggling') OR weight > 0",
                        name="ck_teacher_weight_positive"),
        CheckConstraint("evidence_type <> 'teacher_assessment_emerging' OR weight = 0", name="ck_teacher_emerging_zero"),
        {"schema": "core"},
    )


class GapHypothesis(Base):
    """A SUSPECTED knowledge gap. 'proposed' carries no mastery weight; it becomes 'confirmed' only after failures on
    distinct targeted items or explicit teacher confirmation, and 'refuted' after repeated success or teacher refutation."""
    __tablename__ = "gap_hypotheses"
    id: Mapped[uuid.UUID] = _uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.topics.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    key: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")  # proposed confirmed refuted expired
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # who/what resolved it + ledger evidence ids
    created_at: Mapped[datetime] = _now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("student_id", "topic_id", "key"),
                      CheckConstraint("status IN ('proposed', 'confirmed', 'refuted', 'expired')", name="ck_hypothesis_status"),
                      {"schema": "core"})


class PracticeItem(Base):
    """A generated question. answer_key / rubric are NEVER serialised to students before they submit an answer."""
    __tablename__ = "practice_items"
    id: Mapped[uuid.UUID] = _uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.doubt_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.topics.id"), nullable=False)
    set_index: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    answer_key: Mapped[str] = mapped_column(Text, nullable=False)
    numeric_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    rubric: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str] = mapped_column(String(8), nullable=False)
    source_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    distractor_tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    targets_hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.gap_hypotheses.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="model")     # model | seed_bank
    prompt_hash: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ix_practice_student_prompt", "student_id", "prompt_hash"),
                      CheckConstraint("kind IN ('mcq', 'numeric', 'short_text')", name="ck_practice_kind"),
                      {"schema": "core"})


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.practice_items.id", ondelete="CASCADE"), nullable=False, index=True)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    hints_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="SUBMITTED")  # SUBMITTED GRADED UNCERTAIN UNGRADED
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    partial_credit: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    grader: Mapped[str | None] = mapped_column(String(16), nullable=True)
    grader_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    evidence_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)   # ledger row (by value)
    created_at: Mapped[datetime] = _now()
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("student_id", "idempotency_key", name="uq_attempt_idempotency"),
        # one scored attempt per item: repeated submissions can never farm evidence
        Index("uq_attempt_one_scored_per_item", "item_id", unique=True, postgresql_where=text("status IN ('GRADED', 'UNCERTAIN')")),
        {"schema": "core"},
    )


class LearnerTopicState(Base):
    """Derived cache of the Beta posterior per (student, topic). The ledger is the source of truth: this row can always be
    rebuilt by replaying core.evidence_events (tests assert equality)."""
    __tablename__ = "learner_topic_state"
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), primary_key=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.topics.id"), primary_key=True)
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)   # distinct item / assessment keys seen
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = {"schema": "core"}


class MasteryHistory(Base):
    """One row per applied evidence event: the progress history, each entry pointing back at its ledger row."""
    __tablename__ = "mastery_history"
    id: Mapped[uuid.UUID] = _uuid_pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.topics.id"), nullable=False)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)   # by value: the ledger is append-only
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    mean: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_sources: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False)
    __table_args__ = (UniqueConstraint("evidence_id", name="uq_mastery_history_evidence"), {"schema": "core"})


class TeacherProfile(Base):
    __tablename__ = "teacher_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), primary_key=True)
    bio: Mapped[str] = mapped_column(Text, nullable=False, default="")
    languages: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["en"])
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = {"schema": "core"}


class TeacherCourse(Base):
    """Which courses a teacher may see escalations for. The ONLY source of a teacher's course access."""
    __tablename__ = "teacher_courses"
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), primary_key=True)
    __table_args__ = {"schema": "core"}


class TeacherTopic(Base):
    __tablename__ = "teacher_topics"
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), primary_key=True)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.topics.id"), primary_key=True)
    proficiency: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (CheckConstraint("proficiency >= 0 AND proficiency <= 1", name="ck_teacher_topic_proficiency"),
                      {"schema": "core"})


class AvailabilitySlot(Base):
    __tablename__ = "availability_slots"
    id: Mapped[uuid.UUID] = _uuid_pk()
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    booked_by_escalation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("end_at > start_at", name="ck_slot_order"), {"schema": "core"})


class Escalation(Base):
    __tablename__ = "escalations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.doubt_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)          # by value (orch schema)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False, index=True)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.courses.id"), nullable=False)
    topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.topics.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="OPEN")
    reason_rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    brief: Mapped[dict] = mapped_column(JSONB, nullable=False)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)           # ranked matcher output, for audit
    assigned_teacher_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.users.id"), nullable=True, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resume_status: Mapped[str] = mapped_column(String(8), nullable=False, default="none")   # none | pending | done
    resume_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)
    resume_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    student_helpful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        Index("uq_escalation_one_active_per_run", "run_id", unique=True, postgresql_where=text("status IN ('OPEN', 'ACCEPTED')")),
        CheckConstraint("status IN ('OPEN', 'ACCEPTED', 'RESOLVED', 'EXPIRED', 'CANCELLED')", name="ck_escalation_status"),
        CheckConstraint("resume_status IN ('none', 'pending', 'done')", name="ck_escalation_resume_status"),
        {"schema": "core"},
    )


class EscalationMessage(Base):
    __tablename__ = "escalation_messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    escalation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.escalations.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False)
    author_role: Mapped[str] = mapped_column(String(8), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False)
    __table_args__ = {"schema": "core"}


class EscalationEvent(Base):
    """Append-only audit of an escalation: created, matched, accepted, released, assigned, overridden, message, resolved,
    expired, resume requested/applied."""
    __tablename__ = "escalation_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    escalation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.escalations.id", ondelete="CASCADE"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False)
    __table_args__ = {"schema": "core"}


class TeacherFeedback(Base):
    __tablename__ = "teacher_feedback"
    id: Mapped[uuid.UUID] = _uuid_pk()
    escalation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.escalations.id", ondelete="CASCADE"), nullable=False, unique=True)
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id"), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    topic_assessments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    hypothesis_decisions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _now()
    __table_args__ = {"schema": "core"}


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "core"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = {"schema": "core"}
    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()


# ---------------------------------------------------------------- orch
class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # by value
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # by value
    trigger_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_node: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("session_id", "trigger_id"), {"schema": "orch"})


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orch.workflow_runs.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("run_id", "seq"), {"schema": "orch"})


class DecisionRecord(Base):
    __tablename__ = "decision_records"
    __table_args__ = {"schema": "orch"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orch.workflow_runs.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    inputs_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    advisor: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")   # ledger ids behind the mastery summary
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")         # provider/model/hard-rule precedence
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)


# ---------------------------------------------------------------- know
class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)    # by value
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)   # by value
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)  # private | course
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(String(300), nullable=False)
    ingestion_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)   # the ACTIVE (searchable) version
    extraction_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)         # per-page method / failures
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("owner_id", "course_id", "sha256"), {"schema": "know"})


class DocumentEvent(Base):
    """Append-only lifecycle audit for documents. No foreign key on purpose: the trail survives deletion, and it
    stores metadata only (never document text)."""
    __tablename__ = "document_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(),
                                                 nullable=False)
    __table_args__ = {"schema": "know"}


class IngestionJob(Base):
    """Durable ingestion job. PostgreSQL is the source of truth; Redis only wakes workers up."""
    __tablename__ = "ingestion_jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("know.documents.id", ondelete="CASCADE"), nullable=False,
                                                   index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="ingest")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")  # QUEUED PROCESSING DONE FAILED
    stage: Mapped[str | None] = mapped_column(String(24), nullable=True)               # parsing chunking embedding
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    enqueue_error: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_ingestion_jobs_claim", "status", "available_at"),
        Index("uq_ingestion_jobs_one_active_per_document", "document_id", unique=True,
              postgresql_where=text("status IN ('QUEUED', 'PROCESSING')")),
        {"schema": "know"},
    )


class EmbeddingModel(Base):
    """Registry of embedding models. Vectors from different models are never mixed: search uses the single
    'active' model only, and switching is a controlled operation (python -m app.knowledge.reindex)."""
    __tablename__ = "embedding_models"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dims: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")  # building | active | retired
    created_at: Mapped[datetime] = _now()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("name", "dims"),
        Index("uq_embedding_models_one_active", "status", unique=True, postgresql_where=text("status = 'active'")),
        {"schema": "know"},
    )


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("know.documents.id", ondelete="CASCADE"),
                                                   nullable=False, index=True)
    # denormalised so the ACL filter needs no join (docs/ARCHITECTURE.md section 15.3)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))
    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_course", "course_id"),
        {"schema": "know"},
    )
