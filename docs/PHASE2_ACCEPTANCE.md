# Phase 2 Status and Acceptance: hybrid retrieval, asynchronous ingestion, lifecycle/OCR, evidence ledger

| | |
|---|---|
| **Date** | 2026-10-09 |
| **Scope** | Stages 0–6 of the Phase 2 brief, on top of the Phase 1 modular monolith ([PHASE1_ACCEPTANCE](PHASE1_ACCEPTANCE.md)) |
| **Nature of this document** | Records what was built and what was actually run. Anything under "Not verified" or "Limitations" was not established. Numbers come from saved reports, not from memory. |

## 1. Stage summary

| Stage | Status | Evidence (details below) |
|---|---|---|
| 0 Baseline | **PASS** (with defects found and fixed) | §2 |
| 1 Evaluation baseline | **PASS** (small, single-annotator dataset; limitations in §6) | `eval/`, `eval/THRESHOLDS.md` |
| 2 pgvector + hybrid retrieval | **PASS** (measured: no ranking gain beyond noise; improves recall of "supported" answerable questions) | §4 |
| 3 Asynchronous ingestion (Redis + worker) | **PASS** | §3, `e2e/compose_async_check.py` |
| 4 Lifecycle (replace / re-index / delete) and OCR | **PASS** | §3, `e2e/compose_ocr_lifecycle_check.py` |
| 5 Acknowledgment endpoint + evidence ledger skeleton | **PASS** (foundation only; no mastery model) | §3 |
| 6 Final validation + documentation | **PASS**, with the **unverified** items in §7 | this document |

Phase 2 is **complete for the stages listed above**. It is not a claim that retrieval quality is good: the measured differences between full-text, dense and hybrid retrieval are smaller than the uncertainty of a 16-question evaluation.

## 2. Stage 0: baseline before any change

| Check | Result | Evidence |
|---|---|---|
| Existing backend tests | **PASS** 83 passed | `python -m pytest` (private PostgreSQL 16 + pgvector container) |
| Frontend type-check + production build | **PASS** | `npm run build` |
| Docker build + start | **PASS** | `docker compose up -d --build`; postgres and backend `healthy` |
| Readiness | **PASS** | `/readyz`: database, migrations, storage ok |
| Login, ingest/search seeded document, create doubt, admin trace | **PASS** | scripted `curl` run: 13 chunks, search hits, rule `R6_low_evidence_explain`, steps `understand → load_context → decide → explain`, 3 verified citations |
| Browser smoke test | **PASS** (was NOT RUN in Phase 1) | `e2e/test_browser_smoke.py`, Playwright driving the installed Chrome via the Vite dev server |
| Docker (was NOT RUN in Phase 1) | **PASS** | as above |

Defects found during Stage 0/1 and fixed: none in Phase 1 behaviour. One latent packaging risk was found later (the `.dockerignore` item in §5).

## 3. What was built and how it was verified

### Stage 3: asynchronous ingestion
* PostgreSQL is the **source of truth** for jobs (`know.ingestion_jobs`, claimed with `FOR UPDATE SKIP LOCKED`, leases, bounded retries with exponential backoff, a partial unique index allowing one active job per document). **Redis is only a wake-up channel**; if it is down a job stays durable and the worker finds it by polling.
* Document states: `QUEUED → PROCESSING → READY | FAILED`; upload returns **202** with `indexed: false` until the worker finishes.
* Verified by tests (16) including: concurrent claims never double-process; transient failure retries with backoff; retries are bounded and the error stays visible; permanent failure fails fast; a failure or a **hard crash mid-write** leaves no partial chunks; worker death is recovered by lease expiry; Redis unavailable still accepts and processes; a **real worker subprocess** with restart recovery; authorization on upload/status/search. Mutation checks confirmed the reaper and atomic-write tests fail when those behaviours are broken.
* Verified in Docker (`e2e/compose_async_check.py`): normal flow with Redis wake-up (`queue: notified`); worker stopped → stays `QUEUED` → restarted worker finishes it; Redis stopped → upload accepted (`queue: deferred`) and processed by polling.

