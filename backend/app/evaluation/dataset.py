from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.knowledge.chunker import normalize_text

CATEGORIES = ("answerable", "ambiguous", "out_of_corpus", "in_domain_missing")
SPLITS = ("dev", "test")
DATA_DIR = Path(__file__).resolve().parents[3] / "eval" / "data"
DATASET = DATA_DIR / "retrieval.jsonl"


class DatasetError(ValueError):
    pass


@dataclass
class Question:
    id: str
    split: str
    category: str
    question: str
    gold: list[dict] = field(default_factory=list)  # [{"phrase": str, "grade": 1|2}]


def load_questions(path: Path = DATASET) -> list[Question]:
    out: list[Question] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            q = Question(id=d["id"], split=d["split"], category=d["category"], question=d["question"],
                         gold=d.get("gold", []))
        except (KeyError, json.JSONDecodeError) as e:
            raise DatasetError(f"{path.name}:{n}: {e!r}")
        out.append(q)
    validate(out)
    return out


def validate(qs: list[Question]) -> None:
    ids = [q.id for q in qs]
    if len(set(ids)) != len(ids):
        raise DatasetError("duplicate question ids")
    texts = [q.question.strip().lower() for q in qs]
    if len(set(texts)) != len(texts):
        raise DatasetError("duplicate question text (would leak between dev and test)")
    for q in qs:
        if q.split not in SPLITS:
            raise DatasetError(f"{q.id}: bad split {q.split!r}")
        if q.category not in CATEGORIES:
            raise DatasetError(f"{q.id}: bad category {q.category!r}")
        if q.category == "answerable":
            if not q.gold or not all(g.get("grade") in (1, 2) and g.get("phrase") for g in q.gold):
                raise DatasetError(f"{q.id}: answerable questions need graded gold phrases")
            if not any(g["grade"] == 2 for g in q.gold):
                raise DatasetError(f"{q.id}: needs at least one grade-2 phrase")
        elif q.gold:
            raise DatasetError(f"{q.id}: only answerable questions may carry gold evidence")
    for cat in CATEGORIES:  # both splits must contain every category (stratified)
        for split in SPLITS:
            if not any(q.category == cat and q.split == split for q in qs):
                raise DatasetError(f"split {split} has no {cat} questions")


def resolve_gold(q: Question, chunks: dict[str, str]) -> dict[str, int]:
    """chunk_id -> grade for chunks whose normalised text contains a gold phrase. Raises if a phrase matches nothing."""
    norm = {cid: normalize_text(t).casefold() for cid, t in chunks.items()}
    grades: dict[str, int] = {}
    for g in q.gold:
        phrase = normalize_text(g["phrase"]).casefold()
        hit = [cid for cid, t in norm.items() if phrase in t]
        if not hit:
            raise DatasetError(f"{q.id}: gold phrase not found in any chunk: {g['phrase']!r}")
        for cid in hit:
            grades[cid] = max(grades.get(cid, 0), g["grade"])
    return grades
