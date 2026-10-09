# EduOS

*Don't just answer the doubt. Understand the learner.*

An AI-native education OS for **intelligent doubt resolution** (The Industry Games 2026, District 03, sponsored by ATOMTALK).

> **Status: Phases 1 and 2 implemented as a modular monolith** (the documented fallback): one API process plus one background worker, PostgreSQL (with optional pgvector) and Redis.
> Student doubt → understanding → course-material retrieval → deterministic policy → cited explanation → acknowledgment → persisted, admin-inspectable trace.
> Still **design only**: practice generation, graded attempts and the mastery model, teacher matching and resumable escalation, a real LLM provider.
> What is and isn't verified: [docs/PHASE2_ACCEPTANCE.md](docs/PHASE2_ACCEPTANCE.md) (and [PHASE1](docs/PHASE1_ACCEPTANCE.md)).

## What works today

- FastAPI backend, PostgreSQL schemas `core` / `orch` / `know`, Alembic migrations (0001–0005), structured JSON logs, consistent error envelope.
- Login (argon2 + short-lived JWT), student / teacher / admin roles, server-side authorization and course/owner access control.
- **Asynchronous ingestion:** upload returns `202 QUEUED`; a **worker** (`python -m app.worker`) parses, chunks and indexes. Jobs live in PostgreSQL (retries with backoff, lease-based crash recovery, duplicate protection); Redis only wakes the worker, and ingestion keeps working if Redis is down.
- **Document lifecycle:** replace (`PUT /v1/documents/{id}/file`), re-index, delete — atomic version swap, no obsolete chunks or embeddings left searchable, admin audit trail.
- **Optional OCR** for scanned pages (RapidOCR), page-level, budgeted, in a sandboxed parser process. Off by default.
- **Retrieval:** PostgreSQL full-text search, plus optional **hybrid** (pgvector + reciprocal-rank fusion) with a local embedding model. Falls back to full text — visibly (in traces and `/readyz`) — when embeddings are unavailable.
- Persisted, bounded workflow with a pure, table-tested policy engine (R1–R10), citation verification, and an admin trace.
- **Acknowledgments** ("I understood" / "still confused" / "check me") recorded in an append-only **evidence ledger** with **zero mastery weight**.
- **Fake LLM provider** only (deterministic, extractive, labelled as fake). No AI credentials needed. **Jev is off** and cannot be enabled in this version.
- Retrieval **evaluation harness** and a 54-question labelled set (`eval/`), with pre-registered thresholds.
- Minimal React + TypeScript UI.

## Quickstart (Docker Compose)

```bash
cp .env.example .env     # set POSTGRES_PASSWORD and JWT_SECRET (>= 32 chars: python -c "import secrets;print(secrets.token_urlsafe(48))")
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
| `teacher1@demo.local` | teacher | no teacher features until Phase 5 |
| `admin@demo.local` | admin | trace inspection, document management |

## Try it

1. Sign in as `student1@demo.local`, ask: *"Why does TCP slow start double the congestion window every RTT, but then stop doubling?"* You get an extractive explanation with numbered, verified citations.
2. Click **I understood**: the acknowledgment is stored with zero mastery weight; practice does not exist yet, so the run ends `UNVERIFIED` and says so.
3. As `admin@demo.local`, **Load recent runs** → open the run: rule fired, retrieval mode actually used, chunk IDs, citation checks.
4. Upload a PDF (API: `POST /v1/documents`), watch `GET /v1/documents/{id}` go `QUEUED → PROCESSING → READY`, then search it.

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
| Browser smoke test | `E2E_BASE_URL=http://localhost:5173 python -m pytest e2e/test_browser_smoke.py` (needs `pip install playwright` and Chrome or Edge) |
| Retrieval evaluation | see [docs/EVALUATION_PLAN.md §11](docs/EVALUATION_PLAN.md) |

## Configuration

See [.env.example](.env.example). Secrets come only from environment variables; `.env` is git-ignored and excluded from images. Thresholds are development-selected on a tiny labelled set (`eval/THRESHOLDS.md`) and are **not validated values**.

## Documentation

| Document | Contents |
|---|---|
| [docs/PHASE2_ACCEPTANCE.md](docs/PHASE2_ACCEPTANCE.md) | **Phase 2: what was built, run and measured; defects found; deviations; unverified items** |
| [docs/PHASE1_ACCEPTANCE.md](docs/PHASE1_ACCEPTANCE.md) | Phase 1 record (with the Stage 0 re-verification table) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Target architecture, diagrams, policy, ADR-001 (Jev) and **ADR-008–012 (as built)** |
| [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) | Target contracts; **section 9 = Phase 2 as implemented** |
| [docs/AGENT_SPECIFICATIONS.md](docs/AGENT_SPECIFICATIONS.md) | Component specifications |
| [docs/EVALUATION_PLAN.md](docs/EVALUATION_PLAN.md) | Evaluation plan; **section 11 = what exists** |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Phases, status and next steps |
| [eval/THRESHOLDS.md](eval/THRESHOLDS.md), [eval/README.md](eval/README.md) | Pre-registered threshold selection, run log, dataset notes |

## Safety and honesty notes

- The fake provider is not an AI model; it quotes retrieved sentences. Output is labelled in the API (`provider: "fake"`), logs and UI.
- Mastery is a stub (always "unknown"). Acknowledgments carry zero mastery weight by construction (code and database constraints); nothing in the system infers mastery from them.
- Citation verification proves a quote exists in a retrieved chunk, **not** that it supports the claim; measured citation precision on the small evaluation set is about 0.6–0.7.
- The retrieval evaluation is small, single-annotator and its test split has been looked at many times; differences between full-text, dense and hybrid retrieval are within noise.
- Offline tests do not measure learning outcomes.
- Jev is disabled; configuration rejects any other `JEV_MODE`. No student data is sent to any external service; the first use of the local embedding model downloads public model weights (can be disabled).
