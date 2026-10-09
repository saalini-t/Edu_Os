"""Practice Generation Agent. The model drafts items; deterministic validators decide what is kept (shape, key correctness,
no answer leakage, sources must be retrieved passages, no repeats). If too few valid items survive the agent regenerates
once, then falls back to the hand-authored seed bank, then reports that nothing is available. Answer keys are returned here
for persistence only: the API never sends them to students before submission."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.agents.common import AgentRun, call_provider
from app.agents.seed_bank import BANK
from app.config import Settings
from app.knowledge.service import SearchHit
from app.llm.base import LLMProvider
from app.llm.schemas import ChunkView, PracticeItemDraft, PracticeRequest, PracticeSet


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", prompt.strip().lower()).encode()).hexdigest()[:24]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().casefold())


def parse_number(text: str) -> float | None:
    """Exactly one number in the text (commas and a trailing unit/percent are tolerated); otherwise None."""
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(nums[0]) if len(nums) == 1 else None


def validate_item(it: PracticeItemDraft, allowed_chunk_ids: set[str], seen: set[str], kinds: list[str]) -> str | None:
    """Return None if the item is acceptable, else a short rejection reason."""
    if it.kind not in kinds:
        return "kind_not_requested"
    if prompt_hash(it.prompt) in seen:
        return "duplicate_prompt"
    if it.kind == "mcq":
        opts = it.options or []
        if not 3 <= len(opts) <= 5 or len({_norm(o) for o in opts}) != len(opts):
            return "mcq_options_invalid"
        if it.answer_key not in opts:
            return "mcq_key_not_an_option"
    elif it.kind == "numeric":
        key = parse_number(it.answer_key)
        if key is None:
            return "numeric_key_not_a_number"
        if (it.numeric_tolerance or 0) > max(0.5, 0.05 * abs(key)):
            return "numeric_tolerance_too_loose"
    key_norm = _norm(it.answer_key)
    if it.kind != "mcq" and len(key_norm) > 3 and key_norm in _norm(it.prompt):
        return "answer_leaked_in_prompt"
    if it.kind != "numeric" and not it.source_chunk_ids:
        return "no_source_reference"
    if any(c not in allowed_chunk_ids for c in it.source_chunk_ids):
        return "source_not_in_retrieved_passages"
    return None


def visible_elsewhere(it: PracticeItemDraft, others: list[PracticeItemDraft]) -> bool:
    """True if this item's answer appears in another item's prompt/options, or another item's answer appears in this one's
    prompt/options (the correct option of a multiple-choice item is, of course, allowed inside its OWN options)."""
    def shown(x: PracticeItemDraft) -> list[str]:
        return [_norm(x.prompt), *[_norm(o) for o in (x.options or [])]]

    def hides(owner: PracticeItemDraft, viewer: PracticeItemDraft) -> bool:
        key = _norm(owner.answer_key)
        return len(key) > 3 and any(key == s or key in s for s in shown(viewer))
    return any(hides(it, o) or hides(o, it) for o in others)


@dataclass
class PracticeOutcome:
    items: list[PracticeItemDraft]
    source: str                              # "model" | "seed_bank" | "none"
    rejected: list[str] = field(default_factory=list)
    run: AgentRun | None = None
    provider_calls: int = 0


def generate_practice(provider: LLMProvider, settings: Settings, *, topic_id: str, topic_slug: str | None, topic_name: str,
                      doubt_text: str, hits: list[SearchHit], count: int, difficulty: str, hypothesis: str | None,
                      error_tags: list[str], seen_prompt_hashes: set[str],
                      kinds: tuple[str, ...] = ("mcq", "numeric", "short_text")) -> PracticeOutcome:
    allowed = {h.chunk_id for h in hits}
    views = [ChunkView(chunk_id=h.chunk_id, document_id=h.document_id, page=h.page, text=h.text) for h in hits]
    req = PracticeRequest(topic_id=topic_id, topic_name=topic_name, doubt_text=doubt_text, chunks=views, count=count,
                          difficulty=difficulty, kinds=list(kinds), hypothesis=hypothesis, error_tags=error_tags,
                          avoid_prompt_hashes=sorted(seen_prompt_hashes))
    seen, kept, rejected, calls, run = set(seen_prompt_hashes), [], [], 0, None
    for _ in range(2):                                          # initial attempt + ONE regeneration
        value, run = call_provider(provider, settings, "practice", lambda: provider.generate_practice(req), PracticeSet)
        calls += run.attempts
        if value is None:
            rejected.append(f"provider:{run.error}")
            break                                               # provider failure: do not hammer it
        for it in value.items:
            why = validate_item(it, allowed, seen, list(kinds))
            if why:
                rejected.append(why)
                continue
            if visible_elsewhere(it, kept):
                rejected.append("answer_visible_in_another_item")
                continue
            seen.add(prompt_hash(it.prompt))
            kept.append(it)
            if len(kept) >= count:
                break
        if len(kept) >= count:
            break
    if kept:
        return PracticeOutcome(kept[:count], "model", rejected, run, calls)
    bank = [it for it in BANK.get(topic_slug or "", []) if prompt_hash(it.prompt) not in seen_prompt_hashes and it.kind in kinds]
    if bank:                                                    # last resort: hand-authored items
        if run:
            run.fallback = True
            run.notes.append("model produced no valid items; hand-authored seed bank used")
        return PracticeOutcome(bank[:count], "seed_bank", rejected, run, calls)
    return PracticeOutcome([], "none", rejected, run, calls)
