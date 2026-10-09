"""Builds a disposable evaluation database: real Alembic migration, demo users/course, and the evaluation corpus."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.knowledge.service import KnowledgeService
from app.main import ALEMBIC_INI
from app.models import Chunk, Course, Document, User
from app.seed import SEED_PDF, seed

SUPPLEMENT_PDF = Path(__file__).resolve().parents[3] / "eval" / "data" / "corpus" / "computer_networks_supplement.pdf"
CORPUS = [(SEED_PDF, "Computer Networks: demo notes"), (SUPPLEMENT_PDF, "Computer Networks supplement (synthetic)")]


# Topics covering the SUPPLEMENT corpus. Added in the evaluation environment only (the demo seed is unchanged), so that
# the fake understand step is not the bottleneck for supplement questions.
EVAL_TOPICS = [
    ("application-layer", "Application layer: HTTP and DNS", "http,dns,cookie,cookies,status code,resolver,record,application layer"),
    ("udp-ports", "UDP and port numbers", "udp,port,ports,datagram,datagrams"),
    ("link-layer", "Link layer: Ethernet, switches and ARP", "ethernet,switch,mac,arp,frame,frames,mtu"),
    ("dhcp-nat", "DHCP and NAT", "dhcp,nat,private address,translation"),
    ("tls", "Transport Layer Security", "tls,https,certificate,encryption"),
    ("delay", "Delay, throughput and bandwidth-delay product", "delay,bandwidth,throughput,round-trip time,queuing,bandwidth-delay product"),
]


def assert_disposable(url: str) -> None:
    dbname = url.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in dbname and "eval" not in dbname:
        raise SystemExit(f"Refusing to wipe database {dbname!r}: its name must contain 'test' or 'eval'.")


def reset_database(url: str) -> None:
    assert_disposable(url)
    engine = make_engine(url)
    with engine.begin() as conn:
        for schema in ("core", "orch", "know"):
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    engine.dispose()
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@dataclass
class EvalEnv:
    settings: Settings
    course_id: uuid.UUID
    student_id: uuid.UUID
    chunk_texts: dict[str, str]     # chunk_id -> text, for gold-phrase resolution


def build_env(settings: Settings, *, index_hook=None) -> EvalEnv:
    """Reset DB, seed, ingest the corpus. `index_hook(db, settings)` runs after ingestion (e.g. embedding)."""
    reset_database(settings.database_url)
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as db:
        seed(db, settings, ingest=False)
        admin = db.scalar(select(User).where(User.email == "admin@demo.local"))
        student = db.scalar(select(User).where(User.email == "student1@demo.local"))
        course = db.scalar(select(Course).where(Course.code == "CN101"))
        from app.models import Topic
        base = db.query(Topic).filter(Topic.course_id == course.id).count()
        for i, (slug, name, kw) in enumerate(EVAL_TOPICS):
            db.add(Topic(course_id=course.id, slug=slug, name=name, keywords=kw, sort=base + i))
        db.commit()
        svc = KnowledgeService(db, settings)
        for pdf, title in CORPUS:
            doc = svc.ingest_pdf(owner_id=admin.id, course_id=course.id, visibility="course", title=title,
                                 filename=pdf.name, data=pdf.read_bytes())
            if doc.status != "READY":
                raise RuntimeError(f"corpus ingestion failed for {pdf.name}: {doc.error_code}")
        if index_hook:
            index_hook(db, settings)
        chunks = {str(c.id): c.text for c in db.scalars(select(Chunk))}
        env = EvalEnv(settings, course.id, student.id, chunks)
    engine.dispose()
    return env
