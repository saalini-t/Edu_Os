# Retrieval and policy thresholds: selection record

This file records **how each threshold value was selected**. The selection procedure below was written
**before** any threshold sweep was run. Values are only ever chosen from the **development** split; the held-out
**test** split is used to report, never to tune.

## Pre-registered selection procedure (written 2026-10-09, before the first sweep)

Questions are labelled in `eval/data/retrieval.jsonl` (see `eval/README.md`). For a candidate threshold setting,
a question is *supported* when retrieval returns at least `RET_MIN_CHUNKS` passages that satisfy the support signal.

* **Positive class:** `answerable` questions (retrieval should be supported).
* **Negative class:** `out_of_corpus` and `in_domain_missing` questions (retrieval should NOT be supported).
* `ambiguous` questions are excluded from threshold selection (they are handled by the clarification rule R3).
* **TPR** = fraction of dev `answerable` questions that are supported. **FPR** = fraction of dev negative questions that are supported.
* **Criterion:** maximise Youden's J = TPR − FPR over the dev grid. **Tie-break:** prefer the more conservative setting
  (higher `RET_MIN_TERMS`/`RET_MIN_SIM`, then higher `RET_MIN_CHUNKS`).
* **Grids:** lexical `RET_MIN_TERMS` ∈ {1,2,3,4,5}, `RET_MIN_CHUNKS` ∈ {1,2,3}. If a dense similarity signal is added (Stage 2),
  `RET_MIN_SIM` is swept over a grid fixed before looking at dev results for that signal (recorded in the Stage 2 section).
* **Caveat accepted in advance:** with ~16 dev positives and ~8 dev negatives, J has a wide uncertainty; the selected value is a
  development default, not a validated operating point. Bootstrap CIs are reported next to every metric.
* **Test split rule:** the test split is evaluated for reporting only. Every test-split run is listed in the log below.
  No threshold is changed after a test-split run without a new entry explaining why and acknowledging the test split is then
  no longer held out for that threshold.

## Threshold values

| Threshold | Meaning | Value in use | Selected how | Status |
|---|---|---|---|---|
| `RET_MIN_TERMS` | distinct query terms a passage must match to count as lexical support | 2 | Phase 1 manual probe on 3 queries (not a tuned value) | **untuned default; to be replaced by the dev sweep below** |
| `RET_MIN_CHUNKS` | supported passages needed before the policy treats retrieval as grounding (rule R4) | 1 | Phase 1 default | **untuned default; same sweep** |
| `T_CLARIFY` | classification confidence below which R3 asks for clarification | 0.5 | Phase 1 default | untuned; not swept (depends on the fake provider, meaningless to tune until a real model exists) |
| `T_MASTER`, `N_MIN`, `HALF_LIFE_DAYS` | mastery model | 0.8 / 3 / n/a | Phase 1 defaults | not used until Phase 4 |

## Run log

(Appended by hand after each evaluation run; the machine-readable reports are in `eval/reports/`.)

| Date | Run | Split(s) evaluated | Thresholds used | Report |
|---|---|---|---|---|
| 2026-10-09 | FTS baseline, first run (taxonomy without supplement topics) | dev, test | min_terms=2, min_chunks=1 (untuned defaults) | `reports/20261009T014432-baseline-fts-untuned-defaults` (superseded: per-question workflow rows were not stored) |
| 2026-10-09 | FTS baseline, re-run storing per-question rows | dev, test | min_terms=2, min_chunks=1 | `reports/20261009T014507-baseline-fts-defaults-rows` (superseded: eval taxonomy lacked topics for the supplement) |
| 2026-10-09 | **FTS baseline (reference)** with eval-only supplement topics | dev, test | min_terms=2, min_chunks=1 | `reports/20261009T014540-baseline-fts-defaults-evaltopics` |

| 2026-10-09 | Embedding-model spike (dense only, **dev** questions) | dev | n/a | `reports/embedding-spike/spike.json` |
| 2026-10-09 | Hybrid dev-only threshold sweep (workflow incl.) | dev | grid | `reports/20261009T015401-hybrid-dev-sweep` |
| 2026-10-09 | **Final fts** (dev-selected 4/1) | dev, test | min_terms=4, min_chunks=1 | `reports/20261009T015428-final-fts` |
| 2026-10-09 | **Final dense** | dev, test | min_terms=4, min_chunks=1, min_sim=0.6 | `reports/20261009T015502-final-dense` |
| 2026-10-09 | **Final hybrid** | dev, test | min_terms=4, min_chunks=1, min_sim=0.6 | `reports/20261009T015534-final-hybrid` |

(the superseded-by-determinism note and the final counts are in the section "Correction: non-deterministic tie-breaking" below)

