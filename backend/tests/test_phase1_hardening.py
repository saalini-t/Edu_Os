"""Phase 1 hardening: no lock or connection is held across a model call, duplicate execution is prevented, crashed runs are
recovered, model concurrency is bounded, rate limits and login throttling work, grading resists injection and untrusted model
output cannot create positive mastery on its own, and audit events are append-only."""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.agents.evaluation import ItemForGrading, evaluate_answer, key_terms, lexical_support
from app.db import make_engine, make_session_factory
from app.learner import mastery as m
from app.learner.ledger import EvidenceIn, append_evidence
from app.learner.service import LearnerService
from app.llm import base as llm_base
from app.llm.base import call_with_policy, configure_limits, gate_stats
from app.llm.fake import FakeLLMProvider
from app.llm.schemas import DoubtAnalysis, EvaluationOut
from app.main import create_app
from app.models import AuditEvent, DoubtSession, Message, Topic, User, WorkflowRun, WorkflowStep
from app.ratelimit import SlidingWindow
from app.auth.principal import principal_for
from app.workflow.engine import WorkflowEngine
from app.workflow.recovery import recover_stuck_runs
from tests.conftest import SLOW_START_Q, TEST_DB, ask, login
from tests.test_adaptive_flow import items, solve, submit, to_practice


@pytest.fixture(autouse=True)
def default_gate():
    yield
    configure_limits(4, 20.0)


class Blocking(FakeLLMProvider):
    """Blocks inside chosen operations until released, so the test can inspect the database mid-call."""

    def __init__(self, block_on: set[str]):
        self.block_on, self.entered, self.release = block_on, {op: threading.Event() for op in block_on}, threading.Event()
        self.calls: dict[str, int] = {}

    def _gate(self, op):
        self.calls[op] = self.calls.get(op, 0) + 1
        if op in self.block_on:
            self.entered[op].set()
            assert self.release.wait(30), "test never released the provider"

    def understand(self, req):
        self._gate("understand")
        return super().understand(req)

    def explain(self, req):
        self._gate("explain")
        return super().explain(req)

    def generate_practice(self, req):
        self._gate("practice")
        return super().generate_practice(req)

    def evaluate_answer(self, req):
        self._gate("evaluate")
        return super().evaluate_answer(req)


def raw_conn():
    eng = make_engine(TEST_DB, pooled=False)
    return eng, eng.connect()


def assert_nothing_held(run_filter="true"):
    """With the model call in flight: the run row can be locked by someone else at once, and no connection sits idle in a
    transaction (which would be an application connection waiting on the model)."""
    eng, conn = raw_conn()
    try:
        conn.execute(text(f"SELECT id FROM orch.workflow_runs WHERE {run_filter} FOR UPDATE NOWAIT")).fetchall()   # raises if locked
        idle = conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                 "AND state = 'idle in transaction' AND pid <> pg_backend_pid()")).scalar()
        assert idle == 0, f"{idle} connection(s) idle in transaction while a model call is in flight"
        conn.rollback()
    finally:
        conn.close()
        eng.dispose()


def run_in_thread(fn):
    out = {}

    def go():
        try:
            out["value"] = fn()
        except Exception as e:                          # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=go)
    t.start()
    return t, out


# ----------------------------------------------------------------------------------------------- no lock across model calls
def test_no_row_lock_or_connection_is_held_while_understanding_is_in_flight(settings, student, cn_course_id):
    app = create_app(settings)
    prov = Blocking({"understand"})
    app.state.llm_provider = prov
    client = TestClient(app, raise_server_exceptions=False)
    t, out = run_in_thread(lambda: ask(client, student, cn_course_id, SLOW_START_Q))
    assert prov.entered["understand"].wait(15)
    assert_nothing_held()
    prov.release.set()
    t.join(30)
    r = out["value"]
    assert r.status_code == 202 and r.json()["status"] == "AWAITING_STUDENT"


