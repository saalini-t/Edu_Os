# EduOS

*Don't just answer the doubt. Understand the learner.*

An AI-native education OS for **intelligent doubt resolution** — an entry for The Industry Games 2026, District 03 (sponsored by ATOMTALK).

> **Project status: design only.** This repository currently contains documentation and no application code. Nothing described below has been built, run or tested yet. See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## What it is (planned)

EduOS behaves like an educational decision-support system rather than a generic chatbot. Given a student's doubt, it:

1. Understands the topic, intent and clarity of the question.
2. Looks up the learner's evidence-based state for that topic and its prerequisites.
3. Retrieves passages from the student's own course material (hybrid search, with verified citations).
4. Applies a **deterministic, auditable policy** to choose one intervention: *ask for clarification*, *explain*, *generate practice*, *escalate to a human teacher*, or *complete*.
5. Grades the student's response and records **evidence** (suspected gaps stay hypotheses until evidence supports them).
6. Pauses for a matched teacher when needed, then resumes with the teacher's feedback.

Demo course (planned): **Computer Networks**, with seeded material and teacher accounts.

## Architecture at a glance

| Component | Role |
|---|---|
| `web` | Student, teacher and admin UI |
| `core-api` | Public API; auth, sessions, learner evidence/state, practice, teacher matching and escalation, analytics |
| `orchestrator` | Persisted workflow engine, policy engine, LLM agents (understand, explain, practice), answer evaluator |
| `knowledge-svc` + `ingest-worker` | Document ingestion (parse/OCR/chunk/embed), authorized hybrid retrieval, deletion |
| PostgreSQL | One instance, three service-owned schemas (`core`, `orch`, `know`), pgvector + full-text search |
| Redis | Ingestion queue and event stream |

Key design choices: explicit hand-rolled state machine; deterministic intervention policy; three LLM agents only; Beta-Bernoulli mastery with recency decay over an evidence ledger; pgvector + FTS + reciprocal-rank fusion; LLMs behind a provider interface with a fake provider for credential-free runs; Jev (typed decision API) evaluated in **shadow mode only** behind a `DecisionProvider` interface (ADR-001). A modular-monolith fallback is preserved.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Requirements, approved decisions, diagrams, services, workflow and transitions, policy, learner model, RAG, data model, security controls, ADR-001 (Jev) |
| [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) | Endpoints, JSON contracts, errors, idempotency, events |
| [docs/AGENT_SPECIFICATIONS.md](docs/AGENT_SPECIFICATIONS.md) | Per-component responsibilities, typed schemas, failure handling, tests |
| [docs/EVALUATION_PLAN.md](docs/EVALUATION_PLAN.md) | Datasets, metrics, methodology, Jev experiment, security and fault-injection evaluation |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Phases, acceptance tests, MVP vs stretch, first milestone (M1) |

## Planned repository layout

```
libs/contracts/   services/{core-api,orchestrator,knowledge-svc}/   web/   eval/   deploy/
```

## Planned quickstart (not yet available)

The intended developer flow once Phase 1 is done:

```
cp deploy/.env.example deploy/.env      # names only; fill values locally, never commit
make up                                  # docker compose: postgres, redis, 3 services, worker, web
make seed                                # demo users, Computer Networks course, teachers
make test                                # unit / contract / workflow suites
make eval                                # offline evaluation report
```

By default `LLM_PROVIDER=fake` so the stack runs without credentials; real LLM quality claims require configuring a provider and running the evaluation.

## Configuration and secrets

All secrets live in server-side environment variables (`.env`, untracked). Nothing secret is ever sent to the browser. Variable names are listed in [docs/ARCHITECTURE.md §19](docs/ARCHITECTURE.md); Jev settings (`JEV_MODE=off` by default) are in §13.5.

## Safety and honesty notes

- Mastery is derived from recorded evidence only; model confidence is never treated as mastery.
- Offline metrics measure system behavior on small labeled sets and are **not** evidence of learning outcomes.
- Jev is not on the critical path, receives synthetic data only, and is adopted only if a pre-registered experiment and a privacy review support it.
- No ATOMTALK-specific requirements beyond the public problem statement are assumed.

## Open items

Tracked in [docs/ARCHITECTURE.md §4](docs/ARCHITECTURE.md): LLM provider and budget, embedding model, seed corpus authoring, frontend stack default, Jev key and terms, tooling versions, token handling.
