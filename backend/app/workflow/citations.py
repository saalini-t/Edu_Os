"""Deterministic citation verification. LLM-provided identifiers and quotes are never trusted."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.knowledge.chunker import normalize_text
from app.knowledge.service import SearchHit
from app.llm.schemas import Citation


def _norm(s: str) -> str:
    return normalize_text(s).casefold()


@dataclass
class CitationCheck:
    chunk_id: str
    quote: str
    verified: bool
    reason: str  # ok | chunk_not_retrieved | quote_not_in_chunk | empty_quote


def verify_citations(citations: list[Citation], retrieved: list[SearchHit]) -> list[CitationCheck]:
    by_id = {h.chunk_id: h for h in retrieved}
    out: list[CitationCheck] = []
    for c in citations:
        hit = by_id.get(c.chunk_id)
        if not c.quote.strip():
            out.append(CitationCheck(c.chunk_id, c.quote, False, "empty_quote"))
        elif hit is None:
            out.append(CitationCheck(c.chunk_id, c.quote, False, "chunk_not_retrieved"))
        elif _norm(c.quote) not in _norm(hit.text):
            out.append(CitationCheck(c.chunk_id, c.quote, False, "quote_not_in_chunk"))
        else:
            out.append(CitationCheck(c.chunk_id, c.quote, True, "ok"))
    return out


_INJECTION = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above) (instructions|rules)|disregard .{0,30}instructions|"
    r"you are now|system prompt|reveal .{0,30}(secret|key|password))", re.I)


def injection_flags(chunks: list[SearchHit]) -> list[str]:
    """Chunk ids containing instruction-like text. Logged for review; chunks are only ever treated as data."""
    return [h.chunk_id for h in chunks if _INJECTION.search(h.text)]
