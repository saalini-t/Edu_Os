"""Student erasure / anonymization compatible with the append-only evidence ledger.

Strategy (docs/ARCHITECTURE.md, ADR-013):
  * The ledger is never deleted or rewritten, EXCEPT that this privileged operation may re-point `student_id` of a student's
    evidence rows to a fresh random pseudonym ("tombstone" user) - the database trigger permits that single column change and only
    while the session variable `eduos.anonymizing` is on. Weight, type, topic, timestamps, source and provenance are untouched, so
    the aggregate learning evidence survives but is no longer linked to a person.
  * Everything that holds personal or free-text content is deleted: doubts, messages, interventions, practice items and answers,
    escalations, threads and teacher notes about the student, private documents, workflow runs/traces, idempotency records.
  * The student's account is removed; the mapping from the pseudonym back to the person is NOT stored anywhere.
  * One metadata-only audit event records that an erasure happened (hash of the old id, row counts, actor).
This spans schemas (core, orch, know) in one transaction because the application is a monolith; in a service split each service
would expose its own erase endpoint and the operation would be a saga."""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.knowledge.service import KnowledgeService
from app.models import (
    AuditEvent, Document, DoubtSession, Enrollment, EvidenceEvent, GapHypothesis, IdempotencyKey, LearnerTopicState,
    MasteryHistory, User, WorkflowRun,
)
from app.security import hash_password


def anonymize_student(db: Session, settings: Settings, student_id: uuid.UUID, actor_id: uuid.UUID | None) -> dict:
    student = db.get(User, student_id)
    if student is None or student.role != "student":
        raise AppError(404, "NOT_FOUND", "Student not found")
    pseudonym = uuid.uuid4()
    counts: dict[str, int] = {}

    # 1. pseudonym that owns the retained, de-identified learning evidence (no way back to the person)
    db.add(User(id=pseudonym, email=f"anonymized-{pseudonym}@anonymized.invalid", display_name="Anonymized learner",
                password_hash=hash_password(uuid.uuid4().hex + uuid.uuid4().hex), role="student", active=False, language="en"))
    db.flush()

    # 2. the ONLY permitted rewrite of ledger rows: student_id -> pseudonym (trigger-enforced, this transaction only)
    db.execute(text("SELECT set_config('eduos.anonymizing', 'on', true)"))
    counts["evidence_retained_deidentified"] = db.execute(
        update(EvidenceEvent).where(EvidenceEvent.student_id == student_id).values(student_id=pseudonym)).rowcount
    db.execute(text("SELECT set_config('eduos.anonymizing', 'off', true)"))
    for model in (LearnerTopicState, MasteryHistory):
        db.execute(update(model).where(model.student_id == student_id).values(student_id=pseudonym))
    # hypothesis text may paraphrase what the student said: keep status/topic, drop the text
    counts["hypotheses_retained"] = db.execute(update(GapHypothesis).where(GapHypothesis.student_id == student_id).values(
        student_id=pseudonym, description="[anonymized]", source_run_id=None)).rowcount

    # 3. delete everything personal or free-text
    for d in db.scalars(select(Document).where(Document.owner_id == student_id)).all():
        KnowledgeService(db, settings).delete_document(d, actor_id=actor_id)       # chunks, embeddings, files, jobs
        counts["documents_deleted"] = counts.get("documents_deleted", 0) + 1
    counts["workflow_runs_deleted"] = db.execute(delete(WorkflowRun).where(WorkflowRun.student_id == student_id)).rowcount
    counts["sessions_deleted"] = db.execute(delete(DoubtSession).where(DoubtSession.student_id == student_id)).rowcount
    db.execute(delete(Enrollment).where(Enrollment.student_id == student_id))
    db.execute(delete(IdempotencyKey).where(IdempotencyKey.scope.like(f"%{student_id}%")))
    db.execute(update(AuditEvent).where(AuditEvent.actor_id == student_id).values(actor_id=None))

    # 4. remove the account itself and leave a metadata-only audit record
    db.delete(student)
    db.add(AuditEvent(actor_id=actor_id, action="student_anonymized", entity="student",
                      entity_id=hashlib.sha256(str(student_id).encode()).hexdigest()[:16], meta=counts))
    db.commit()
    return counts
