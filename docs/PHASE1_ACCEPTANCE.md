# Phase 1 / Milestone M1: Implementation Status and Acceptance

| | |
|---|---|
| **Date** | 2026-10-09 |
| **Scope** | M1 vertical slice as a **modular monolith** (documented fallback, [ARCHITECTURE §14](ARCHITECTURE.md)) |
| **Nature of this document** | Records what was actually built and what was actually run. Anything not listed under "Verified" is unverified. |

> **Update (2026-10-09, Phase 2):** this document is the Phase 1 record and is kept for history. Several of its statements are now **superseded**; see [PHASE2_ACCEPTANCE](PHASE2_ACCEPTANCE.md) and the Stage 0 re-verification table at the end of this file.
> Superseded: "Docker never run" (now built, started and checked end to end); "UI never exercised in a browser" (a Playwright smoke test exists and passes); "synchronous ingestion", "no OCR", "no vector search" and "no Redis/worker" (all now implemented; sync ingestion remains as a mode); the `RET_MIN_RANK` heuristic (replaced by matched-term/similarity support); the test count (see Phase 2 for current numbers).
> Still true: mastery is a stub, escalation is only a recorded outcome, the fake provider is not a language model, no rate limiting.

## 1. Verified results

| Check | How it was verified | Result |
|---|---|---|
| Backend test suite | `python -m pytest` in the repo's `backend/`, and again in a **clean virtualenv** built only from `requirements-dev.txt` | **83 passed** (0 failed) both times |
| Tests exercise the real migration | Session fixture drops schemas and runs `alembic upgrade head` on a disposable PostgreSQL 18 database | Passed |
| Tests can fail | Temporarily disabled the course/owner ACL and the citation quote check; the isolation and fabricated-citation tests failed; code restored and suite green again | Done once, manually |
| README backend quickstart | In the clean venv against a fresh empty database: `alembic upgrade head` → `python -m app.seed` → `uvicorn` → `GET /readyz` | `ready` (database, migrations, storage ok) |
| Real HTTP path | uvicorn on :8000 and the Vite dev server on :5173 proxying `/v1`; login → documents → create doubt through the proxy with `curl` | Returned `AWAITING_STUDENT` with an explanation |
| Frontend type-check and build | `npm run build` (`tsc --noEmit` + `vite build`) | Succeeded |
| `docker compose config` | Syntax and required-variable interpolation | Parses; refuses to start without `JWT_SECRET` / `POSTGRES_PASSWORD` |

Test environment: Windows 11, Python 3.12.4, a private PostgreSQL 18.1 started from the installed binaries (port 54329) because the Docker daemon was unavailable and the system PostgreSQL required credentials we do not have.

## 2. Not verified

- **`docker compose up --build`**, the backend `Dockerfile` and `docker-entrypoint.sh` were written but **never run** (the Docker daemon was not running). Treat them as untested.
- **The React UI was never exercised in a browser** by a person or automated browser. It type-checks, builds, and its API paths were exercised with `curl` through the dev proxy, but layout, interaction and error states are unseen.
- Concurrency behavior (two simultaneous requests on the same run) is untested.
- Behavior on PostgreSQL versions other than 18 is untested (the compose file pins `pgvector/pgvector:pg16`, never started).
- No load, latency or retrieval-quality measurements exist. No evaluation harness exists yet.

## 3. M1 acceptance checklist

