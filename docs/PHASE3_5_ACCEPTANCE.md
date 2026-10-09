# Phases 3–5 and the innovation addendum: acceptance record

Date: 2026-10-09. Everything below was run on one Windows 11 machine (Docker Desktop, local Ollama with `qwen2.5:3b`). "NOT RUN" means exactly that: it was not executed and is not claimed.

## 1. What exists, and where it is wired

| Capability | Backend | API | UI |
|---|---|---|---|
| Model provider layer (fake / Ollama / OpenAI-compatible) | `app/llm/*` | `/readyz`, `/v1/admin/system` | Admin: System health |
| Doubt Understanding, Grounded Explanation, Practice, Evaluation agents | `app/agents/*` | `/v1/doubts*` | Student workspace |
| Persisted Explain → Practise → Verify loop (R1–R10 unchanged) | `app/workflow/*` | `POST /v1/doubts`, `/ack`, `/answers`, `/request-teacher` | Student workspace |
| Evidence ledger, mastery, hypotheses | `app/learner/*` | `/v1/learners/me/{evidence,progress,history}` | Gap Map, Passport |
| Learning Gap Map | `app/learner/views.py` | `GET /v1/learners/me/gap-map` | Student: Gap Map |
| Learning Passport (deterministic digest) | `app/learner/views.py` | `GET /v1/learners/me/passport` | Student: Passport |
| Explainable decisions | `app/workflow/explain.py`, `decision_records.evidence_refs/context` | `GET /v1/doubts/{id}/decisions` | "Why did the system choose this step?", Admin trace |
| Teacher matching, inbox, thread, resolve, resume, expiry, assign | `app/teaching/*` | `/v1/escalations*`, `/v1/teacher/*`, `/v1/admin/escalations*` | Teacher: Case inbox/Case/Availability; Admin: Escalations |
| Anonymisation (ADR-013) | `app/learner/anonymize.py` | `POST /v1/admin/students/{id}/anonymize` | none (API only, by design) |
| Jev | **removed** (code, config, compose, tests) | - | - |

Migrations: 0006 practice/attempts/mastery, 0007 teaching, 0008 ledger anonymisation, 0009 topic prerequisites and decision evidence references.

## 2. Commands run and results

| Check | Command | Result |
|---|---|---|
| Backend suite (disposable pgvector DB), including the opt-in live-Ollama tests | `TEST_DATABASE_URL=…55430/eduos_test OLLAMA_TEST_MODEL=qwen2.5:3b python -m pytest` in `backend/` | **300 passed, 1 skipped** (the skip is the POSIX-only parser memory-limit test, verified in Docker in Phase 2) |
| Frontend production build | `cd frontend && npm run build` (`tsc --noEmit` + `vite build`) | passed |
| 12-step acceptance scenario, **fake** provider, Docker Compose, hybrid retrieval | `python e2e/acceptance_scenario.py` | **passed**, all assertions exact (rule path R6 → R7 → R9 → R7 → R5) |
| Same scenario, **Ollama `qwen2.5:3b`**, Docker Compose, hybrid retrieval | `python e2e/acceptance_scenario.py` | **passed** (rule path R6 → R7 → R5); explanation, practice and grading came from the real model |
| Browser journey (Chrome via Playwright): wrong password, ask, citation, decision explanation, practice, gap map, passport, teacher request, teacher accept/reply/resolve, student sees resolution, role isolation, admin health/trace/escalations/ingestion, mobile (390 px) has no horizontal scroll | `E2E_BASE_URL=http://localhost:5173 E2E_SLOW=1 python -m pytest e2e/test_browser_smoke.py` against the **Ollama** stack | **passed** (1 test, 40 s) |
| Docker retrieval: hybrid with embeddings; `RETRIEVAL_MODE=fts`; hybrid with `EMBEDDING_PROVIDER=none` | restart backend per setting, read `/readyz` | hybrid in effect (0 missing embeddings); `fts`; **hybrid degraded visibly to `fts`** with `fallback_reason=embeddings_not_configured` |
| Labelled retrieval evaluation, full-text, dev+test, workflow level | `python -m app.evaluation.retrieval_eval --mode fts --splits dev test --workflow` | ran; report `eval/reports/20261009T094545-phase35-fts`. Used the CLI defaults (min_terms 2), **not** the deployed thresholds; dev nDCG@3 0.94, citation precision micro 0.71 on 38 citations |

## 3. Defects found and fixed in this round (all by running things)