def test_no_row_lock_or_connection_is_held_while_explaining_or_generating_practice(settings, student, cn_course_id, db):
    app = create_app(settings)
    prov = Blocking({"explain"})
    app.state.llm_provider = prov
    client = TestClient(app, raise_server_exceptions=False)
    prov.release.set()                                        # understand is not blocked; only explain waits
    prov.release.clear()
    t, out = run_in_thread(lambda: ask(client, student, cn_course_id, SLOW_START_Q))
    assert prov.entered["explain"].wait(15)
    assert_nothing_held()
    prov.release.set()
    t.join(30)
    assert out["value"].status_code == 202

    sid = out["value"].json()["session_id"]
    prov2 = Blocking({"practice"})
    app.state.llm_provider = prov2
    from tests.test_ack_evidence import ack
    t2, out2 = run_in_thread(lambda: ack(client, student, sid, "check_me"))
    assert prov2.entered["practice"].wait(15)
    assert_nothing_held()
    prov2.release.set()
    t2.join(30)
    assert out2["value"].status_code == 202 and out2["value"].json()["status"] == "AWAITING_ANSWER"


def test_no_row_lock_or_connection_is_held_while_a_free_text_answer_is_graded(settings, student, cn_course_id, db):
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    sid, rid = to_practice(client, student, cn_course_id)
    short = next(i for i in items(db, sid) if i.kind == "short_text")
    item_id, answer = short.id, solve(short)                 # plain values: the thread must not touch the test's own session
    db.rollback()                                             # and that session must not sit idle in a transaction
    prov = Blocking({"evaluate"})
    app.state.llm_provider = prov
    t, out = run_in_thread(lambda: submit(client, student, sid, item_id, answer))
    assert prov.entered["evaluate"].wait(15)
    assert_nothing_held()
    prov.release.set()
    t.join(30)
    r = out["value"]
    assert r.status_code == 200 and r.json()["scored"] is True


# ----------------------------------------------------------------------------------------------- duplicate execution is prevented
def make_run(db, settings, student_email="student1@demo.local"):
    user = db.scalar(select(User).where(User.email == student_email))
    course = principal_for(db, user)
    cid = next(iter(course.allowed_course_ids))
    s = DoubtSession(student_id=user.id, course_id=cid, status="CREATED")
    db.add(s)
    db.flush()
    prov = FakeLLMProvider()
    eng = WorkflowEngine(db, settings, prov, course)
    eng.core.add_message(s.id, "student", SLOW_START_Q)
    run = eng.create_run(session_id=s.id, student_id=user.id, course_id=cid, doubt_text=SLOW_START_Q, trigger_id=f"t-{uuid.uuid4()}")
    return user, course, s, run


def test_a_second_advance_on_a_run_that_is_mid_call_does_nothing(settings, db):
    user, principal, s, run = make_run(db, settings)
    prov = Blocking({"understand"})
    factory = make_session_factory(make_engine(TEST_DB, pooled=False))

    def first():
        with factory() as d2:
            return WorkflowEngine(d2, settings, prov, principal).advance(run.id)
    t, out = run_in_thread(first)
    assert prov.entered["understand"].wait(15)
    # a concurrent driver (retry, duplicate request, sweeper) arrives while the first is waiting on the model
    again = WorkflowEngine(db, settings, FakeLLMProvider(), principal).advance(run.id)
    assert again.status == "RUNNING"
    assert db.scalar(select(func.count()).select_from(Message).where(Message.session_id == s.id, Message.role == "system")) == 0
    prov.release.set()
    t.join(30)
    db.expire_all()
    assert db.get(WorkflowRun, run.id).status == "AWAITING_STUDENT"
    assert prov.calls["understand"] == 1                          # understood once, not twice (the run then went on to explain)
    assert db.scalar(select(func.count()).select_from(WorkflowStep).where(WorkflowStep.run_id == run.id, WorkflowStep.node == "understand")) == 1


