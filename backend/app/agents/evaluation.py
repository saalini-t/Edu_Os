"""Answer Evaluation Agent. Objective items (mcq, numeric) are graded DETERMINISTICALLY against the saved key. Only
short free text uses the model, with the saved rubric and reference answer. A model verdict is accepted as evidence only
when it validates AND the model's own uncertainty is below a threshold; the verdict never sets mastery by itself
(evidence weights and the mastery model decide that, and LLM-graded evidence is weighted lower)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.common import AgentRun, call_provider
from app.agents.practice import parse_number
from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.schemas import EvaluateRequest, EvaluationOut


@dataclass
class ItemForGrading:
    kind: str
    prompt: str
    options: list[str] | None
    answer_key: str
    numeric_tolerance: float | None
    rubric: str | None
    distractor_tags: dict[str, str] = field(default_factory=dict)


@dataclass
class EvaluationOutcome:
    correct: bool | None
    partial_credit: float | None
    feedback: str
    error_tags: list[str]
    evidence: str
    uncertainty: float | None
    grader: str                      # "exact" | "llm_rubric"
    grader_status: str               # "ok" | "uncertain" | "unavailable"
    run: AgentRun | None = None

    @property
    def counts_as_evidence(self) -> bool:
        return self.grader_status == "ok" and self.correct is not None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().casefold())


def resolve_mcq_choice(answer: str, options: list[str]) -> str | None:
    """Accept the option text or its letter/number (A-E / 1-5). None if the answer is not one of the options."""
    a = _norm(answer)
    for o in options:
        if _norm(o) == a:
            return o
    m = re.fullmatch(r"\(?([a-e1-5])\)?[.)]?", a)
    if m:
        ch = m.group(1)
        idx = "abcde".index(ch) if ch.isalpha() else int(ch) - 1
        if idx < len(options):
            return options[idx]
    return None


def _grade_mcq(it: ItemForGrading, answer: str) -> EvaluationOutcome:
    choice = resolve_mcq_choice(answer, it.options or [])
    ok = choice is not None and _norm(choice) == _norm(it.answer_key)
    tag = [] if ok else [it.distractor_tags.get(choice or "", "wrong_option")]
    return EvaluationOutcome(ok, 1.0 if ok else 0.0, "Correct." if ok else "That is not the correct option.", tag,
                             f"chose: {choice}", 0.0, "exact", "ok")


def _grade_numeric(it: ItemForGrading, answer: str) -> EvaluationOutcome:
    given, key = parse_number(answer), parse_number(it.answer_key)
    if given is None or key is None:
        return EvaluationOutcome(False, 0.0, "Please answer with a single number.", ["not_a_number"], "", 0.0, "exact", "ok")
    ok = abs(given - key) <= (it.numeric_tolerance or 0.0) + 1e-9
    return EvaluationOutcome(ok, 1.0 if ok else 0.0, "Correct." if ok else "That number is not correct.",
                             [] if ok else ["numeric_error"], f"answered: {given:g}", 0.0, "exact", "ok")


def evaluate_answer(provider: LLMProvider, settings: Settings, item: ItemForGrading, answer: str) -> EvaluationOutcome:
    if item.kind == "mcq":
        return _grade_mcq(item, answer)
    if item.kind == "numeric":
        return _grade_numeric(item, answer)
    req = EvaluateRequest(kind="short_text", prompt=item.prompt, answer_key=item.answer_key, rubric=item.rubric,
                          student_answer=answer)
    value, run = call_provider(provider, settings, "evaluate", lambda: provider.evaluate_answer(req), EvaluationOut)
    if value is None:                           # model failed or returned invalid output: no verdict, no evidence
        run.fallback = True
        return EvaluationOutcome(None, None, "I could not grade this answer automatically right now. You can try again, "
                                 "or ask for a teacher.", [], "", None, "llm_rubric", "unavailable", run)
    status = "ok" if value.uncertainty <= settings.grader_max_uncertainty else "uncertain"
    fb = value.feedback if status == "ok" else ("The automatic grader is not sure about this answer, so it will not count "
                                                "as evidence. " + value.feedback)
    return EvaluationOutcome(value.correct, value.partial_credit, fb, [t[:40] for t in value.error_tags], value.evidence,
                             value.uncertainty, "llm_rubric", status, run)
