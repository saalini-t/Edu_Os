"""Embedding-model compatibility and performance spike (Stage 2). Dense-only, no database, DEV split only.
Selection rule is pre-registered in eval/THRESHOLDS.md. Usage: python scripts/embedding_spike.py [model ...]
Only public model weights are downloaded; no student data is involved."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.evaluation import dataset  # noqa: E402
from app.evaluation.harness import CORPUS  # noqa: E402
from app.evaluation.metrics import ndcg_at_k, recall_at_k, mrr  # noqa: E402
from app.knowledge.chunker import chunk_pages  # noqa: E402
from app.knowledge.pdf import extract_pages  # noqa: E402

CANDIDATES = ["sentence-transformers/all-MiniLM-L6-v2", "sentence-transformers/multi-qa-MiniLM-L6-cos-v1",
              "BAAI/bge-small-en-v1.5"]
QUERY_PREFIX = {"BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: "}


def main(models: list[str]) -> None:
    from sentence_transformers import SentenceTransformer
    chunks = {}
    for pdf, title in CORPUS:
        for c in chunk_pages(extract_pages(pdf.read_bytes(), 200), 120, 20):
            chunks[f"{title}#{c.chunk_index}"] = c.text
    ids, texts = list(chunks), list(chunks.values())
    dev = [q for q in dataset.load_questions() if q.split == "dev" and q.category == "answerable"]
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n_chunks": len(ids),
           "n_dev_questions": len(dev), "split": "dev", "models": {}}
    for name in models:
        t0 = time.perf_counter()
        try:
            m = SentenceTransformer(name, device="cpu")
        except Exception as e:  # download/compat failure is a result, not a crash
            out["models"][name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(name, "FAILED", type(e).__name__)
            continue
        load_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        D = m.encode(texts, normalize_embeddings=True, batch_size=16)
        enc_s = time.perf_counter() - t1
        pre = QUERY_PREFIX.get(name, "")
        Q = m.encode([pre + q.question for q in dev], normalize_embeddings=True)
        nd, rc, mr = [], [], []
        for q, qv in zip(dev, Q):
            order = np.argsort(-(D @ qv))
            ranked = [ids[i] for i in order[:6]]
            grades = dataset.resolve_gold(q, chunks)
            nd.append(ndcg_at_k(ranked, grades, 6)); rc.append(recall_at_k(ranked, set(grades), 3)); mr.append(mrr(ranked, set(grades)))
        out["models"][name] = {"dims": int(D.shape[1]), "load_seconds": round(load_s, 2),
                               "encode_seconds_for_corpus": round(enc_s, 3),
                               "chunks_per_second": round(len(texts) / enc_s, 1),
                               "dev_ndcg@6": round(float(np.mean(nd)), 4), "dev_recall@3": round(float(np.mean(rc)), 4),
                               "dev_mrr": round(float(np.mean(mr)), 4), "max_seq_length": m.max_seq_length}
        print(name, out["models"][name])
    ok = {k: v for k, v in out["models"].items() if "error" not in v}
    if ok:
        best = max(ok.values(), key=lambda v: v["dev_ndcg@6"])["dev_ndcg@6"]
        near = {k: v for k, v in ok.items() if best - v["dev_ndcg@6"] <= 0.02}
        pick = min(near, key=lambda k: (near[k]["dims"], near[k]["load_seconds"] + near[k]["encode_seconds_for_corpus"]))
        out["selected_by_preregistered_rule"] = pick
    d = ROOT.parent / "eval" / "reports" / "embedding-spike"
    d.mkdir(parents=True, exist_ok=True)
    (d / "spike.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("selected:", out.get("selected_by_preregistered_rule"))


if __name__ == "__main__":
    main(sys.argv[1:] or CANDIDATES)
