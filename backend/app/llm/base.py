from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.schemas import (
    DoubtAnalysis, EvaluateRequest, EvaluationOut, ExplainRequest, Explanation, PracticeRequest, PracticeSet,
    UnderstandRequest,
)

log = logging.getLogger("eduos.llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # timeout | provider_error | invalid_output


class LLMProvider(Protocol):
    name: str
    model: str

    def prompt_version(self, op: str) -> str: ...
    def status(self) -> dict: ...
    def understand(self, req: UnderstandRequest) -> DoubtAnalysis: ...
    def explain(self, req: ExplainRequest) -> Explanation: ...
    def generate_practice(self, req: PracticeRequest) -> PracticeSet: ...
    def evaluate_answer(self, req: EvaluateRequest) -> EvaluationOut: ...


@dataclass
class CallResult:
    value: BaseModel | None
    error: str | None
    attempts: int
    latency_ms: int


_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")


def call_with_policy(fn: Callable[[], T], schema: type[T], *, timeout_s: float, max_retries: int,
                     provider: str) -> CallResult:
    """Timeout + bounded retries + output re-validation. Never raises; the caller applies a fallback."""
    start = time.perf_counter()
    last_err: str | None = None
    attempts = 0
    for attempts in range(1, max_retries + 2):
        try:
            raw = _pool.submit(fn).result(timeout=timeout_s)
            value = schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
            return CallResult(value, None, attempts, int((time.perf_counter() - start) * 1000))
        except FutureTimeout:
            last_err = "timeout"
        except ValidationError:
            last_err = "invalid_output"
        except Exception as e:  # provider failure; message only, never payloads
            last_err = f"provider_error:{type(e).__name__}"
        log.warning("llm call failed", extra={"provider": provider, "error": last_err, "attempt": attempts})
        if last_err == "invalid_output":
            break  # retrying a deterministic schema failure is pointless
    return CallResult(None, last_err, attempts, int((time.perf_counter() - start) * 1000))