### Stage 4: lifecycle and OCR
* Documents have an **active version**; replace and re-index build the new version's chunks and swap them in with **one transaction** (all old chunks deleted, embeddings cascade, version bumped). A failed replacement leaves the live version untouched (`READY`, old content searchable); replaced content is not searchable afterwards; delete removes chunks, embeddings, jobs and every stored file immediately; a metadata-only audit trail (`know.document_events`) survives deletion.
* OCR is **optional** (`OCR_ENGINE=none` default) and runs **per page only where the text layer is missing**, with a page budget, a pixel cap, per-page failure reporting, and a **sandboxed child process** with a timeout and (POSIX) memory limit. Engine: RapidOCR (pip-installable, local). Tesseract is **not** implemented.
* Verified by tests (12 lifecycle + 16 OCR/parser) including real RapidOCR end to end, timeout/crash isolation, and re-indexing a mixed document after enabling OCR. In Docker (`e2e/compose_ocr_lifecycle_check.py`): a scanned PDF OCRed inside the worker container; replace → re-index → delete with the audit trail; **Linux memory limit** (`PARSER_MEMORY_MB=32` → `PARSER_RESOURCE_LIMIT`, `1024` → success). The POSIX-only memory test is skipped on Windows and covered by this Docker check.

### Stage 5: acknowledgment and evidence ledger
* `POST /v1/doubts/{id}/ack` (`understood | still_confused | check_me`, `Idempotency-Key` required) records a row in the **append-only** `core.evidence_events` (database trigger rejects UPDATE/DELETE). Only three zero-weight types exist; CHECK constraints make a self-report with non-zero weight impossible even through raw SQL. The ack is **never** turned into mastery: the policy still sees `mastery.status = unknown` and a run ends `COMPLETED / UNVERIFIED` (practice generation does not exist yet).
* Verified by tests (12): authorization, validation, duplicate delivery applied once, a concurrent race with exactly one winner, DB-level constraints and append-only behaviour; mutation checks (replay removed; ack given mastery weight) fail the tests.
* No mastery algorithm exists. `learner_topic_state`, hypotheses and Beta updates are Phase 4.

## 4. Retrieval evaluation (Stage 1–2)

Dataset: 54 labelled questions (32 answerable, 6 ambiguous, 8 out-of-corpus, 8 in-domain-but-missing), split 27 dev / 27 test, over 25 chunks (the 6-page demo PDF + a 7-page synthetic supplement). Reports: `eval/reports/*det-final-*`. Thresholds: `eval/THRESHOLDS.md`. **n = 16 answerable and 8 unanswerable questions per split.**

Reference results (deterministic ordering; repeats were identical; thresholds 4/1, plus similarity 0.6 for dense/hybrid):

| mode | split | nDCG@6 [95% CI] | Recall@3 [95% CI] | MRR [95% CI] | TPR | FPR | answerable answered with citation | citation precision (relevant/emitted) | unanswerable answered with citation | ambiguous asked clarification |
|---|---|---|---|---|---|---|---|---|---|---|
| fts | dev | 0.939 [0.85, 1.00] | 0.969 [0.91, 1.00] | 0.927 [0.82, 1.00] | 0.875 | 0.000 | 0.875 | 0.711 (27/38) | 0.000 | 1.000 |
| fts | test | 0.933 [0.83, 1.00] | 0.938 [0.81, 1.00] | 0.911 [0.78, 1.00] | 0.875 | 0.000 | 0.812 | 0.588 (20/34) | 0.000 | 0.667 |
| dense | dev | 0.961 [0.89, 1.00] | 0.906 [0.75, 1.00] | 0.953 [0.86, 1.00] | 1.000 | 0.125 | 0.938 | 0.737 (28/38) | 0.125 | 1.000 |
| dense | test | 0.923 [0.84, 1.00] | 1.000 [1.00, 1.00] | 0.896 [0.78, 1.00] | 1.000 | 0.000 | 0.875 | 0.657 (23/35) | 0.000 | 0.667 |
| hybrid | dev | 0.924 [0.84, 1.00] | 0.969 [0.91, 1.00] | 0.906 [0.81, 1.00] | 1.000 | 0.125 | 0.938 | 0.737 (28/38) | 0.125 | 1.000 |
| hybrid | test | 0.946 [0.87, 1.00] | 1.000 [1.00, 1.00] | 0.927 [0.82, 1.00] | 1.000 | 0.000 | 0.875 | 0.676 (23/34) | 0.000 | 0.667 |

("TPR/FPR": share of answerable / unanswerable questions for which retrieval is "supported" at the selected thresholds; the workflow columns use the fake provider, so they measure retrieval + policy + citation plumbing, not LLM quality. Citation **verification** passed for every emitted citation in every run; "precision" is whether the cited chunk contains the gold evidence.)

