"""Retrieval + citation + insufficient-context evaluation. See eval/README.md and eval/THRESHOLDS.md.

    python -m app.evaluation.retrieval_eval --mode fts --splits dev test --label baseline-fts --workflow

Reads EVAL_DATABASE_URL (a DISPOSABLE database: schemas are dropped). Nothing here invents numbers: every figure
in a report is computed from the labelled data in that run. Threshold sweeps are restricted to the dev split."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.evaluation.dataset import DATASET, Question, load_questions, resolve_gold
from app.evaluation.harness import EvalEnv, build_env
from app.evaluation.metrics import bootstrap_ci, hit_at_k, mrr, ndcg_at_k, recall_at_k
from app.auth.principal import principal_for
from app.knowledge.service import KnowledgeService
from app.models import User

REPORT_DIR = Path(__file__).resolve().parents[3] / "eval" / "reports"
TOP_K = 6
NEGATIVES = ("out_of_corpus", "in_domain_missing")


# ---------------------------------------------------------------------------------------- retrieval collection
def collect_retrieval(env: EvalEnv, questions: list[Question], mode: str, retriever_factory=None) -> list[dict]:
    engine = make_engine(env.settings.database_url)
    records = []
    with make_session_factory(engine)() as db:
        student = db.get(User, env.student_id)
        principal = principal_for(db, student)
        retriever = (retriever_factory or default_retriever)(db, env.settings, mode)
        for q in questions:
            res = retriever.search(q.question, principal, course_id=env.course_id, top_k=TOP_K)
            grades = resolve_gold(q, env.chunk_texts) if q.category == "answerable" else {}
            records.append({
                "id": q.id, "split": q.split, "category": q.category, "question": q.question,
                "mode_used": getattr(res, "mode_used", getattr(res, "method", mode)),
                "n_terms": len(res.terms),
                "ranked": [{"chunk_id": h.chunk_id, "matched_terms": h.matched_terms,
                            "dense": getattr(h, "dense_similarity", None), "grade": grades.get(h.chunk_id, 0),
                            "page": h.page} for h in res.hits],
                "gold_grades": grades})
    engine.dispose()
    return records


def default_retriever(db: Session, settings: Settings, mode: str):
    if mode == "fts":
        return KnowledgeService(db, settings).retriever()
    from app.knowledge.retrieval import build_retriever  # added in Stage 2
    return build_retriever(db, settings, mode)


# ---------------------------------------------------------------------------------------- metrics
def supported(rec: dict, min_terms: int, min_chunks: int, min_sim: float | None = None) -> bool:
    need = max(1, min(min_terms, rec["n_terms"]))
    n = 0
    for h in rec["ranked"]:
        ok = h["matched_terms"] >= need
        if min_sim is not None and h.get("dense") is not None:
            ok = ok or h["dense"] >= min_sim
        n += ok
    return n >= min_chunks


def _agg(values: list[float]) -> dict:
    m, lo, hi = bootstrap_ci(values)
    return {"n": len(values), "mean": round(m, 4), "ci95": [round(lo, 4), round(hi, 4)]}


def ranking_metrics(records: list[dict]) -> dict:
    ans = [r for r in records if r["category"] == "answerable"]
    out: dict = {"n_answerable": len(ans)}
    for k in (3, 6):
        out[f"recall@{k}"] = _agg([recall_at_k([h["chunk_id"] for h in r["ranked"]], {c for c in r["gold_grades"]}, k) for r in ans])
        out[f"hit@{k}"] = _agg([hit_at_k([h["chunk_id"] for h in r["ranked"]], set(r["gold_grades"]), k) for r in ans])
        out[f"ndcg@{k}"] = _agg([ndcg_at_k([h["chunk_id"] for h in r["ranked"]], r["gold_grades"], k) for r in ans])
    out["mrr"] = _agg([mrr([h["chunk_id"] for h in r["ranked"]], set(r["gold_grades"])) for r in ans])
    return out


def support_metrics(records: list[dict], min_terms: int, min_chunks: int, min_sim: float | None = None) -> dict:
    pos = [r for r in records if r["category"] == "answerable"]
    neg = [r for r in records if r["category"] in NEGATIVES]
    tpr = [float(supported(r, min_terms, min_chunks, min_sim)) for r in pos]
    fpr = [float(supported(r, min_terms, min_chunks, min_sim)) for r in neg]
    by_cat = {c: _agg([float(supported(r, min_terms, min_chunks, min_sim)) for r in records if r["category"] == c])
              for c in ("answerable", "out_of_corpus", "in_domain_missing", "ambiguous")
              if any(r["category"] == c for r in records)}
    t, f = (sum(tpr) / len(tpr) if tpr else float("nan")), (sum(fpr) / len(fpr) if fpr else float("nan"))
    return {"min_terms": min_terms, "min_chunks": min_chunks, "min_sim": min_sim,
            "tpr_answerable_supported": _agg(tpr), "fpr_unanswerable_supported": _agg(fpr),
            "youden_j": round(t - f, 4), "supported_rate_by_category": by_cat}


def sweep_dev(dev_records: list[dict], min_sims: list[float | None]) -> tuple[list[dict], dict]:
    """Pre-registered grid and criterion (eval/THRESHOLDS.md). Dev records ONLY."""
    assert all(r["split"] == "dev" for r in dev_records), "threshold sweeps may only use the dev split"
    rows = []
    for ms in min_sims:
        for mt in (1, 2, 3, 4, 5):
            for mc in (1, 2, 3):
                m = support_metrics(dev_records, mt, mc, ms)
                rows.append({"min_terms": mt, "min_chunks": mc, "min_sim": ms, "J": m["youden_j"],
                             "tpr": m["tpr_answerable_supported"]["mean"], "fpr": m["fpr_unanswerable_supported"]["mean"]})
    # max J; tie-break: more conservative (higher min_sim, then min_terms, then min_chunks)
    best = max(rows, key=lambda r: (r["J"], r["min_sim"] if r["min_sim"] is not None else -1, r["min_terms"], r["min_chunks"]))
    return rows, best


def failure_cases(records: list[dict], min_terms: int, min_chunks: int, min_sim: float | None) -> dict:
    ans_miss, ans_late, false_support = [], [], []
    for r in records:
        ids = [h["chunk_id"] for h in r["ranked"]]
        if r["category"] == "answerable":
            rel = set(r["gold_grades"])
            first = next((i for i, c in enumerate(ids, 1) if c in rel), None)
            if first is None:
                ans_miss.append({"id": r["id"], "question": r["question"]})
            elif first > 1:
                ans_late.append({"id": r["id"], "question": r["question"], "first_relevant_rank": first})
            if not supported(r, min_terms, min_chunks, min_sim):
                ans_miss.append({"id": r["id"], "question": r["question"], "reason": "not_supported_by_threshold"})
        elif r["category"] in NEGATIVES and supported(r, min_terms, min_chunks, min_sim):
            false_support.append({"id": r["id"], "category": r["category"], "question": r["question"],
                                  "top_matched_terms": [h["matched_terms"] for h in r["ranked"][:3]]})
    return {"answerable_missed": ans_miss, "answerable_relevant_not_first": ans_late,
            "unanswerable_falsely_supported": false_support}


# ---------------------------------------------------------------------------------------- workflow-level
def collect_workflow(env: EvalEnv, questions: list[Question], records: list[dict]) -> list[dict]:
    s = env.settings
    client = TestClient(__import__("app.main", fromlist=["create_app"]).create_app(s))
    pw = s.seed_demo_password
    tok = lambda e: {"Authorization": "Bearer " + client.post("/v1/auth/login", json={"email": e, "password": pw}).json()["access_token"]}
    stu, adm = tok("student1@demo.local"), tok("admin@demo.local")
    grades = {r["id"]: r["gold_grades"] for r in records}
    out = []
    for i, q in enumerate(questions):
        r = client.post("/v1/doubts", headers={**stu, "Idempotency-Key": f"eval-{q.id}-{i:04d}"},
                        json={"course_id": str(env.course_id), "text": q.question})
        d = client.get(f"/v1/doubts/{r.json()['session_id']}", headers=stu).json()
        t = client.get(f"/v1/admin/runs/{r.json()['run_id']}/trace", headers=adm).json()
        li = d["latest_intervention"] or {}
        cites = (li.get("explanation") or {}).get("citations", [])
        g = grades.get(q.id, {})
        answered = li.get("action") == "GENERATE_EXPLANATION" and li.get("fallback") is None and len(cites) >= 1
        out.append({"id": q.id, "split": q.split, "category": q.category, "action": li.get("action"),
                    "rules": t["summary"]["rules_fired"], "answered": answered, "fallback": li.get("fallback"),
                    "n_citations": len(cites),
                    "n_relevant_citations": sum(1 for c in cites if g.get(c["chunk_id"], 0) > 0),
                    "all_citations_verified": all(c.get("verified") for c in cites)})
    return out


def workflow_metrics(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["category"] == "answerable"]
    neg = [r for r in rows if r["category"] in NEGATIVES]
    amb = [r for r in rows if r["category"] == "ambiguous"]
    cited = [r for r in ans if r["answered"]]
    tot_c, rel_c = sum(r["n_citations"] for r in cited), sum(r["n_relevant_citations"] for r in cited)
    return {
        "answerable_answered_with_citation": _agg([float(r["answered"]) for r in ans]),
        "answerable_has_relevant_citation": _agg([float(r["n_relevant_citations"] > 0) for r in ans]),
        "citation_precision_micro": {"n_citations": tot_c, "relevant": rel_c, "value": round(rel_c / tot_c, 4) if tot_c else None},
        "unanswerable_answered_with_citation": _agg([float(r["answered"]) for r in neg]),
        "unanswerable_first_action": _count([r["action"] for r in neg]),
        "ambiguous_asked_clarification": _agg([float(r["action"] == "ASK_CLARIFICATION") for r in amb]),
        "all_emitted_citations_verified": all(r["all_citations_verified"] for r in rows),
    }


def _count(xs):
    d: dict = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return d


# ---------------------------------------------------------------------------------------- reporting
def render_md(rep: dict) -> str:
    L = [f"# Retrieval evaluation: {rep['label']}", "",
         f"- generated: {rep['generated_at']}  |  git: {rep['git_commit']}  |  mode: `{rep['mode']}`",
         f"- dataset: `{rep['dataset']}` ({rep['n_questions']} questions)  |  corpus chunks: {rep['n_chunks']}",
         f"- thresholds used: `{rep['thresholds']}`", f"- embedding: `{rep.get('embedding')}`", ""]
    for split, sec in rep["splits"].items():
        L += [f"## Split: {split}", "", "### Ranking (answerable questions)", "", "| metric | n | mean | 95% CI |", "|---|---|---|---|"]
        for k, v in sec["ranking"].items():
            if isinstance(v, dict):
                L.append(f"| {k} | {v['n']} | {v['mean']} | {v['ci95'][0]} to {v['ci95'][1]} |")
        sm = sec["support"]
        L += ["", "### Support / insufficient-context (retrieval level)", "",
              f"TPR answerable supported: {sm['tpr_answerable_supported']['mean']} (CI {sm['tpr_answerable_supported']['ci95']}, n={sm['tpr_answerable_supported']['n']})  ",
              f"FPR unanswerable supported: {sm['fpr_unanswerable_supported']['mean']} (CI {sm['fpr_unanswerable_supported']['ci95']}, n={sm['fpr_unanswerable_supported']['n']})  ",
              f"Youden J: {sm['youden_j']}", "", "| category | n | supported rate |", "|---|---|---|"]
        for c, v in sm["supported_rate_by_category"].items():
            L.append(f"| {c} | {v['n']} | {v['mean']} |")
        if "workflow" in sec:
            L += ["", "### Workflow level (fake provider, full pipeline)", "", "```json", json.dumps(sec["workflow"], indent=1), "```"]
        L += ["", "### Failure cases", "", "```json", json.dumps(sec["failures"], indent=1), "```", ""]
    if "sweep" in rep:
        L += ["## Dev threshold sweep (pre-registered grid; dev split only)", "",
              f"Selected by max J, conservative tie-break: **{rep['sweep']['selected']}**", "",
              "| min_terms | min_chunks | min_sim | J | TPR | FPR |", "|---|---|---|---|---|---|"]
        for r in sorted(rep["sweep"]["grid"], key=lambda r: -r["J"])[:15]:
            L.append(f"| {r['min_terms']} | {r['min_chunks']} | {r['min_sim']} | {r['J']:.3f} | {r['tpr']:.3f} | {r['fpr']:.3f} |")
        L.append("")
    L += ["## Limitations", ""] + [f"- {x}" for x in rep["limitations"]]
    return "\n".join(L) + "\n"


LIMITATIONS = [
    "Small corpus (tens of chunks); many candidates are trivially separable and metrics saturate easily.",
    "Labels written by a single author, not double-annotated; evidence phrases favour lexical overlap with the questions.",
    "Dev/test samples are small (see n); confidence intervals are wide, differences smaller than the CI are not evidence of improvement.",
    "Workflow-level numbers use the deterministic FAKE provider, so they measure retrieval, policy and citation plumbing, not LLM quality.",
]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPORT_DIR.parent, text=True, stderr=subprocess.DEVNULL).strip() + "+worktree"
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="fts", choices=["fts", "dense", "hybrid"])
    ap.add_argument("--splits", nargs="+", default=["dev"], choices=["dev", "test"])
    ap.add_argument("--min-terms", type=int, default=2)
    ap.add_argument("--min-chunks", type=int, default=1)
    ap.add_argument("--min-sim", type=float, default=None)
    ap.add_argument("--sweep", action="store_true", help="dev-only threshold sweep (needs 'dev' in --splits)")
    ap.add_argument("--sim-grid", type=float, nargs="*", default=None, help="min_sim grid (fixed before looking at dev results)")
    ap.add_argument("--workflow", action="store_true")
    ap.add_argument("--label", default="run")
    ap.add_argument("--embedding-provider", default=None)
    args = ap.parse_args(argv)

    url = os.environ.get("EVAL_DATABASE_URL")
    if not url:
        print("Set EVAL_DATABASE_URL to a DISPOSABLE database (name must contain 'test' or 'eval').", file=sys.stderr)
        return 2
    extra = {"embedding_provider": args.embedding_provider} if args.embedding_provider else {}
    settings = Settings(_env_file=None, app_env="test", database_url=url, jwt_secret=secrets.token_urlsafe(48),
                        storage_dir=str(Path(os.environ.get("EVAL_STORAGE", ".eval_storage")).resolve()),
                        ret_min_terms=args.min_terms, ret_min_chunks=args.min_chunks,
                        **({"ret_min_sim": args.min_sim} if args.min_sim is not None else {}), **extra)
    questions = load_questions()
    hook = None
    if args.mode != "fts":
        from app.knowledge.retrieval import eval_index_hook
        hook = eval_index_hook
    env = build_env(settings, index_hook=hook)

    rep: dict = {"label": args.label, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "git_commit": git_commit(), "mode": args.mode, "dataset": str(DATASET.name),
                 "n_questions": len(questions), "n_chunks": len(env.chunk_texts),
                 "thresholds": {"min_terms": args.min_terms, "min_chunks": args.min_chunks, "min_sim": args.min_sim},
                 "embedding": getattr(settings, "embedding_model", None) if args.mode != "fts" else None,
                 "splits": {}, "limitations": LIMITATIONS}
    all_records = collect_retrieval(env, [q for q in questions if q.split in args.splits], args.mode)
    for split in args.splits:
        qs = [q for q in questions if q.split == split]
        recs = [r for r in all_records if r["split"] == split]
        sec = {"ranking": ranking_metrics(recs),
               "support": support_metrics(recs, args.min_terms, args.min_chunks, args.min_sim),
               "failures": failure_cases(recs, args.min_terms, args.min_chunks, args.min_sim),
               "mode_used": sorted({r["mode_used"] for r in recs}), "records": recs}
        if args.workflow:
            rows = collect_workflow(env, qs, recs)
            sec["workflow"] = workflow_metrics(rows)
            sec["workflow_rows"] = rows
            sec["failures"]["answerable_not_answered_by_workflow"] = [
                {"id": r["id"], "action": r["action"], "rules": r["rules"], "fallback": r["fallback"]}
                for r in rows if r["category"] == "answerable" and not r["answered"]]
        rep["splits"][split] = sec
    if args.sweep:
        if "dev" not in args.splits:
            print("--sweep needs the dev split", file=sys.stderr)
            return 2
        dev = [r for r in all_records if r["split"] == "dev"]
        grid, best = sweep_dev(dev, [None] + list(args.sim_grid or []))
        rep["sweep"] = {"grid": grid, "selected": best}
    out = REPORT_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    (out / "report.md").write_text(render_md(rep), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
