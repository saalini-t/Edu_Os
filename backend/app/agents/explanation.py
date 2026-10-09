"""Grounded Explanation Agent. Uses ONLY authorised retrieved passages. Every citation is verified deterministically
(chunk was retrieved AND the quote exists in it); unverified citations are stripped, one regeneration is attempted when
none survive, and the last resort is the verified passages themselves. An insufficient-context answer carries no citations."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.common import AgentRun, call_provider
from app.config import Settings
from app.knowledge.service import SearchHit
from app.llm.base import LLMProvider
from app.llm.schemas import ChunkView, Citation, DoubtAnalysis, ExplainRequest, Explanation
from app.workflow.citations import verify_citations


@dataclass
class ExplanationOutcome:
    text: str
    citations: list[dict]                 # verified only, renumbered 1..n, with document/page metadata
    fallback: str | None                  # None | "passages_only" | "insufficient_context"
    regenerated: bool
    checks: list[dict]                    # every citation the model proposed, with verification result
    n_verified: int
    n_stripped: int
    follow_up_offered: bool
    provider_note: str | None
    run: AgentRun
    provider_calls: int = 0


def explain(provider: LLMProvider, settings: Settings, *, doubt_text: str, analysis: DoubtAnalysis,
            hits: list[SearchHit], attempt: int) -> ExplanationOutcome:
    by_id = {h.chunk_id: h for h in hits}
    views = [ChunkView(chunk_id=h.chunk_id, document_id=h.document_id, page=h.page, text=h.text) for h in hits]
    total_calls, agent_run = 0, None

    def generate():
        nonlocal total_calls, agent_run
        value, run = call_provider(provider, settings, "explain", lambda: provider.explain(
            ExplainRequest(doubt_text=doubt_text, analysis=analysis, chunks=views, attempt=attempt)), Explanation)
        total_calls += run.attempts
        agent_run = run
        return value, (verify_citations(value.citations, hits) if value else [])

    exp, checks = generate()
    regenerated = False
    if exp is not None and not exp.insufficient_context and not any(c.verified for c in checks):
        regenerated = True                                 # regenerate once when no citation survives verification
        exp, checks = generate()
    fallback: str | None = None
    if exp is None or (not exp.insufficient_context and not any(c.verified for c in checks)):
        fallback = "passages_only"
    elif exp.insufficient_context:
        fallback = "insufficient_context"
    agent_run.fallback = fallback == "passages_only"
    if fallback == "passages_only":
        text = "I could not produce a verified explanation. Here are the most relevant passages from your course material:"
        proposed = [Citation(chunk_id=h.chunk_id, quote=h.text[:300]) for h in hits[:3]]
        checks = verify_citations(proposed, hits)
        verified = [c for c in checks if c.verified]
        text += "".join(f" [{i}]" for i in range(1, len(verified) + 1))
    elif fallback == "insufficient_context":
        text, verified = exp.text, []
    else:
        verified = [c for c in checks if c.verified]
        mapping, n = {}, 0
        for i, c in enumerate(checks, start=1):
            if c.verified:
                n += 1
                mapping[i] = n
        text = re.sub(r"\s?\[(\d+)\]", lambda m: f" [{mapping[int(m.group(1))]}]" if int(m.group(1)) in mapping else "", exp.text)
    cites = []
    for n, c in enumerate(verified, start=1):
        h = by_id[c.chunk_id]
        cites.append({"n": n, "chunk_id": h.chunk_id, "document_id": h.document_id, "document_title": h.document_title,
                      "page": h.page, "quote": c.quote, "verified": True})
    return ExplanationOutcome(text=text, citations=cites, fallback=fallback, regenerated=regenerated,
                              checks=[c.__dict__ for c in checks], n_verified=len(verified),
                              n_stripped=len(checks) - len(verified), follow_up_offered=bool(exp and exp.follow_up_check),
                              provider_note=exp.provider_note if exp else None, run=agent_run, provider_calls=total_calls)
