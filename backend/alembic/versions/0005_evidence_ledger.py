"""evidence ledger

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 08:01:13.645114
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('evidence_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('course_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=True),
    sa.Column('evidence_type', sa.String(length=40), nullable=False),
    sa.Column('weight', sa.Float(), server_default='0', nullable=False),
    sa.Column('source_run_id', sa.UUID(), nullable=False),
    sa.Column('source_ref', sa.String(length=128), nullable=False),
    sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("evidence_type <> 'check_requested' OR weight = 0", name='ck_check_requested_zero_weight'),
    sa.CheckConstraint("evidence_type IN ('self_report_understood', 'self_report_confused', 'check_requested')", name='ck_evidence_type_known'),
    sa.CheckConstraint("evidence_type NOT LIKE 'self_report%%' OR weight = 0", name='ck_self_report_zero_weight'),
    sa.CheckConstraint('weight >= 0 AND weight <= 1', name='ck_evidence_weight_range'),
    sa.ForeignKeyConstraint(['course_id'], ['core.courses.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('evidence_type', 'source_ref', name='uq_evidence_type_source_ref'),
    schema='core'
    )
    op.create_index(op.f('ix_core_evidence_events_student_id'), 'evidence_events', ['student_id'], unique=False, schema='core')
    _append_only()


def _append_only() -> None:
    op.execute(sa.text("""
CREATE OR REPLACE FUNCTION core.evidence_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'core.evidence_events is append-only (% is not allowed)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""))
    op.execute(sa.text("""
CREATE TRIGGER trg_evidence_events_immutable BEFORE UPDATE OR DELETE ON core.evidence_events
FOR EACH ROW EXECUTE FUNCTION core.evidence_events_immutable();
"""))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_evidence_events_immutable ON core.evidence_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.evidence_events_immutable()"))
    op.drop_index(op.f('ix_core_evidence_events_student_id'), table_name='evidence_events', schema='core')
    op.drop_table('evidence_events', schema='core')