1. **`qwen2.5:3b` returned `Infinity` / near-empty objects** under the full internal JSON schemas, so every `understand` call failed validation. Fix: compact model-facing schemas (`app/llm/slim.py`, ADR-014).
2. The small model called clear questions "ambiguous". Fix: a deterministic gate (≥ 6 words and names the topic or a keyword ⇒ treated as clear, recorded in the trace).
3. Cold model load and runaway generation caused 120–240 s timeouts and truncated JSON. Fix: `keep_alive`, non-blocking warm-up at start-up, per-operation output caps, and warmer temperature on an identical retry.
4. Practice items with options inside the prompt, a letter as the answer key, or no rubric were all rejected. Fix: deterministic repair (nothing invented) and per-item dropping.
5. `retrieval.mode` object crashed the admin trace page (React). Fix, plus a root error boundary.
6. After two practice sets with uncertain model grades the policy fell to R10 with a meaningless "name the topic" question. The rules were left untouched; the message now says verification failed and offers a teacher.
7. Windows reserved port 54330 after a restart; the test database moved to 55430.
8. Tests found a real weakness in the fake practice generator (an answer visible inside another item); fixed with a set-level check.

## 4. Acceptance scenario (docs: the 12 steps)

Steps 1–12 are exercised by `e2e/acceptance_scenario.py` through the real HTTP API: topic identified, authorised retrieval, citations verified, practice generated with keys hidden, one scored attempt per item (duplicate delivery replays; second attempt refused), evidence written once, acknowledgement weight 0, decisions list their evidence, repeated failure triggers R5, the matched teacher (and no teacher of another course) sees the case, thread, idempotent resolution, workflow resumed, gap map and passport updated with teacher evidence while one assessment is **not** mastery, passport digest reproducible.

Also covered by automated tests: authorisation matrix (students, teachers, admin, anonymous), duplicate requests, provider failure and invalid output (fallbacks), insufficient retrieval context, no matching teacher (stays open for the course's teachers), interrupted resume (reconciler applies it exactly once), expiry, accept race (one winner), anonymisation.

## 5. Not run / not verified

- **NOT RUN:** the browser journey with the **fake** provider (it was run against Ollama only); `e2e/compose_async_check.py` and `e2e/compose_ocr_lifecycle_check.py` were not re-run this round; hybrid retrieval evaluation on the host (no sentence-transformers there; hybrid was verified operationally in Docker, not re-measured); automated accessibility audit (axe/Lighthouse); cross-browser tests (Chrome only); load/performance tests; migration downgrade tests; Linux/macOS runs.
- Keyboard and contrast behaviour was designed (labels, focus rings, `aria-*`, semantic buttons, system dark mode) but **not audited by a tool or by a screen reader**.
- Not verifiable offline: answer quality of any real model on real students.

## 6. Service boundaries (decision)

The modular monolith is kept (ADR-017). No `knowledge-svc` or other service was extracted: modules already communicate through explicit gateways and own their schemas, only the ingestion worker is a separate process, and extraction would add a network hop, service authentication and a deploy unit without fixing a present problem. This is a deliberate choice under the plan's "extract only when there is a concrete benefit", not a claim that extraction is done. Service-to-service auth and health checks therefore do not exist; `/healthz` and `/readyz` cover the API, and the worker has a container health check.

## 7. Limitations (read these)

- **Fake-provider tests prove plumbing, not AI quality.** The live `qwen2.5:3b` runs prove the system stays valid and safe with a small model; they do not prove good teaching. Its explanations can contain errors; citation checks prove a quote exists in a source, not that it supports the claim. Measured citation precision on the small evaluation set is about 0.7.
- With a small model, free-text answers are often graded "uncertain" and (correctly) not counted as evidence, so a learner can run out of automatic help without a *verified* failure; the system then asks for clarification or a teacher instead of R5. Objective items (multiple choice, numeric) are graded exactly.
- Mastery thresholds, weights, the "improving" cut-off and the matching weights are **untuned development values** (see `eval/THRESHOLDS.md`). No real learner data exists.
- The retrieval evaluation is tiny (54 questions), single-annotator, and its test split is no longer fully held out. No claim is made that hybrid beats full-text.
- The access token is kept only in browser memory: reloading the page signs the user out (deliberate).
- A re-asked identical question raises the model temperature on the retry, so a real-model run is not bit-for-bit reproducible after the first call.
- The teacher case view reads course-visible chunks directly (a cross-module read that would become a service call on extraction).
- Anonymisation de-identifies, it does not guarantee unlinkability; backups are outside it.
- Demo accounts and passwords are for local use only; seeding refuses `APP_ENV=production`.

## 8. How to run it (the deliverable)

```bash
cp .env.example .env        # POSTGRES_PASSWORD, JWT_SECRET; for Ollama: LLM_PROVIDER=ollama LLM_MODEL=qwen2.5:3b LLM_BASE_URL=http://host.docker.internal:11434
ollama pull qwen2.5:3b      # if using Ollama; use LLM_PROVIDER=fake for the offline deterministic demo
docker compose up --build   # postgres(pgvector), redis, backend (migrates + seeds), worker
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Sign in with the accounts in the README. `python e2e/acceptance_scenario.py` replays the 12-step scenario against the running stack.
