"""Evidence ledger (core-api, module `learner`). Append-only (database trigger). Explicit validation: only registered
evidence types are accepted, self-reports can never carry mastery weight, graded evidence needs its provenance (attempt,
item, grader), and teacher evidence needs its escalation and teacher. The mastery model reads this ledger; it never writes
to it, and nothing here infers mastery from acknowledgments."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.models import EvidenceEvent

# type -> maximum permitted weight (0 = the type can never carry mastery weight)
ALLOWED_TYPES: dict[str, float] = {
    "self_report_understood": 0.0,
    "self_report_confused": 0.0,
    "check_requested": 0.0,
    "attempt_correct": 1.0,
    "attempt_incorrect": 1.0,
    "teacher_assessment_solid": 3.0,
    "teacher_assessment_struggling": 3.0,
    "teacher_assessment_emerging": 0.0,           # informational only, like a self-report
}
POSITIVE = frozenset({"attempt_correct", "teacher_assessment_solid"})
NEGATIVE = frozenset({"attempt_incorrect", "teacher_assessment_struggling"})
WEIGHTED = POSITIVE | NEGATIVE
REQUIRED_PROVENANCE = {
    "self_report": ("source", "session_id", "run_id"),
    "attempt": ("source", "session_id", "run_id", "attempt_id", "item_id", "grader"),
    "teacher": ("source", "session_id", "run_id", "escalation_id", "teacher_id"),
}
ACK_TO_TYPE = {"understood": "self_report_understood", "still_confused": "self_report_confused",
               "check_me": "check_requested"}
Ack = Literal["understood", "still_confused", "check_me"]


class EvidenceRejected(ValueError):
    """The evidence failed validation and was NOT written."""


def _family(t: str) -> str:
    return "attempt" if t.startswith("attempt_") else ("teacher" if t.startswith("teacher_") else "self_report")


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: uuid.UUID
    course_id: uuid.UUID
    topic_id: uuid.UUID | None = None
    evidence_type: str
    weight: float = Field(default=0.0, ge=0.0, le=3.0)
    source_run_id: uuid.UUID
    source_ref: str = Field(min_length=3, max_length=128)       # idempotency key of the producer
    provenance: dict
    created_at: datetime | None = None                          # tests / back-fills only; production leaves the DB clock

    @model_validator(mode="after")
    def _validate(self) -> "EvidenceIn":
        t = self.evidence_type
        if t not in ALLOWED_TYPES:
            raise EvidenceRejected(f"unknown evidence type {t!r}")
        if self.weight > ALLOWED_TYPES[t]:
            raise EvidenceRejected(f"{t} may carry weight at most {ALLOWED_TYPES[t]}, got {self.weight}")
        if t in WEIGHTED and self.weight <= 0:
            raise EvidenceRejected(f"{t} must carry a positive weight")
        if t not in WEIGHTED and self.weight != 0:
            raise EvidenceRejected(f"{t} must carry weight 0, got {self.weight}")
        if t in WEIGHTED and self.topic_id is None:
            raise EvidenceRejected("weighted evidence must name its topic")
        for key in REQUIRED_PROVENANCE[_family(t)]:
            if key not in self.provenance:
                raise EvidenceRejected(f"provenance is missing {key!r}")
        if str(self.provenance["run_id"]) != str(self.source_run_id):
            raise EvidenceRejected("provenance.run_id must equal source_run_id")
        return self


def append_evidence(db: Session, ev: EvidenceIn) -> EvidenceEvent:
    """Add a validated row to the session (the caller commits, so it is atomic with the state change that caused it).
    Duplicate (evidence_type, source_ref) is rejected by a unique constraint at commit time."""
    row = EvidenceEvent(student_id=ev.student_id, course_id=ev.course_id, topic_id=ev.topic_id,
                        evidence_type=ev.evidence_type, weight=ev.weight, source_run_id=ev.source_run_id,
                        source_ref=ev.source_ref, provenance=ev.provenance)
    if ev.created_at is not None:
        row.created_at = ev.created_at
    db.add(row)
    return row
