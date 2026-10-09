"""Sentence-aligned chunking. Chunks never cross pages so page metadata is exact, and every
sentence in a chunk is an exact substring of the chunk text (needed for citation verification)."""
from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text).strip()


def split_sentences(text: str) -> list[str]:
    text = normalize_text(text)
    return [s for s in _SENT.split(text) if s] if text else []


@dataclass(frozen=True)
class ChunkSpec:
    page: int
    chunk_index: int
    text: str


def _words(s: str) -> int:
    return len(s.split())


def chunk_pages(pages: list[str], chunk_words: int, overlap_words: int) -> list[ChunkSpec]:
    """pages[i] is the text of page i+1. Greedy sentence packing with sentence-level overlap."""
    if overlap_words >= chunk_words:
        raise ValueError("overlap must be smaller than chunk size")
    out: list[ChunkSpec] = []
    for page_no, page_text in enumerate(pages, start=1):
        sentences = split_sentences(page_text)
        i = 0
        while i < len(sentences):
            cur: list[str] = []
            count = 0
            j = i
            while j < len(sentences) and (count == 0 or count + _words(sentences[j]) <= chunk_words):
                cur.append(sentences[j])
                count += _words(sentences[j])
                j += 1
            out.append(ChunkSpec(page_no, len(out), " ".join(cur)))
            if j >= len(sentences):
                break
            # step back so the next chunk overlaps by at most `overlap_words`, but always advances
            back, k = 0, j
            while k - 1 > i and back + _words(sentences[k - 1]) <= overlap_words:
                back += _words(sentences[k - 1])
                k -= 1
            i = k
    return out
