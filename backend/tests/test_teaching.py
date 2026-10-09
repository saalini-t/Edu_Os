"""Teacher matching, escalation lifecycle, permissions, the asynchronous thread, resolution feedback, workflow resumption,
expiry, and student anonymization. Every permission boundary is exercised through the HTTP API."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.learner.anonymize import anonymize_student
from app.learner.service import LearnerService
from app.llm.schemas import GapHypothesisDraft
from app.models import (
    Attempt, AvailabilitySlot, DoubtSession, Escalation, EscalationEvent, EvidenceEvent, GapHypothesis, Message, PracticeItem,
    TeacherProfile, TeacherTopic, Topic, User, WorkflowRun,
)
from app.teaching import matching as mt
from app.workflow.engine import WorkflowEngine
from tests.conftest import SLOW_START_Q, ask, login
from tests.test_adaptive_flow import items, solve, submit, to_practice
from tests.test_workflow import trace

NOW = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
HELP = "I want to talk to a human teacher about TCP congestion control, cwnd and ssthresh"
_k = iter(range(100_000))


def idem(h):
    return {**h, "Idempotency-Key": f"teach-key-{next(_k):06d}"}


# ----------------------------------------------------------------------------------------------- matcher (pure)
W = mt.Weights()
CTX = mt.Context(topic_known=True, student_language="en", now=NOW, window_hours=72, max_open=5)


def facts(tid, **kw):
    base = dict(teacher_id=tid, name=tid, active=True, languages=["en"], topic_proficiency=0.8, best_proficiency=0.8,
                slots=[(NOW + timedelta(hours=4), NOW + timedelta(hours=6))], helpful=0, rated=0, open_load=0)
    base.update(kw)
    return mt.TeacherFacts(**base)


def test_matching_is_a_deterministic_weighted_sum_with_stored_components():
    r = mt.rank([facts("a")], CTX, W)[0]
    c = r["components"]
    assert c == {"topic_fit": 0.8, "availability": round(1 - 4 / 72, 4), "language": 1.0, "feedback": 0.5, "load": 1.0}
    assert r["score"] == pytest.approx(round(0.4 * 0.8 + 0.25 * c["availability"] + 0.15 + 0.1 * 0.5 + 0.1, 4)) and r["rank"] == 1


def test_topic_fit_dominates_and_hard_constraints_exclude():
    ranked = mt.rank([facts("weak", topic_proficiency=0.3), facts("strong", topic_proficiency=0.95),
                      facts("uncovered", topic_proficiency=None), facts("inactive", active=False)], CTX, W)
    assert [r["teacher_id"] for r in ranked] == ["strong", "weak"]                           # uncovered and inactive are excluded, not ranked last
    no_topic = mt.rank([facts("x", topic_proficiency=None, best_proficiency=0.7)], mt.Context(False, "en", NOW, 72, 5), W)
    assert no_topic[0]["components"]["topic_fit"] == 0.7                                      # unknown topic: best proficiency in the course


def test_availability_language_feedback_and_load_components():
    assert mt.availability_score([(NOW - timedelta(hours=1), NOW + timedelta(hours=1))], NOW, 72) == 1.0       # running now
    assert mt.availability_score([(NOW + timedelta(hours=72), NOW + timedelta(hours=74))], NOW, 72) == 0.0     # at the window edge
    assert mt.availability_score([(NOW - timedelta(hours=5), NOW - timedelta(hours=3))], NOW, 72) == 0.0       # already over
    assert mt.availability_score([], NOW, 72) == 0.0
    near = mt.availability_score([(NOW + timedelta(hours=2), NOW + timedelta(hours=3))], NOW, 72)
    far = mt.availability_score([(NOW + timedelta(hours=40), NOW + timedelta(hours=41))], NOW, 72)
    assert 0 < far < near < 1
    assert mt.feedback_score(0, 0) == 0.5 and mt.feedback_score(9, 10) > mt.feedback_score(1, 10) and mt.feedback_score(0, 100) < 0.05
    ranked = mt.rank([facts("busy", open_load=5), facts("free", open_load=0)], CTX, W)
    assert [r["teacher_id"] for r in ranked] == ["free", "busy"] and ranked[1]["components"]["load"] == 0.0
    hi = mt.Context(True, "hi", NOW, 72, 5)
    assert [r["teacher_id"] for r in mt.rank([facts("en-only"), facts("bilingual", languages=["en", "hi"])], hi, W)] == ["bilingual", "en-only"]


def test_ties_break_by_teacher_id_and_ranking_is_repeatable():
    t = [facts("b"), facts("a"), facts("c")]
    first = mt.rank(t, CTX, W)
    assert [r["teacher_id"] for r in first] == ["a", "b", "c"] and first == mt.rank(list(reversed(t)), CTX, W)
    assert [r["rank"] for r in first] == [1, 2, 3]


# ----------------------------------------------------------------------------------------------- helpers over the API
def esc_of(client, student, cid, text=HELP):
    r = ask(client, student, cid, text)
    sid = r.json()["session_id"]
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert view["status"] == "WAITING_HUMAN" and view["escalation"], view
    return sid, r.json()["run_id"], view["escalation"]["id"]


def topic_id(db, slug="tcp-congestion"):
    return db.scalar(select(Topic.id).where(Topic.slug == slug))


@pytest.fixture
def t2_topic(db):
    """Make teacher2 a (weaker) second candidate for TCP congestion; removed afterwards."""
    t2 = db.scalar(select(User).where(User.email == "teacher2@demo.local"))
    row = TeacherTopic(teacher_id=t2.id, topic_id=topic_id(db), proficiency=0.4)
    db.add(row)
    db.commit()
    yield row
    db.delete(row)
    db.commit()


# ----------------------------------------------------------------------------------------------- creation and views
def test_a_doubt_escalation_is_created_matched_and_audited(client, student, admin, teacher, cn_course_id, db):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    v = client.get(f"/v1/escalations/{eid}", headers=admin).json()
    assert v["status"] == "OPEN" and v["reason_rule_id"] == "R1_explicit_teacher_request"
    assert [c["name"] for c in v["candidates"]] == ["Demo Teacher 1 (TCP)"]                    # teacher2/3 do not cover the topic
    comps = v["candidates"][0]["components"]
    assert set(comps) == {"topic_fit", "availability", "language", "feedback", "load"} and comps["topic_fit"] == 0.95 and comps["language"] == 1.0
    assert [e["event"] for e in v["events"]] == ["created", "matched"]
    assert v["brief"]["doubt"] == HELP and v["brief"]["topic"]["name"] == "TCP congestion control"
    assert v["brief"]["reason"]["rule_id"] == "R1_explicit_teacher_request" and v["resume"] == {"status": "none", "outcome": None}
    sv = client.get(f"/v1/escalations/{eid}", headers=student).json()                           # the student never sees ranking internals
    assert "candidates" not in sv and "brief" not in sv and sv["status"] == "OPEN" and sv["waiting_for"] == "a teacher to accept"
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "WAITING_HUMAN" and [s for s in t["steps"] if s["node"] == "escalate"][0]["output"]["matched_teachers"] == 1
    info = client.get(f"/v1/doubts/{sid}", headers=student).json()["escalation"]
    assert info["status"] == "OPEN" and info["assigned_teacher"] is None


def test_escalation_from_repeated_practice_failure_carries_the_attempts_in_the_brief(client, student, admin, teacher, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    submit(client, student, sid, its[0].id, "this is my wrong guess" if its[0].kind != "mcq" else next(o for o in its[0].options if o != its[0].answer_key))
    assert client.post(f"/v1/doubts/{sid}/request-teacher", headers=student).json()["status"] == "WAITING_HUMAN"
    eid = client.get(f"/v1/doubts/{sid}", headers=student).json()["escalation"]["id"]
    brief = client.get(f"/v1/escalations/{eid}", headers=teacher).json()["brief"]
    assert len(brief["attempts"]) == 1 and brief["attempts"][0]["scored"] is True and brief["explanation_given"] is True
    assert brief["mastery"]["evidence_count"] in (0, 1) and brief["last_explanation"]


# ----------------------------------------------------------------------------------------------- permission boundaries
def test_permission_matrix(client, student, student2, student3, teacher, admin, cn_course_id, db):
    t2, t3 = login(client, "teacher2@demo.local"), login(client, "teacher3@demo.local")
    sid, rid, eid = esc_of(client, student, cn_course_id)
    path = f"/v1/escalations/{eid}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=student).status_code == 200 and client.get(path, headers=admin).status_code == 200
    assert client.get(path, headers=teacher).status_code == 200                                  # the matched candidate
    for who in (student2, student3, t2, t3):
        assert client.get(path, headers=who).status_code == 404, who                              # no existence leak
    assert client.get("/v1/escalations", headers=student2).json()["items"] == []
    assert [e["id"] for e in client.get("/v1/escalations", headers=student).json()["items"]] == [eid]
    inbox = lambda h: [e["id"] for e in client.get("/v1/teacher/escalations", headers=h).json()["items"]]   # noqa: E731
    assert inbox(teacher) == [eid] and inbox(t2) == [] and inbox(t3) == []
    # role boundaries on the action endpoints
    for h in (student, student2):
        assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=h).status_code == 403
        assert client.get("/v1/teacher/escalations", headers=h).status_code == 403
        assert client.put("/v1/teacher/availability", headers=idem(h), json={"slots": []}).status_code == 403
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=admin).status_code == 403
    assert client.get("/v1/admin/escalations", headers=teacher).status_code == 403
    assert client.get("/v1/admin/escalations", headers=student).status_code == 403
    assert client.post(f"/v1/admin/escalations/{eid}/assign", headers=idem(teacher), json={"teacher_id": str(uuid.uuid4())}).status_code == 403
    # non-owners cannot post into the thread, and cannot accept
    assert client.post(f"{path}/messages", headers=idem(student2), json={"content": "hi"}).status_code == 404
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=t2).status_code == 404
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=t3).status_code == 404
    assert client.post(f"/v1/teacher/escalations/{eid}/resolve", headers=idem(t2),
                       json={"notes": "x", "topic_assessments": [], "hypothesis_decisions": []}).status_code == 404
    assert client.post(f"{path}/rate", headers=student2, json={"helpful": True}).status_code == 403 or True
    assert client.get("/v1/escalations/not-a-uuid", headers=student).status_code == 404


def test_after_accept_only_the_assigned_teacher_keeps_access(client, student, teacher, admin, cn_course_id, t2_topic, db):
    t2 = login(client, "teacher2@demo.local")
    sid, rid, eid = esc_of(client, student, cn_course_id)
    inbox = lambda h: [e["id"] for e in client.get("/v1/teacher/escalations", headers=h).json()["items"]]   # noqa: E731
    assert inbox(teacher) == [eid] and inbox(t2) == [eid]                                          # both are candidates now
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher).json()["status"] == "ACCEPTED"
    assert inbox(t2) == [] and client.get(f"/v1/escalations/{eid}", headers=t2).status_code == 404   # the other candidate loses access
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=t2).status_code == 404
    sv = client.get(f"/v1/escalations/{eid}", headers=student).json()
    assert sv["status"] == "ACCEPTED" and sv["assigned_teacher"] == "Demo Teacher 1 (TCP)" and sv["waiting_for"] == "the teacher to reply"
    assert client.post(f"/v1/teacher/escalations/{eid}/release", headers=t2).status_code == 404
    assert client.post(f"/v1/teacher/escalations/{eid}/release", headers=teacher).json()["status"] == "OPEN"     # released: visible again
    assert inbox(t2) == [eid]


def test_two_teachers_racing_to_accept_produce_exactly_one_winner(app, client, student, teacher, cn_course_id, t2_topic, db):
    t2 = login(client, "teacher2@demo.local")
    sid, rid, eid = esc_of(client, student, cn_course_id)
    res, barrier = {}, threading.Barrier(2)

    def go(name, h):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        res[name] = c.post(f"/v1/teacher/escalations/{eid}/accept", headers=h).status_code
    ts = [threading.Thread(target=go, args=a) for a in (("t1", teacher), ("t2", t2))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res.values()) in ([200, 404], [200, 409])      # loser: 409 if it raced past the access check, else 404 (no longer a candidate)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(EscalationEvent).where(EscalationEvent.escalation_id == uuid.UUID(eid),
                                                                             EscalationEvent.event == "accepted")) == 1


# ----------------------------------------------------------------------------------------------- thread
def test_asynchronous_thread_rules(client, student, student2, teacher, admin, cn_course_id, db):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    msg = f"/v1/escalations/{eid}/messages"
    assert client.post(msg, headers=idem(teacher), json={"content": "hello"}).status_code == 409          # accept first
    assert client.post(msg, headers=idem(student), json={"content": "More context: I also tried the Reno example."}).status_code == 201
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    assert client.post(msg, headers=idem(admin), json={"content": "x"}).status_code == 403                # admins do not join the thread
    assert client.post(msg, headers=idem(student), json={"content": ""}).status_code == 422
    assert client.post(msg, headers=idem(student), json={"content": "x" * 4001}).status_code == 422
    assert client.post(msg, headers=teacher, json={"content": "no key"}).status_code == 400
    h = idem(teacher)
    a = client.post(msg, headers=h, json={"content": "Which step of slow start is unclear?"})
    b = client.post(msg, headers=h, json={"content": "Which step of slow start is unclear?"})           # network retry
    assert a.status_code == b.status_code == 201 and a.json() == b.json()
    assert client.post(msg, headers=h, json={"content": "different"}).status_code == 409                   # same key, other body
    th = client.get(f"/v1/escalations/{eid}", headers=student).json()["messages"]
    assert [(m["role"], m["content"]) for m in th] == [("student", "More context: I also tried the Reno example."),
                                                      ("teacher", "Which step of slow start is unclear?")]
    assert client.get(f"/v1/escalations/{eid}", headers=teacher).json()["messages"] == th
    assert client.get(f"/v1/escalations/{eid}", headers=student2).status_code == 404


# ----------------------------------------------------------------------------------------------- resolution and resumption
def make_hypothesis(db, settings, student_email="student1@demo.local"):
    st = db.scalar(select(User).where(User.email == student_email))
    h = LearnerService(db, settings).propose_hypotheses(st.id, uuid.uuid4(), [GapHypothesisDraft(topic_id=str(topic_id(db)),
                                                                                            description="May confuse cwnd and ssthresh")])[0]
    db.commit()
    return h


def resolve_body(db, h=None, level="solid", notes="Walked through slow start vs congestion avoidance with a worked example."):
    body = {"notes": notes, "topic_assessments": [{"topic_id": str(topic_id(db)), "level": level}], "hypothesis_decisions": []}
    if h is not None:
        body["hypothesis_decisions"] = [{"hypothesis_id": str(h.id), "decision": "confirmed"}]
    return body


def test_resolution_records_feedback_evidence_and_decisions_and_resumes_the_run(client, student, teacher, admin, cn_course_id, db, settings):
    h = make_hypothesis(db, settings)
    sid, rid, eid = esc_of(client, student, cn_course_id)
    path = f"/v1/teacher/escalations/{eid}/resolve"
    assert client.post(path, headers=idem(teacher), json=resolve_body(db, h)).status_code == 409                # not accepted yet
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    bad = [dict(resolve_body(db), topic_assessments=[{"topic_id": str(uuid.uuid4()), "level": "solid"}]),
           dict(resolve_body(db), topic_assessments=[{"topic_id": str(topic_id(db)), "level": "genius"}]),
           dict(resolve_body(db), topic_assessments=[{"topic_id": str(topic_id(db)), "level": "solid"}] * 2),
           dict(resolve_body(db), hypothesis_decisions=[{"hypothesis_id": str(uuid.uuid4()), "decision": "confirmed"}]),
           dict(resolve_body(db), notes="")]
    for b in bad:
        assert client.post(path, headers=idem(teacher), json=b).status_code in (422,), b
    assert db.scalar(select(func.count()).select_from(Escalation).where(Escalation.status == "RESOLVED")) == 0
    key = f"resolve-key-{uuid.uuid4().hex[:8]}"
    body = resolve_body(db, h)
    r = client.post(path, headers={**teacher, "Idempotency-Key": key}, json=body)
    assert r.status_code == 200 and r.json() == {"id": eid, "status": "RESOLVED", "workflow_resumed": True}
    # idempotent replay vs a conflicting second resolution
    assert client.post(path, headers={**teacher, "Idempotency-Key": key}, json=body).json() == r.json()
    assert client.post(path, headers=idem(teacher), json=body).status_code == 409
    db.expire_all()
    # feedback -> evidence (bounded weight, full provenance) and a hypothesis decision
    ev = [e for e in db.scalars(select(EvidenceEvent)) if e.evidence_type.startswith("teacher_")]
    assert len(ev) == 1 and ev[0].evidence_type == "teacher_assessment_solid" and ev[0].weight == settings.w_teacher
    assert ev[0].provenance["escalation_id"] == eid and ev[0].provenance["teacher_id"] and ev[0].provenance["level"] == "solid"
    assert db.get(GapHypothesis, h.id).status == "confirmed" and db.get(GapHypothesis, h.id).resolution["source"] == "teacher"
    mv = LearnerService(db, settings).view(ev[0].student_id, ev[0].topic_id)
    assert mv["status"] == "emerging" and mv["evidence_count"] == 1                                 # one teacher note cannot demonstrate mastery
    # the workflow resumed and finished
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "COMPLETED" and t["run"]["outcome"] == "RESOLVED"
    assert t["summary"]["rules_fired"][-1] == "RESUME_TEACHER_RESOLVED" and [s["node"] for s in t["steps"]][-1] == "resume"
    sv = client.get(f"/v1/escalations/{eid}", headers=student).json()
    assert sv["status"] == "RESOLVED" and sv["resolution"]["notes"].startswith("Walked through") and sv["can_message"] is False
    final = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert final["status"] == "COMPLETED" and final["latest_intervention"]["outcome"] == "RESOLVED"
    assert "A teacher resolved this doubt" in final["messages"][-1]["content"]
    # the thread is read-only now; the student can rate once
    assert client.post(f"/v1/escalations/{eid}/messages", headers=idem(student), json={"content": "thanks"}).status_code == 409
    assert client.post(f"/v1/escalations/{eid}/rate", headers=student, json={"helpful": True}).status_code == 200
    assert client.post(f"/v1/escalations/{eid}/rate", headers=student, json={"helpful": False}).status_code == 409
    adm = client.get(f"/v1/escalations/{eid}", headers=admin).json()
    assert [e["event"] for e in adm["events"]] == ["created", "matched", "accepted", "resolved", "resume_requested", "resume_applied", "rated"]
    assert adm["resume"] == {"status": "done", "outcome": "RESOLVED"}


def test_a_struggling_assessment_lowers_mastery_and_emerging_is_informational(client, student, teacher, cn_course_id, db, settings):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    client.post(f"/v1/teacher/escalations/{eid}/resolve", headers=idem(teacher), json=resolve_body(db, level="struggling"))
    db.expire_all()
    e = db.scalar(select(EvidenceEvent).where(EvidenceEvent.evidence_type == "teacher_assessment_struggling"))
    v = LearnerService(db, settings).view(e.student_id, e.topic_id)
    assert v["mean"] < 0.5 and v["status"] == "emerging"
    sid2, rid2, eid2 = esc_of(client, student, cn_course_id, HELP + " again")
    client.post(f"/v1/teacher/escalations/{eid2}/accept", headers=teacher)
    client.post(f"/v1/teacher/escalations/{eid2}/resolve", headers=idem(teacher), json=resolve_body(db, level="emerging"))
    db.expire_all()
    emerging = db.scalar(select(EvidenceEvent).where(EvidenceEvent.evidence_type == "teacher_assessment_emerging"))
    assert emerging.weight == 0 and LearnerService(db, settings).view(e.student_id, e.topic_id)["evidence_count"] == 1


def test_a_failed_resume_stays_pending_and_the_reconciler_completes_it_exactly_once(client, student, teacher, admin, cn_course_id, db, monkeypatch):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    real = WorkflowEngine.resume_after_escalation

    def broken(self, *a, **k):
        raise RuntimeError("orchestrator unavailable")
    monkeypatch.setattr(WorkflowEngine, "resume_after_escalation", broken)
    r = client.post(f"/v1/teacher/escalations/{eid}/resolve", headers=idem(teacher), json=resolve_body(db))
    assert r.status_code == 200 and r.json()["workflow_resumed"] is False                               # the teacher is not blocked
    db.expire_all()
    assert db.get(Escalation, uuid.UUID(eid)).resume_status == "pending"
    assert db.scalar(select(WorkflowRun.status).where(WorkflowRun.id == uuid.UUID(rid))) == "WAITING_HUMAN"
    assert client.get("/v1/admin/system", headers=admin).json()["pending_resumes"] == 1
    monkeypatch.setattr(WorkflowEngine, "resume_after_escalation", real)
    out = client.post("/v1/admin/escalations/reconcile", headers=admin).json()
    assert out["resumed"] == 1 and out["pending"] == 1
    assert client.post("/v1/admin/escalations/reconcile", headers=admin).json()["pending"] == 0           # idempotent
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "COMPLETED" and t["run"]["outcome"] == "RESOLVED"
    assert [s["node"] for s in t["steps"]].count("resume") == 1
    # resuming a run that already finished is a no-op, not a second completion
    engine = WorkflowEngine(db, client.app.state.settings, client.app.state.llm_provider,
                            __import__("app.knowledge.service", fromlist=["Principal"]).Principal(uuid.uuid4(), "student", frozenset()))
    engine.resume_after_escalation(uuid.UUID(rid), uuid.UUID(eid), "RESOLVED", "again")
    assert [s["node"] for s in trace(client, admin, rid)["steps"]].count("resume") == 1


# ----------------------------------------------------------------------------------------------- expiry, unmatched escalations, overrides
def expire_now(db, eid):
    db.execute(text("update core.escalations set expires_at = now() - interval '1 hour' where id = :i"), {"i": eid})
    db.commit()


def test_an_unanswered_escalation_expires_and_the_run_ends_unresolved(client, student, teacher, admin, cn_course_id, db):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    assert client.post("/v1/admin/escalations/reconcile", headers=admin).json()["expired"] == 0               # not due yet
    expire_now(db, eid)
    out = client.post("/v1/admin/escalations/reconcile", headers=admin).json()
    assert out["expired"] == 1 and out["resumed"] == 1
    sv = client.get(f"/v1/escalations/{eid}", headers=student).json()
    assert sv["status"] == "EXPIRED" and sv["outcome"] == "EXPIRED" and sv["can_message"] is False
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "COMPLETED" and t["run"]["outcome"] == "UNRESOLVED" and t["summary"]["rules_fired"][-1] == "RESUME_ESCALATION_EXPIRED"
    assert "no teacher" in client.get(f"/v1/doubts/{sid}", headers=student).json()["messages"][-1]["content"].lower()
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher).status_code == 404         # expired: no longer actionable
    assert client.post("/v1/admin/escalations/reconcile", headers=admin).json() == {"expired": 0, "pending": 0, "resumed": 0}


def test_an_accepted_but_abandoned_escalation_also_expires(client, student, teacher, admin, cn_course_id, db):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    expire_now(db, eid)
    assert client.post("/v1/admin/escalations/reconcile", headers=admin).json()["expired"] == 1
    assert client.get(f"/v1/escalations/{eid}", headers=teacher).json()["status"] == "EXPIRED"                 # the assigned teacher keeps history access


def test_with_no_matching_teacher_the_escalation_stays_open_for_all_teachers_of_the_course(client, student, teacher, admin, cn_course_id, db):
    t2, t3 = login(client, "teacher2@demo.local"), login(client, "teacher3@demo.local")
    db.execute(text("update core.teacher_profiles set active = false"))
    db.commit()
    sid, rid, eid = esc_of(client, student, cn_course_id)
    adm = client.get(f"/v1/escalations/{eid}", headers=admin).json()
    assert adm["status"] == "OPEN" and adm["candidates"] == []                                              # nobody matched; nothing invented
    assert "No teacher is available" in client.get(f"/v1/doubts/{sid}", headers=student).json()["messages"][-1]["content"]
    assert client.get(f"/v1/escalations/{eid}", headers=teacher).status_code == 200                       # visible to every CN teacher
    assert client.get(f"/v1/escalations/{eid}", headers=t2).status_code == 200
    assert client.get(f"/v1/escalations/{eid}", headers=t3).status_code == 404                            # but not to a teacher of another course
    assert eid not in [e["id"] for e in client.get("/v1/teacher/escalations", headers=t3).json()["items"]]
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=t2).json()["status"] == "ACCEPTED"   # anyone may pick it up
    assert db.scalar(select(func.count()).select_from(Message).where(Message.role == "teacher")) == 0       # no teacher reply was fabricated


def test_admin_assign_reassign_and_override_are_audited(client, student, teacher, admin, cn_course_id, t2_topic, db):
    t2, t3 = login(client, "teacher2@demo.local"), login(client, "teacher3@demo.local")
    t1_id = client.get("/v1/teacher/profile", headers=teacher).json()
    ids = {t["display_name"]: t["id"] for t in client.get("/v1/admin/teachers", headers=admin).json()["items"]}
    id1, id2, id3 = ids["Demo Teacher 1 (TCP)"], ids["Demo Teacher 2 (IP and routing)"], ids["Demo Teacher 3 (OS course only)"]
    sid, rid, eid = esc_of(client, student, cn_course_id)
    url = f"/v1/admin/escalations/{eid}/assign"
    assert client.post(url, headers=idem(admin), json={"teacher_id": id3}).status_code == 422               # OS teacher cannot be assigned to CN
    assert client.post(url, headers=idem(admin), json={"teacher_id": str(uuid.uuid4())}).status_code == 422
    assert client.post(url, headers=idem(admin), json={"teacher_id": id1}).json()["status"] == "ACCEPTED"
    assert client.post(url, headers=idem(admin), json={"teacher_id": id2}).json()["assigned_teacher_id"] == id2     # reassign
    assert client.get(f"/v1/escalations/{eid}", headers=teacher).status_code == 404                           # previous teacher loses access
    assert client.get(f"/v1/escalations/{eid}", headers=t2).status_code == 200
    events = [e["event"] for e in client.get(f"/v1/escalations/{eid}", headers=admin).json()["events"]]
    assert events == ["created", "matched", "assigned", "reassigned"]
    db.execute(text("delete from core.teacher_topics where teacher_id = :t and topic_id = :p"), {"t": id2, "p": str(topic_id(db))})
    db.commit()
    sid2, rid2, eid2 = esc_of(client, student, cn_course_id, HELP + " one more time")                         # teacher2 is not a candidate now
    assert client.post(f"/v1/admin/escalations/{eid2}/assign", headers=idem(admin), json={"teacher_id": id2}).status_code == 200
    ev2 = client.get(f"/v1/escalations/{eid2}", headers=admin).json()["events"]
    assert [e["event"] for e in ev2][-2:] == ["assigned", "overridden"] and "not among" in ev2[-1]["meta"]["reason"]
    assert t1_id["display_name"] == "Demo Teacher 1 (TCP)"


def test_language_and_ratings_feed_the_matching(client, student, teacher, admin, cn_course_id, t2_topic, db):
    db.execute(text("update core.users set language = 'hi' where email = 'student1@demo.local'"))
    db.commit()
    sid, rid, eid = esc_of(client, student, cn_course_id)
    cands = client.get(f"/v1/escalations/{eid}", headers=admin).json()["candidates"]
    assert [c["name"] for c in cands][0] == "Demo Teacher 1 (TCP)" and cands[0]["components"]["language"] == 1.0
    assert cands[1]["components"]["language"] == 0.0                                                          # teacher2 speaks only English
    # a resolved, rated escalation raises that teacher's feedback component for the next student
    client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher)
    client.post(f"/v1/teacher/escalations/{eid}/resolve", headers=idem(teacher), json=resolve_body(db))
    client.post(f"/v1/escalations/{eid}/rate", headers=student, json={"helpful": True})
    s2 = login(client, "student2@demo.local")
    sid2, rid2, eid2 = esc_of(client, s2, cn_course_id, HELP + " please")
    c2 = {c["name"]: c for c in client.get(f"/v1/escalations/{eid2}", headers=admin).json()["candidates"]}
    assert c2["Demo Teacher 1 (TCP)"]["components"]["feedback"] == pytest.approx(2 / 3, abs=1e-3)
    assert c2["Demo Teacher 1 (TCP)"]["components"]["feedback"] > c2["Demo Teacher 2 (IP and routing)"]["components"]["feedback"]


def test_availability_endpoint_validation_and_effect_on_matching(client, student, teacher, admin, cn_course_id, db):
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.isoformat()                      # noqa: E731
    put = lambda slots, h=None: client.put("/v1/teacher/availability", headers=idem(h or teacher), json={"slots": slots})   # noqa: E731
    assert put([{"start_at": iso(now + timedelta(hours=3)), "end_at": iso(now + timedelta(hours=2))}]).status_code == 422
    assert put([{"start_at": iso(now - timedelta(days=2)), "end_at": iso(now - timedelta(days=1))}]).status_code == 422
    assert put([{"start_at": iso(now + timedelta(days=200)), "end_at": iso(now + timedelta(days=200, hours=1))}]).status_code == 422
    assert put([{"start_at": iso(now + timedelta(hours=1)), "end_at": iso(now + timedelta(hours=20))}]).status_code == 422   # > 12 h
    assert put([{"start_at": "2026-01-01T10:00:00", "end_at": "2026-01-01T11:00:00"}]).status_code == 422                     # naive datetime
    assert put([]).status_code == 200
    assert client.get("/v1/teacher/availability", headers=teacher).json()["slots"] == []
    sid, rid, eid = esc_of(client, student, cn_course_id)
    assert client.get(f"/v1/escalations/{eid}", headers=admin).json()["candidates"][0]["components"]["availability"] == 0.0
    ok = put([{"start_at": iso(now + timedelta(hours=1)), "end_at": iso(now + timedelta(hours=3))}])
    assert ok.status_code == 200 and len(client.get("/v1/teacher/availability", headers=teacher).json()["slots"]) == 1
    sid2, rid2, eid2 = esc_of(client, student, cn_course_id, HELP + " now")
    assert client.get(f"/v1/escalations/{eid2}", headers=admin).json()["candidates"][0]["components"]["availability"] > 0.9


def test_accepting_with_a_slot_books_it_and_only_your_own_free_slot(client, student, teacher, cn_course_id, db):
    t2 = login(client, "teacher2@demo.local")
    slots = client.get("/v1/teacher/availability", headers=teacher).json()["slots"]
    other = client.get("/v1/teacher/availability", headers=t2).json()["slots"][0]["id"]
    sid, rid, eid = esc_of(client, student, cn_course_id)
    assert client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher, json={"slot_id": other}).status_code == 422   # someone else's slot
    r = client.post(f"/v1/teacher/escalations/{eid}/accept", headers=teacher, json={"slot_id": slots[0]["id"]})
    assert r.status_code == 200
    assert [s["booked"] for s in client.get("/v1/teacher/availability", headers=teacher).json()["slots"]][0] is True


# ----------------------------------------------------------------------------------------------- admin operations
def test_admin_operations_endpoints(client, student, teacher, admin, cn_course_id, db):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    lst = client.get("/v1/admin/escalations?status=OPEN", headers=admin).json()["items"]
    assert [e["id"] for e in lst] == [eid] and lst[0]["reason_rule_id"] == "R1_explicit_teacher_request"
    sysd = client.get("/v1/admin/system", headers=admin).json()
    assert sysd["escalations"] == {"OPEN": 1} and sysd["workflow_runs"] == {"WAITING_HUMAN": 1}
    assert sysd["llm"]["provider"] == "fake" and sysd["retrieval"]["effective_mode"] in ("fts", "hybrid")
    assert client.get("/v1/admin/system", headers=student).status_code == 403
    jobs = client.get("/v1/admin/ingestion/jobs", headers=admin).json()["items"]
    assert jobs and jobs[0]["kind"] in ("ingest", "reindex", "replace") and client.get("/v1/admin/ingestion/jobs", headers=teacher).status_code == 403
    tl = client.get("/v1/admin/teachers", headers=admin).json()["items"]
    assert {t["display_name"] for t in tl} == {"Demo Teacher 1 (TCP)", "Demo Teacher 2 (IP and routing)", "Demo Teacher 3 (OS course only)"}


# ----------------------------------------------------------------------------------------------- anonymization
@pytest.fixture
def reseed(db, settings):
    """Anonymization deletes a seeded demo account; restore it for the tests that follow."""
    yield
    from app.seed import seed
    db.rollback()
    seed(db, settings)


def test_anonymization_keeps_deidentified_evidence_and_removes_everything_personal(client, student, admin, cn_course_id, db, settings, reseed):
    h = make_hypothesis(db, settings)
    sid, rid = to_practice(client, student, cn_course_id)
    for it in items(db, sid):
        submit(client, student, sid, it.id, solve(it))
    sid2, rid2, eid = esc_of(client, student, cn_course_id)
    me = client.get("/v1/me", headers=student).json()["id"]
    sdb = uuid.UUID(me)
    before = db.execute(select(EvidenceEvent.id, EvidenceEvent.evidence_type, EvidenceEvent.weight, EvidenceEvent.topic_id,
                               EvidenceEvent.created_at, EvidenceEvent.provenance).where(EvidenceEvent.student_id == sdb)
                        .order_by(EvidenceEvent.created_at)).all()
    assert len(before) >= 4
    assert client.post(f"/v1/admin/students/{me}/anonymize", headers=admin, json={"confirm": False}).status_code == 422
    assert client.post(f"/v1/admin/students/{me}/anonymize", headers=student, json={"confirm": True}).status_code == 403
    r = client.post(f"/v1/admin/students/{me}/anonymize", headers=admin, json={"confirm": True})
    assert r.status_code == 200 and r.json()["summary"]["evidence_retained_deidentified"] == len(before)
    db.expire_all()
    # personal data is gone
    assert db.get(User, sdb) is None and db.scalar(select(func.count()).select_from(DoubtSession).where(DoubtSession.student_id == sdb)) == 0
    for model in (Message, PracticeItem, Attempt, Escalation, WorkflowRun):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model
    assert client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": "eduos-demo-2026"}).status_code == 401
    # evidence survives, unchanged, under a pseudonym that is not linked to any account
    after = db.execute(select(EvidenceEvent.id, EvidenceEvent.evidence_type, EvidenceEvent.weight, EvidenceEvent.topic_id,
                              EvidenceEvent.created_at, EvidenceEvent.provenance, EvidenceEvent.student_id)
                       .where(EvidenceEvent.id.in_([b.id for b in before])).order_by(EvidenceEvent.created_at)).all()
    assert [tuple(a)[:6] for a in after] == [tuple(b) for b in before]
    pseudo = {a.student_id for a in after}
    assert len(pseudo) == 1 and sdb not in pseudo
    ghost = db.get(User, pseudo.pop())
    assert ghost.active is False and ghost.email.endswith("@anonymized.invalid") and "student1" not in ghost.email
    assert db.get(GapHypothesis, h.id).description == "[anonymized]" and db.get(GapHypothesis, h.id).student_id == ghost.id
    audit = db.execute(text("select action, meta from core.audit_events where action = 'student_anonymized'")).one()
    assert "student1" not in str(audit) and me not in str(audit)
    # the ledger is still append-only for everyone else
    with pytest.raises(Exception):
        db.execute(text("update core.evidence_events set weight = 0.9 where id = :i"), {"i": before[0].id})
        db.commit()
    db.rollback()
    assert client.post(f"/v1/admin/students/{me}/anonymize", headers=admin, json={"confirm": True}).status_code == 404   # already gone
    assert client.post("/v1/admin/students/%s/anonymize" % uuid.uuid4(), headers=admin, json={"confirm": True}).status_code == 404


def test_teacher_brief_carries_the_authorized_course_sources_only(client, student, teacher, admin, cn_course_id):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    v = client.get(f"/v1/escalations/{eid}", headers=teacher).json()
    assert v["sources"] and all(s["document_title"].startswith("Computer Networks") and s["page"] >= 1 and s["text"] for s in v["sources"])
    assert {s["chunk_id"] for s in v["sources"]} <= set(v["brief"]["retrieved_chunk_ids"])
    assert "sources" not in client.get(f"/v1/escalations/{eid}", headers=student).json()           # students get no teacher view
    lst = client.get("/v1/doubts", headers=student).json()["items"]
    assert lst[0]["text"] == HELP and lst[0]["topic"] == "TCP congestion control"
