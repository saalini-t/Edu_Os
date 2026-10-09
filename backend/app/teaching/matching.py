"""Deterministic teacher matching. Pure functions: no I/O, no clock, no randomness, no model. The same inputs always give the
same ranking, and every component of every score is stored with the escalation so a match can be audited and explained.

score = w_topic*topic_fit + w_availability*availability + w_language*language + w_feedback*feedback + w_load*load
Hard constraints (candidate is excluded, not merely down-ranked): teacher inactive; teacher does not cover the topic when the
escalation names one. Ties are broken by teacher id."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Weights:
    topic: float = 0.40
    availability: float = 0.25
    language: float = 0.15
    feedback: float = 0.10
    load: float = 0.10


@dataclass
class TeacherFacts:
    teacher_id: str
    name: str
    active: bool
    languages: list[str]
    topic_proficiency: float | None           # proficiency for the escalation topic (None = teacher does not cover it)
    best_proficiency: float                   # best proficiency in the course (used when the escalation has no topic)
    slots: list[tuple[datetime, datetime]] = field(default_factory=list)   # free, unbooked slots
    helpful: int = 0                          # resolved escalations the students rated helpful
    rated: int = 0                            # resolved escalations that were rated at all
    open_load: int = 0                        # currently OPEN-assigned / ACCEPTED escalations


@dataclass
class Context:
    topic_known: bool
    student_language: str
    now: datetime
    window_hours: int
    max_open: int


def availability_score(slots: list[tuple[datetime, datetime]], now: datetime, window_hours: int) -> float:
    """1.0 if a slot is running now; otherwise decays linearly to 0 at `window_hours` before the next slot starts."""
    best = 0.0
    for start, end in slots:
        if end <= now:
            continue
        if start <= now:
            return 1.0
        hours = (start - now).total_seconds() / 3600.0
        best = max(best, max(0.0, 1.0 - hours / window_hours))
    return best


def feedback_score(helpful: int, rated: int) -> float:
    """Laplace-smoothed helpful rate: a teacher with no ratings starts at the neutral 0.5 instead of 0."""
    return (helpful + 1) / (rated + 2)


def score(f: TeacherFacts, ctx: Context, w: Weights) -> dict | None:
    if not f.active:
        return None
    if ctx.topic_known and not f.topic_proficiency:
        return None
    comp = {
        "topic_fit": round(f.topic_proficiency if ctx.topic_known else f.best_proficiency, 4),
        "availability": round(availability_score(f.slots, ctx.now, ctx.window_hours), 4),
        "language": 1.0 if ctx.student_language in f.languages else 0.0,
        "feedback": round(feedback_score(f.helpful, f.rated), 4),
        "load": round(max(0.0, 1.0 - f.open_load / max(ctx.max_open, 1)), 4),
    }
    total = (w.topic * comp["topic_fit"] + w.availability * comp["availability"] + w.language * comp["language"]
             + w.feedback * comp["feedback"] + w.load * comp["load"])
    return {"teacher_id": f.teacher_id, "name": f.name, "score": round(total, 4), "components": comp}


def rank(facts: list[TeacherFacts], ctx: Context, w: Weights) -> list[dict]:
    scored = [s for s in (score(f, ctx, w) for f in facts) if s is not None]
    scored.sort(key=lambda s: (-s["score"], s["teacher_id"]))
    for i, s in enumerate(scored, start=1):
        s["rank"] = i
    return scored
