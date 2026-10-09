"""Typed provider inputs/outputs (docs/AGENT_SPECIFICATIONS.md). Every model output is validated against these
schemas; anything that does not validate is treated as a provider failure, never trusted."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Difficulty = Literal["easy", "medium", "hard"]
QuestionKind = Literal["mcq", "numeric", "short_text"]


class TopicRef(Strict):
    id: str
    slug: str
    name: str
    keywords: list[str] = []


# ------------------------------------------------------------------------------------------ doubt understanding
class UnderstandRequest(Strict):
    doubt_text: str
    history: list[str] = []
    topics: list[TopicRef]


class GapHypothesisDraft(Strict):
    topic_id: str
    description: str = Field(max_length=300)


class DoubtAnalysis(Strict):
    schema_version: Literal["1"] = "1"
    topic_id: str | None = None
    subtopic: str | None = Field(default=None, max_length=120)
    secondary_topic_ids: list[str] = []
    intent: Literal["conceptual", "procedural", "error_diagnosis", "fact_lookup", "other"] = "other"
    clarity: Literal["clear", "ambiguous", "off_topic"] = "ambiguous"
    clarification_question: str | None = None
    difficulty_estimate: Difficulty = "medium"
    gap_hypotheses: list[GapHypothesisDraft] = Field(default_factory=list, max_length=3)
    safety_flag: bool = False
    explicit_teacher_request: bool = False
    classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # model self-report, NOT mastery


# ------------------------------------------------------------------------------------------ grounded explanation
class ChunkView(Strict):
    chunk_id: str
    document_id: str
    page: int
    text: str


class ExplainRequest(Strict):
    doubt_text: str
    analysis: DoubtAnalysis
    chunks: list[ChunkView]
    attempt: int = 1


class Citation(Strict):
    chunk_id: str
    quote: str


class Explanation(Strict):
    schema_version: Literal["1"] = "1"
    text: str
    citations: list[Citation] = []
    insufficient_context: bool = False
    uses_general_knowledge: bool = False
    follow_up_check: str | None = None
    provider_note: str | None = None


# ------------------------------------------------------------------------------------------ practice generation
class PracticeRequest(Strict):
    topic_id: str
    topic_name: str
    doubt_text: str
    chunks: list[ChunkView]
    count: int = Field(default=3, ge=1, le=6)
    difficulty: Difficulty = "medium"
    kinds: list[QuestionKind] = ["mcq", "numeric", "short_text"]
    hypothesis: str | None = None                 # description of the suspected gap being targeted (a hypothesis, not a fact)
    error_tags: list[str] = []                    # recent verified error tags
    avoid_prompt_hashes: list[str] = []           # hashes of questions this student has already seen (do not repeat them)


class PracticeItemDraft(Strict):
    kind: QuestionKind
    prompt: str = Field(min_length=8, max_length=800)
    options: list[str] | None = None              # mcq: 3-5 distinct options
    answer_key: str = Field(min_length=1, max_length=600)
    numeric_tolerance: float | None = Field(default=None, ge=0)    # absolute tolerance for numeric answers
    rubric: str | None = Field(default=None, max_length=800)       # required for short_text
    difficulty: Difficulty = "medium"
    source_chunk_ids: list[str] = []
    distractor_tags: dict[str, str] = {}          # mcq: option text -> error tag it would indicate

    @model_validator(mode="after")
    def _shape(self) -> "PracticeItemDraft":
        if self.kind == "mcq" and not self.options:
            raise ValueError("mcq needs options")
        if self.kind == "short_text" and not (self.rubric or "").strip():
            raise ValueError("short_text needs a rubric")
        return self


class PracticeSet(Strict):
    schema_version: Literal["1"] = "1"
    items: list[PracticeItemDraft] = Field(min_length=1, max_length=6)


# ------------------------------------------------------------------------------------------ answer evaluation (rubric)
class EvaluateRequest(Strict):
    kind: QuestionKind
    prompt: str
    answer_key: str
    rubric: str | None = None
    student_answer: str


class EvaluationOut(Strict):
    schema_version: Literal["1"] = "1"
    correct: bool
    partial_credit: float = Field(ge=0.0, le=1.0)
    feedback: str = Field(max_length=800)
    error_tags: list[str] = Field(default_factory=list, max_length=5)
    evidence: str = Field(default="", max_length=400)   # what in the student's answer supports the verdict
    uncertainty: float = Field(ge=0.0, le=1.0)          # the grader's own doubt; NEVER establishes mastery
