"""Acknowledgment endpoint + append-only evidence ledger skeleton.
Principle under test: a self-reported "I understood" / "I am confused" / "check me" is RECORDED but carries ZERO mastery weight."""
import threading
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.learner.ledger import EvidenceIn, EvidenceRejected
from app.models import EvidenceEvent, WorkflowRun
from tests.conftest import SLOW_START_Q, ask, login
from tests.test_workflow import trace

_n = iter(range(10_000))


def ack(client, headers, sid, value, key=None):
    return client.post(f"/v1/doubts/{sid}/ack", headers={**headers, "Idempotency-Key": key or f"ack-key-{next(_n):05d}"},
                       json={"ack": value})


def explained(client, student, cid):
    r = ask(client, student, cid, SLOW_START_Q)
    assert r.json()["status"] == "AWAITING_STUDENT"
    return r.json()["session_id"], r.json()["run_id"]


def ledger(db):
    db.expire_all()
    return list(db.scalars(select(EvidenceEvent).order_by(EvidenceEvent.created_at)))


# ------------------------------------------------------------------------------------------- behaviour
def test_understood_is_recorded_with_zero_weight_and_does_not_verify_mastery(client, student, admin, cn_course_id, db):
    sid, rid = explained(client, student, cn_course_id)
    r = ack(client, student, sid, "understood")
    assert r.status_code == 202 and r.json()["mastery_credit"] == 0.0 and r.json()["ack"] == "understood"
    rows = ledger(db)
    assert len(rows) == 1
    e = rows[0]
    assert e.evidence_type == "self_report_understood" and e.weight == 0.0 and str(e.source_run_id) == rid
    assert e.provenance["source"] == "student_ack" and e.provenance["session_id"] == sid and e.provenance["run_id"] == rid
    assert e.provenance["ack"] == "understood" and e.provenance["intervention_id"] and e.created_at and e.topic_id
    # "I understood" does NOT end the doubt: the policy asks for a practice check (rule R7) because nothing has verified it
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "AWAITING_ANSWER" and t["run"]["outcome"] is None
    assert t["summary"]["rules_fired"] == ["R6_low_evidence_explain", "R7_check_after_explanation"]
    last = t["decisions"][-1]["inputs_snapshot"]
    assert last["acked"] == "understood" and last["mastery"]["status"] in ("unknown", "hypothesis") and last["mastery"]["evidence_count"] == 0
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert view["latest_intervention"]["practice"]["available"] is True and len(view["latest_intervention"]["practice"]["items"]) >= 1
    assert "answer_key" not in str(view)                                   # keys are never sent before submission


def test_check_me_is_recorded_as_a_zero_weight_request(client, student, cn_course_id, db):
    sid, _ = explained(client, student, cn_course_id)
    assert ack(client, student, sid, "check_me").status_code == 202
    e = ledger(db)[0]
    assert e.evidence_type == "check_requested" and e.weight == 0.0


def test_still_confused_continues_within_budget_and_never_adds_weight(client, student, admin, cn_course_id, db):
    sid, rid = explained(client, student, cn_course_id)
    view = lambda: client.get(f"/v1/doubts/{sid}", headers=student).json()          # noqa: E731
    r1 = ack(client, student, sid, "still_confused")
    assert r1.status_code == 202 and r1.json()["status"] == "AWAITING_STUDENT"
    assert view()["latest_intervention"]["explanation"]                              # 2nd explanation attempt (rule R6)
    r2 = ack(client, student, sid, "still_confused")
    assert r2.status_code == 202 and r2.json()["status"] == "AWAITING_STUDENT"
    assert view()["latest_intervention"]["clarification_question"]                   # explanation budget spent -> R10 clarifies
    assert ack(client, student, sid, "still_confused").status_code == 409            # not waiting for an ack any more
    for _ in range(10):                                                              # replying forever is bounded by the action budget
        status = client.post(f"/v1/doubts/{sid}/messages", headers=student, json={"text": "I still do not get the window growth"}).json()["status"]
        if status != "AWAITING_STUDENT":
            break
    assert status == "WAITING_HUMAN"
    rows = ledger(db)
    assert len(rows) == 2 and all(r.evidence_type == "self_report_confused" and r.weight == 0.0 for r in rows)
    t = trace(client, admin, rid)
    assert t["summary"]["rules_fired"][:3] == ["R6_low_evidence_explain", "R6_low_evidence_explain", "R10_default_clarify"]
    assert t["summary"]["rules_fired"][-1] == "R2_budget_exhausted"
    assert t["summary"]["rules_fired"].count("R6_low_evidence_explain") == t["summary"]["budgets"]["max_explain_attempts"]
    assert all(d["inputs_snapshot"]["mastery"]["status"] == "unknown" for d in t["decisions"])       # never inferred


