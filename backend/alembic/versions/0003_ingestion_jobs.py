"""ingestion jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 07:30:22.780613
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('ingestion_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('stage', sa.String(length=24), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('worker_id', sa.String(length=64), nullable=True),
    sa.Column('error_code', sa.String(length=48), nullable=True),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('enqueue_error', sa.String(length=120), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['know.documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    schema='know'
    )
    op.create_index('ix_ingestion_jobs_claim', 'ingestion_jobs', ['status', 'available_at'], unique=False, schema='know')
    op.create_index(op.f('ix_know_ingestion_jobs_document_id'), 'ingestion_jobs', ['document_id'], unique=False, schema='know')
    op.create_index('uq_ingestion_jobs_one_active_per_document', 'ingestion_jobs', ['document_id'], unique=True, schema='know', postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"))



def downgrade() -> None:

    op.drop_index('uq_ingestion_jobs_one_active_per_document', table_name='ingestion_jobs', schema='know', postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"))
    op.drop_index(op.f('ix_know_ingestion_jobs_document_id'), table_name='ingestion_jobs', schema='know')
    op.drop_index('ix_ingestion_jobs_claim', table_name='ingestion_jobs', schema='know')
    op.drop_table('ingestion_jobs', schema='know')