What the numbers support, and what they do not:
* **Ranking:** all three modes are within each other's confidence intervals on both splits. There is **no evidence that dense or hybrid ranks better** than full text on this corpus; the corpus is tiny and questions share vocabulary with the text.
* **Support signal:** with the dev-selected thresholds, hybrid supports every answerable question in both splits (TPR 1.0 vs 0.875 for full text). Cost: one in-domain-but-missing question on dev (`m07`, "TCP BBR congestion control") is falsely supported; full text supported none. That is a difference of one or two questions.
* **Honest failure cases:** full-text misses `q01`, `q13` (dev) and `q06`, `q12` (test) at the 4-term threshold; in the full workflow the fake `understand` step asks for clarification instead of answering `q13` (dev), `q02` and `q32` (test) in both modes, and with **full text only** rule R4 escalates `q19` ("What is BGP?") and `q22` (recursive DNS resolver) as ungrounded; hybrid answers both. Hypothesis (not tested): the workflow appends the topic name to the query, which raises the number of terms a passage must match.
* **Citation precision** is 0.59–0.74: roughly a third of emitted citations point at passages that do not contain the gold evidence even though they pass verification. Verification proves a quote exists in a retrieved chunk, not that it supports the claim.
* **Threshold caveats** (details in `eval/THRESHOLDS.md`): `RET_MIN_SIM=0.6` is at the edge of the pre-registered grid and ties with "similarity off"; the test split has been looked at several times (always for reporting) and should no longer be treated as held out.
* **Correction found in Stage 6:** ties were broken by random chunk UUIDs, which made ranking numbers vary between identical runs. Fixed (stable content-based tie-break, with regression tests); earlier reports are superseded; thresholds were re-derived and did not change.

Embedding model: `sentence-transformers/all-MiniLM-L6-v2` (384 dims), chosen by the pre-registered rule from a three-model dev-only spike (`eval/reports/embedding-spike/spike.json`); the choice is practical, not evidence of superiority.

## 5. Defects found and fixed during Phase 2

| # | Defect | How found | Fix |
|---|---|---|---|
| 1 | The backend image copied `backend/.env` (git-ignored, dev-only secrets) because there was no `.dockerignore`. The Stage 0 local image therefore contained throwaway dev secrets (never pushed) | review while building the worker | `backend/.dockerignore`; images rebuilt |
| 2 | Docker image lacked the `redis` client library, so every wake-up silently failed and polling hid it | the `queue` field read `deferred` while Redis was up | `redis` added to `requirements.txt`; `compose_async_check.py` now asserts `notified` |
| 3 | `RET_MIN_SIM=` (empty) crashed settings parsing | first Compose start with the new variable | empty string now means "unset"; test added |
| 4 | `docker-entrypoint.sh` got CRLF line endings after a scripted edit on Windows (container failed to start) | Compose start | converted to LF; `.gitattributes` added |
| 5 | Random-UUID tie-breaking made evaluation results non-reproducible | re-confirmation run differed from the earlier identical run | stable tie-break + tests; see §4 |
| 6 | Test suite leaked database connections (one pooled engine per test app) → 500s once 100 connections were used; appeared only in a clean venv with newer dependencies | clean-venv run | `NullPool` for `APP_ENV=test` apps |
| 7 | Two of my own async tests ran unscoped `UPDATE`s that failed the seeded document for later tests | full-suite run | scoped to the test's own document |
| 8 | A wildcard `rm -rf` of the reports folder was blocked by a safety check; I did not work around it | tool refusal | superseded reports are kept and labelled instead of deleted |

## 6. Deviations from the design documents

| Design said | Built | Why |
|---|---|---|
| Redis + `arq` queue and a separate `ingest-worker` service | Same application image started as a `worker` service; PostgreSQL job table is the source of truth, Redis only wakes workers; no `arq` | recovery, idempotency and duplicate-job protection need durable state anyway; fewer moving parts |
| Document status `UPLOADED → PARSING → INDEXING → READY` | `QUEUED → PROCESSING → READY \| FAILED` plus job `stage` (`parsing`, `chunking`, `done`) | matches the Phase 2 brief's wording |
| `retrieval_support` from dense similarity or rank | support = a passage matches ≥ `RET_MIN_TERMS` distinct query terms **or** has cosine similarity ≥ `RET_MIN_SIM` | raw FTS rank did not separate relevant from irrelevant passages (Phase 1 probe) |
| Document delete + `document.deleted` stream event | transactional delete + `know.document_events` audit row; no Redis Stream events yet | monolith: no consumer needs them |
| Parser isolation "where practical" | child process with timeout everywhere; memory limit on POSIX only | Windows has no `RLIMIT_AS` |
| `evidence_events` with graded attempts, teacher feedback | only three zero-weight self-report types; the table rejects everything else | Phase 4 owns mastery evidence |

## 7. Not verified / limitations

