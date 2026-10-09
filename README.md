# EduOS

*Don't just answer the doubt. Understand the learner.*

An AI-native education OS for **intelligent doubt resolution** (The Industry Games 2026, District 03, sponsored by ATOMTALK).

> **Status: Phases 1–5 implemented as a modular monolith** (a documented, deliberate choice: see [PHASE3_5_ACCEPTANCE §6](docs/PHASE3_5_ACCEPTANCE.md)): one API process plus one background worker, PostgreSQL (pgvector) and Redis.
> Doubt → understanding → authorized retrieval → cited, verified explanation → targeted practice → graded evidence → mastery estimate → deterministic policy (R1–R10) → teacher escalation and resume, with a React workspace for students, teachers and administrators.
> What was run, what passed, and what was **not** verified: [docs/PHASE3_5_ACCEPTANCE.md](docs/PHASE3_5_ACCEPTANCE.md).
> **Jev is out of scope** and has been removed from the code and configuration.

## What works today

- **Explain → Practise → Verify**, persisted and resumable: understand → retrieve → explain (citations checked against the retrieved chunks) → generate practice (answer keys stay server-side) → grade (exact for objective items, structured rubric grading with an uncertainty gate for free text) → evidence → mastery → decide again. Duplicate submissions, retries and restarts are idempotent.
- **Learning Gap Map** and **Learning Passport**: per-topic status (not assessed / suspected gap / practising / improving / demonstrated) derived from the append-only evidence ledger, each linked to its evidence and timestamps, with curated prerequisites as *suggestions*. Acknowledgements ("understood", "still confused", "check me") carry **zero** mastery weight; one correct answer is never mastery; a gap is confirmed only after two failed attempts on distinct targeted items or by a teacher.
- **Smart human escalation**: deterministic teacher matching (topic fit, availability, language, feedback, load), inbox, case brief with authorized course sources, asynchronous thread, resolution that writes bounded evidence and **resumes the workflow**, expiry, admin assign/override, full audit trail.
- **Explainable decisions**: each policy decision is stored with its rule, reasons, evidence references and provider/model, and shown in plain language to students and in detail to admins. A language model never overrides a rule.
- **Model providers**: `fake` (offline, deterministic, labelled), `ollama` (local, no paid key) and any OpenAI-compatible endpoint. Output is validated against strict schemas, with timeouts, bounded retries and deterministic fallbacks. Credentials are server-side only.
- Hybrid retrieval (full-text + pgvector, RRF) with visible fallback, async ingestion worker, document lifecycle, optional OCR, labelled retrieval evaluation (`eval/`).
- Student anonymization that keeps the append-only ledger intact (pseudonymised, content deleted): see ADR-013 in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- React + TypeScript UI for the three roles, responsive, with loading / empty / error / success states.

## Quickstart (Docker Compose)

```bash
cp .env.example .env     # set POSTGRES_PASSWORD and JWT_SECRET (>= 32 chars: python -c "import secrets;print(secrets.token_urlsafe(48))")
# Real local model (recommended): install Ollama, `ollama pull qwen2.5:3b`, then in .env:
#   LLM_PROVIDER=ollama  LLM_MODEL=qwen2.5:3b  LLM_BASE_URL=http://host.docker.internal:11434
# Offline deterministic demo instead: LLM_PROVIDER=fake
docker compose up --build      # postgres(pgvector), redis, backend (migrates + seeds), worker
curl localhost:8000/readyz
cd frontend && npm install && npm run dev      # http://localhost:5173 (proxies /v1 to :8000)
```
Optional image features (build-time): `INSTALL_OCR=true` + `OCR_ENGINE=rapidocr`; `INSTALL_EMBEDDINGS=true` + `EMBEDDING_PROVIDER=sentence_transformers` (then run `docker compose exec backend python -m app.knowledge.reindex`). **The embeddings image build was not exercised**; the default images run full-text retrieval. See [PHASE2_ACCEPTANCE §7](docs/PHASE2_ACCEPTANCE.md).

## Quickstart (local, without Docker)

