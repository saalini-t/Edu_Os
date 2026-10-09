"""HTTP backends that return ONE JSON object for a schema. Credentials are server-side only; secrets and response bodies
are never logged or put into exception messages."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Protocol

import httpx

log = logging.getLogger("eduos.llm.backend")


class BackendError(Exception):
    """Provider call failed (HTTP error, bad JSON, unreachable). Message contains no payloads or secrets."""


class JsonBackend(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict: ...
    def ping(self) -> bool: ...


def _loads(content: str) -> dict:
    try:
        out = json.loads(content)
    except (TypeError, ValueError) as e:
        raise BackendError("model returned non-JSON output") from e
    if not isinstance(out, dict):
        raise BackendError("model returned JSON that is not an object")
    return out


NUM_CTX = 8192      # room for the system prompt plus several retrieved passages


class OllamaBackend:
    name = "ollama"

    def __init__(self, base_url: str, model: str, temperature: float, timeout_s: float):
        self.base_url, self.model, self.temperature = base_url.rstrip("/"), model, temperature
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)))
        self._seen: dict[str, int] = {}

    def _temperature_for(self, system: str, user: str) -> float:
        """The first request is as configured (0.0 = reproducible). Asking the model the IDENTICAL question again (a retry after
        invalid output, or a regeneration after duplicates) would just repeat the same answer, so each repeat is warmer."""
        k = hashlib.sha256((system + "|" + user).encode()).hexdigest()
        n = self._seen.get(k, 0)
        if len(self._seen) > 200:
            self._seen.clear()
        self._seen[k] = n + 1
        return min(self.temperature + 0.35 * n, 1.0)

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict:
        body = {"model": self.model, "stream": False, "format": schema,           # structured output: constrained to the schema
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "keep_alive": "30m", "options": {"temperature": self._temperature_for(system, user), "num_ctx": NUM_CTX, "repeat_penalty": 1.1,
                            **({"num_predict": max_tokens} if max_tokens else {})}}   # a token cap bounds runaway generation
        try:
            r = self._client.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            return _loads(r.json()["message"]["content"])
        except httpx.TimeoutException as e:
            raise BackendError("ollama request timed out") from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise BackendError(f"ollama request failed ({type(e).__name__})") from e

    def warm(self) -> None:
        """Load the model into memory (a cold load can exceed the request timeout). Failure is harmless: requests still work."""
        try:
            self._client.post(f"{self.base_url}/api/chat", json={"model": self.model, "stream": False, "keep_alive": "30m",
                              "messages": [{"role": "user", "content": "ok"}], "options": {"num_ctx": NUM_CTX, "num_predict": 1}},
                              timeout=httpx.Timeout(600.0, connect=5.0))
        except Exception as e:                                           # noqa: BLE001
            log.info("model warm-up skipped (%s)", type(e).__name__)

    def ping(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/api/tags", timeout=1.5).status_code == 200
        except Exception:
            return False


class OpenAICompatBackend:
    """Any server exposing /chat/completions with json_schema response_format (vLLM, LM Studio, llama.cpp server, hosted APIs)."""
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str | None, temperature: float, timeout_s: float):
        self.base_url, self.model, self.temperature = base_url.rstrip("/"), model, temperature
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)), headers=headers)

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict:
        body = {"model": self.model, "temperature": self.temperature, **({"max_tokens": max_tokens} if max_tokens else {}),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}}
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body)
            r.raise_for_status()
            return _loads(r.json()["choices"][0]["message"]["content"])
        except httpx.TimeoutException as e:
            raise BackendError("request timed out") from e
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise BackendError(f"request failed ({type(e).__name__})") from e

    def ping(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/models", timeout=1.5).status_code < 500
        except Exception:
            return False