# ------------------------------------------------------------------------------------------- authorization & validation
def test_ack_authorization(client, student, student2, teacher, admin, cn_course_id, db):
    sid, _ = explained(client, student, cn_course_id)
    path = f"/v1/doubts/{sid}/ack"
    assert client.post(path, headers={"Idempotency-Key": "anon-key-1"}, json={"ack": "understood"}).status_code == 401
    assert ack(client, student2, sid, "understood").status_code == 404                  # another student: no existence leak
    assert ack(client, teacher, sid, "understood").status_code == 403
    assert ack(client, admin, sid, "understood").status_code == 403
    assert ledger(db) == []                                                              # nothing written by any of them
    assert ack(client, student, sid, "understood").status_code == 202


def test_ack_input_validation(client, student, cn_course_id, db):
    sid, _ = explained(client, student, cn_course_id)
    assert ack(client, student, sid, "mastered").status_code == 422                      # not an allowed value
    assert client.post(f"/v1/doubts/{sid}/ack", headers={**student, "Idempotency-Key": "valid-key-77"},
                       json={"ack": "understood", "weight": 1.0}).status_code == 422      # no client-controlled weight
    assert client.post(f"/v1/doubts/{sid}/ack", headers=student, json={"ack": "understood"}).status_code == 400   # key required
    assert client.post("/v1/doubts/not-a-uuid/ack", headers={**student, "Idempotency-Key": "valid-key-78"},
                       json={"ack": "understood"}).status_code == 404
    assert ledger(db) == []


def test_ack_only_when_the_run_is_waiting_for_one(client, student, cn_course_id, db):
    r = ask(client, student, cn_course_id, "tell me")                                     # clarification pending, not an ack
    sid = r.json()["session_id"]
    assert ack(client, student, sid, "understood").json()["error"]["code"] == "INVALID_RUN_STATE"
    sid2, _ = explained(client, student, cn_course_id)
    client.post(f"/v1/doubts/{sid2}/request-teacher", headers=student)                    # now WAITING_HUMAN
    assert ack(client, student, sid2, "understood").status_code == 409
    sid3, _ = explained(client, student, cn_course_id)
    assert ack(client, student, sid3, "understood").status_code == 202                    # run COMPLETED
    assert ack(client, student, sid3, "understood").status_code == 409                    # a second, different delivery
    assert len([e for e in ledger(db)]) == 1


# ------------------------------------------------------------------------------------------- duplicate processing
def test_duplicate_delivery_is_applied_once(client, student, admin, cn_course_id, db):
    sid, rid = explained(client, student, cn_course_id)
    a = ack(client, student, sid, "understood", key="dup-delivery-1")
    steps_after_first = len(trace(client, admin, rid)["steps"])
    b = ack(client, student, sid, "understood", key="dup-delivery-1")                    # network retry
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert len(ledger(db)) == 1 and len(trace(client, admin, rid)["steps"]) == steps_after_first
    c = ack(client, student, sid, "still_confused", key="dup-delivery-1")                # same key, different content
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(ledger(db)) == 1


