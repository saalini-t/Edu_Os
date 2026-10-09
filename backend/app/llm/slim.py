"""Compact, model-facing output schemas. Small local models (3B class) degrade badly when constrained by the full
internal schemas (many optional fields, bounded floats): they emit near-empty objects or `Infinity`. The model is therefore
asked for a flat shape with every field required, enums instead of free numbers, and the result is mapped onto the strict
internal schemas. Unknown fields are still rejected and every mapped value is still validated by the strict schemas."""
from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import ValidationError, field_validator

from app.llm.schemas import (
    Citation, DoubtAnalysis, EvaluationOut, Explanation, GapHypothesisDraft, PracticeItemDraft, PracticeSet, Strict,
)

LEVEL = {"low": 0.1, "medium": 0.4, "high": 0.8}
CONF = {"low": 0.3, "medium": 0.6, "high": 0.9}


def _required(model: type[Strict]) -> dict:
    schema = model.model_json_schema()
    schema["required"] = list(schema.get("properties", {}))
    return schema


class SlimUnderstand(Strict):
    topic_id: str | None = None
    intent: Literal["conceptual", "procedural", "error_diagnosis", "fact_lookup", "other"] = "other"
    clarity: Literal["clear", "ambiguous", "off_topic"] = "ambiguous"
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    clarification_question: str | None = None
    suspected_gap: str | None = None
    explicit_teacher_request: bool = False
    safety_flag: bool = False
    confidence: Literal["low", "medium", "high"] = "low"

    def to_internal(self) -> DoubtAnalysis:
        gaps = [GapHypothesisDraft(topic_id=self.topic_id, description=self.suspected_gap[:300])] if self.suspected_gap and self.topic_id else []
        return DoubtAnalysis(topic_id=self.topic_id, intent=self.intent, clarity=self.clarity, difficulty_estimate=self.difficulty,
                             clarification_question=self.clarification_question or None, gap_hypotheses=gaps,
                             safety_flag=self.safety_flag, explicit_teacher_request=self.explicit_teacher_request,
                             classification_confidence=CONF[self.confidence])


class SlimExplain(Strict):
    insufficient_context: bool = False
    text: str
    citations: list[Citation] = []

    def to_internal(self) -> Explanation:
        return Explanation(text=self.text, citations=self.citations, insufficient_context=self.insufficient_context)


class SlimItem(Strict):
    kind: Literal["mcq", "numeric", "short_text"]
    prompt: str
    options: list[str] = []
    answer_key: str
    rubric: str = ""
    numeric_tolerance: float = 0.0
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    source_chunk_ids: list[str] = []


_LETTERED = re.compile(r"^\s*\(?([A-E])[.)]\s+(.+?)\s*$", re.M)


def _salvage(i: "SlimItem") -> "SlimItem":
    """Small models often put 'A) ... B) ...' inside the prompt, answer with a letter, or omit the rubric. These are repaired
    deterministically (nothing is invented); anything still malformed is rejected by the strict schema and the agent validators."""
    upd: dict = {}
    if i.kind == "mcq" and len(i.options) < 2:
        found = _LETTERED.findall(i.prompt)
        if len(found) >= 3:
            upd["options"] = [t for _, t in found]
            upd["prompt"] = _LETTERED.sub("", i.prompt).strip()
            letter = re.match(r"^\(?([A-E])\)?[.)]?$", i.answer_key.strip(), re.I)
            if letter:
                idx = ord(letter.group(1).upper()) - 65
                if idx < len(found):
                    upd["answer_key"] = found[idx][1]
    if i.kind == "short_text" and not i.rubric.strip():
        upd["rubric"] = "A correct answer conveys the key points of the reference answer: " + i.answer_key[:300]
    return i.model_copy(update=upd) if upd else i


class SlimPractice(Strict):
    items: list[SlimItem]

    def to_internal(self) -> PracticeSet:
        """Items the model malformed (e.g. a multiple-choice question without options) are dropped one by one; the set is
        invalid only if nothing usable remains. Deterministic validators in the practice agent then check the survivors."""
        out = []
        for i in self.items:
            i = _salvage(i)
            tol = abs(i.numeric_tolerance) if math.isfinite(i.numeric_tolerance) else 0.0
            try:
                out.append(PracticeItemDraft(kind=i.kind, prompt=i.prompt, options=i.options or None, answer_key=i.answer_key,
                                             rubric=i.rubric or None, numeric_tolerance=tol if i.kind == "numeric" else None,
                                             difficulty=i.difficulty, source_chunk_ids=i.source_chunk_ids))
            except ValidationError:
                continue
        return PracticeSet(items=out[:6])          # raises ValidationError when empty (min_length=1): treated as invalid output


class SlimEval(Strict):
    correct: bool
    partial_credit: float = 0.0
    feedback: str
    error_tags: list[str] = []
    evidence: str = ""
    uncertainty: Literal["low", "medium", "high"] = "high"

    @field_validator("partial_credit")
    @classmethod
    def _bounded(cls, v: float) -> float:
        return min(max(v, 0.0), 1.0) if math.isfinite(v) else 0.0

    def to_internal(self) -> EvaluationOut:
        return EvaluationOut(correct=self.correct, partial_credit=self.partial_credit, feedback=self.feedback[:800],
                             error_tags=self.error_tags[:5], evidence=self.evidence[:400], uncertainty=LEVEL[self.uncertainty])


SLIM = {"understand": SlimUnderstand, "explain": SlimExplain, "practice": SlimPractice, "evaluate": SlimEval}


def schema_for(op: str) -> dict:
    return _required(SLIM[op])
