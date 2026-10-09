"""practice attempts mastery

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 09:04:35.388110
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('gap_hypotheses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('key', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source_run_id', sa.UUID(), nullable=True),
    sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('proposed', 'confirmed', 'refuted', 'expired')", name='ck_hypothesis_status'),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('student_id', 'topic_id', 'key'),
    schema='core'
    )
    op.create_index(op.f('ix_core_gap_hypotheses_student_id'), 'gap_hypotheses', ['student_id'], unique=False, schema='core')
    op.create_table('learner_topic_state',
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=False),
    sa.Column('alpha', sa.Float(), nullable=False),
    sa.Column('beta', sa.Float(), nullable=False),
    sa.Column('evidence_count', sa.Integer(), nullable=False),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_evidence_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('student_id', 'topic_id'),
    schema='core'
    )
    op.create_table('mastery_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=False),
    sa.Column('evidence_id', sa.UUID(), nullable=False),
    sa.Column('alpha', sa.Float(), nullable=False),
    sa.Column('beta', sa.Float(), nullable=False),
    sa.Column('mean', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('evidence_count', sa.Integer(), nullable=False),
    sa.Column('distinct_sources', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('evidence_id', name='uq_mastery_history_evidence'),
    schema='core'
    )
    op.create_index(op.f('ix_core_mastery_history_student_id'), 'mastery_history', ['student_id'], unique=False, schema='core')
    op.create_table('practice_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=False),
    sa.Column('set_index', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('answer_key', sa.Text(), nullable=False),
    sa.Column('numeric_tolerance', sa.Float(), nullable=True),
    sa.Column('rubric', sa.Text(), nullable=True),
    sa.Column('difficulty', sa.String(length=8), nullable=False),
    sa.Column('source_chunk_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('distractor_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('targets_hypothesis_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.String(length=12), nullable=False),
    sa.Column('prompt_hash', sa.String(length=24), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('mcq', 'numeric', 'short_text')", name='ck_practice_kind'),
    sa.ForeignKeyConstraint(['session_id'], ['core.doubt_sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['targets_hypothesis_id'], ['core.gap_hypotheses.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('id'),
    schema='core'
    )
    op.create_index(op.f('ix_core_practice_items_session_id'), 'practice_items', ['session_id'], unique=False, schema='core')
    op.create_index(op.f('ix_core_practice_items_student_id'), 'practice_items', ['student_id'], unique=False, schema='core')
    op.create_index('ix_practice_student_prompt', 'practice_items', ['student_id', 'prompt_hash'], unique=False, schema='core')
    op.create_table('attempts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('item_id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('hints_used', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('correct', sa.Boolean(), nullable=True),
    sa.Column('partial_credit', sa.Float(), nullable=True),
    sa.Column('error_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('feedback', sa.Text(), nullable=True),
    sa.Column('evidence_text', sa.Text(), nullable=True),
    sa.Column('uncertainty', sa.Float(), nullable=True),
    sa.Column('grader', sa.String(length=16), nullable=True),
    sa.Column('grader_status', sa.String(length=12), nullable=True),
    sa.Column('evidence_event_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('graded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['item_id'], ['core.practice_items.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('student_id', 'idempotency_key', name='uq_attempt_idempotency'),
    schema='core'
    )
    op.create_index(op.f('ix_core_attempts_item_id'), 'attempts', ['item_id'], unique=False, schema='core')
    op.create_index(op.f('ix_core_attempts_student_id'), 'attempts', ['student_id'], unique=False, schema='core')
    op.create_index('uq_attempt_one_scored_per_item', 'attempts', ['item_id'], unique=True, schema='core', postgresql_where=sa.text("status IN ('GRADED', 'UNCERTAIN')"))


    # ---- widen the evidence ledger: graded attempts and teacher assessments become valid evidence types.
    # Self-reports stay pinned to weight 0 by the unchanged CHECK constraints.
    for name in ("ck_evidence_type_known", "ck_evidence_weight_range"):
        op.drop_constraint(name, "evidence_events", schema="core", type_="check")
    op.create_check_constraint("ck_evidence_type_known", "evidence_events",
                               "evidence_type IN ('self_report_understood', 'self_report_confused', 'check_requested', 'attempt_correct', 'attempt_incorrect', 'teacher_assessment_solid', 'teacher_assessment_struggling', 'teacher_assessment_emerging')", schema="core")
    op.create_check_constraint("ck_evidence_weight_range", "evidence_events", "weight >= 0 AND weight <= 3", schema="core")
    op.create_check_constraint("ck_attempt_weight", "evidence_events",
                               "evidence_type NOT LIKE 'attempt_%' OR (weight > 0 AND weight <= 1)", schema="core")
    op.create_check_constraint("ck_teacher_weight_positive", "evidence_events",
                               "evidence_type NOT IN ('teacher_assessment_solid', 'teacher_assessment_struggling') OR weight > 0",
                               schema="core")
    op.create_check_constraint("ck_teacher_emerging_zero", "evidence_events",
                               "evidence_type <> 'teacher_assessment_emerging' OR weight = 0", schema="core")


def downgrade() -> None:
    # NOTE: if the ledger already holds attempt/teacher rows, re-creating the old constraint FAILS on purpose:
    # ledger history is never deleted by a migration.
    for name in ("ck_teacher_emerging_zero", "ck_teacher_weight_positive", "ck_attempt_weight", "ck_evidence_weight_range",
                 "ck_evidence_type_known"):
        op.drop_constraint(name, "evidence_events", schema="core", type_="check")
    op.create_check_constraint("ck_evidence_type_known", "evidence_events", "evidence_type IN ('self_report_understood', 'self_report_confused', 'check_requested')", schema="core")
    op.create_check_constraint("ck_evidence_weight_range", "evidence_events", "weight >= 0 AND weight <= 1", schema="core")

    op.drop_index('uq_attempt_one_scored_per_item', table_name='attempts', schema='core', postgresql_where=sa.text("status IN ('GRADED', 'UNCERTAIN')"))
    op.drop_index(op.f('ix_core_attempts_student_id'), table_name='attempts', schema='core')
    op.drop_index(op.f('ix_core_attempts_item_id'), table_name='attempts', schema='core')
    op.drop_table('attempts', schema='core')
    op.drop_index('ix_practice_student_prompt', table_name='practice_items', schema='core')
    op.drop_index(op.f('ix_core_practice_items_student_id'), table_name='practice_items', schema='core')
    op.drop_index(op.f('ix_core_practice_items_session_id'), table_name='practice_items', schema='core')
    op.drop_table('practice_items', schema='core')
    op.drop_index(op.f('ix_core_mastery_history_student_id'), table_name='mastery_history', schema='core')
    op.drop_table('mastery_history', schema='core')
    op.drop_table('learner_topic_state', schema='core')
    op.drop_index(op.f('ix_core_gap_hypotheses_student_id'), table_name='gap_hypotheses', schema='core')
    op.drop_table('gap_hypotheses', schema='core')
