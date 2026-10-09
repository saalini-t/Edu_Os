"""Shared agent plumbing: every agent calls the provider through the same timeout / bounded-retry / strict-validation
policy and reports provider, model, prompt version, attempts, latency and error so the workflow trace can show them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from app.config import Settings
from app.llm.base import LLMProvider, call_with_policy


@dataclass
class AgentRun:
    provider: str
    model: str
    prompt_version: str
    attempts: int = 0
    latency_ms: int = 0
    error: str | None = None          # timeout | invalid_output | provider_error:<Type> (None on success)
    fallback: bool = False            # a deterministic fallback was used instead of the model's output
    notes: list[str] = field(default_factory=list)

    def trace(self) -> dict:
        return {"provider": self.provider, "model": self.model, "prompt_version": self.prompt_version,
                "attempts": self.attempts, "latency_ms": self.latency_ms, "error": self.error, "fallback": self.fallback,
                "notes": self.notes}


_DELIMITERS = re.compile(r"</?(doubt|answer|history|passage)[^>]*>", re.I)


def strip_delimiters(value: Any) -> Any:
    """Small models echo the <doubt> / <passage> tags we wrap untrusted text in. They are prompt plumbing, never content a
    student should see, so they are removed from every string of a validated model output (recursively)."""
    if isinstance(value, str):
        return _DELIMITERS.sub("", value).strip()
    if isinstance(value, BaseModel):
        return value.model_copy(update={n: strip_delimiters(getattr(value, n)) for n in type(value).model_fields})
    if isinstance(value, list):
        return [strip_delimiters(v) for v in value]
    return value


def call_provider(provider: LLMProvider, settings: Settings, op: str, fn: Callable[[], Any], schema: type) -> tuple[Any, AgentRun]:
    """Returns (validated value or None, AgentRun). Never raises."""
    res = call_with_policy(fn, schema, timeout_s=settings.llm_timeout_s, max_retries=settings.llm_max_retries,
                           provider=provider.name)
    run = AgentRun(provider=provider.name, model=provider.model, prompt_version=provider.prompt_version(op),
                   attempts=res.attempts, latency_ms=res.latency_ms, error=res.error)
    return strip_delimiters(res.value), run
