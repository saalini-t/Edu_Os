"""Standard ranking metrics. Graded relevance (0/1/2) for nDCG, binary for Recall/MRR/HitRate."""
from __future__ import annotations

import math
import random
from collections.abc import Sequence


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def hit_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return 1.0 if set(ranked[:k]) & relevant else 0.0


def mrr(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, c in enumerate(ranked, start=1):
        if c in relevant:
            return 1.0 / i
    return 0.0


def _dcg(grades: Sequence[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at_k(ranked: Sequence[str], grades: dict[str, int], k: int) -> float:
    """grades: chunk_id -> relevance grade for every relevant chunk. Ideal ranking uses all relevant chunks."""
    ideal = _dcg(sorted(grades.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return _dcg([grades.get(c, 0) for c in ranked[:k]]) / ideal


def bootstrap_ci(values: Sequence[float], n: int = 2000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float, float]:
    """(mean, lo, hi) percentile bootstrap. Deterministic for a given seed. Wide for small samples; report n alongside."""
    vals = list(values)
    if not vals:
        return (float("nan"),) * 3
    mean = sum(vals) / len(vals)
    if len(vals) == 1:
        return mean, mean, mean
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(vals, k=len(vals))) / len(vals) for _ in range(n))
    return mean, means[int(alpha / 2 * n)], means[int((1 - alpha / 2) * n) - 1]
