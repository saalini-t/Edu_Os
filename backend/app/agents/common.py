"""Shared agent plumbing: every agent calls the provider through the same timeout / bounded-retry / strict-validation
policy and reports provider, model, prompt version, attempts, latency and error so the workflow trace can show them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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


def call_provider(provider: LLMProvider, settings: Settings, op: str, fn: Callable[[], Any], schema: type) -> tuple[Any, AgentRun]:
    """Returns (validated value or None, AgentRun). Never raises."""
    res = call_with_policy(fn, schema, timeout_s=settings.llm_timeout_s, max_retries=settings.llm_max_retries,
                           provider=provider.name)
    run = AgentRun(provider=provider.name, model=provider.model, prompt_version=provider.prompt_version(op),
                   attempts=res.attempts, latency_ms=res.latency_ms, error=res.error)
    return res.value, run
