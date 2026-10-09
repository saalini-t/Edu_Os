"""Embedding provider interface. Providers run LOCALLY; text never leaves the machine.
`sentence_transformers` downloads public model weights once (when allowed) and then works offline.
`hash` is a deterministic test-only stand-in that only captures word overlap."""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from functools import lru_cache
from typing import Protocol

from app.config import Settings

log = logging.getLogger("eduos.embeddings")

QUERY_PREFIX = {"BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: "}


class EmbeddingUnavailable(Exception):
    """The configured embedding model cannot be loaded or run."""


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dims: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Feature-hashing bag of words, L2-normalised. NOT semantic; deterministic; for tests only."""
    name = "hash"

    def __init__(self, dims: int = 64):
        self.dims, self.model = dims, f"hash-{dims}"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            v[h % self.dims] += 1.0 if (h >> 63) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class SentenceTransformerProvider:
    name = "sentence_transformers"

    def __init__(self, model: str, allow_download: bool, batch_size: int = 16):
        self.model, self.allow_download, self.batch_size = model, allow_download, batch_size
        self._m = None
        self._dims: int | None = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._m is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as e:
                    raise EmbeddingUnavailable("sentence-transformers is not installed "
                                               "(pip install -r requirements-embeddings.txt)") from e
                try:
                    try:
                        self._m = SentenceTransformer(self.model, device="cpu", local_files_only=True)
                    except Exception:
                        if not self.allow_download:
                            raise
                        self._m = SentenceTransformer(self.model, device="cpu")
                except Exception as e:
                    raise EmbeddingUnavailable(f"cannot load embedding model {self.model!r}: {type(e).__name__}") from e
                self._dims = int(self._m.get_sentence_embedding_dimension())
        return self._m

    @property
    def dims(self) -> int:   # type: ignore[override]
        self._load()
        return self._dims or 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        m = self._load()
        return [list(map(float, v)) for v in m.encode(texts, normalize_embeddings=True, batch_size=self.batch_size)]

    def embed_query(self, text: str) -> list[float]:
        m = self._load()
        return [float(x) for x in m.encode([QUERY_PREFIX.get(self.model, "") + text], normalize_embeddings=True)[0]]


@lru_cache(maxsize=4)
def _cached(provider: str, model: str, allow_download: bool, batch: int) -> EmbeddingProvider | None:
    if provider == "none":
        return None
    if provider == "hash":
        return HashEmbeddingProvider()
    return SentenceTransformerProvider(model, allow_download, batch)


def get_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """Process-wide cached provider (the model is loaded once). None when embeddings are not configured."""
    return _cached(settings.embedding_provider, settings.embedding_model, settings.embedding_allow_download,
                   settings.embedding_batch_size)
