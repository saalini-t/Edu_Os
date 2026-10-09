"""Mastery model: Beta-Bernoulli posterior per (student, topic) with exponential recency decay toward the prior.
PURE functions (no I/O, no clock): everything is deterministic given the evidence and `now`, so replaying the ledger always
reproduces the cached state. Parameters are configuration, not constants.

Status rules (docs/ARCHITECTURE.md section 9):
  unknown        no weighted evidence and no suspected gap
  hypothesis     no weighted evidence, but a suspected gap exists (a HYPOTHESIS: carries no weight)
  emerging       some weighted evidence, not enough to call it demonstrated
  demonstrated   decayed mean >= T_MASTER  AND  evidence_count >= N_MIN  AND  >= MIN_DISTINCT distinct sources
One correct answer can never demonstrate mastery; acknowledgments carry zero weight and never reach this module."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.learner.ledger import NEGATIVE, POSITIVE


@dataclass(frozen=True)
class MasteryParams:
    alpha0: float = 1.0
    beta0: float = 1.0
    half_life_days: float = 14.0
    t_master: float = 0.75
    n_min: int = 3
    min_distinct: int = 2
    w_exact: float = 1.0
    w_llm: float = 0.5
    w_teacher: float = 2.0
    easy_factor: float = 0.75

    @classmethod
    def from_settings(cls, s: Settings) -> "MasteryParams":
        return cls(s.mastery_alpha0, s.mastery_beta0, s.mastery_half_life_days, s.t_master, s.n_min,
                   s.mastery_min_distinct_sources, s.w_attempt_exact, s.w_attempt_llm, s.w_teacher, s.difficulty_factor_easy)


@dataclass
class Posterior:
    alpha: float
    beta: float
    evidence_count: int = 0
    sources: list[str] = field(default_factory=list)       # distinct item / assessment keys, insertion ordered
    last_at: datetime | None = None

    @classmethod
    def prior(cls, p: MasteryParams) -> "Posterior":
        return cls(p.alpha0, p.beta0)


@dataclass(frozen=True)
class Event:
    evidence_id: str
    evidence_type: str
    weight: float
    source_key: str            # item id for attempts, "teacher:<escalation>" for assessments
    at: datetime


def decay_factor(days: float, p: MasteryParams) -> float:
    return 0.5 ** (max(days, 0.0) / p.half_life_days)


def decayed(post: Posterior, now: datetime | None, p: MasteryParams) -> tuple[float, float]:
    """(alpha, beta) after forgetting between the last evidence and `now`: the counts relax toward the prior."""
    if post.last_at is None or now is None or now <= post.last_at:
        return post.alpha, post.beta
    f = decay_factor((now - post.last_at).total_seconds() / 86400.0, p)
    return p.alpha0 + (post.alpha - p.alpha0) * f, p.beta0 + (post.beta - p.beta0) * f


def apply(post: Posterior, ev: Event, p: MasteryParams) -> Posterior:
    """Decay to the event time, then add the weight to alpha (positive) or beta (negative). Zero-weight / unknown types
    are ignored, which is how acknowledgments are guaranteed to have no effect."""
    if ev.evidence_type not in POSITIVE and ev.evidence_type not in NEGATIVE:
        return post
    a, b = decayed(post, ev.at, p)
    if ev.evidence_type in POSITIVE:
        a += ev.weight
    else:
        b += ev.weight
    sources = post.sources if ev.source_key in post.sources else [*post.sources, ev.source_key]
    return Posterior(a, b, post.evidence_count + 1, sources, ev.at)


def replay(events: list[Event], p: MasteryParams) -> Posterior:
    post = Posterior.prior(p)
    for ev in sorted(events, key=lambda e: (e.at, e.evidence_id)):
        post = apply(post, ev, p)
    return post


def mean(a: float, b: float) -> float:
    return a / (a + b)


def status(post: Posterior, now: datetime | None, p: MasteryParams, has_open_hypothesis: bool) -> str:
    if post.evidence_count == 0:
        return "hypothesis" if has_open_hypothesis else "unknown"
    a, b = decayed(post, now, p)
    if mean(a, b) >= p.t_master and post.evidence_count >= p.n_min and len(post.sources) >= p.min_distinct:
        return "demonstrated"
    return "emerging"


def attempt_weight(grader: str, difficulty: str, hints_used: int, p: MasteryParams) -> float:
    """Weight of a graded attempt: model-graded counts for less than objectively graded, easy items for less, hints reduce it.
    Always in (0, 1]."""
    base = p.w_exact if grader == "exact" else p.w_llm
    diff = p.easy_factor if difficulty == "easy" else 1.0
    return min(1.0, base * diff / (1 + max(hints_used, 0)))
