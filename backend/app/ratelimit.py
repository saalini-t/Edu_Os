"""Per-user rate limits for the endpoints that can trigger model calls.

A sliding window kept in this process's memory. That is correct for the single API process this prototype runs; with several API
processes each would enforce its own window (move the counters to Redis or PostgreSQL first). A limit of 0 disables a bucket.
Login throttling is separate and database-backed (see auth/router.py), so it already holds across processes."""
from __future__ import annotations

import math
import threading
import time
from collections import deque

from fastapi import Depends, Request

from app.auth.deps import current_user
from app.errors import AppError
from app.models import User


class SlidingWindow:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> float | None:
        """Record one hit. Returns None if allowed, else the seconds until a slot frees up. Rejected hits are not recorded."""
        if self.limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return max(0.0, q[0] + self.window_s - now)
            q.append(now)
            if len(self._hits) > 10_000:                      # bound memory: forget idle keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window_s]:
                    self._hits.pop(k, None)
            return None


def build_limiters(settings) -> dict[str, SlidingWindow]:
    return {"llm_actions": SlidingWindow(settings.rate_llm_actions_per_min, 60.0),
            "doubts": SlidingWindow(settings.rate_doubts_per_hour, 3600.0)}


def rate_limit(*buckets: str):
    """FastAPI dependency: charge the authenticated user's budget in each bucket or answer 429 with Retry-After."""
    def dep(request: Request, user: User = Depends(current_user)) -> None:
        limiters = request.app.state.limiters
        for b in buckets:
            wait = limiters[b].check(str(user.id))
            if wait is not None:
                secs = max(1, math.ceil(wait))
                raise AppError(429, "RATE_LIMITED", f"Too many requests. Please wait {secs} s and try again.",
                               {"bucket": b, "retry_after_s": secs}, headers={"Retry-After": str(secs)})
    return dep
