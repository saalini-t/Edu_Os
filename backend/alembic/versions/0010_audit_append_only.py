"""core.audit_events becomes append-only (insert only); login-throttle index

The single permitted rewrite is the privacy erasure: during an anonymization transaction (SET LOCAL eduos.anonymizing = on)
actor_id may be set to NULL; every other column must stay identical. TRUNCATE is not row-level and is not blocked here
(test cleanup and operators with table ownership can still use it; normal application roles must not own the table).

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

FUNC = """
CREATE OR REPLACE FUNCTION core.audit_events_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND current_setting('eduos.anonymizing', true) = 'on'
       AND NEW.actor_id IS NULL
       AND NEW.id = OLD.id AND NEW.action = OLD.action AND NEW.entity = OLD.entity
       AND NEW.entity_id IS NOT DISTINCT FROM OLD.entity_id AND NEW.meta = OLD.meta AND NEW."at" = OLD."at" THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'core.audit_events is append-only (% is not allowed)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(sa.text(FUNC))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON core.audit_events"))
    op.execute(sa.text("CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON core.audit_events "
                       "FOR EACH ROW EXECUTE FUNCTION core.audit_events_immutable()"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_events_action_at ON core.audit_events (action, \"at\")"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS core.ix_audit_events_action_at"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON core.audit_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.audit_events_immutable()"))
