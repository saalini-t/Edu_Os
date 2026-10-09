"""document versions events

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 07:44:11.000495
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('document_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    schema='know'
    )
    op.create_index(op.f('ix_know_document_events_document_id'), 'document_events', ['document_id'], unique=False, schema='know')
    op.add_column('chunks', sa.Column('ingestion_version', sa.Integer(), server_default='1', nullable=False), schema='know')
    op.add_column('documents', sa.Column('extraction_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True), schema='know')
    op.add_column('documents', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False), schema='know')



def downgrade() -> None:

    op.drop_column('documents', 'updated_at', schema='know')
    op.drop_column('documents', 'extraction_report', schema='know')
    op.drop_column('chunks', 'ingestion_version', schema='know')
    op.drop_index(op.f('ix_know_document_events_document_id'), table_name='document_events', schema='know')
    op.drop_table('document_events', schema='know')