def test_a_result_is_discarded_when_the_run_changed_during_the_model_call(settings, db):
    user, principal, s, run = make_run(db, settings)

    class Takeover(FakeLLMProvider):
        def understand(self, req):
            eng, c = raw_conn()                                   # another executor advances the run meanwhile
            c.execute(text("UPDATE orch.workflow_runs SET version = version + 1 WHERE id = :i"), {"i": run.id})
            c.commit()
            c.close(); eng.dispose()
            return super().understand(req)
    out = WorkflowEngine(db, settings, Takeover(), principal).advance(run.id)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(WorkflowStep).where(WorkflowStep.run_id == run.id)) == 0   # nothing applied
    assert db.scalar(select(func.count()).select_from(Message).where(Message.session_id == s.id, Message.role == "system")) == 0


def test_a_crashed_running_run_is_recovered_once_and_a_healthy_one_is_left_alone(settings, db):
    user, principal, s, run = make_run(db, settings)
    db.execute(text("UPDATE orch.workflow_runs SET status = 'RUNNING' WHERE id = :i"), {"i": run.id})
    db.commit()
    prov = FakeLLMProvider()
    assert recover_stuck_runs(db, settings, prov) == {"stuck": 0, "resumed": 0}          # lease is fresh: someone may be working
    db.execute(text("UPDATE orch.workflow_runs SET updated_at = now() - interval '2 hours' WHERE id = :i"), {"i": run.id})
    db.commit()
    assert recover_stuck_runs(db, settings, prov) == {"stuck": 1, "resumed": 1}
    db.expire_all()
    assert db.get(WorkflowRun, run.id).status == "AWAITING_STUDENT"
    assert recover_stuck_runs(db, settings, prov) == {"stuck": 0, "resumed": 0}          # idempotent
    msgs = db.scalars(select(Message).where(Message.session_id == s.id, Message.role == "system")).all()
    assert len(msgs) == 1                                                                 # exactly one explanation, not duplicated


