"""embedding model registry

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 07:20:57.941572
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('embedding_models',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('dims', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'dims'),
    schema='know'
    )
    op.create_index('uq_embedding_models_one_active', 'embedding_models', ['status'], unique=True, schema='know', postgresql_where=sa.text("status = 'active'"))
    # pgvector is OPTIONAL infrastructure: if the server does not offer it, the migration still succeeds and the
    # application serves full-text-only retrieval (python -m app.knowledge.reindex creates the table later).
    op.execute(sa.text("""
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS know.chunk_embeddings (
        chunk_id uuid NOT NULL REFERENCES know.chunks(id) ON DELETE CASCADE,
        model_id uuid NOT NULL REFERENCES know.embedding_models(id) ON DELETE CASCADE,
        embedding vector NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (chunk_id, model_id)
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector not available (%): skipping chunk_embeddings', SQLERRM;
END $$;
"""))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS know.chunk_embeddings"))
    op.drop_index('uq_embedding_models_one_active', table_name='embedding_models', schema='know', postgresql_where=sa.text("status = 'active'"))
    op.drop_table('embedding_models', schema='know')
