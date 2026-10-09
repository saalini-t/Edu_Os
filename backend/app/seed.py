"""Idempotent demo seeding: synthetic accounts, the Computer Networks course and its seeded PDF.
Refuses to run in production. Usage: python -m app.seed"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.knowledge.service import KnowledgeService
from app.logging_setup import setup_logging
from datetime import datetime, timedelta, timezone

from app.models import (
    AvailabilitySlot, Course, Document, Enrollment, TeacherCourse, TeacherProfile, TeacherTopic, Topic, User,
)
from app.security import hash_password

log = logging.getLogger("eduos.seed")
SEED_PDF = Path(__file__).resolve().parent.parent / "seed" / "computer_networks.pdf"

USERS = [  # (email, role, display name)
    ("student1@demo.local", "student", "Demo Student 1"),
    ("student2@demo.local", "student", "Demo Student 2"),
    ("student3@demo.local", "student", "Demo Student 3 (OS only)"),
    ("teacher1@demo.local", "teacher", "Demo Teacher 1 (TCP)"),
    ("teacher2@demo.local", "teacher", "Demo Teacher 2 (IP and routing)"),
    ("teacher3@demo.local", "teacher", "Demo Teacher 3 (OS course only)"),
    ("admin@demo.local", "admin", "Demo Admin"),
]
COURSES = [("CN101", "Computer Networks"), ("OS101", "Operating Systems (empty demo course)")]
CN_TOPICS = [  # (slug, name, keywords)
    ("layering", "Protocol layering", "layer,layering,osi,encapsulation,header,protocol stack,decapsulation"),
    ("tcp-reliability", "TCP connections and reliability",
     "handshake,three-way,syn,sequence number,retransmission,timeout,reliable,acknowledgment,fin"),
    ("tcp-flow-control", "TCP flow control", "flow control,receive window,rwnd,receiver,buffer,advertised window"),
    ("tcp-congestion", "TCP congestion control",
     "congestion,cwnd,congestion window,slow start,ssthresh,congestion avoidance,packet loss,fast retransmit,"
     "fast recovery,aimd,reno,loss"),
    ("ip-addressing", "IP addressing and subnetting", "ip address,subnet,subnetting,cidr,mask,prefix,netmask,broadcast"),
    ("routing", "Routing", "routing,router,distance vector,link state,ospf,bgp,forwarding table,dijkstra,bellman-ford"),
]
PREREQS = {   # curated prerequisite topics (slugs); the Learning Gap Map uses them to suggest a possible root cause, never to infer one
    "tcp-reliability": ["layering"], "tcp-flow-control": ["tcp-reliability"],
    "tcp-congestion": ["tcp-reliability", "tcp-flow-control"], "ip-addressing": ["layering"], "routing": ["ip-addressing"],
}
ENROLLMENTS = {"student1@demo.local": ["CN101"], "student2@demo.local": ["CN101"],
               "student3@demo.local": ["OS101"]}


# teacher -> (languages, course code, {topic slug: proficiency})
TEACHERS = {
    "teacher1@demo.local": (["en", "hi"], "CN101", {"tcp-reliability": 0.9, "tcp-flow-control": 0.85, "tcp-congestion": 0.95, "layering": 0.6}),
    "teacher2@demo.local": (["en"], "CN101", {"ip-addressing": 0.9, "routing": 0.9, "layering": 0.8, "tcp-reliability": 0.4}),
    "teacher3@demo.local": (["en"], "OS101", {}),
}


def reset_slots(db: Session) -> None:
    """(Re)create the standard availability: two slots per teacher in the coming days. Existing slots are replaced."""
    now = datetime.now(timezone.utc)
    for slot in db.scalars(select(AvailabilitySlot)):
        db.delete(slot)
    db.flush()
    for email in TEACHERS:
        t = db.scalar(select(User).where(User.email == email))
        for start_h in (2, 26):
            db.add(AvailabilitySlot(teacher_id=t.id, start_at=now + timedelta(hours=start_h),
                                    end_at=now + timedelta(hours=start_h + 2)))
    db.commit()


def seed_teachers(db: Session, users: dict, courses: dict) -> None:
    for email, (langs, code, topics) in TEACHERS.items():
        t = users[email]
        if db.get(TeacherProfile, t.id) is None:
            db.add(TeacherProfile(user_id=t.id, bio=f"Demo teacher for {code}.", languages=langs, active=True))
        if db.get(TeacherCourse, (t.id, courses[code].id)) is None:
            db.add(TeacherCourse(teacher_id=t.id, course_id=courses[code].id))
        for slug, prof in topics.items():
            topic = db.scalar(select(Topic).where(Topic.course_id == courses[code].id, Topic.slug == slug))
            if topic is not None and db.get(TeacherTopic, (t.id, topic.id)) is None:
                db.add(TeacherTopic(teacher_id=t.id, topic_id=topic.id, proficiency=prof))
    db.commit()


def seed(db: Session, settings: Settings, *, ingest: bool = True) -> None:
    if settings.app_env == "production":
        raise RuntimeError("Refusing to seed demo accounts when APP_ENV=production")
    users = {}
    for email, role, name in USERS:
        u = db.scalar(select(User).where(User.email == email))
        if u is None:
            u = User(email=email, role=role, display_name=name, password_hash=hash_password(settings.seed_demo_password))
            db.add(u)
        users[email] = u
    courses = {}
    for code, name in COURSES:
        c = db.scalar(select(Course).where(Course.code == code))
        if c is None:
            c = Course(code=code, name=name)
            db.add(c)
        courses[code] = c
    db.flush()
    for i, (slug, name, kw) in enumerate(CN_TOPICS):
        t = db.scalar(select(Topic).where(Topic.course_id == courses["CN101"].id, Topic.slug == slug))
        if t is None:
            db.add(Topic(course_id=courses["CN101"].id, slug=slug, name=name, keywords=kw, sort=i, prerequisites=PREREQS.get(slug, [])))
        elif not t.prerequisites and PREREQS.get(slug):
            t.prerequisites = PREREQS[slug]
    for email, codes in ENROLLMENTS.items():
        for code in codes:
            if db.get(Enrollment, (users[email].id, courses[code].id)) is None:
                db.add(Enrollment(student_id=users[email].id, course_id=courses[code].id))
    db.commit()
    seed_teachers(db, users, courses)
    if db.scalar(select(AvailabilitySlot.id).limit(1)) is None:
        reset_slots(db)
    if ingest:
        admin, cn = users["admin@demo.local"], courses["CN101"]
        svc = KnowledgeService(db, settings)
        doc = svc.ingest_pdf(owner_id=admin.id, course_id=cn.id, visibility="course",
                             title="Computer Networks: demo notes", filename=SEED_PDF.name, data=SEED_PDF.read_bytes())
        log.info("seed document", extra={"document_id": str(doc.id), "status": doc.status,
                                         "chunks": doc.chunk_count})
        if doc.status != "READY":
            raise RuntimeError(f"Seed document ingestion failed: {doc.error_code}")


def main() -> None:
    setup_logging()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as db:
        seed(db, settings)
    print("seed complete")


if __name__ == "__main__":
    main()
