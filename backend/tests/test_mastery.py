"""Mastery model, evidence-to-mastery rules, hypotheses (suspected vs confirmed) and the progress history. Deterministic:
time is injected, never read from the clock, so half-life behaviour is asserted exactly."""
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.agents.evaluation import EvaluationOutcome
from app.learner import mastery as m
from app.learner.ledger import EvidenceIn, append_evidence
from app.learner.service import LearnerService
from app.llm.schemas import GapHypothesisDraft
from app.models import (
    Attempt, Course, DoubtSession, EvidenceEvent, GapHypothesis, LearnerTopicState, MasteryHistory, PracticeItem, Topic, User,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
P = m.MasteryParams()


def ev(i, kind, w=1.0, key=None, days=0.0):
    return m.Event(f"e{i}", kind, w, key or f"item{i}", T0 + timedelta(days=days))


# ----------------------------------------------------------------------------------------------- the pure model
def test_prior_status_and_mean():
    post = m.Posterior.prior(P)
    assert m.mean(post.alpha, post.beta) == 0.5
    assert m.status(post, T0, P, False) == "unknown" and m.status(post, T0, P, True) == "hypothesis"


def test_one_correct_answer_never_demonstrates_mastery():
    post = m.apply(m.Posterior.prior(P), ev(1, "attempt_correct"), P)
    assert post.evidence_count == 1 and m.mean(post.alpha, post.beta) > P.t_master - 0.1
    assert m.status(post, T0, P, False) == "emerging"
    big = m.apply(m.Posterior.prior(P), ev(1, "attempt_correct", w=1.0), P)
    assert m.status(big, T0, P, False) == "emerging"


def test_demonstrated_needs_mean_count_and_distinct_sources():
    a = m.replay([ev(i, "attempt_correct", key="same-item") for i in range(1, 5)], P)       # four correct, but ONE item
    assert a.evidence_count == 4 and len(a.sources) == 1 and m.status(a, T0, P, False) == "emerging"
    b = m.replay([ev(1, "attempt_correct"), ev(2, "attempt_correct")], P)                    # two distinct but count < N_MIN
    assert m.status(b, T0, P, False) == "emerging"
    c = m.replay([ev(i, "attempt_correct") for i in range(1, 4)], P)                         # 3 correct on 3 items
    assert m.status(c, T0, P, False) == "demonstrated"
    d = m.replay([ev(1, "attempt_correct"), ev(2, "attempt_correct"), ev(3, "attempt_correct"), ev(4, "attempt_incorrect"),
                  ev(5, "attempt_incorrect")], P)
    assert m.status(d, T0, P, False) == "emerging"                                           # failures pull the mean below T_MASTER


def test_acknowledgments_and_informational_types_have_no_effect():
    base = m.replay([ev(1, "attempt_correct")], P)
    for t in ("self_report_understood", "self_report_confused", "check_requested", "teacher_assessment_emerging", "made_up"):
        assert m.apply(base, ev(9, t, w=1.0), P) is base                                    # ignored, even if someone passes a weight


def test_half_life_decay_is_exact_and_relaxes_to_the_prior():
    post = m.replay([ev(i, "attempt_correct") for i in range(1, 4)], P)
    a0, _ = m.decayed(post, T0, P)
    a14, b14 = m.decayed(post, T0 + timedelta(days=14), P)
    assert a14 - P.alpha0 == pytest.approx((post.alpha - P.alpha0) / 2)                      # one half-life halves the excess
    assert b14 == pytest.approx(P.beta0)
    assert m.status(post, T0 + timedelta(days=200), P, False) == "emerging"                  # forgetting: no longer demonstrated
    far_a, far_b = m.decayed(post, T0 + timedelta(days=10_000), P)
    assert far_a == pytest.approx(P.alpha0, abs=1e-6) and m.mean(far_a, far_b) == pytest.approx(0.5, abs=1e-6)
    assert m.decayed(post, T0 - timedelta(days=5), P) == (post.alpha, post.beta)             # no negative time


def test_replay_is_order_independent_and_equals_incremental_application():
    rng = random.Random(3)
    events = [ev(i, rng.choice(["attempt_correct", "attempt_incorrect"]), w=rng.choice([0.5, 0.75, 1.0]), days=rng.random() * 40)
              for i in range(40)]
    inc = m.Posterior.prior(P)
    for e in sorted(events, key=lambda e: (e.at, e.evidence_id)):
        inc = m.apply(inc, e, P)
    shuffled = list(events)
    rng.shuffle(shuffled)
    rep = m.replay(shuffled, P)
    assert (rep.alpha, rep.beta, rep.evidence_count, rep.sources) == (inc.alpha, inc.beta, inc.evidence_count, inc.sources)


def test_attempt_weights_are_bounded_and_ordered():
    w = lambda g, d, h: m.attempt_weight(g, d, h, P)               # noqa: E731
    assert w("exact", "medium", 0) == 1.0 and w("llm_rubric", "medium", 0) == 0.5 < w("exact", "medium", 0)
    assert w("exact", "easy", 0) == 0.75 and w("exact", "medium", 1) == 0.5 and w("exact", "medium", 3) == 0.25
    assert all(0 < w(g, d, h) <= 1 for g in ("exact", "llm_rubric") for d in ("easy", "medium", "hard") for h in range(6))


# ----------------------------------------------------------------------------------------------- database-backed service
@pytest.fixture
def world(db, settings):
    student = db.scalar(select(User).where(User.email == "student1@demo.local"))
    course = db.scalar(select(Course).where(Course.code == "CN101"))
    topic = db.scalar(select(Topic).where(Topic.course_id == course.id, Topic.slug == "tcp-congestion"))
    sess = DoubtSession(student_id=student.id, course_id=course.id, status="CREATED", run_id=uuid.uuid4())
    db.add(sess)
    db.flush()
    return type("W", (), {"db": db, "svc": LearnerService(db, settings), "student": student, "course": course, "topic": topic,
                          "sess": sess, "n": 0})


def attempt(w, *, correct=True, grader="exact", hints=0, difficulty="medium", hyp=None, item=None, at=None, status="GRADED", unc=0.0):
    w.n += 1
    if item is None:
        item = PracticeItem(student_id=w.student.id, session_id=w.sess.id, run_id=w.sess.run_id, topic_id=w.topic.id, set_index=1,
                            position=w.n, kind="mcq", prompt=f"Question number {w.n}?", options=["a", "b", "c"], answer_key="a",
                            difficulty=difficulty, source_chunk_ids=[], distractor_tags={}, targets_hypothesis_id=hyp.id if hyp else None,
                            source="model", prompt_hash=f"h{w.n}-{uuid.uuid4().hex[:8]}")
        w.db.add(item)
        w.db.flush()
    a = Attempt(item_id=item.id, student_id=w.student.id, session_id=w.sess.id, run_id=w.sess.run_id, answer="a", hints_used=hints,
                idempotency_key=f"k-{uuid.uuid4().hex}", status=status, correct=correct)
    w.db.add(a)
    w.db.flush()
    out = EvaluationOutcome(correct, 1.0 if correct else 0.0, "fb", [] if correct else ["wrong_option"], "", unc, grader,
                            "ok" if status == "GRADED" else "uncertain")
    row = w.svc.record_graded_attempt(attempt=a, item=item, outcome=out, course_id=w.course.id, at=at)   # `at`: tests only
    return a, item, row


def test_single_correct_attempt_makes_the_topic_emerging_not_demonstrated(world):
    a, it, row = attempt(world)
    v = world.svc.view(world.student.id, world.topic.id)
    assert v["status"] == "emerging" and v["evidence_count"] == 1 and row.evidence_type == "attempt_correct" and row.weight == 1.0


def test_three_distinct_correct_items_demonstrate_mastery_and_two_do_not(world):
    attempt(world)
    attempt(world)
    assert world.svc.view(world.student.id, world.topic.id)["status"] == "emerging"
    attempt(world)
    v = world.svc.view(world.student.id, world.topic.id)
    assert v["status"] == "demonstrated" and v["distinct_sources"] == 3 and v["mean"] >= 0.75
    s = world.svc.summary(world.student.id, world.topic.id)
    assert s.status == "demonstrated" and s.distinct_items == 3 and s.evidence_count == 3


def test_recording_the_same_attempt_twice_never_duplicates_evidence(world, db):
    a, it, row = attempt(world)
    again = world.svc.record_graded_attempt(attempt=a, item=it, outcome=EvaluationOutcome(True, 1.0, "", [], "", 0.0, "exact", "ok"),
                                            course_id=world.course.id)
    assert again.id == row.id
    assert db.scalar(select(func.count()).select_from(EvidenceEvent).where(EvidenceEvent.source_ref == f"attempt:{a.id}")) == 1
    assert db.scalar(select(func.count()).select_from(MasteryHistory).where(MasteryHistory.student_id == world.student.id)) == 1
    assert world.svc.apply_row(row) is None                                  # applying the same ledger row again is a no-op


def test_uncertain_or_unavailable_grades_do_not_touch_mastery(world):
    a, it, row = attempt(world, grader="llm_rubric", status="UNCERTAIN", unc=0.9)
    assert row is None and a.evidence_event_id is None
    assert world.svc.view(world.student.id, world.topic.id)["evidence_count"] == 0


def test_model_graded_evidence_counts_for_less(world):
    _, _, exact = attempt(world)
    _, _, llm = attempt(world, grader="llm_rubric", unc=0.1)
    assert llm.weight == 0.5 < exact.weight
    assert llm.provenance["grader"] == "llm_rubric" and llm.provenance["uncertainty"] == 0.1


def test_incremental_state_equals_a_replay_of_the_ledger(world, db):
    for i, ok in enumerate([True, True, False, True, False, True]):
        attempt(world, correct=ok, at=T0 + timedelta(days=i * 3))
    db.flush()
    row = db.get(LearnerTopicState, (world.student.id, world.topic.id))
    rep = world.svc.rebuild(world.student.id, world.topic.id)
    assert (row.alpha, row.beta, row.evidence_count, row.sources) == pytest.approx((rep.alpha, rep.beta, rep.evidence_count, rep.sources)) \
        or (round(row.alpha, 9), round(row.beta, 9), row.evidence_count, row.sources) == (round(rep.alpha, 9), round(rep.beta, 9), rep.evidence_count, rep.sources)


def test_a_single_teacher_assessment_cannot_demonstrate_mastery_but_adds_to_other_evidence(world, db):
    def teacher(kind, level_w):
        row = append_evidence(db, EvidenceIn(student_id=world.student.id, course_id=world.course.id, topic_id=world.topic.id,
                                             evidence_type=kind, weight=level_w, source_run_id=world.sess.run_id,
                                             source_ref=f"teacher:{uuid.uuid4()}:x",
                                             provenance={"source": "teacher_feedback", "session_id": "s", "run_id": str(world.sess.run_id),
                                                         "escalation_id": str(uuid.uuid4()), "teacher_id": "t"}))
        db.flush()
        world.svc.apply_row(row)
    teacher("teacher_assessment_solid", 2.0)
    v = world.svc.view(world.student.id, world.topic.id)
    assert v["status"] == "emerging" and v["evidence_count"] == 1 and v["mean"] >= 0.75      # strong, but one assessment is not enough
    attempt(world)
    attempt(world)
    assert world.svc.view(world.student.id, world.topic.id)["status"] == "demonstrated"


def test_mastery_decays_over_time_in_the_service_view(world):
    for i in range(3):
        attempt(world, at=T0 + timedelta(minutes=i))
    assert world.svc.view(world.student.id, world.topic.id, now=T0 + timedelta(hours=1))["status"] == "demonstrated"
    assert world.svc.view(world.student.id, world.topic.id, now=T0 + timedelta(days=300))["status"] == "emerging"


def test_progress_history_traces_every_update_back_to_its_attempt(world, db):
    attempts = [attempt(world)[0] for _ in range(3)]
    hist = world.svc.history(world.student.id)
    assert len(hist) == 3 and {h["evidence"]["attempt_id"] for h in hist} == {str(a.id) for a in attempts}
    assert all(h["evidence"]["type"] == "attempt_correct" and h["evidence"]["item_id"] and h["evidence"]["grader"] == "exact" for h in hist)
    assert [h["status"] for h in reversed(hist)] == ["emerging", "emerging", "demonstrated"]       # newest first
    prog = world.svc.progress(world.student.id)
    assert prog[0]["status"] == "demonstrated" and prog[0]["topic"] == world.topic.name


# ----------------------------------------------------------------------------------------------- hypotheses
def propose(world, text="May confuse ssthresh with cwnd"):
    return world.svc.propose_hypotheses(world.student.id, world.sess.run_id, [GapHypothesisDraft(topic_id=str(world.topic.id), description=text)])[0]


def test_suspected_gaps_are_hypotheses_with_no_weight_and_are_not_duplicated(world, db):
    h = propose(world)
    assert h.status == "proposed" and propose(world).id == h.id                               # deduplicated
    assert world.svc.view(world.student.id, world.topic.id)["status"] == "hypothesis"
    assert world.svc.view(world.student.id, world.topic.id)["evidence_count"] == 0            # a suspicion is not evidence
    prog = world.svc.progress(world.student.id)[0]
    assert prog["hypotheses"][0]["kind"] == "suspected"
    h.status = "refuted"
    db.flush()
    assert propose(world).status == "refuted"                                                 # a settled hypothesis is never re-proposed


def test_a_gap_is_confirmed_only_after_two_failures_on_distinct_targeted_items(world):
    h = propose(world)
    attempt(world, correct=False, hyp=h)
    assert h.status == "proposed"                                                             # one failure: still only suspected
    attempt(world, correct=False)                                                             # failure on an UNTARGETED item does not count
    assert h.status == "proposed"
    attempt(world, correct=False, hyp=h)
    assert h.status == "confirmed" and h.resolution["source"] == "attempts" and len(h.resolution["evidence_ids"]) == 2
    assert world.svc.progress(world.student.id)[0]["hypotheses"][0]["kind"] == "confirmed"


def test_failing_the_same_item_cannot_confirm_a_gap(world):
    h = propose(world)
    _, it, _ = attempt(world, correct=False, hyp=h)
    # a second scored attempt on the SAME item is blocked by the database; even if it existed it would not be a distinct item
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        attempt(world, correct=False, item=it)
        world.db.flush()
    world.db.rollback()


def test_a_gap_is_refuted_by_two_correct_answers_without_hints_only(world):
    h = propose(world)
    attempt(world, correct=True, hyp=h, hints=2)
    attempt(world, correct=True, hyp=h, hints=1)
    assert h.status == "proposed"                                                             # hinted successes do not refute
    attempt(world, correct=True, hyp=h)
    assert h.status == "proposed"
    attempt(world, correct=True, hyp=h)
    assert h.status == "refuted" and h.resolution["rule"].startswith(">=2 correct")


def test_a_confirmed_gap_is_not_silently_undone_by_later_success(world):
    h = propose(world)
    attempt(world, correct=False, hyp=h)
    attempt(world, correct=False, hyp=h)
    assert h.status == "confirmed"
    attempt(world, correct=True, hyp=h)
    attempt(world, correct=True, hyp=h)
    assert h.status == "confirmed"                                                            # only a teacher can overturn it


def test_teacher_decisions_confirm_or_refute_with_a_history(world):
    h = propose(world)
    teacher = world.db.scalar(select(User).where(User.email == "teacher1@demo.local"))
    esc = uuid.uuid4()
    world.svc.teacher_decide(h.id, "confirmed", teacher.id, esc)
    assert h.status == "confirmed" and h.resolution["source"] == "teacher"
    world.svc.teacher_decide(h.id, "refuted", teacher.id, esc)
    hist = h.resolution["history"]
    assert h.status == "refuted" and [x["decision"] for x in hist] == ["confirmed", "refuted"] and hist[1]["previous_status"] == "confirmed"
    with pytest.raises(ValueError):
        world.svc.teacher_decide(h.id, "maybe", teacher.id, esc)


def test_stale_suspicions_expire_but_recent_ones_do_not(world, db):
    old, fresh = propose(world, "old suspicion"), propose(world, "fresh suspicion")
    days = world.svc.s.hypothesis_expiry_days
    old.created_at, fresh.created_at = T0, T0 + timedelta(days=days)
    db.flush()
    assert world.svc.expire_stale(now=T0 + timedelta(days=days + 1)) == 1
    assert old.status == "expired" and old.resolution["source"] == "expiry" and fresh.status == "proposed"