def test_concurrent_answers_to_different_items_both_succeed_and_the_same_item_scores_once(settings, student, cn_course_id, db):
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    sid, rid = to_practice(client, student, cn_course_id)
    its = items(db, sid)
    shorts = [i for i in its if i.kind == "short_text"]
    assert shorts, "fake provider is expected to include a free-text item"
    results = []
    barrier = threading.Barrier(2)

    def go(item, key):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        results.append(submit(c, student, sid, item.id, solve(item), key=key).status_code)
    ts = [threading.Thread(target=go, args=(shorts[0], "dup-key-a-0001")), threading.Thread(target=go, args=(shorts[0], "dup-key-b-0002"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [200, 409]                                                  # same item twice: exactly one wins
    db.expire_all()
    from app.models import Attempt
    assert db.scalar(select(func.count()).select_from(Attempt).where(Attempt.item_id == shorts[0].id, Attempt.status.in_(("GRADED", "UNCERTAIN")))) == 1


def test_retrying_an_answer_with_the_same_key_after_it_was_scored_replays_without_a_second_attempt(settings, student, cn_course_id, db):
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    sid, rid = to_practice(client, student, cn_course_id)
    it = next(i for i in items(db, sid) if i.kind == "short_text")
    a = submit(client, student, sid, it.id, solve(it), key="retry-key-000001")
    b = submit(client, student, sid, it.id, solve(it), key="retry-key-000001")
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    c = submit(client, student, sid, it.id, "something else entirely", key="retry-key-000001")
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ----------------------------------------------------------------------------------------------- bounded model concurrency
def test_model_calls_never_exceed_the_configured_concurrency():
    configure_limits(2, 5.0)
    live, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def slow():
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        time.sleep(0.15)
        with lock:
            live["n"] -= 1
        return DoubtAnalysis()
    res = []
    ts = [threading.Thread(target=lambda: res.append(call_with_policy(slow, DoubtAnalysis, timeout_s=5, max_retries=0, provider="t")))
          for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak["n"] <= 2 and all(r.value is not None for r in res) and gate_stats()["peak"] <= 2


def test_when_all_slots_are_busy_a_call_fails_fast_as_busy_and_a_timed_out_call_keeps_its_slot():
    configure_limits(1, 0.1)
    done = threading.Event()

    def hang():
        done.wait(5)
        return DoubtAnalysis()
    first = call_with_policy(hang, DoubtAnalysis, timeout_s=0.1, max_retries=0, provider="t")
    assert first.error == "timeout"
    t0 = time.perf_counter()
    second = call_with_policy(lambda: DoubtAnalysis(), DoubtAnalysis, timeout_s=1, max_retries=2, provider="t")
    assert second.error == "busy" and second.attempts == 1 and time.perf_counter() - t0 < 1.0   # the abandoned call still counts
    done.set()
    time.sleep(0.2)
    assert call_with_policy(lambda: DoubtAnalysis(), DoubtAnalysis, timeout_s=1, max_retries=0, provider="t").value is not None


def test_a_busy_model_degrades_to_the_documented_fallback_instead_of_failing(settings, student, cn_course_id):
    app = create_app(settings.model_copy(update={"llm_max_concurrency": 1, "llm_queue_wait_s": 0.05}))
    hold = threading.Event()
    configure_limits(1, 0.05)
    blocker = threading.Thread(target=lambda: call_with_policy(lambda: hold.wait(5) or DoubtAnalysis(), DoubtAnalysis,
                                                               timeout_s=0.05, max_retries=0, provider="t"))
    blocker.start()
    time.sleep(0.2)                                               # the abandoned call now occupies the only slot
    client = TestClient(app, raise_server_exceptions=False)
    configure_limits(1, 0.05)                                     # create_app reset the gate; occupy it again
    blocker2 = threading.Thread(target=lambda: call_with_policy(lambda: hold.wait(5) or DoubtAnalysis(), DoubtAnalysis,
                                                                timeout_s=0.05, max_retries=0, provider="t"))
    blocker2.start()
    time.sleep(0.2)
    r = ask(client, student, cn_course_id, SLOW_START_Q)
    hold.set()
    blocker.join(); blocker2.join()
    assert r.status_code == 202 and r.json()["status"] == "AWAITING_STUDENT"       # understanding fell back to a clarification


# ----------------------------------------------------------------------------------------------- rate limits
def test_sliding_window_is_exact_and_independent_per_key():
    w = SlidingWindow(3, 60)
    assert [w.check("a", now=t) for t in (0, 1, 2)] == [None, None, None]
    assert w.check("a", now=3) == pytest.approx(57)                  # oldest hit leaves the window at t=60
    assert w.check("b", now=3) is None                                # another user is unaffected
    assert w.check("a", now=60.5) is None                             # window slid
    assert SlidingWindow(0, 60).check("a") is None                    # 0 disables


def test_per_user_doubt_limit_returns_429_with_retry_after(settings, student, student2, cn_course_id):
    app = create_app(settings.model_copy(update={"rate_doubts_per_hour": 2}))
    client = TestClient(app, raise_server_exceptions=False)
    ok = [ask(client, student, cn_course_id, SLOW_START_Q).status_code for _ in range(2)]
    r = ask(client, student, cn_course_id, SLOW_START_Q)
    assert ok == [202, 202] and r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["details"]["bucket"] == "doubts"
    assert ask(client, student2, cn_course_id, SLOW_START_Q).status_code == 202            # per user, not global


def test_action_limit_covers_acks_answers_and_messages_and_can_be_disabled(settings, student, cn_course_id):
    app = create_app(settings.model_copy(update={"rate_llm_actions_per_min": 3}))
    client = TestClient(app, raise_server_exceptions=False)
    r = ask(client, student, cn_course_id, SLOW_START_Q)                                    # 1
    sid = r.json()["session_id"]
    from tests.test_ack_evidence import ack
    assert ack(client, student, sid, "check_me").status_code == 202                         # 2
    assert client.post(f"/v1/doubts/{sid}/request-teacher", headers=student).status_code == 202   # 3
    assert client.post(f"/v1/doubts/{sid}/request-teacher", headers=student).status_code == 429
    assert client.get(f"/v1/doubts/{sid}", headers=student).status_code == 200              # reads are never limited
    off = TestClient(create_app(settings.model_copy(update={"rate_llm_actions_per_min": 0, "rate_doubts_per_hour": 0})),
                     raise_server_exceptions=False)
    assert all(ask(off, student, cn_course_id, SLOW_START_Q).status_code == 202 for _ in range(4))


def test_login_is_throttled_after_repeated_failures_and_stores_no_address(settings, db):
    app = create_app(settings.model_copy(update={"login_max_failures": 3}))
    client = TestClient(app, raise_server_exceptions=False)
    bad = {"email": "student2@demo.local", "password": "wrong-password"}
    assert [client.post("/v1/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
    blocked = client.post("/v1/auth/login", json={**bad, "password": "eduos-demo-2026"})      # even the right password is refused
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "RATE_LIMITED" and "Retry-After" in blocked.headers
    assert client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": "eduos-demo-2026"}).status_code == 200
    rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "login_failed")).all()
    assert rows and all(set(r.meta) == {"email_sha"} and "@" not in str(r.meta) for r in rows)


# ----------------------------------------------------------------------------------------------- grading hardening
SHORT = ItemForGrading(kind="short_text", prompt="Why does the window stop doubling?", options=None,
                       answer_key="At ssthresh TCP enters congestion avoidance and grows linearly.",
                       numeric_tolerance=None, rubric="Mentions: ssthresh, congestion avoidance, linear")


class Verdict(FakeLLMProvider):
    def __init__(self, **kw):
        self.kw = kw
        self.calls = 0

    def evaluate_answer(self, req):
        self.calls += 1
        base = dict(correct=True, partial_credit=1.0, feedback="Good.", uncertainty=0.05)
        base.update(self.kw)
        return EvaluationOut(**base)


def test_a_positive_model_verdict_without_the_key_terms_is_not_evidence(settings):
    o = evaluate_answer(Verdict(), settings, SHORT, "The sender slows down because the network is busy and routers drop many packets.")
    assert o.grader_status == "uncertain" and not o.counts_as_evidence and "key ideas" in o.feedback
    ok = evaluate_answer(Verdict(), settings, SHORT, "At ssthresh it enters congestion avoidance and then grows in a linear way")
    assert ok.grader_status == "ok" and ok.counts_as_evidence and "key-term coverage" in ok.evidence


def test_a_self_contradictory_model_verdict_is_not_evidence(settings):
    for kw in (dict(correct=True, partial_credit=0.1), dict(correct=False, partial_credit=0.95)):
        o = evaluate_answer(Verdict(**kw), settings, SHORT, "ssthresh congestion avoidance linear")
        assert o.grader_status == "uncertain" and not o.counts_as_evidence and "disagree" in o.feedback


def test_a_negative_model_verdict_still_counts_and_a_model_cannot_set_its_own_uncertainty_to_force_acceptance(settings):
    wrong = evaluate_answer(Verdict(correct=False, partial_credit=0.0), settings, SHORT, "no idea at all")
    assert wrong.grader_status == "ok" and wrong.counts_as_evidence and wrong.correct is False
    unsure = evaluate_answer(Verdict(uncertainty=0.9), settings, SHORT, "ssthresh congestion avoidance linear")
    assert unsure.grader_status == "uncertain" and not unsure.counts_as_evidence


def test_key_terms_and_lexical_support_are_deterministic():
    assert key_terms("x", "Mentions: ssthresh, congestion avoidance, linear") == ["ssthresh", "congestion avoidance", "linear"]
    assert "ssthresh" in key_terms("At ssthresh TCP enters congestion avoidance", "describes the switch")
    assert lexical_support("the sender grows linearly after ssthresh", ["ssthresh", "linear"]) == 1.0
    assert lexical_support("nothing relevant", ["ssthresh", "linear"]) == 0.0
    assert lexical_support("anything", []) == 1.0


def test_model_graded_evidence_alone_can_never_demonstrate_mastery():
    p = m.MasteryParams(min_trusted_positive=1)
    post = m.replay([m.Event(f"e{i}", "attempt_correct", 0.5, f"item{i}", datetime(2026, 1, 1, tzinfo=timezone.utc)) for i in range(1, 8)], p)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert m.mean(post.alpha, post.beta) >= p.t_master                              # the estimate is high ...
    assert m.status(post, now, p, False, trusted_positive=0) == "emerging"          # ... but it is not demonstrated
    assert m.status(post, now, p, False, trusted_positive=1) == "demonstrated"
    assert m.status(post, now, p, False) == "demonstrated"                          # callers that do not supply the count are unchanged


def test_in_the_ledger_only_exact_grading_and_teachers_count_as_trusted(settings, db):
    student = db.scalar(select(User).where(User.email == "student1@demo.local"))
    topic = db.scalar(select(Topic).where(Topic.slug == "tcp-congestion"))
    svc = LearnerService(db, settings)
    run_id = uuid.uuid4()

    def add(i, grader, kind="attempt_correct"):
        prov = {"source": "practice_attempt", "schema_version": 1, "session_id": str(uuid.uuid4()), "run_id": str(run_id),
                "attempt_id": str(uuid.uuid4()), "item_id": str(uuid.uuid4()), "grader": grader}
        row = append_evidence(db, EvidenceIn(student_id=student.id, course_id=topic.course_id, topic_id=topic.id, evidence_type=kind,
                                             weight=0.5 if grader == "llm_rubric" else 1.0, source_run_id=run_id,
                                             source_ref=f"t:{i}:{uuid.uuid4()}", provenance=prov))
        db.flush()
        svc.apply_row(row)
    for i in range(4):
        add(i, "llm_rubric")
    v = svc.view(student.id, topic.id)
    assert v["mean"] >= settings.t_master and v["evidence_count"] >= 3 and v["status"] == "emerging"      # only the model vouches
    assert svc.trusted_positive(student.id, topic.id) == 0
    add(9, "exact")
    assert svc.trusted_positive(student.id, topic.id) == 1 and svc.view(student.id, topic.id)["status"] == "demonstrated"
    db.rollback()


# ----------------------------------------------------------------------------------------------- audit integrity
def test_audit_events_are_append_only(db):
    ev = AuditEvent(actor_id=None, action="phase1_test", entity="test", entity_id="1", meta={"k": 1})
    db.add(ev)
    db.commit()                                                                     # inserts are fine
    for sql in ("UPDATE core.audit_events SET action = 'tampered' WHERE id = :i",
                "UPDATE core.audit_events SET meta = '{}'::jsonb WHERE id = :i",
                "UPDATE core.audit_events SET actor_id = NULL WHERE id = :i",       # allowed only inside an anonymization transaction
                "DELETE FROM core.audit_events WHERE id = :i"):
        with pytest.raises((DBAPIError, OperationalError)) as e:
            db.execute(text(sql), {"i": ev.id})
            db.commit()
        db.rollback()
        assert "append-only" in str(e.value)
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.id == ev.id)) == 1


def test_anonymization_may_clear_actor_ids_but_nothing_else(client, student, admin, cn_course_id, db):
    me = client.get("/v1/me", headers=student).json()["id"]
    ask(client, student, cn_course_id, SLOW_START_Q)
    db.add(AuditEvent(actor_id=uuid.UUID(me), action="phase1_actor", entity="test", entity_id="2", meta={"k": 2}))
    db.commit()
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.actor_id == uuid.UUID(me))) >= 1
    r = client.post(f"/v1/admin/students/{me}/anonymize", headers=admin, json={"confirm": True})
    assert r.status_code == 200
    db.expire_all()
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "phase1_actor"))
    assert row.actor_id is None and row.meta == {"k": 2} and row.entity_id == "2"       # only the actor reference was cleared
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.actor_id == uuid.UUID(me))) == 0
    from app.seed import seed
    seed(db, client.app.state.settings)                                                  # restore the demo account for later tests


def test_cors_allows_put_because_availability_and_document_replacement_use_it(client):
    r = client.options("/v1/teacher/availability", headers={"Origin": "http://localhost:5173",
                                                           "Access-Control-Request-Method": "PUT"})
    assert r.status_code == 200 and "PUT" in r.headers["access-control-allow-methods"]
