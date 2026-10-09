"""allow the privileged anonymization to re-point evidence_events.student_id (nothing else)

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NEW = """
CREATE OR REPLACE FUNCTION core.evidence_events_immutable() RETURNS trigger AS $$
BEGIN
    -- The ledger is append-only. The single exception: during a privileged anonymization transaction
    -- (SET LOCAL eduos.anonymizing = on) the student_id column may be re-pointed to a pseudonym; every other column must be identical.
    IF TG_OP = 'UPDATE'
       AND current_setting('eduos.anonymizing', true) = 'on'
       AND NEW.id = OLD.id AND NEW.course_id = OLD.course_id AND NEW.topic_id IS NOT DISTINCT FROM OLD.topic_id
       AND NEW.evidence_type = OLD.evidence_type AND NEW.weight = OLD.weight AND NEW.source_run_id = OLD.source_run_id
       AND NEW.source_ref = OLD.source_ref AND NEW.provenance = OLD.provenance AND NEW.created_at = OLD.created_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'core.evidence_events is append-only (% is not allowed)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""
OLD = """
CREATE OR REPLACE FUNCTION core.evidence_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'core.evidence_events is append-only (% is not allowed)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(sa.text(NEW))


def downgrade() -> None:
    op.execute(sa.text(OLD))