| 2026-10-09 | Re-confirm after the ingestion/lifecycle refactor (fts, hybrid) | dev, test | 4/1 (+ sim 0.6 for hybrid) | `reports/20261009T024908-reconfirm-fts`, `reports/20261009T025003-reconfirm-hybrid` (**superseded**, see correction) |
| 2026-10-09 | Deterministic dev-only sweeps (fts, hybrid) | dev | grid | `reports/20261009T025204-det-fts-dev-sweep`, `reports/20261009T025200-det-hybrid-dev-sweep` |
| 2026-10-09 | **Reference runs, deterministic ordering**: fts ×2, dense ×1, hybrid ×2 | dev, test | fts 4/1; dense and hybrid 4/1/sim 0.6 | `reports/20261009T025234-det-final-fts-a`, `-fts-b`, `reports/20261009T025328-det-final-dense-a`, `reports/20261009T025401-det-final-hybrid-a`, `-hybrid-b` |

## Stage 1 result: lexical (full-text) support signal

All three baseline runs above used the same untuned thresholds; the re-runs only changed the harness (stored rows; added eval-only topics),
so the **test split has been evaluated 3 times with unchanged thresholds** and was not used to choose anything.
The two superseded reports were kept (a deletion attempt was blocked by a safety check) and are not the reference.

Dev-only sweep of the pre-registered grid on the FTS signal (top rows by the criterion; J = TPR − FPR, n_pos = 16, n_neg = 8):

| RET_MIN_TERMS | RET_MIN_CHUNKS | J | TPR | FPR |
|---|---|---|---|---|
| 4 | 1 | 0.875 | 0.875 | 0.000 |
| 3 | 1 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.625 | 0.625 | 0.000 |
| 2 | 3 | 0.500 | 0.750 | 0.250 |
| 2 | 2 | 0.375 | 0.750 | 0.375 |
| 2 | 1 | 0.375 | 1.000 | 0.625 |

Selected for the lexical signal: **RET_MIN_TERMS = 4, RET_MIN_CHUNKS = 1** (max J = 0.875).
This is **not yet applied** to `config.py`: the final operating point is selected after hybrid retrieval exists (Stage 2), because the
support signal changes. With 16 positives and 8 negatives the dev J has a very wide uncertainty.

## Stage 2 pre-registration: embedding-model selection (written before running the spike)