* **Hybrid retrieval inside Docker was not run.** The Compose images were built **without** the embedding stack (`INSTALL_EMBEDDINGS=false`, `EMBEDDING_PROVIDER=none`), so the stack serves full text and reports `effective_mode: fts, embeddings_not_configured`. Hybrid with the real model was evaluated only through the evaluation harness on the host (PostgreSQL 16 + pgvector container). The `INSTALL_EMBEDDINGS` build path in the `Dockerfile` has **never been built**.
* The first use of the real model downloads its **public weights** from the model hub (this happened during the spike); no student or corpus data is transmitted. `EMBEDDING_ALLOW_DOWNLOAD=false` disables downloads.
* OCR was tested on clean synthetic text rendered to images. Accuracy on real scans, handwriting, tables or equations is **unknown**. Tesseract is not implemented.
* Evaluation is small (54 questions), single-annotator, with vocabulary overlap between questions and corpus; the test split has been inspected many times. Workflow numbers use the fake LLM.
* Browser smoke test: Chrome only, via the Vite **dev** server; the production bundle served by a static host was not browser-tested. No visual/accessibility review.
* The ledger's append-only trigger conflicts with a future "delete a student's data" requirement; a controlled anonymization path must be designed in Phase 4 (not done).
* No rate limiting, no antivirus scanning of uploads, no encryption at rest, no load or latency measurements, and no multi-node concurrency testing beyond lease/claim and ack-race tests.
* `still_confused` followed by more clarifications ends in teacher escalation by budget (rule R2) because the policy has no dedicated rule for repeated confusion; this follows the documented rules but is pedagogically crude.
* Jev remains off (`JEV_MODE=off` is the only accepted value); no real LLM was used; no external transmission of student data occurs.

## 8. Test results (exact)

| Run | Result |
|---|---|
| Backend, dev environment (Windows, Python 3.12, PostgreSQL 16 + pgvector container, Redis container) | **167 passed, 1 skipped** (POSIX-only memory test) |
| Backend, **clean virtualenv** from `requirements-dev.txt` (newest dependency versions) | **166 passed, 2 skipped** (memory test; real RapidOCR, which is optional and not in the dev requirements) |
| Backend against PostgreSQL 18 **without** pgvector (before the last three tie-break tests were added) | 150 passed, 15 skipped (vector-dependent tests skip cleanly; full-text path works) |
| Migrations | upgrade from `0001` with data in place, `head → 0001 → head`, and a server without pgvector: all pass (`tests/test_migrations.py`); also exercised by every test session |
| Frontend `npm run build` | passes |
| `e2e/compose_async_check.py` | **ALL COMPOSE ASYNC CHECKS PASSED** |
| `e2e/compose_ocr_lifecycle_check.py` | **ALL COMPOSE OCR / LIFECYCLE CHECKS PASSED** |
| `e2e/test_browser_smoke.py` | **1 passed** (login, wrong-password error, ask, citation expand, acknowledgment, admin trace) |

## 9. Exact commands

```bash
docker compose -f docker-compose.test.yml up -d --wait                  # disposable pgvector Postgres :55430 + Redis :63790
cd backend && pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+psycopg2://eduos:devpass-test-only@127.0.0.1:55430/eduos_test python -m pytest
EVAL_DATABASE_URL=postgresql+psycopg2://eduos:devpass-test-only@127.0.0.1:55430/eduos_eval_test \
  python -m app.evaluation.retrieval_eval --mode hybrid --embedding-provider sentence_transformers \
  --splits dev test --min-terms 4 --min-chunks 1 --min-sim 0.6 --workflow --label my-run   # needs requirements-embeddings.txt
docker compose up -d --build                                             # app stack (root .env: POSTGRES_PASSWORD, JWT_SECRET)
python e2e/compose_async_check.py && python e2e/compose_ocr_lifecycle_check.py   # latter needs INSTALL_OCR=true OCR_ENGINE=rapidocr
cd frontend && npm run dev &  E2E_BASE_URL=http://localhost:5173 python -m pytest e2e/test_browser_smoke.py -p no:cacheprovider
```

## 10. Next steps (Phase 3/4 entry points)

1. Build the Compose image with `INSTALL_EMBEDDINGS=true`, run `python -m app.knowledge.reindex` in the worker container, and re-run the Compose checks in hybrid mode (closes the main unverified item).
2. Replace the evaluation dataset with a larger, double-annotated, partly student-written set before making any further threshold or model decision; keep a genuinely untouched test split.
3. Improve the weak links the failure cases show: test whether the topic-name-augmented query is what makes full text escalate `q19`/`q22`, and raise citation precision (~0.6).
4. Phase 4: graded attempts and practice generation, the Beta/decay learner model consuming the ledger, hypothesis lifecycle, and a designed anonymization path for the append-only ledger.
5. Phase 5: teacher matching and a resumable escalation (escalation is still only a recorded outcome).
