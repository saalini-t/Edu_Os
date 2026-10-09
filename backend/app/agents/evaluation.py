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
from app.knowledge.service import STOPWORDS
from app.llm.schemas import EvaluateRequest, EvaluationOut

# Text in a STUDENT ANSWER that tries to talk to the grader. Such an answer is never sent to the model.
_GRADER_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|rubric|above|previous|prior|reference)|"
    r"(mark|grade|score|rate|set|give|treat)\b.{0,40}\b(correct|right|full (marks|credit)|100|perfect|pass)|"
    r"\b(you are now|act as|new instructions?|system prompt|developer message|as the grader|the grader (must|should|will))\b|"
    r"</?\s*(answer|doubt|passage|history|system)\b|"
    r"[\"']?(correct|partial_credit|uncertainty|error_tags)[\"']?\s*[:=]\s*(true|false|[01](\.\d+)?)\b", re.I | re.S)


def looks_like_grader_injection(text: str) -> bool:
    return bool(_GRADER_INJECTION.search(text))


def key_terms(answer_key: str, rubric: str | None, limit: int = 8) -> list[str]:
    """Deterministic key terms a correct free-text answer should contain: the rubric's "Mentions: a, b" list when it has that
    form, otherwise the distinctive words of the reference answer."""
    if rubric and rubric.strip().lower().startswith("mentions:"):
        raw = [t.strip().lower() for t in rubric.split(":", 1)[1].split(",") if t.strip()]
        if raw:
            return raw[:limit]
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", answer_key.lower()):
        if len(tok) >= 4 and tok not in STOPWORDS and tok not in out:
            out.append(tok)
    return out[:limit]


def lexical_support(answer: str, terms: list[str]) -> float:
    """Fraction of key terms present in the answer (prefix match, so simple inflections still count). 1.0 when there are none."""
    if not terms:
        return 1.0
    low = answer.lower()
    toks = re.findall(r"[a-z0-9]+", low)
    hit = sum(1 for t in terms if t in low or any(w.startswith(t[: max(4, len(t) - 2)]) for w in toks if len(t) >= 4))
    return hit / len(terms)


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
    if looks_like_grader_injection(answer):
        # never forward instructions aimed at the grader to the model; the attempt is closed without evidence
        return EvaluationOutcome(None, None, "This answer contains text that looks like instructions to the grader, so it was "
                                 "not graded automatically and does not count as evidence. Please answer the question in your "
                                 "own words, or ask for a teacher.", ["instruction_like_text"], "", None, "llm_rubric",
                                 "uncertain", None)
    req = EvaluateRequest(kind="short_text", prompt=item.prompt, answer_key=item.answer_key, rubric=item.rubric,
                          student_answer=answer)
    value, run = call_provider(provider, settings, "evaluate", lambda: provider.evaluate_answer(req), EvaluationOut)
    if value is None:                           # model failed or returned invalid output: no verdict, no evidence
        run.fallback = True
        return EvaluationOutcome(None, None, "I could not grade this answer automatically right now. You can try again, "
                                 "or ask for a teacher.", [], "", None, "llm_rubric", "unavailable", run)
    status = "ok" if value.uncertainty <= settings.grader_max_uncertainty else "uncertain"
    note = ""
    if status == "ok" and ((value.correct and value.partial_credit < 0.5) or (not value.correct and value.partial_credit > 0.8)):
        status, note = "uncertain", "The grader's verdict and score disagree, so this will not count as evidence. "
    support = lexical_support(answer, key_terms(item.answer_key, item.rubric))
    if status == "ok" and value.correct and support < settings.grader_min_lexical_support:
        # a model verdict alone never creates positive evidence: the answer must also contain the reference answer's key terms
        status, note = "uncertain", "The answer does not clearly mention the key ideas, so this will not count as evidence. "
    fb = value.feedback if status == "ok" else (note or "The automatic grader is not sure about this answer, so it will not "
                                                "count as evidence. ") + value.feedback
    tags = [t[:40] for t in value.error_tags]
    return EvaluationOutcome(value.correct, value.partial_credit, fb, tags,
                             f"{value.evidence} [key-term coverage {support:.2f}]".strip(), value.uncertainty, "llm_rubric",
                             status, run)