Candidates (all small, permissively licensed, runnable on CPU): `sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0),
`sentence-transformers/multi-qa-MiniLM-L6-cos-v1` (Apache-2.0), `BAAI/bge-small-en-v1.5` (MIT).
Procedure: embed the evaluation-corpus chunks and the **dev** `answerable` questions, rank chunks by cosine similarity (dense-only,
no database), and compare **dev nDCG@6**. **Selection rule:** highest dev nDCG@6; if two candidates are within 0.02 of each other,
prefer the smaller embedding dimension, then the faster load + encode time. The **test** split is not touched in the spike.
Also recorded (not used for selection): dimension, model load time, encode throughput, on-disk size where available.
Requirement: the model must load and run **offline after the first download** (no student data is ever sent anywhere; only public
model weights are downloaded once).

## Stage 2 pre-registration: dense support threshold and fusion (written before the hybrid sweep)

* `RET_MIN_SIM` (cosine similarity a passage must reach to count as semantic support) is swept over the grid
  {0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60} (fixed now; independent of the model's actual score range, which may make some values trivial).
  A passage counts as support if it meets the lexical rule **or** the dense rule.
* Same criterion (max Youden J on **dev**, conservative tie-break) over `RET_MIN_TERMS` ∈ {1..5}, `RET_MIN_CHUNKS` ∈ {1,2,3}, `RET_MIN_SIM` ∈ grid ∪ {off}.
* RRF constant `k = 60` and a candidate pool of 20 per ranker are fixed a priori (the commonly used default) and **not tuned**.
* The test split is evaluated once with the selected values (plus once more for each deliberate re-run, which will be logged).

## Stage 2 result: embedding model and hybrid support signal

**Embedding model.** The three candidates were run dense-only on the dev questions (`reports/embedding-spike/spike.json`): all load and run on CPU with 384 dims.
Dev nDCG@6: all-MiniLM-L6-v2 0.961, bge-small-en-v1.5 0.9645, multi-qa-MiniLM-L6-cos-v1 0.9344. The two leaders are within the pre-registered 0.02 band and have the
same dimension, so the pre-registered tie-break (faster load + encode, about 5.9 s vs 14.6 s) selects **`sentence-transformers/all-MiniLM-L6-v2`**.
The differences are far below what 16 questions can resolve; the choice is a practical one, not evidence that this model is better.

**Hybrid dev sweep** (pre-registered grid; criterion max J, tie-break toward larger values; n_pos = 16, n_neg = 8):

| RET_MIN_TERMS | RET_MIN_CHUNKS | RET_MIN_SIM | J | TPR | FPR |
|---|---|---|---|---|---|
| 4 | 1 | 0.6 | 0.875 | 1.000 | 0.125 |
| 4 | 1 | None | 0.875 | 0.875 | 0.000 |
| 3 | 1 | 0.6 | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.55 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.5 | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.5 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.45 | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.45 | 0.750 | 1.000 | 0.250 |

Selected by the rule as written: **RET_MIN_TERMS = 4, RET_MIN_CHUNKS = 1, RET_MIN_SIM = 0.6**. Two caveats recorded honestly:
(1) it **ties** on J with "similarity support off" (also 0.875; TPR 0.875 / FPR 0.0). The written tie-break prefers the larger value, which picks 0.6, although "off" is arguably the more
conservative operating point. (2) 0.6 is the **edge of the pre-registered grid**, so the true optimum for this model may lie above it. Neither caveat was resolved by looking at the test split.
`RET_MIN_SIM` applies only to this model: it is set in `.env.example`, not as a code default.
`RET_MIN_TERMS = 4` is now the code default (it was the dev optimum for both the lexical-only and the hybrid signal). `RET_MIN_CHUNKS = 1` is unchanged.
Fusion constants (RRF k = 60, pool = 20) were fixed a priori and not tuned.

## Correction: non-deterministic tie-breaking (found and fixed during Stage 6)

Re-running the same configuration produced slightly different ranking numbers (e.g. FTS test nDCG@6 0.929 vs 0.926; hybrid test nDCG@6 0.923 vs 0.969).
Cause: when scores tied, results were ordered by the chunk UUID, which is regenerated on every ingestion, so the order (and nDCG/MRR) varied from run to run.
Fix: ties are now broken by stable content keys (document title, page, chunk index) in the full-text, dense and fused rankers; regression tests cover all three.

Consequences, stated plainly:
* Every ranking metric (nDCG, Recall, MRR) and the workflow-level citation numbers in the earlier `final-*` and `reconfirm-*` reports, and in the baselines, **carried random tie-breaking noise**. Those reports are kept for the record but are **superseded**; the reference results are the `det-final-*` reports.
* The threshold sweep selection is based on support rates (TPR/FPR), which do not depend on tie order. It was **re-run with deterministic ordering and selected the same values** (FTS 4/1; hybrid 4/1/0.6, same tie with "similarity support off" and same grid-edge caveat), so no threshold changed.
* Repeating the deterministic runs gave **identical** metrics on both splits (fts a/b, hybrid a/b).
* The size of the earlier run-to-run swings (up to ~0.05 nDCG on 16 questions) is itself a measure of how little this evaluation can resolve.

### Test-split evaluation count (all with unchanged selected thresholds; nothing was tuned on test)
FTS: 3 baselines (untuned 2/1) + `final` + `reconfirm` + 2 deterministic = **7**. Dense: `final` + 1 deterministic = **2**. Hybrid: `final` + `reconfirm` + 2 deterministic = **4**.
Because the test split has been looked at this many times (always for reporting), it should be treated as **no longer fully held out**; a new labelled set is needed before any further threshold decision.

## Values currently in use

| Threshold | Value | Where set | Status |
|---|---|---|---|
| `RET_MIN_TERMS` | 4 | code default (`config.py`) | dev-selected, tiny set |
| `RET_MIN_CHUNKS` | 1 | code default | unchanged default; dev-selected value equals it |
| `RET_MIN_SIM` | 0.6 for `all-MiniLM-L6-v2` only | `.env.example` (unset in code) | dev-selected at the grid edge; model-specific |
| RRF `k`, candidate pool | 60, 20 | code defaults | fixed a priori, never tuned |
| `T_CLARIFY`, `T_MASTER`, `N_MIN` | 0.5, 0.8, 3 | code defaults | **untuned**; depend on a real LLM / the Phase 4 learner model |


## Phase 3–5 additions (mastery and escalation parameters)

| Parameter | Value | Status |
|---|---|---|
| Mastery model | Beta-Bernoulli, prior alpha0 = beta0 = 1, exponential decay with half-life `MASTERY_HALF_LIFE_DAYS` = 14 | design choice, not fitted |
| `T_MASTER` | 0.75 (lowered from 0.8 so three clean correct answers can pass) | **untuned** |
| `N_MIN`, distinct sources | 3 evidence items from >= 2 distinct items/assessments | **untuned** |
| Attempt weights | exact-graded 1.0, model-graded less (`W_LLM`), easy items reduced, hints reduce | **untuned** |
| Model-grade evidence gate | grader uncertainty must be <= 0.5 to count | **untuned** |
| Teacher assessment weight `W_TEACHER` | 2.0 (bounded <= 3); "emerging" carries zero weight | **untuned** |
| Gap confirmation | 2 failed attempts on distinct targeted items, or a teacher decision | policy (not a fitted value) |
| Gap Map "improving" | emerging with mean >= 0.6 | **untuned** |
| Escalation TTL | 48 h | arbitrary default |
| Matching weights | topic 0.4, availability 0.25, language 0.15, feedback 0.1, load 0.1 | **untuned**, deterministic |

None of these were validated on real learner data. No real learner data exists for this prototype.
