# Phase H1: transaction hygiene, abuse controls, grading and audit hardening

Date: 2026-10-09. Design and rationale: ADR-018 in [ARCHITECTURE.md](ARCHITECTURE.md). No framework or dependency was added.

## Where locks were held across model calls (before)

| Path | Row lock | Pooled connection |
|---|---|---|
| `POST /doubts` → `advance` → `_node_understand` | held | held |
| `_node_explain` | not held | held (`get_chunks` opened a transaction first) |
| `_node_practice` | not held | held (reads, then up to 2 model calls) |
| `_on_practice_answer`, free-text items | held (inside `apply_event`) | held |

## Changes

- `app/workflow/engine.py`: `_offload` (commit, call, re-lock with `populate_existing`, revalidate), `StaleRun`, run lease (`_lease_fresh`, `advance(own=...)`), `_lock` now reads the row as it is in the database.
- `app/workflow/recovery.py` (new) and `app/worker.py`: crashed `RUNNING` runs are resumed by the sweep.
- `app/workflow/router.py`: answer submission reuses an existing `Attempt` for the same `Idempotency-Key`; rate-limit dependencies on the five model-triggering endpoints.
- `app/llm/base.py`: bounded concurrency gate with honest slot accounting; `busy` fails fast.
- `app/ratelimit.py` (new), `app/errors.py` (`Retry-After` support), `app/main.py` (wiring, CORS now allows `PUT`), `app/auth/router.py` (login throttle).
- `app/agents/evaluation.py`, `app/learner/mastery.py`, `app/learner/service.py`: grader-injection screen, verdict consistency and key-term corroboration, model-only mastery cap.
- `alembic/versions/0010_audit_append_only.py`, `app/learner/anonymize.py`: append-only audit events with the single anonymization exception.
- `app/config.py`, `.env.example`: new settings (names only in the example).
- Tests: `tests/test_phase1_hardening.py` (24 new), `tests/test_agents.py` (injection test rewritten), `tests/test_migrations.py` (head 0010).

## Tests actually run

| Run | Result |
|---|---|
| `python -m pytest` in `backend/` against the disposable pgvector DB, with `OLLAMA_TEST_MODEL=qwen2.5:3b` (includes the live Ollama tests) | **325 passed, 1 skipped** (POSIX-only parser memory-limit test) |
| `tests/test_phase1_hardening.py` alone | 24 passed |
| Mutation check: removing the commit in `_offload` | the 3 "no lock / no connection while the model runs" tests **failed** (as they must); code restored afterwards |
| Docker: rebuilt backend and worker, `/readyz`, `alembic_version`, trigger present, login probe with a throw-away address | migrations ok, version `0010`, `trg_audit_events_immutable` present, 10 × 401 then 429 with `Retry-After: 900` |

**NOT RUN:** the Chrome journey and the 12-step acceptance script after this phase (the demo learner data in the dev database would distort them and I did not reset it); `npm run build` (no frontend source changed in this phase); a sustained load test against Docker; a multi-process API test.

## Acceptance criteria

| Criterion | Result |
|---|---|
| No row lock and no idle-in-transaction connection while understand / explain / practice / free-text grading run | **Passed** (tests inspect `FOR UPDATE NOWAIT` and `pg_stat_activity` mid-call) |
| Idempotency, retry and resume preserved | **Passed** (duplicate advance, same-key retry, double submission of one item, stale result discarded, crashed run recovered once) |
| Bounded model concurrency | **Passed** (peak never above the limit; timed-out call keeps its slot; busy degrades to the clarification fallback) |
| Configurable per-user rate limits | **Passed** (429, `Retry-After`, per-user, 0 disables) |
| Student-answer injection cannot steer the grader | **Passed** for the covered patterns; a regex is not a proof (see risks) |
| Untrusted model output cannot create positive mastery on its own | **Passed** (key-term corroboration; model-only evidence never "demonstrated") |
| Audit events protected | **Passed** (UPDATE/DELETE blocked; only actor clearing during anonymization) |
| Existing suites | **Passed** (325 / 1 skipped) |

## Known risks and remaining work

- The injection screen is a pattern list. A cleverly phrased answer can pass it; the corroboration and the cap on model-only mastery are the second and third lines, and none of the three is a guarantee. A real-model adversarial test set belongs to the evaluation phase.
- Key-term corroboration trades recall for safety: heavy paraphrases are shown but not counted.
- Rate limits are per process memory. Multiple API processes need a shared store first.
- `TRUNCATE` on `audit_events` is not blocked (test cleanup depends on it); operators with ownership can also drop the trigger. Restrict the application's database role in production.
- Two simultaneous `POST /v1/doubts` with the same `Idempotency-Key` can still both create a session (the key is stored after the work finishes). Pre-existing; worth a claim-first fix.
- Recovery latency is about one lease (default 600 s) plus one sweep interval (15 s).
- Posted to a model that is slow rather than down, each doubt still occupies a request thread for the call's duration (only the lock and connection were released).

## Next recommended phase

H2: the large-PDF path (configurable upload limit, OCR budget, a real Docker ingest with timings, upload and progress UI). It is the first thing a demo with a real textbook will hit, and the connection and concurrency work above is now in place to support it.
