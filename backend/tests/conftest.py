"""Test fixtures. Tests run against a DISPOSABLE PostgreSQL database (TEST_DATABASE_URL); the schemas are dropped
and rebuilt through the real Alembic migration at session start, so the migration itself is exercised."""
from __future__ import annotations

import itertools
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.main import ALEMBIC_INI, create_app
from app.seed import seed

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
if not TEST_DB:
    pytest.exit("Set TEST_DATABASE_URL to a DISPOSABLE database (its core/orch/know schemas are dropped).", 2)
if "test" not in TEST_DB.rsplit("/", 1)[-1].lower():
    pytest.exit("Refusing to run: the TEST_DATABASE_URL database name must contain 'test'.", 2)
PASSWORD = "eduos-demo-2026"
_counter = itertools.count()


def _settings(storage: Path, **over) -> Settings:
    base = dict(app_env="test", database_url=TEST_DB, jwt_secret="test-only-secret-0123456789-0123456789-ab",
                storage_dir=str(storage), seed_demo_password=PASSWORD, ingestion_mode="sync", parser_isolation="inline")
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture(scope="session")
def storage_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("storage")


@pytest.fixture(scope="session")
def settings(storage_dir) -> Settings:
    return _settings(storage_dir)


@pytest.fixture(scope="session", autouse=True)
def database(settings):
    engine = make_engine(TEST_DB)
    with engine.begin() as conn:
        for schema in ("core", "orch", "know"):
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB)
    command.upgrade(cfg, "head")
    factory = make_session_factory(engine)
    with factory() as db:
        seed(db, settings)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(database):
    """Per-test isolation: wipe workflow/session/learner/teaching data, non-seed documents, and restore the standard slots."""
    yield
    with database.begin() as conn:
        conn.execute(text("TRUNCATE core.doubt_sessions, core.gap_hypotheses, core.learner_topic_state, core.mastery_history, "
                          "core.evidence_events, orch.workflow_runs, core.idempotency_keys, core.audit_events CASCADE"))
        # (TRUNCATE is test cleanup only: the append-only trigger guards ordinary UPDATE/DELETE of ledger rows)
        conn.execute(text("DELETE FROM know.embedding_models"))   # cascades to chunk_embeddings
        conn.execute(text("DELETE FROM know.documents WHERE visibility <> 'course' OR title NOT LIKE 'Computer Networks%'"))
        conn.execute(text("UPDATE core.users SET active = true, language = 'en'"))
        conn.execute(text("UPDATE core.teacher_profiles SET active = true"))
    from app.seed import reset_slots
    with make_session_factory(database)() as session:
        reset_slots(session)


@pytest.fixture
def db(database):
    with make_session_factory(database)() as session:
        yield session


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    r = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def student(client):
    return login(client, "student1@demo.local")


@pytest.fixture
def student2(client):
    return login(client, "student2@demo.local")


@pytest.fixture
def student3(client):
    return login(client, "student3@demo.local")


@pytest.fixture
def admin(client):
    return login(client, "admin@demo.local")


@pytest.fixture
def teacher(client):
    return login(client, "teacher1@demo.local")


@pytest.fixture
def cn_course_id(client, student) -> str:
    return client.get("/v1/documents", headers=student).json()["items"][0]["course_id"]


def ask(client, headers, course_id, text_, key=None):
    key = key or f"key-{next(_counter)}-{uuid.uuid4().hex[:6]}"
    return client.post("/v1/doubts", headers={**headers, "Idempotency-Key": key},
                       json={"course_id": course_id, "text": text_})


SLOW_START_Q = "Why does TCP slow start double the congestion window every RTT, but then stop doubling?"


def make_pdf(paragraphs: list[str]) -> bytes:
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    buf = io.BytesIO()
    st = getSampleStyleSheet()
    SimpleDocTemplate(buf, pagesize=A4).build([Paragraph(p, st["BodyText"]) for p in paragraphs] or [])
    return buf.getvalue()
