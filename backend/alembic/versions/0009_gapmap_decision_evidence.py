"""curated topic prerequisites (Learning Gap Map) and evidence references / provider context on every policy decision

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("prerequisites", postgresql.JSONB(), server_default="[]", nullable=False), schema="core")
    op.add_column("decision_records", sa.Column("evidence_refs", postgresql.JSONB(), server_default="[]", nullable=False), schema="orch")
    op.add_column("decision_records", sa.Column("context", postgresql.JSONB(), server_default="{}", nullable=False), schema="orch")


def downgrade() -> None:
    op.drop_column("decision_records", "context", schema="orch")
    op.drop_column("decision_records", "evidence_refs", schema="orch")
    op.drop_column("topics", "prerequisites", schema="core")