def test_concurrent_acks_race_has_exactly_one_winner(app, client, student, cn_course_id, db):
    sid, _ = explained(client, student, cn_course_id)
    from fastapi.testclient import TestClient
    results, barrier = [], threading.Barrier(2)

    def go(n):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        results.append(ack(c, student, sid, "understood", key=f"race-key-{n}").status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [202, 409]
    assert len(ledger(db)) == 1


# ------------------------------------------------------------------------------------------- ledger integrity
def _ev(**kw):
    base = dict(student_id=uuid.uuid4(), course_id=uuid.uuid4(), evidence_type="self_report_understood", weight=0.0,
                source_run_id=uuid.uuid4(), source_ref="ref-abc", provenance={})
    base.update(kw)
    base["provenance"] = base["provenance"] or {"source": "t", "session_id": "s", "run_id": str(base["source_run_id"])}
    return EvidenceIn(**base)


def test_application_level_evidence_validation():
    assert _ev().weight == 0.0
    for bad in (dict(evidence_type="attempt_correct", weight=1.0), dict(evidence_type="teacher_assessment"),
                dict(evidence_type="made_up_type"), dict(weight=0.1), dict(evidence_type="check_requested", weight=0.5),
                dict(provenance={"source": "x"})):
        with pytest.raises((EvidenceRejected, ValueError)):
            _ev(**bad)
    with pytest.raises(Exception):
        _ev(source_ref="x")                                             # too short
    with pytest.raises((EvidenceRejected, ValueError)):
        _ev(provenance={"source": "t", "session_id": "s", "run_id": str(uuid.uuid4())})   # run id mismatch


@pytest.fixture
def one_row(client, student, cn_course_id, db):
    sid, rid = explained(client, student, cn_course_id)
    ack(client, student, sid, "understood")
    return ledger(db)[0]


def test_database_rejects_mastery_weight_unknown_types_and_duplicates(db, one_row):
    base = dict(s=one_row.student_id, c=one_row.course_id, r=one_row.source_run_id)

    def raw(etype, weight, ref):
        db.execute(text("insert into core.evidence_events (id, student_id, course_id, evidence_type, weight, source_run_id, "
                        "source_ref, provenance) values (gen_random_uuid(), :s, :c, :t, :w, :r, :ref, '{}'::jsonb)"),
                   {**base, "t": etype, "w": weight, "ref": ref})
        db.commit()
    with pytest.raises(IntegrityError):
        raw("self_report_understood", 0.7, "raw-1")                       # self-report with mastery weight
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("made_up_type", 1.0, "raw-2")                                 # unknown type
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("attempt_correct", 1.5, "raw-2b")                             # graded evidence is capped at 1 per attempt
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("attempt_incorrect", 0.0, "raw-2c")                           # graded evidence must carry weight
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("teacher_assessment_emerging", 0.5, "raw-2d")                 # informational teacher note carries no weight
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("check_requested", 0.2, "raw-3")
    db.rollback()
    with pytest.raises(IntegrityError):
        raw("self_report_understood", 0.0, one_row.source_ref)            # duplicate (type, source_ref)
    db.rollback()
    raw("self_report_confused", 0.0, "raw-ok")                             # a valid row still works
    assert db.scalar(select(func.count()).select_from(EvidenceEvent)) == 2


def test_ledger_is_append_only_at_the_database_level(db, one_row):
    with pytest.raises(DBAPIError):
        db.execute(text("update core.evidence_events set weight = 1 where id = :i"), {"i": one_row.id})
        db.commit()
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("delete from core.evidence_events where id = :i"), {"i": one_row.id})
        db.commit()
    db.rollback()
    row = db.get(EvidenceEvent, one_row.id)
    assert row.weight == 0.0 and row.evidence_type == "self_report_understood"


# ------------------------------------------------------------------------------------------- reading the ledger
def test_evidence_listing_is_scoped_and_labelled(client, student, student2, teacher, admin, cn_course_id):
    sid, _ = explained(client, student, cn_course_id)
    ack(client, student, sid, "understood")
    mine = client.get("/v1/learners/me/evidence", headers=student).json()
    assert len(mine["items"]) == 1 and mine["items"][0]["weight"] == 0.0 and "zero mastery weight" in mine["note"]
    assert client.get("/v1/learners/me/evidence", headers=student2).json()["items"] == []
    assert client.get("/v1/learners/me/evidence").status_code == 401
    assert client.get("/v1/learners/me/evidence", headers=teacher).status_code == 403
    uid = client.get("/v1/me", headers=student).json()["id"]
    assert len(client.get(f"/v1/admin/learners/{uid}/evidence", headers=admin).json()["items"]) == 1
    assert client.get(f"/v1/admin/learners/{uid}/evidence", headers=student).status_code == 403
    assert client.get("/v1/admin/learners/nope/evidence", headers=admin).status_code == 404
