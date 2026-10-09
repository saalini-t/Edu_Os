"""Migration tests on a throw-away database: upgrade from the oldest revision WITH data in place, then down/up cycles."""
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.main import ALEMBIC_INI
from tests.conftest import TEST_DB

HEAD = "0009"


@pytest.fixture
def scratch_url():
    base, _, _ = TEST_DB.rpartition("/")
    name = f"eduos_migtest_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:      # no CREATE DATABASE privilege
        admin.dispose()
        pytest.skip(f"cannot create a scratch database: {type(e).__name__}")
    yield f"{base}/{name}"
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def cfg(url):
    c = Config(str(ALEMBIC_INI))
    c.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    c.set_main_option("sqlalchemy.url", url)
    return c


def test_upgrade_from_0001_preserves_data_and_applies_defaults(scratch_url):
    command.upgrade(cfg(scratch_url), "0001")
    eng = create_engine(scratch_url)
    uid, cid, did, chid = (uuid.uuid4() for _ in range(4))
    with eng.begin() as c:      # data written by the Phase 1 schema
        c.execute(text("insert into core.users (id, email, password_hash, role, display_name, active) "
                       "values (:i, 'old@x.local', 'h', 'student', 'Old', true)"), {"i": uid})
        c.execute(text("insert into know.documents (id, owner_id, course_id, visibility, title, filename, sha256, size_bytes, "
                       "status, storage_path, ingestion_version) values (:d, :u, :c, 'course', 't', 'f.pdf', 'abc', 1, 'READY', 'x.pdf', 1)"),
                  {"d": did, "u": uid, "c": cid})
        c.execute(text("insert into know.chunks (id, document_id, course_id, owner_id, visibility, page, chunk_index, text, content_hash) "
                       "values (:k, :d, :c, :u, 'course', 1, 0, 'Old chunk about routers.', 'h')"),
                  {"k": chid, "d": did, "c": cid, "u": uid})
    command.upgrade(cfg(scratch_url), "head")
    with eng.begin() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == HEAD
        row = c.execute(text("select d.status, d.ingestion_version, d.extraction_report, c.ingestion_version, c.text "
                             "from know.documents d join know.chunks c on c.document_id = d.id")).one()
        assert tuple(row) == ("READY", 1, None, 1, "Old chunk about routers.")     # data intact, new columns defaulted
        # search still works on the migrated row (generated tsvector column survived)
        assert c.execute(text("select count(*) from know.chunks where tsv @@ to_tsquery('english', 'routers')")).scalar() == 1
        for table in ("know.ingestion_jobs", "know.document_events", "know.embedding_models", "core.evidence_events",
                      "core.practice_items", "core.attempts", "core.gap_hypotheses", "core.learner_topic_state",
                      "core.mastery_history", "core.escalations", "core.teacher_profiles"):
            assert c.execute(text("select to_regclass(:t)"), {"t": table}).scalar() is not None
        assert c.execute(text("select count(*) from pg_trigger where tgrelid = 'core.evidence_events'::regclass "
                              "and not tgisinternal")).scalar() == 1
    eng.dispose()


def test_down_and_up_cycles_are_clean(scratch_url):
    command.upgrade(cfg(scratch_url), "head")
    command.downgrade(cfg(scratch_url), "0001")
    eng = create_engine(scratch_url)
    with eng.begin() as c:
        assert c.execute(text("select to_regclass('core.evidence_events')")).scalar() is None
        assert c.execute(text("select to_regclass('know.ingestion_jobs')")).scalar() is None
        assert c.execute(text("select to_regclass('core.users')")).scalar() is not None
    command.upgrade(cfg(scratch_url), "head")
    with eng.begin() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar() == HEAD
    eng.dispose()


def test_application_works_without_pgvector_tables(scratch_url):
    """A server without pgvector: 0002 skips chunk_embeddings, every later migration still applies."""
    command.upgrade(cfg(scratch_url), "head")
    eng = create_engine(scratch_url)
    with eng.begin() as c:
        c.execute(text("drop table if exists know.chunk_embeddings"))     # simulate absence
    from app.knowledge import vectors
    from sqlalchemy.orm import Session
    with Session(eng) as db:
        assert vectors.vector_available(db) is False
        ok = vectors.ensure_vector_support(db)                            # re-creates the table when pgvector exists
        assert vectors.vector_available(db) is ok
    eng.dispose()
