"""teaching

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09 09:08:51.692699
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('availability_slots',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('teacher_id', sa.UUID(), nullable=False),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('booked_by_escalation_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('end_at > start_at', name='ck_slot_order'),
    sa.ForeignKeyConstraint(['teacher_id'], ['core.users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    schema='core'
    )
    op.create_index(op.f('ix_core_availability_slots_teacher_id'), 'availability_slots', ['teacher_id'], unique=False, schema='core')
    op.create_table('teacher_courses',
    sa.Column('teacher_id', sa.UUID(), nullable=False),
    sa.Column('course_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['core.courses.id'], ),
    sa.ForeignKeyConstraint(['teacher_id'], ['core.users.id'], ),
    sa.PrimaryKeyConstraint('teacher_id', 'course_id'),
    schema='core'
    )
    op.create_table('teacher_profiles',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('bio', sa.Text(), nullable=False),
    sa.Column('languages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['core.users.id'], ),
    sa.PrimaryKeyConstraint('user_id'),
    schema='core'
    )
    op.create_table('teacher_topics',
    sa.Column('teacher_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=False),
    sa.Column('proficiency', sa.Float(), nullable=False),
    sa.CheckConstraint('proficiency >= 0 AND proficiency <= 1', name='ck_teacher_topic_proficiency'),
    sa.ForeignKeyConstraint(['teacher_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('teacher_id', 'topic_id'),
    schema='core'
    )
    op.create_table('escalations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('course_id', sa.UUID(), nullable=False),
    sa.Column('topic_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('reason_rule_id', sa.String(length=64), nullable=False),
    sa.Column('brief', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('candidates', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('assigned_teacher_id', sa.UUID(), nullable=True),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resume_status', sa.String(length=8), nullable=False),
    sa.Column('resume_outcome', sa.String(length=12), nullable=True),
    sa.Column('resume_message', sa.Text(), nullable=True),
    sa.Column('student_helpful', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("resume_status IN ('none', 'pending', 'done')", name='ck_escalation_resume_status'),
    sa.CheckConstraint("status IN ('OPEN', 'ACCEPTED', 'RESOLVED', 'EXPIRED', 'CANCELLED')", name='ck_escalation_status'),
    sa.ForeignKeyConstraint(['assigned_teacher_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['course_id'], ['core.courses.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['core.doubt_sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['student_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['topic_id'], ['core.topics.id'], ),
    sa.PrimaryKeyConstraint('id'),
    schema='core'
    )
    op.create_index(op.f('ix_core_escalations_assigned_teacher_id'), 'escalations', ['assigned_teacher_id'], unique=False, schema='core')
    op.create_index(op.f('ix_core_escalations_session_id'), 'escalations', ['session_id'], unique=False, schema='core')
    op.create_index(op.f('ix_core_escalations_student_id'), 'escalations', ['student_id'], unique=False, schema='core')
    op.create_index('uq_escalation_one_active_per_run', 'escalations', ['run_id'], unique=True, schema='core', postgresql_where=sa.text("status IN ('OPEN', 'ACCEPTED')"))
    op.create_table('escalation_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('escalation_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.ForeignKeyConstraint(['escalation_id'], ['core.escalations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    schema='core'
    )
    op.create_index(op.f('ix_core_escalation_events_escalation_id'), 'escalation_events', ['escalation_id'], unique=False, schema='core')
    op.create_table('escalation_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('escalation_id', sa.UUID(), nullable=False),
    sa.Column('author_id', sa.UUID(), nullable=False),
    sa.Column('author_role', sa.String(length=8), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['core.users.id'], ),
    sa.ForeignKeyConstraint(['escalation_id'], ['core.escalations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    schema='core'
    )
    op.create_index(op.f('ix_core_escalation_messages_escalation_id'), 'escalation_messages', ['escalation_id'], unique=False, schema='core')
    op.create_table('teacher_feedback',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('escalation_id', sa.UUID(), nullable=False),
    sa.Column('teacher_id', sa.UUID(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('topic_assessments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('hypothesis_decisions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['escalation_id'], ['core.escalations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['teacher_id'], ['core.users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('escalation_id'),
    schema='core'
    )
    op.add_column('users', sa.Column('language', sa.String(length=8), server_default='en', nullable=False), schema='core')



def downgrade() -> None:

    op.drop_column('users', 'language', schema='core')
    op.drop_table('teacher_feedback', schema='core')
    op.drop_index(op.f('ix_core_escalation_messages_escalation_id'), table_name='escalation_messages', schema='core')
    op.drop_table('escalation_messages', schema='core')
    op.drop_index(op.f('ix_core_escalation_events_escalation_id'), table_name='escalation_events', schema='core')
    op.drop_table('escalation_events', schema='core')
    op.drop_index('uq_escalation_one_active_per_run', table_name='escalations', schema='core', postgresql_where=sa.text("status IN ('OPEN', 'ACCEPTED')"))
    op.drop_index(op.f('ix_core_escalations_student_id'), table_name='escalations', schema='core')
    op.drop_index(op.f('ix_core_escalations_session_id'), table_name='escalations', schema='core')
    op.drop_index(op.f('ix_core_escalations_assigned_teacher_id'), table_name='escalations', schema='core')
    op.drop_table('escalations', schema='core')
    op.drop_table('teacher_topics', schema='core')
    op.drop_table('teacher_profiles', schema='core')
    op.drop_table('teacher_courses', schema='core')
    op.drop_index(op.f('ix_core_availability_slots_teacher_id'), table_name='availability_slots', schema='core')
    op.drop_table('availability_slots', schema='core')