Prerequisites: Python 3.11+, Node 18+, PostgreSQL 14+ you can create a database on (pgvector optional), Redis optional.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                    # + requirements-embeddings.txt / requirements-ocr.txt for the optional features
cp ../.env.example .env                                # edit DATABASE_URL, JWT_SECRET; set REDIS_URL if you run Redis
alembic upgrade head && python -m app.seed
uvicorn app.main:app_factory --factory --port 8000     # API
python -m app.worker                                   # second terminal: ingestion worker (needed while INGESTION_MODE=async)
```
Without a worker, uploads stay `QUEUED`. For quick experiments set `INGESTION_MODE=sync` to index inside the request.

## Demo accounts (synthetic; seeding refuses to run with `APP_ENV=production`)

All use the password in `SEED_DEMO_PASSWORD` (default `eduos-demo-2026`).

| Email | Role | Notes |
|---|---|---|
| `student1@demo.local` | student | enrolled in Computer Networks |
| `student2@demo.local` | student | enrolled in Computer Networks (isolation tests) |
| `student3@demo.local` | student | enrolled only in the empty OS course (course-access tests) |
| `teacher1@demo.local` | teacher | TCP / reliability, English + Hindi |
| `teacher2@demo.local` | teacher | IP addressing and routing |
| `teacher3@demo.local` | teacher | Operating Systems course only (must never see Computer Networks cases) |
| `admin@demo.local` | admin | traces, system health, escalations, ingestion |

## Try it

1. Sign in as `student2@demo.local`, ask: *"Why does TCP slow start double the congestion window every RTT, but then stop doubling?"* Open a citation to see the verified source quote.
2. Choose **Check me with practice**, answer the questions: feedback and the reference answer appear only after you submit. Open **Gap Map** and **Learning Passport** to see the status and the evidence behind it.
3. Ask *"I want to talk to a human teacher about TCP congestion control"*. Sign in as `teacher1@demo.local`: accept the case, reply, resolve. The student's doubt completes and shows the teacher's note.
4. As `admin@demo.local`: System health (provider, retrieval mode), Workflow runs (decisions explained, citation validation), Escalations (ranked candidates, audit trail), Ingestion.

## Tests and checks

```bash
docker compose -f docker-compose.test.yml up -d --wait      # disposable pgvector Postgres :55430 and Redis :63790
cd backend
TEST_DATABASE_URL=postgresql+psycopg2://eduos:devpass-test-only@127.0.0.1:55430/eduos_test python -m pytest
```
`TEST_DATABASE_URL` is **required** and its database name must contain `test`: the session **drops and recreates** the `core`, `orch` and `know` schemas through the real migrations. Tests that need pgvector or Redis skip themselves when unavailable. Last recorded results: [PHASE2_ACCEPTANCE §8](docs/PHASE2_ACCEPTANCE.md).

| Check | Command |
|---|---|
| Frontend | `cd frontend && npm run build` |
| Compose: async ingestion, worker/Redis outages | `python e2e/compose_async_check.py` (stack running) |
| Compose: OCR, lifecycle, parser memory limit | `python e2e/compose_ocr_lifecycle_check.py` (needs `INSTALL_OCR=true`, `OCR_ENGINE=rapidocr`) |
| 12-step acceptance scenario over the real API (stack running; fake or Ollama) | `python e2e/acceptance_scenario.py` |
| Browser journey: student → teacher → admin, mobile layout | `E2E_BASE_URL=http://localhost:5173 python -m pytest e2e/test_browser_smoke.py` (add `E2E_SLOW=1` for a real model; needs `pip install playwright` and Chrome or Edge) |
| Live local-model tests (opt-in) | `OLLAMA_TEST_MODEL=qwen2.5:3b python -m pytest tests/test_llm_providers.py tests/test_live_ollama_workflow.py -s` |
| Retrieval evaluation | see [docs/EVALUATION_PLAN.md §11](docs/EVALUATION_PLAN.md) |

## Configuration

See [.env.example](.env.example). Secrets come only from environment variables; `.env` is git-ignored and excluded from images. Thresholds are development-selected on a tiny labelled set (`eval/THRESHOLDS.md`) and are **not validated values**.

## Documentation

| Document | Contents |
|---|---|
| [docs/PHASE3_5_ACCEPTANCE.md](docs/PHASE3_5_ACCEPTANCE.md) | **Phases 3–5 and the innovation addendum: what was built, run and measured; limitations; NOT RUN items** |
| [docs/PHASE2_ACCEPTANCE.md](docs/PHASE2_ACCEPTANCE.md) | Phase 2: what was built, run and measured; defects found; deviations; unverified items** |
| [docs/PHASE1_ACCEPTANCE.md](docs/PHASE1_ACCEPTANCE.md) | Phase 1 record (with the Stage 0 re-verification table) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Target architecture, diagrams, policy, ADR-001 (Jev) and **ADR-008–012 (as built)** |
| [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) | Target contracts; **section 9 = Phase 2 as implemented** |
| [docs/AGENT_SPECIFICATIONS.md](docs/AGENT_SPECIFICATIONS.md) | Component specifications |
| [docs/EVALUATION_PLAN.md](docs/EVALUATION_PLAN.md) | Evaluation plan; **section 11 = what exists** |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Phases, status and next steps |
| [eval/THRESHOLDS.md](eval/THRESHOLDS.md), [eval/README.md](eval/README.md) | Pre-registered threshold selection, run log, dataset notes |

## Safety and honesty notes

- The fake provider is not an AI model; it quotes retrieved sentences and is labelled everywhere. **Passing tests with the fake provider say nothing about the quality of a real model.** A 3B local model (`qwen2.5:3b`) works end to end but its explanations and questions can be wrong or weak; every explanation is citation-checked, yet that proves a quote exists in a source, **not** that it supports the claim.
- Mastery is a transparent Beta-Bernoulli estimate with decay (parameters in [eval/THRESHOLDS.md](eval/THRESHOLDS.md)); the thresholds are **untuned development values**, not validated. The system describes a learner's work on topics; it never infers ability or intelligence.
- The ledger is append-only. Deletion/anonymisation never silently rewrites history: see ADR-013. There is no promise of permanent retention.
- The retrieval evaluation is small, single-annotator and its test split has been looked at many times; differences between full-text, dense and hybrid retrieval are within noise. No hybrid superiority is claimed.
- No student data is sent to an external service: non-private model hosts are refused unless `LLM_ALLOW_EXTERNAL=true`. The first use of the local embedding model downloads public model weights.
