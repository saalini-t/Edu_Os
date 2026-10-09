from __future__ import annotations

import logging
import threading
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


class _Gate:
    """Bounded model-call concurrency for this process. A slot is held until the call REALLY finishes, even if the caller
    gave up on it (a timed-out call keeps running in its thread and must keep counting against the limit)."""

    def __init__(self, max_concurrency: int = 4, queue_wait_s: float = 20.0):
        self.max_concurrency, self.queue_wait_s = max_concurrency, queue_wait_s
        self.slots = threading.BoundedSemaphore(max_concurrency)
        self.pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="llm")
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)

    def leave(self) -> None:
        with self._lock:
            self.in_flight -= 1
        self.slots.release()


_gate = _Gate()


def configure_limits(max_concurrency: int, queue_wait_s: float) -> None:
    """Called once at application start (and by tests). Replaces the gate; calls still running on the old one finish normally."""
    global _gate
    _gate = _Gate(max_concurrency, queue_wait_s)


def gate_stats() -> dict:
    g = _gate
    return {"max_concurrency": g.max_concurrency, "in_flight": g.in_flight, "peak": g.peak}


def call_with_policy(fn: Callable[[], T], schema: type[T], *, timeout_s: float, max_retries: int,
                     provider: str) -> CallResult:
    """Bounded concurrency + timeout + bounded retries + output re-validation. Never raises; the caller applies a fallback.
    The timeout covers execution only: waiting for a free slot is bounded separately and fails fast as "busy"."""
    start = time.perf_counter()
    last_err: str | None = None
    attempts = 0
    gate = _gate
    for attempts in range(1, max_retries + 2):
        if not gate.slots.acquire(timeout=gate.queue_wait_s):
            last_err = "busy"
            log.warning("llm call rejected: all model slots busy", extra={"provider": provider, "in_flight": gate.in_flight})
            break                                            # retrying immediately would only add load
        gate.enter()

        def run():
            try:
                return fn()
            finally:
                gate.leave()
        try:
            future = gate.pool.submit(run)
        except Exception:                                    # pool shut down: give the slot back
            gate.leave()
            raise
        try:
            raw = future.result(timeout=timeout_s)
            value = schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
            return CallResult(value, None, attempts, int((time.perf_counter() - start) * 1000))
        except FutureTimeout:
            last_err = "timeout"
        except ValidationError:
            last_err = "invalid_output"
        except Exception as e:  # provider failure; message only, never payloads
            last_err = f"provider_error:{type(e).__name__}" + (f":{e}"[:120] if type(e).__name__ == "BackendError" else "")   # BackendError text is payload-free by design
        log.warning("llm call failed", extra={"provider": provider, "error": last_err, "attempt": attempts})
        if last_err == "invalid_output":
            break  # retrying a deterministic schema failure is pointless
    return CallResult(None, last_err, attempts, int((time.perf_counter() - start) * 1000))
