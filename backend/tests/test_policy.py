"""Table-driven tests for the pure policy engine (docs/ARCHITECTURE.md section 8.2)."""
import random

import pytest

from app.workflow.policy import (
    Budgets, CheckResult, Counters, Flags, MasterySummary, PolicyInput, RetrievalSupport, Thresholds, decide,
)

BUDGETS = Budgets(max_actions=6, max_clarify_rounds=2, max_explain_attempts=2, max_practice_sets=2)
TH = Thresholds(t_clarify=0.5, ret_min_chunks=1, t_master=0.8, n_min=3)


def inp(**kw) -> PolicyInput:
    base = dict(clarity="clear", classification_confidence=0.9,
                retrieval=RetrievalSupport(available=True, n_chunks_above_threshold=3, max_matched_terms=4,
                                           lexical_hits=3),
                mastery=MasterySummary(), counters=Counters(), budgets=BUDGETS, flags=Flags(), thresholds=TH)
    base.update(kw)
    return PolicyInput(**base)


CASES = [
    # (id, input, expected rule, expected action)
    ("R1", inp(flags=Flags(explicit_teacher_request=True)), "R1_explicit_teacher_request", "ESCALATE_TO_TEACHER"),
    ("R1b", inp(flags=Flags(safety_flag=True)), "R1b_safety_flag", "ESCALATE_TO_TEACHER"),
    ("R2", inp(counters=Counters(actions_used=6)), "R2_budget_exhausted", "ESCALATE_TO_TEACHER"),
    ("R2 below", inp(counters=Counters(actions_used=5)), "R6_low_evidence_explain", "GENERATE_EXPLANATION"),
    ("R3 ambiguous", inp(clarity="ambiguous"), "R3_needs_clarification", "ASK_CLARIFICATION"),
    ("R3 off topic", inp(clarity="off_topic"), "R3_needs_clarification", "ASK_CLARIFICATION"),
    ("R3 low conf", inp(classification_confidence=0.49), "R3_needs_clarification", "ASK_CLARIFICATION"),
    ("R3 boundary conf", inp(classification_confidence=0.5), "R6_low_evidence_explain", "GENERATE_EXPLANATION"),
    ("R3 exhausted -> R4", inp(clarity="ambiguous", counters=Counters(clarify_rounds=2),
                                retrieval=RetrievalSupport(available=True, n_chunks_above_threshold=0)),
     "R4_no_grounding", "ESCALATE_TO_TEACHER"),
    ("R4 none", inp(retrieval=RetrievalSupport(available=True, n_chunks_above_threshold=0)),
     "R4_no_grounding", "ESCALATE_TO_TEACHER"),
    ("R4 unavailable", inp(retrieval=RetrievalSupport(available=False)), "R4_no_grounding", "ESCALATE_TO_TEACHER"),
    ("R5 failed checks", inp(counters=Counters(failed_checks=2)), "R5_repeated_failure", "ESCALATE_TO_TEACHER"),
    ("R5 repeated error", inp(mastery=MasterySummary(repeated_error_tag_max=3)), "R5_repeated_failure",
     "ESCALATE_TO_TEACHER"),
    ("R6 unknown", inp(), "R6_low_evidence_explain", "GENERATE_EXPLANATION"),
    ("R6 hypothesis", inp(mastery=MasterySummary(status="hypothesis")), "R6_low_evidence_explain",
     "GENERATE_EXPLANATION"),
    ("R6 emerging low", inp(mastery=MasterySummary(status="emerging", mean=0.5, evidence_count=2)),
     "R6_low_evidence_explain", "GENERATE_EXPLANATION"),
    ("R6 still confused", inp(explained=True, acked="still_confused", counters=Counters(explain_attempts=1)),
     "R6_low_evidence_explain", "GENERATE_EXPLANATION"),
    ("R7", inp(explained=True, acked="understood", counters=Counters(explain_attempts=1)),
     "R7_check_after_explanation", "GENERATE_PRACTICE"),
    ("R7 check_me", inp(explained=True, acked="check_me", counters=Counters(explain_attempts=1)),
     "R7_check_after_explanation", "GENERATE_PRACTICE"),
    ("R8", inp(explained=True, acked="understood", last_check=CheckResult(passed=True),
               mastery=MasterySummary(status="demonstrated", mean=0.9, evidence_count=4, distinct_items=3),
               counters=Counters(explain_attempts=1, practice_sets=1)),
     "R8_mastery_demonstrated", "COMPLETE"),
    ("R9", inp(explained=True, acked="understood", last_check=CheckResult(passed=False),
               mastery=MasterySummary(status="emerging", mean=0.4, evidence_count=1),
               counters=Counters(explain_attempts=1, practice_sets=1, failed_checks=1)),
     "R9_retry_explain_new_angle", "GENERATE_EXPLANATION"),
    ("R10 default", inp(explained=True, acked="none", counters=Counters(explain_attempts=1)),
     "R10_default_clarify", "ASK_CLARIFICATION"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_rule_table(case):
    _, i, rule, action = case
    d = decide(i)
    assert (d.rule_id, d.action) == (rule, action)
    assert d.reasons and d.schema_version == "1"


def test_r6_does_not_apply_when_mastery_demonstrated_and_not_forced():
    """R6 must not be forced: with demonstrated mastery and nothing else to do, the documented fallback is R10."""
    d = decide(inp(mastery=MasterySummary(status="demonstrated", mean=0.9, evidence_count=5, distinct_items=3)))
    assert d.rule_id == "R10_default_clarify"


def test_precedence_hard_rules_beat_soft_rules():
    d = decide(inp(flags=Flags(explicit_teacher_request=True, safety_flag=True), clarity="ambiguous",
                   retrieval=RetrievalSupport(available=False)))
    assert d.rule_id == "R1_explicit_teacher_request"
    assert decide(inp(flags=Flags(safety_flag=True), counters=Counters(actions_used=99))).rule_id == "R1b_safety_flag"
    assert decide(inp(counters=Counters(actions_used=6), clarity="ambiguous")).rule_id == "R2_budget_exhausted"


def test_teacher_unavailable_ends_unresolved_instead_of_looping():
    for kw in (dict(counters=Counters(actions_used=6)), dict(retrieval=RetrievalSupport(available=False)),
               dict(counters=Counters(failed_checks=2))):
        d = decide(inp(flags=Flags(teacher_unavailable=True), **kw))
        assert d.action == "COMPLETE" and d.outcome == "UNRESOLVED"


def test_decide_is_deterministic_and_pure():
    i = inp(clarity="ambiguous")
    assert decide(i) == decide(i.model_copy(deep=True))
    before = i.model_dump()
    decide(i)
    assert i.model_dump() == before


def test_zero_budget_escalates_immediately():
    i = inp(budgets=Budgets(max_actions=0, max_clarify_rounds=2, max_explain_attempts=2, max_practice_sets=2))
    assert decide(i).rule_id == "R2_budget_exhausted"


def test_termination_property_every_simulated_run_ends_or_waits_for_a_human():
    """Simulate action effects on counters; the loop must reach ESCALATE/COMPLETE within max_actions + 1 decisions."""
    rng = random.Random(7)
    for _ in range(500):
        b = Budgets(max_actions=rng.randint(0, 8), max_clarify_rounds=rng.randint(0, 3),
                    max_explain_attempts=rng.randint(0, 3), max_practice_sets=rng.randint(0, 3))
        c = Counters()
        explained, acked, last = False, "none", None
        clarity = rng.choice(["clear", "ambiguous", "off_topic"])
        n_ret = rng.choice([0, 0, 1, 3])
        for step in range(b.max_actions + 3):
            i = inp(budgets=b, counters=c.model_copy(), clarity=clarity, explained=explained, acked=acked,
                    last_check=last, retrieval=RetrievalSupport(available=True, n_chunks_above_threshold=n_ret))
            d = decide(i)
            if d.action in ("ESCALATE_TO_TEACHER", "COMPLETE"):
                break
            c.actions_used += 1
            if d.action == "ASK_CLARIFICATION":
                c.clarify_rounds += 1
                clarity = rng.choice(["clear", "ambiguous"])
            elif d.action == "GENERATE_EXPLANATION":
                c.explain_attempts += 1
                explained, acked = True, rng.choice(["understood", "still_confused", "check_me", "none"])
                last = None if last is None else last
            elif d.action == "GENERATE_PRACTICE":
                c.practice_sets += 1
                passed = rng.random() < 0.3
                last = CheckResult(passed=passed)
                c.failed_checks += 0 if passed else 1
        else:
            pytest.fail(f"did not terminate: budgets={b} counters={c}")
        assert c.actions_used <= b.max_actions
