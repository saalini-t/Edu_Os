"""Redis wake-up queue. Redis is an optimisation, never the source of truth: if it is down, jobs stay QUEUED in
PostgreSQL and workers find them by polling the database."""
from __future__ import annotations

import logging

log = logging.getLogger("eduos.queue")
QUEUE_KEY = "eduos:ingest:wakeup"


class WakeupQueue:
    def __init__(self, url: str | None):
        self.url = url
        self._r = None

    def _client(self):
        if self.url is None:
            return None
        if self._r is None:
            import redis
            self._r = redis.Redis.from_url(self.url, socket_connect_timeout=1, socket_timeout=3, decode_responses=True)
        return self._r

    def notify(self, job_id: str) -> str | None:
        """Push a wake-up. Returns None on success or a short error string (job is still durable in PostgreSQL)."""
        if self.url is None:
            return "redis_not_configured"
        try:
            self._client().lpush(QUEUE_KEY, job_id)
            return None
        except Exception as e:
            log.warning("redis notify failed; the database poller will pick the job up", extra={"error": type(e).__name__})
            return f"redis_unavailable:{type(e).__name__}"

    def wait(self, timeout_s: float) -> bool:
        """Block up to timeout_s for a wake-up. False on timeout or Redis failure (caller falls back to polling)."""
        r = None
        try:
            r = self._client()
            if r is None:
                return False
            return r.brpop(QUEUE_KEY, timeout=max(1, int(timeout_s))) is not None
        except Exception as e:
            log.warning("redis wait failed; polling the database", extra={"error": type(e).__name__})
            self._r = None
            return False

    def ping(self) -> bool:
        try:
            r = self._client()
            return bool(r and r.ping())
        except Exception:
            return False