| Criterion ([task §8](IMPLEMENTATION_PLAN.md)) | Status | Evidence |
|---|---|---|
| A fresh developer can follow the setup instructions | **Passed for the non-Docker backend path**; Docker path unverified; frontend path partially verified (build + dev proxy) | Clean-venv run above |
| Application and dependencies report healthy | **Passed** | `/readyz` checks DB, migrations at head, storage writable; returns 503 when the DB is unreachable (`test_readyz_reports_unavailable_database`) |
| Seeded accounts can log in | **Passed** | `test_login_success_and_me` |
| Role and course access restrictions tested | **Passed** | `test_role_based_access`, `test_student_cannot_read_another_students_session`, `test_course_access_control_*`, `test_private_upload_is_isolated_between_students`, forged/expired/`alg=none`/inflated-role token tests |
| Seeded Computer Networks PDF ingested and searchable | **Passed** | `test_seeded_document_is_ready`, `test_search_returns_passages_with_source_metadata` (6 pages, 13 chunks) |
| Student submits a doubt and receives a cited explanation via the documented policy path | **Passed** (rule R6, fake provider) | `test_r6_grounded_explanation_end_to_end` |
| Citation references and quoted text validated | **Passed** | Verified in the response against the DB; fabricated/altered citations are stripped (`test_fabricated_citations_*`), all-fabricated → one regeneration → real-passage fallback |
| Workflow and policy decision persist | **Passed** | Run state, 4 steps and the decision record re-read through an independent DB session |
| Admin can inspect the trace | **Passed** | `GET /v1/admin/runs/{id}/trace`: rule, inputs snapshot, retrieval query/chunk ids, citation checks, provider, timings; no secrets in the payload; student/teacher/anonymous denied |
| Relevant automated tests pass | **Passed** (83/83) | See §1 |
| Known limitations and unverified checks documented | **This document** | §2, §5 |
| Jev off; no real LLM credentials required | **Passed** | Config rejects `JEV_MODE != off` and `LLM_PROVIDER != fake` (`test_jev_and_real_llm_cannot_be_enabled_in_m1`); `/readyz` reports `jev_mode: off`; no Jev code exists |

The 14 test areas requested for M1 map as follows: startup/readiness, login, RBAC, student isolation, ingestion/search, course ACL, R6, R6-not-applicable (R3/R4/R1/R1b/R2 paths), citation validity, insufficient context (no fabricated citations), persistence, admin trace authorization/contents, step-budget enforcement (`max_actions=0`, clarification bound, hard node guard), and invalid/unreadable/empty/oversized/non-PDF files. All have passing tests.

## 4. Deviations from the design documents (and why)

| # | Design said | M1 does | Reason |
|---|---|---|---|
| 1 | Three services + ingest worker + Redis | One FastAPI process; module boundaries and the `core` / `orch` / `know` schemas kept; no Redis/worker | Approved fallback; Docker unavailable; no improvement to the demo |
| 2 | Internal HTTP APIs between services | In-process interfaces (`CoreGateway`, `Retriever`, `KnowledgeService` with a `Principal` carrying `allowed_course_ids`) | Same boundaries, replaceable by HTTP clients later |
| 3 | Asynchronous ingestion (queue + worker) | Synchronous ingestion inside the request; statuses `PARSING → INDEXING → READY/FAILED` recorded | Allowed for M1 |
| 4 | Hybrid dense + lexical retrieval | PostgreSQL full-text search only; the `Retriever` interface is ready for pgvector/RRF | Scope; no vector search is claimed |
| 5 | `retrieval_support = {n_chunks_above_threshold, top_dense_similarity, lexical_hits}` with `RET_MIN_SIM` | `max_matched_terms` replaces `top_dense_similarity`; a passage counts as support when it matches at least `RET_MIN_TERMS` distinct query terms | Raw FTS rank was found not to separate relevant from irrelevant passages in a manual probe. This threshold is an **untuned development default** |
| 6 | `POST /v1/doubts` returns 202 and the run proceeds asynchronously | Run executes inline; response is 202 with the post-run status | No worker yet |
| 7 | `LLMProvider.generate_structured` | Typed operations `understand` / `explain` with timeout, bounded retries and schema re-validation in `call_with_policy` | Simpler for a fake provider; same guarantees |
| 8 | `Explanation` fields | Added `insufficient_context`, `provider_note` | Needed to represent "not enough context" honestly |
| 9 | Search only as internal endpoint | Added public `GET /v1/search` (student/admin) | Requested for M1 ("expose a search endpoint") |
| 10 | Idempotency on all state-changing calls | Implemented on `POST /v1/doubts` only | Scope |
| 11 | Student `GET` hides `rule_id` | Same (admins see it) | Matches the contract |
| 12 | Optimistic `version` checks, heartbeat, reconciler | `version` increments on each save; row lock is held only inside a node's transaction; no heartbeat or reconciler | Scope |

