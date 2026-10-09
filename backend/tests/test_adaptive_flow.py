"""The complete adaptive loop through the HTTP API with the deterministic fake provider:
understand -> load_context -> decide -> explain -> (ack) -> practice -> answer -> evaluate -> update_evidence -> decide again."""
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.learner.service import LearnerService
from app.llm.fake import FakeLLMProvider
from app.llm.schemas import EvaluationOut
from app.main import create_app
from app.models import Attempt, EvidenceEvent, PracticeItem
from tests.conftest import SLOW_START_Q, ask, login
from tests.test_ack_evidence import ack
from tests.test_workflow import trace

_k = iter(range(100_000))


def submit(client, headers, sid, item_id, answer, key=None, hints=0):
    return client.post(f"/v1/doubts/{sid}/answers", headers={**headers, "Idempotency-Key": key or f"ans-key-{next(_k):06d}"},
                       json={"item_id": str(item_id), "answer": answer, "hints_used": hints})


def solve(it: PracticeItem) -> str:
    return it.answer_key


def wrong(it: PracticeItem) -> str:
    if it.kind == "mcq":
        return next(o for o in it.options if o != it.answer_key)
    return "-1" if it.kind == "numeric" else "I do not know"


def items(db, sid):
    db.expire_all()
    return list(db.scalars(select(PracticeItem).where(PracticeItem.session_id == uuid.UUID(sid)).order_by(PracticeItem.set_index, PracticeItem.position)))


def to_practice(client, student, cid, question=SLOW_START_Q, how="understood"):
    r = ask(client, student, cid, question)
    sid, rid = r.json()["session_id"], r.json()["run_id"]
    assert r.json()["status"] == "AWAITING_STUDENT"
    a = ack(client, student, sid, how)
    assert a.status_code == 202 and a.json()["status"] == "AWAITING_ANSWER", a.text
    return sid, rid


def evidence(db):
    db.expire_all()
    return list(db.scalars(select(EvidenceEvent).order_by(EvidenceEvent.created_at)))


