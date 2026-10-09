"""Deterministic intervention policy: ordered rules R1-R10 from docs/ARCHITECTURE.md section 8.2.
`decide` is a pure function: no I/O, no clock, no randomness, no model calls."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["ASK_CLARIFICATION", "GENERATE_EXPLANATION", "GENERATE_PRACTICE",
                 "ESCALATE_TO_TEACHER", "COMPLETE"]
RULE_IDS = ["R1_explicit_teacher_request", "R1b_safety_flag", "R2_budget_exhausted", "R3_needs_clarification",
            "R4_no_grounding", "R5_repeated_failure", "R6_low_evidence_explain", "R7_check_after_explanation",
            "R8_mastery_demonstrated", "R9_retry_explain_new_angle", "R10_default_clarify"]


class P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Counters(P):
    actions_used: int = 0
    clarify_rounds: int = 0
    explain_attempts: int = 0
    practice_sets: int = 0
    failed_checks: int = 0
    llm_calls: int = 0


class Budgets(P):
    max_actions: int
    max_clarify_rounds: int
    max_explain_attempts: int
    max_practice_sets: int


class Thresholds(P):
    t_clarify: float
    ret_min_chunks: int
    t_master: float
    n_min: int


class Flags(P):
    explicit_teacher_request: bool = False
    safety_flag: bool = False
    teacher_unavailable: bool = False


class RetrievalSupport(P):
    available: bool
    n_chunks_above_threshold: int = 0
    max_matched_terms: int = 0      # lexical support (full-text search)
    max_dense_similarity: float = 0.0   # semantic support (0.0 when dense retrieval did not run)
    lexical_hits: int = 0


class MasterySummary(P):
    status: Literal["unknown", "hypothesis", "emerging", "demonstrated"] = "unknown"
    mean: float | None = None
    evidence_count: int = 0
    distinct_items: int = 0
    repeated_error_tag_max: int = 0


class CheckResult(P):
    passed: bool


class PolicyInput(P):
    schema_version: Literal["1"] = "1"
    clarity: Literal["clear", "ambiguous", "off_topic"]
    classification_confidence: float
    retrieval: RetrievalSupport
    mastery: MasterySummary
    counters: Counters
    budgets: Budgets
    flags: Flags
    explained: bool = False
    acked: Literal["none", "understood", "still_confused", "check_me"] = "none"
    last_check: CheckResult | None = None
    thresholds: Thresholds


class Decision(P):
    schema_version: Literal["1"] = "1"
    action: Action
    rule_id: str
    outcome: Literal["RESOLVED", "UNRESOLVED"] | None = None   # only with COMPLETE
    reasons: list[str] = Field(default_factory=list)


def _escalate_or_unresolved(rule_id: str, i: PolicyInput, reason: str) -> Decision:
    if i.flags.teacher_unavailable:
        return Decision(action="COMPLETE", rule_id=rule_id, outcome="UNRESOLVED",
                        reasons=[reason, "no teacher available, ending as unresolved"])
    return Decision(action="ESCALATE_TO_TEACHER", rule_id=rule_id, reasons=[reason])


def decide(i: PolicyInput) -> Decision:
    c, b, t, m = i.counters, i.budgets, i.thresholds, i.mastery

    if i.flags.explicit_teacher_request:                                                    # R1
        return Decision(action="ESCALATE_TO_TEACHER", rule_id="R1_explicit_teacher_request",
                        reasons=["student explicitly asked for a teacher"])
    if i.flags.safety_flag:                                                                 # R1b
        return Decision(action="ESCALATE_TO_TEACHER", rule_id="R1b_safety_flag",
                        reasons=["content flagged for human review"])
    if c.actions_used >= b.max_actions:                                                     # R2
        return _escalate_or_unresolved("R2_budget_exhausted", i,
                                       f"action budget exhausted ({c.actions_used}/{b.max_actions})")
    unclear = i.clarity != "clear" or i.classification_confidence < t.t_clarify
    if unclear and c.clarify_rounds < b.max_clarify_rounds:                                 # R3
        return Decision(action="ASK_CLARIFICATION", rule_id="R3_needs_clarification", reasons=[
            f"clarity={i.clarity}, classification_confidence={i.classification_confidence:.2f} "
            f"(threshold {t.t_clarify}), clarify round {c.clarify_rounds + 1}/{b.max_clarify_rounds}"])
    if not i.retrieval.available or i.retrieval.n_chunks_above_threshold < t.ret_min_chunks:  # R4
        return _escalate_or_unresolved("R4_no_grounding", i, (
            "retrieval unavailable" if not i.retrieval.available else
            f"only {i.retrieval.n_chunks_above_threshold} passage(s) above threshold, need {t.ret_min_chunks}"))
    if c.failed_checks >= 2 or m.repeated_error_tag_max >= 3:                               # R5
        return _escalate_or_unresolved("R5_repeated_failure", i, (
            f"failed_checks={c.failed_checks}, repeated_error_tag_max={m.repeated_error_tag_max}"))
    low = m.status in ("unknown", "hypothesis") or (
        m.status == "emerging" and (m.mean is None or m.mean < t.t_master))
    if c.explain_attempts < b.max_explain_attempts and (
            (not i.explained and low) or i.acked == "still_confused"):                      # R6
        return Decision(action="GENERATE_EXPLANATION", rule_id="R6_low_evidence_explain", reasons=[
            f"mastery status={m.status} (evidence_count={m.evidence_count}), "
            f"explain attempt {c.explain_attempts + 1}/{b.max_explain_attempts}"])
    if (i.explained and i.acked in ("understood", "check_me") and i.last_check is None
            and c.practice_sets < b.max_practice_sets):                                     # R7
        return Decision(action="GENERATE_PRACTICE", rule_id="R7_check_after_explanation",
                        reasons=["explanation delivered and acknowledged; a check is needed for evidence"])
    if i.last_check is not None and i.last_check.passed and m.status == "demonstrated":     # R8
        return Decision(action="COMPLETE", rule_id="R8_mastery_demonstrated", outcome="RESOLVED",
                        reasons=["check passed and mastery demonstrated from evidence"])
    if (i.last_check is not None and not i.last_check.passed
            and c.explain_attempts < b.max_explain_attempts):                               # R9
        return Decision(action="GENERATE_EXPLANATION", rule_id="R9_retry_explain_new_angle",
                        reasons=["check failed once; retry explanation from a new angle"])
    return Decision(action="ASK_CLARIFICATION", rule_id="R10_default_clarify",               # R10
                    reasons=["no rule matched; defaulting to clarification"])