Document-level observation: rule **R6** as written covers only unknown/hypothesis/low-mean-emerging mastery. A student with `demonstrated` mastery who asks a fresh doubt matches no rule before R10 and is asked to clarify. This was implemented literally (see `test_r6_does_not_apply_when_mastery_demonstrated_and_not_forced`); it is unreachable in M1 (mastery is always `unknown`) and should be revisited in Phase 4.

## 5. Known limitations

- **Mastery is a stub** (always `unknown`): no evidence ledger, hypotheses are not persisted, practice generation and answer evaluation do not exist. `GENERATE_PRACTICE` raises "not implemented" and fails the run visibly if ever selected (unreachable today).
- **Escalation is a recorded outcome only**: `ESCALATE_TO_TEACHER` sets the run to `WAITING_HUMAN` and tells the student a human is needed. There is no teacher matching, teacher UI, resume endpoint or expiry. The run is currently not resumable by a teacher.
- Student acknowledgments (`understood` / `still_confused` / `check_me`) have no endpoint; after an explanation the run waits and only a teacher request is accepted.
- The fake provider is extractive keyword matching. Answer quality is that of selecting overlapping sentences, not reasoning. Non-English text is unsupported.
- No OCR: scanned PDFs fail with `NO_EXTRACTABLE_TEXT`. Chunking is sentence-based with an English-centric splitter.
- Login has no rate limiting or lockout. Upload/doubt rate limits are not implemented.
- Tokens are bearer JWTs held in browser memory (a reload signs the user out); there is no refresh or revocation.
- Admin accounts can read any doubt text through the trace (needed for audit); student identity in the trace is the opaque `student_ref` plus `student_id`.
- Stored PDFs are kept on local disk under a UUID name; there is no antivirus scanning or encryption at rest.
- The development `.env` used here points at a private local database and contains a dev-only secret; it is git-ignored.

## 6. Next steps (Phase 2)

1. Add pgvector embeddings (local model, spike first) and RRF fusion behind the existing `Retriever` interface; keep FTS as the lexical half; extend the retrieval evaluation set.
2. Move ingestion to a queue/worker (Redis + `arq`) with the existing status machine; add OCR for image-only pages.
3. Re-index/versioning and a deletion event for already-issued citations ("source removed").
4. Replace the `RET_MIN_TERMS` heuristic with a tuned support signal using the dev/test split from the evaluation plan.
5. Run `docker compose up --build` end to end and add a compose smoke test; add a browser smoke test for the UI.
6. Begin the Phase 4 prerequisites that Phase 2/3 tests depend on: acknowledgment endpoint and the evidence ledger skeleton.

## 7. Stage 0 re-verification at the start of Phase 2 (2026-10-09)

Checked against the working tree before any Phase 2 change. PASS / FAIL / NOT RUN with evidence:

| Check | Result | Evidence |
|---|---|---|
| Backend tests | **PASS** (83 passed) | `python -m pytest` against a disposable PostgreSQL 16 + pgvector container |
| Frontend type-check + production build | **PASS** | `npm run build` |
| Dockerfile, Compose file, env variables, migrations, seed script | **PASS** (inspected and exercised) | `docker compose up -d --build`: postgres and backend `healthy`; migrations applied; seed ran |
| Docker build and start | **PASS** (previously NOT RUN) | as above; a root `.env` with throw-away values is required (`POSTGRES_PASSWORD`, `JWT_SECRET`) |
| Readiness | **PASS** | `/readyz` ready |
| Login → ingest/search seeded document → create doubt → admin trace | **PASS** | scripted run against the container: 13 chunks, search hits on page 4, rule `R6_low_evidence_explain`, 3 verified citations |
| Browser smoke test (login, doubt, citation expansion, admin trace) | **PASS** (previously NOT RUN) | `e2e/test_browser_smoke.py`, Playwright with the installed Chrome via the Vite dev server |
| Contradictions between docs and code | none that block work | the doc statements listed in the banner above were simply out of date after Phase 2 |
