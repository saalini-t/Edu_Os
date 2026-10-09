"""Doubt Understanding Agent: topic, subtopic, intent, ambiguity, difficulty estimate, suspected-gap hypotheses.
The model proposes; deterministic code disposes: identifiers must exist, flags are also pattern-detected, and a failed
or invalid model call degrades to a safe clarification instead of guessing."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.common import AgentRun, call_provider
from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.schemas import DoubtAnalysis, TopicRef, UnderstandRequest

_TEACHER_REQUEST = re.compile(r"\b(talk|speak|chat|connect|escalate)\b.{0,25}\b(teacher|tutor|instructor|human)\b|"
                              r"\b(human|real|live) (teacher|tutor)\b|\bneed a (teacher|tutor)\b", re.I)
_SAFETY = re.compile(r"\b(kill myself|hurt myself|end my life|suicid\w*|self[- ]harm)\b", re.I)
GENERIC_CLARIFICATION = "Could you add more detail or name the topic your question is about?"


def _specific_for(topics: list[TopicRef], topic_id: str, text: str) -> bool:
    """Small models over-ask for clarification. A question of >= 6 words that mentions the chosen topic's name or a keyword
    is specific enough to answer from the course material (retrieval still decides whether sources exist)."""
    t = next((x for x in topics if x.id == topic_id), None)
    low = text.lower()
    return bool(t) and len(text.split()) >= 6 and any(k and k.lower() in low for k in [t.name, *t.keywords])


@dataclass
class UnderstandingOutcome:
    analysis: DoubtAnalysis
    run: AgentRun


def understand(provider: LLMProvider, settings: Settings, *, doubt_text: str, history: list[str],
               topics: list[TopicRef]) -> UnderstandingOutcome:
    req = UnderstandRequest(doubt_text=doubt_text, history=history, topics=topics)
    value, run = call_provider(provider, settings, "understand", lambda: provider.understand(req), DoubtAnalysis)
    if value is None:                                   # provider failed or produced invalid output: safe fallback
        run.fallback = True
        value = DoubtAnalysis(clarity="ambiguous", classification_confidence=0.0,
                              clarification_question="Could you rephrase your question and name the topic it is about?")
    ids = {t.id for t in topics}
    upd: dict = {}
    if value.topic_id not in ids:                       # never trust model-provided identifiers
        if value.topic_id is not None:
            run.notes.append("model topic_id not in taxonomy; discarded")
        upd.update(topic_id=None, subtopic=None)
        if value.clarity == "clear":
            upd["clarity"] = "ambiguous"
    upd["secondary_topic_ids"] = [t for t in value.secondary_topic_ids if t in ids]
    kept = [g for g in value.gap_hypotheses if g.topic_id in ids]
    if len(kept) != len(value.gap_hypotheses):
        run.notes.append("gap hypotheses with unknown topic ids discarded")
    upd["gap_hypotheses"] = kept
    value = value.model_copy(update=upd)
    if value.clarity == "ambiguous" and value.topic_id and not run.fallback and _specific_for(topics, value.topic_id, doubt_text):
        run.notes.append("model said ambiguous but the question is long enough and names the topic; treated as clear")
        value = value.model_copy(update={"clarity": "clear", "clarification_question": None})
    if value.clarity == "ambiguous" and not value.clarification_question:
        value = value.model_copy(update={"clarification_question": GENERIC_CLARIFICATION})
    full = " ".join([doubt_text, *history])
    if _TEACHER_REQUEST.search(full):                   # rules R1/R1b never depend only on the model
        value = value.model_copy(update={"explicit_teacher_request": True})
    if _SAFETY.search(full):
        value = value.model_copy(update={"safety_flag": True})
    return UnderstandingOutcome(value, run)