# ----------------------------------------------------------------------------------------------- the full loop
def test_full_adaptive_loop_resolves_only_when_mastery_is_demonstrated(client, student, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    assert len(its) == 3 and {i.set_index for i in its} == {1}
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert view["status"] == "AWAITING_ANSWER" and view["latest_intervention"]["practice"]["available"] is True
    statuses = []
    for n, it in enumerate(its, start=1):
        r = submit(client, student, sid, it.id, solve(it))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["correct"] is True and body["counted_as_evidence"] is True and body["remaining"] == 3 - n
        assert body["reveal"]["correct_answer"] == it.answer_key
        statuses.append(body["mastery"]["status"])
    assert statuses == ["emerging", "emerging", "demonstrated"]                       # one or two correct answers are not mastery
    t = trace(client, admin, rid)
    assert t["run"]["status"] == "COMPLETED" and t["run"]["outcome"] == "RESOLVED"
    assert t["summary"]["rules_fired"] == ["R6_low_evidence_explain", "R7_check_after_explanation", "R8_mastery_demonstrated"]
    nodes = [s["node"] for s in t["steps"]]
    assert nodes == ["understand", "load_context", "decide", "explain", "decide", "practice", "evaluate", "update_evidence",
                     "evaluate", "update_evidence", "evaluate", "update_evidence", "check", "decide", "complete"]
    ev = evidence(db)
    assert [e.evidence_type for e in ev] == ["self_report_understood"] + ["attempt_correct"] * 3
    assert ev[0].weight == 0 and all(0 < e.weight <= 1 for e in ev[1:])
    assert ev[1].provenance["grader"] == "exact" and any(e.provenance["grader"] == "llm_rubric" for e in ev[1:])
    prog = client.get("/v1/learners/me/progress", headers=student).json()["topics"][0]
    assert prog["status"] == "demonstrated" and prog["evidence_count"] == 3 and prog["distinct_sources"] == 3
    hist = client.get("/v1/learners/me/history", headers=student).json()["items"]
    assert len(hist) == 3 and {h["evidence"]["attempt_id"] for h in hist} == {str(a.id) for a in db.scalars(select(Attempt))}
    final = client.get(f"/v1/doubts/{sid}", headers=student).json()["latest_intervention"]
    assert final["outcome"] == "RESOLVED"


def test_a_single_correct_answer_does_not_establish_mastery_and_a_failed_check_triggers_a_new_explanation(client, student, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    first, second, third = items(db, sid)
    r1 = submit(client, student, sid, first.id, solve(first)).json()
    assert r1["mastery"]["status"] == "emerging" and r1["mastery"]["evidence_count"] == 1
    submit(client, student, sid, second.id, wrong(second))
    last = submit(client, student, sid, third.id, wrong(third)).json()
    assert last["run_status"] == "AWAITING_STUDENT" and last["mastery"]["status"] == "emerging"
    t = trace(client, admin, rid)
    assert t["summary"]["rules_fired"][-1] == "R9_retry_explain_new_angle" and t["summary"]["counters"]["failed_checks"] == 1
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert view["latest_intervention"]["explanation"]["text"] and view["status"] == "AWAITING_STUDENT"


def test_repeated_failure_ends_in_a_teacher_escalation_not_an_endless_loop(client, student, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    for it in items(db, sid):
        submit(client, student, sid, it.id, wrong(it))
    assert client.get(f"/v1/doubts/{sid}", headers=student).json()["status"] == "AWAITING_STUDENT"      # R9: explained again
    assert ack(client, student, sid, "understood").json()["status"] == "AWAITING_ANSWER"                 # R7: second, easier check
    second_set = [i for i in items(db, sid) if i.set_index == 2]
    rank = {"easy": 0, "medium": 1, "hard": 2}
    first_set = [i for i in items(db, sid) if i.set_index == 1]
    assert second_set and max(rank[i.difficulty] for i in second_set) < max(rank[i.difficulty] for i in first_set)   # easier after a failure
    assert not ({i.prompt_hash for i in second_set} & {i.prompt_hash for i in items(db, sid) if i.set_index == 1})   # new questions
    for it in second_set:
        submit(client, student, sid, it.id, wrong(it))
    v = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert v["status"] == "WAITING_HUMAN" and v["escalation"]["status"] == "OPEN"
    t = trace(client, admin, rid)
    assert t["summary"]["rules_fired"][-1] == "R5_repeated_failure" and t["summary"]["counters"]["failed_checks"] == 2
    assert t["summary"]["counters"]["practice_sets"] == 2 <= t["summary"]["budgets"]["max_practice_sets"]
    assert [e.evidence_type for e in evidence(db)].count("attempt_incorrect") == 6


def test_a_pass_that_does_not_demonstrate_mastery_leads_to_another_check_not_to_completion(client, student, admin, cn_course_id, db):
    """One failed round, then a perfect round: the earlier failures keep the mean below T_MASTER, so the run does NOT resolve."""
    sid, rid = to_practice(client, student, cn_course_id)
    for it in items(db, sid):
        submit(client, student, sid, it.id, wrong(it))
    ack(client, student, sid, "understood")
    last = None
    for it in [i for i in items(db, sid) if i.set_index == 2]:
        last = submit(client, student, sid, it.id, solve(it)).json()
    assert last["mastery"]["status"] == "emerging"                                                        # evidence is mixed
    t = trace(client, admin, rid)
    assert t["run"]["outcome"] is None and t["run"]["status"] != "COMPLETED"
    assert "R8_mastery_demonstrated" not in t["summary"]["rules_fired"]


# ----------------------------------------------------------------------------------------------- answer keys
def test_answer_keys_are_hidden_until_the_question_is_answered(client, student, cn_course_id, db):
    sid, _ = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    raw = client.get(f"/v1/doubts/{sid}", headers=student).text
    for banned in ("answer_key", "rubric", "distractor_tags", "reveal", "correct_answer"):
        assert banned not in raw, banned
    practice_json = json.dumps(client.get(f"/v1/doubts/{sid}", headers=student).json()["practice"])
    for it in its:
        if it.kind != "mcq":                               # mcq options legitimately contain the right answer among distractors
            assert it.answer_key not in practice_json
    submit(client, student, sid, its[0].id, wrong(its[0]))
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    sets = view["practice"][0]["items"]
    assert sets[0]["attempt"]["reveal"]["correct_answer"] == its[0].answer_key                          # revealed after submission
    assert all(i["attempt"] is None for i in sets[1:])                                                  # still hidden for the rest
    assert "answer_key" not in client.get(f"/v1/doubts/{sid}", headers=student).text


# ----------------------------------------------------------------------------------------------- idempotency and protection
def test_duplicate_submission_is_applied_once(client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    it = items(db, sid)[0]
    a = submit(client, student, sid, it.id, solve(it), key="same-answer-key")
    b = submit(client, student, sid, it.id, solve(it), key="same-answer-key")                 # network retry
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.item_id == it.id)) == 1
    assert [e.evidence_type for e in evidence(db)].count("attempt_correct") == 1
    c = submit(client, student, sid, it.id, wrong(it), key="same-answer-key")                 # same key, different body
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_an_item_cannot_be_scored_twice_so_evidence_cannot_be_farmed(client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    it = items(db, sid)[0]
    assert submit(client, student, sid, it.id, solve(it)).status_code == 200
    again = submit(client, student, sid, it.id, solve(it))                                    # new key, same item
    assert again.status_code == 409 and again.json()["error"]["code"] == "ITEM_ALREADY_ANSWERED"
    assert db.scalar(select(func.count()).select_from(EvidenceEvent).where(EvidenceEvent.evidence_type == "attempt_correct")) == 1


def test_concurrent_submissions_for_one_item_produce_one_scored_attempt(app, client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    it = items(db, sid)[0]
    out, barrier = [], threading.Barrier(2)

    def go(n):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        out.append(submit(c, student, sid, it.id, solve(it), key=f"race-answer-{n}").status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [200, 409]
    assert [e.evidence_type for e in evidence(db)].count("attempt_correct") == 1


def test_authorization_and_validation_of_answers(client, student, student2, teacher, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    mcq = next(i for i in items(db, sid) if i.kind == "mcq")
    path = f"/v1/doubts/{sid}/answers"
    body = {"item_id": str(mcq.id), "answer": solve(mcq)}
    assert client.post(path, headers={"Idempotency-Key": "anon-answer-1"}, json=body).status_code == 401
    assert submit(client, student2, sid, mcq.id, solve(mcq)).status_code == 404                  # someone else's session
    assert submit(client, teacher, sid, mcq.id, solve(mcq)).status_code == 403
    assert submit(client, admin, sid, mcq.id, solve(mcq)).status_code == 403
    assert client.post(path, headers=student, json=body).status_code == 400                     # idempotency key required
    assert submit(client, student, sid, mcq.id, "not one of the options").status_code == 422
    assert submit(client, student, sid, uuid.uuid4(), "x").status_code == 404                    # unknown question
    other_sid, _ = to_practice(client, student, cn_course_id, "Why does TCP slow start double the window every RTT?")
    assert submit(client, student, other_sid, mcq.id, solve(mcq)).status_code == 404            # question of a different session
    r = client.post(path, headers={**student, "Idempotency-Key": "extra-field-1"}, json={**body, "correct": True})
    assert r.status_code == 422                                                                 # no client-controlled verdict
    assert evidence(db) == [e for e in evidence(db) if e.evidence_type.startswith("self_report")]   # none of the above wrote evidence
    assert client.get("/v1/learners/me/progress", headers=student2).json()["topics"] == []


def test_answering_outside_the_waiting_state_is_rejected(client, student, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    for it in its:
        submit(client, student, sid, it.id, solve(it))
    assert client.get(f"/v1/doubts/{sid}", headers=student).json()["status"] == "COMPLETED"
    late = submit(client, student, sid, its[0].id, solve(its[0]))
    assert late.status_code == 409 and late.json()["error"]["code"] == "ITEM_ALREADY_ANSWERED"


def test_a_hard_rule_beats_the_practice_loop(client, student, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    submit(client, student, sid, its[0].id, solve(its[0]))
    assert client.post(f"/v1/doubts/{sid}/request-teacher", headers=student).json()["status"] == "WAITING_HUMAN"
    t = trace(client, admin, rid)
    assert t["summary"]["rules_fired"][-1] == "R1_explicit_teacher_request"
    late = submit(client, student, sid, its[1].id, solve(its[1]))
    assert late.status_code == 409 and late.json()["error"]["code"] == "INVALID_RUN_STATE"


# ----------------------------------------------------------------------------------------------- interruptions and restarts
def test_an_interrupted_request_leaves_no_partial_state_and_a_retry_succeeds(client, student, cn_course_id, db, monkeypatch):
    sid, rid = to_practice(client, student, cn_course_id)
    it = items(db, sid)[0]
    real = LearnerService.record_graded_attempt
    calls = {"n": 0}

    def crash_once(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process died while writing evidence")
        return real(self, **kw)
    monkeypatch.setattr(LearnerService, "record_graded_attempt", crash_once)
    r = submit(client, student, sid, it.id, solve(it), key="retry-after-crash")
    assert r.status_code == 500
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Attempt)) == 0                          # nothing half-written
    assert [e.evidence_type for e in evidence(db)] == ["self_report_understood"]
    assert client.get(f"/v1/doubts/{sid}", headers=student).json()["status"] == "AWAITING_ANSWER"
    ok = submit(client, student, sid, it.id, solve(it), key="retry-after-crash")             # the client retries with the SAME key
    assert ok.status_code == 200 and ok.json()["correct"] is True
    assert [e.evidence_type for e in evidence(db)].count("attempt_correct") == 1


def test_a_brand_new_application_instance_continues_the_run_from_persisted_state(app, client, student, cn_course_id, db, settings):
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    submit(client, student, sid, its[0].id, solve(its[0]))
    fresh = TestClient(create_app(settings), raise_server_exceptions=False)             # simulates a restart: no in-memory state
    s2 = login(fresh, "student1@demo.local")
    for it in its[1:]:
        r = submit(fresh, s2, sid, it.id, solve(it))
        assert r.status_code == 200
    assert fresh.get(f"/v1/doubts/{sid}", headers=s2).json()["status"] == "COMPLETED"


# ----------------------------------------------------------------------------------------------- provider failures in the loop
class FlakyGrader(FakeLLMProvider):
    mode = "ok"

    def evaluate_answer(self, req):
        if self.mode == "down":
            raise RuntimeError("grader down")
        if self.mode == "unsure":
            return EvaluationOut(correct=True, partial_credit=1.0, feedback="Looks right.", uncertainty=0.95)
        return super().evaluate_answer(req)


def short_item(db, sid):
    return next(i for i in items(db, sid) if i.kind == "short_text")


def test_an_unavailable_grader_produces_no_evidence_and_the_student_can_resubmit(app, client, student, cn_course_id, db):
    prov = FlakyGrader()
    app.state.llm_provider = prov
    sid, rid = to_practice(client, student, cn_course_id)
    it = short_item(db, sid)
    prov.mode = "down"
    r = submit(client, student, sid, it.id, solve(it)).json()
    assert r["status"] == "UNGRADED" and r["scored"] is False and r["counted_as_evidence"] is False and r["remaining"] == 3
    assert "reveal" not in r and [e.evidence_type for e in evidence(db)] == ["self_report_understood"]
    prov.mode = "ok"
    again = submit(client, student, sid, it.id, solve(it)).json()                           # a new key: the first was never scored
    assert again["status"] == "GRADED" and again["counted_as_evidence"] is True and again["remaining"] == 2


def test_an_unsure_model_grade_is_shown_but_never_becomes_evidence(app, client, student, cn_course_id, db):
    prov = FlakyGrader()
    app.state.llm_provider = prov
    sid, rid = to_practice(client, student, cn_course_id)
    prov.mode = "unsure"
    it = short_item(db, sid)
    r = submit(client, student, sid, it.id, solve(it)).json()
    assert r["status"] == "UNCERTAIN" and r["correct"] is None and r["counted_as_evidence"] is False
    assert "not sure" in r["feedback"] and r["reveal"]["correct_answer"] == it.answer_key
    assert [e.evidence_type for e in evidence(db)] == ["self_report_understood"]
    assert r["mastery"]["evidence_count"] == 0


def test_practice_generation_failure_falls_back_to_the_hand_authored_bank(app, client, student, cn_course_id, db):
    class NoGen(FakeLLMProvider):
        def generate_practice(self, req):
            raise RuntimeError("generator down")
    app.state.llm_provider = NoGen()
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    assert len(its) == 3 and {i.source for i in its} == {"seed_bank"} and {i.provider for i in its} == {"seed_bank"}
    view = client.get(f"/v1/doubts/{sid}", headers=student).json()
    assert view["latest_intervention"]["practice"]["source"] == "seed_bank"


def test_no_valid_practice_at_all_ends_honestly_as_unverified(app, client, student, admin, cn_course_id, db, monkeypatch):
    class NoGen(FakeLLMProvider):
        def generate_practice(self, req):
            raise RuntimeError("generator down")
    import app.agents.practice as pa
    monkeypatch.setattr(pa, "BANK", {})
    app.state.llm_provider = NoGen()
    r = ask(client, student, cn_course_id, SLOW_START_Q)
    ack(client, student, r.json()["session_id"], "understood")
    t = trace(client, admin, r.json()["run_id"])
    assert t["run"]["status"] == "COMPLETED" and t["run"]["outcome"] == "UNVERIFIED"
    view = client.get(f"/v1/doubts/{r.json()['session_id']}", headers=student).json()
    assert view["latest_intervention"]["practice"]["available"] is False


def test_provider_and_model_are_recorded_in_the_trace(client, student, admin, cn_course_id, db):
    sid, rid = to_practice(client, student, cn_course_id)
    for it in items(db, sid):
        submit(client, student, sid, it.id, solve(it))
    t = trace(client, admin, rid)
    by_node = {s["node"]: s for s in t["steps"]}
    for node in ("understand", "explain", "practice"):
        assert by_node[node]["provider"] == "fake" and by_node[node]["model"] == "fake-extractive-1"
        assert by_node[node]["prompt_version"].startswith("fake-")
        assert by_node[node]["output"]["agent"]["attempts"] >= 1
    ev_steps = [s for s in t["steps"] if s["node"] == "evaluate"]
    assert {s["output"]["grader"] for s in ev_steps} <= {"exact", "llm_rubric"}
    assert any(s["provider"] == "fake" for s in ev_steps if s["output"]["grader"] == "llm_rubric")
    assert all(s["provider"] is None for s in ev_steps if s["output"]["grader"] == "exact")             # no model involved in exact grading
