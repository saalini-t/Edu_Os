"""Learning Gap Map, Learning Passport and explainable decisions: derived from the ledger, deterministic, owner-only."""
import pytest
from sqlalchemy import select

from app.models import Topic
from tests.conftest import SLOW_START_Q, ask, login
from tests.test_adaptive_flow import items, solve, submit, to_practice, wrong
from tests.test_teaching import HELP, esc_of, make_hypothesis


def node(gm, slug):
    return next(t for t in gm["topics"] if t["slug"] == slug)


def gap_map(client, h, **q):
    return client.get("/v1/learners/me/gap-map", headers=h, params=q)


def test_a_new_learner_has_every_topic_not_assessed_and_acknowledgements_change_nothing(client, student, cn_course_id, db):
    gm = gap_map(client, student).json()
    assert [t["slug"] for t in gm["topics"]] == ["layering", "tcp-reliability", "tcp-flow-control", "tcp-congestion", "ip-addressing", "routing"]
    assert {t["status"] for t in gm["topics"]} == {"not_assessed"} and gm["counts"]["not_assessed"] == 6
    assert node(gm, "tcp-congestion")["prerequisites"] and all(t["evidence"] == [] for t in gm["topics"])
    sid, rid = to_practice(client, student, cn_course_id, how="understood")                  # an "I understood" acknowledgement
    gm = gap_map(client, student).json()
    assert node(gm, "tcp-congestion")["status"] == "not_assessed" and node(gm, "tcp-congestion")["evidence_count"] == 0


def test_graded_work_moves_the_status_and_links_to_timestamped_evidence(client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    r = submit(client, student, sid, its[0].id, solve(its[0]))
    assert r.status_code == 200
    n = node(gap_map(client, student).json(), "tcp-congestion")
    assert n["status"] in ("practising", "improving") and n["status"] != "demonstrated"          # one correct answer is never mastery
    assert n["evidence_count"] == 1                                                              # mastery evidence; the ack row is listed with weight 0
    assert [e["weight"] for e in n["evidence"] if e["type"].startswith("self_report")] == [0.0]
    ev = next(e for e in n["evidence"] if e["type"] == "attempt_correct")
    assert ev["type"] == "attempt_correct" and ev["weight"] > 0 and ev["at"] and ev["attempt_id"] == r.json()["attempt_id"]
    assert n["last_evidence_at"] and "estimate" in n["reason"].lower()
    for it in its[1:]:
        submit(client, student, sid, it.id, solve(it))
    n = node(gap_map(client, student).json(), "tcp-congestion")
    assert n["evidence_count"] == len(its) and n["status"] in ("improving", "demonstrated")


def test_suspected_and_confirmed_gaps_are_distinguished_and_prerequisites_suggest_where_to_look(client, student, cn_course_id, db, settings):
    h = make_hypothesis(db, settings)
    n = node(gap_map(client, student).json(), "tcp-congestion")
    assert n["status"] == "suspected_gap" and n["gap_kind"] == "suspected" and "not confirmed" in n["reason"]
    assert n["hypotheses"][0]["status"] == "proposed" and n["hypotheses"][0]["id"] == str(h.id)
    assert {p["name"] for p in n["check_prerequisites"]} == {"TCP connections and reliability", "TCP flow control"}      # suggestion only
    sid, rid = to_practice(client, student, cn_course_id)
    for it in items(db, sid)[:2]:
        submit(client, student, sid, it.id, wrong(it))                                           # two failures on distinct targeted items
    db.expire_all()
    n = node(gap_map(client, student).json(), "tcp-congestion")
    assert n["status"] == "suspected_gap" and n["gap_kind"] in ("suspected", "confirmed") and n["evidence_count"] >= 1


def test_gap_map_and_passport_are_owner_only_and_course_scoped(client, student, student2, student3, teacher, admin, cn_course_id):
    assert client.get("/v1/learners/me/gap-map").status_code == 401
    assert client.get("/v1/learners/me/gap-map", headers=teacher).status_code == 403
    assert client.get("/v1/learners/me/passport", headers=admin).status_code == 403
    assert gap_map(client, student, course_id="not-a-uuid").status_code == 404
    assert gap_map(client, student3, course_id=cn_course_id).status_code == 404                  # not enrolled in that course
    assert gap_map(client, student2, course_id=cn_course_id).status_code == 200
    sid, rid = to_practice(client, student, cn_course_id)
    assert all(t["evidence_count"] == 0 for t in gap_map(client, student2).json()["topics"])      # nothing leaks between learners


def test_the_passport_is_deterministic_complete_and_honest_about_unresolved_doubts(client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    submit(client, student, sid, its[0].id, solve(its[0]))
    sid2, rid2, eid = esc_of(client, student, cn_course_id)
    a = client.get("/v1/learners/me/passport", headers=student).json()
    b = client.get("/v1/learners/me/passport", headers=student).json()
    assert a == b and len(a["digest"]) == 64                                                     # reproducible
    assert a["totals"] == {"doubts": 2, "unresolved": 2, "graded_attempts": 1}
    assert {d["session_id"] for d in a["doubts"]} == {sid, sid2} and all(d["unresolved"] for d in a["doubts"])
    assert any(d["last_rule"] == "R1_explicit_teacher_request" for d in a["doubts"])
    assert a["history"] and a["history"][0]["evidence"]["id"] and "never labels ability" in a["note"] and "anonymize" in a["retention"]
    assert node(a, "tcp-congestion")["attempts"] == {"attempts": 1, "correct": 1}
    submit(client, student, sid, its[1].id, solve(its[1]))
    assert client.get("/v1/learners/me/passport", headers=student).json()["digest"] != a["digest"]   # new evidence -> new digest


def test_each_decision_is_explained_from_recorded_rules_and_evidence(client, student, student2, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    for it in its:
        submit(client, student, sid, it.id, solve(it))
    d = client.get(f"/v1/doubts/{sid}/decisions", headers=student).json()
    assert d["items"] and all(i["decision_id"] and i["title"] and i["reasons"] and i["rule_id"] for i in d["items"])
    assert [i["rule_id"] for i in d["items"]][:2] == ["R6_low_evidence_explain", "R7_check_after_explanation"]
    last = d["items"][-1]
    assert last["evidence"] and any(e["type"].startswith("attempt_") for e in last["evidence"]) and all(e["at"] for e in last["evidence"])    # evidence the decision used
    assert last["provider"] == "fake" and last["hard_rule"] is False and "no hard rule" in last["precedence"]
    assert all(i["model_calls"] is None and i["errors"] is None for i in d["items"])                  # students see no internals
    adm = client.get(f"/v1/doubts/{sid}/decisions", headers=admin).json()["items"]
    assert adm[0]["model_calls"] is not None and adm[0]["decision_id"] == d["items"][0]["decision_id"]
    assert client.get(f"/v1/doubts/{sid}/decisions", headers=student2).status_code == 404
    assert client.get(f"/v1/doubts/{sid}/decisions").status_code == 401


def test_a_hard_rule_is_labelled_as_taking_precedence(client, student, cn_course_id):
    sid, rid, eid = esc_of(client, student, cn_course_id)
    d = client.get(f"/v1/doubts/{sid}/decisions", headers=student).json()["items"][-1]
    assert d["rule_id"] == "R1_explicit_teacher_request" and d["hard_rule"] is True and "no model can override" in d["precedence"]
    assert d["title"] == "You asked for a teacher"
