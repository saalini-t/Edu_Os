# EduOS API Contracts

> **Scope update (Phases 3–5):** Jev is no longer in scope. Sections that discuss Jev / `JevDecisionProvider` are kept only as the historical record of ADR-001; nothing in the code depends on it and no Jev configuration exists. The deterministic R1–R10 engine is the only decision-maker; a decision-provider experiment may be added in a future phase. The as-built description is in [PHASE3_5_ACCEPTANCE.md](PHASE3_5_ACCEPTANCE.md).


| | |
|---|---|
| **Status** | Target contracts; JSON examples are illustrative, not captured responses. Only a subset of the public endpoints exists (auth, documents incl. lifecycle, search, doubts incl. acknowledgment, evidence, admin trace/events); no `/internal/*` endpoints exist because the system is a monolith. **Section 9 below documents what Phase 2 actually implemented and overrides sections 2.2 and 5 where they differ.** **Update (2026-10-09):** the M1 vertical slice is implemented as a modular monolith; see [PHASE1_ACCEPTANCE](PHASE1_ACCEPTANCE.md) for what exists, what was verified, and deviations from this design. |
| **Version** | 0.1 (2026-10-09) |
| **Related** | [ARCHITECTURE](ARCHITECTURE.md) (ownership, workflow, data model), [AGENT_SPECIFICATIONS](AGENT_SPECIFICATIONS.md) (Pydantic agent schemas) |

Machine-readable schemas will live in `libs/contracts` (Pydantic v2) and OpenAPI is generated from FastAPI. Where this document and generated OpenAPI disagree after implementation, OpenAPI wins and this document must be corrected.

---

## 1. Conventions

| Topic | Rule |
|---|---|
| Base paths | Public: `core-api` `/v1/...`. Internal: `/internal/v1/...` on each service (internal network only). |
| Format | JSON, UTF-8. Timestamps ISO-8601 UTC (`2026-10-09T10:15:00Z`). IDs are UUID strings (examples use short IDs for readability). |
| Public auth | `Authorization: Bearer <JWT>`; claims `sub`, `role` (`student`/`teacher`/`admin`), `exp`. |
| Internal auth | `Authorization: Bearer <service token>`; short-lived signed token with `iss` (calling service), `aud`, `sub` (acting user id, when acting on behalf of one), `scope`. Internal endpoints reject user JWTs. |
| Trace | `X-Trace-Id` accepted/generated at `core-api`, propagated on every call, echoed in responses and error bodies. |
| Idempotency | `Idempotency-Key` header **required** on all state-changing calls marked *(idem)*. Same key + same body → stored response replayed. Same key + different body → `409 IDEMPOTENCY_KEY_REUSED`. Keys retained for a configurable window. |
| Validation | Unknown fields rejected (`extra=forbid`) → `422 VALIDATION_ERROR`. |
| Pagination | `?limit=` (default 25, max 100) and `?cursor=`; response `{"items": [...], "next_cursor": "..."|null}`. |
| Rate limiting | `429 RATE_LIMITED` with `Retry-After`. |

### 1.1 Error envelope

```json
{
  "error": {
    "code": "INVALID_RUN_STATE",
    "message": "Run r_55 is COMPLETED and cannot accept events.",
    "details": {"status": "COMPLETED"},
    "trace_id": "9f3c1a…"
  }
}
```

| HTTP | `code` examples |
|---|---|
| 400 | `BAD_REQUEST` |
| 401 | `UNAUTHENTICATED`, `TOKEN_EXPIRED` |
| 403 | `FORBIDDEN` |
| 404 | `NOT_FOUND` (also returned for resources the caller may not know exist) |
| 409 | `IDEMPOTENCY_KEY_REUSED`, `INVALID_RUN_STATE`, `ESCALATION_ALREADY_ACCEPTED`, `VERSION_CONFLICT` |
| 413 / 415 | `FILE_TOO_LARGE`, `UNSUPPORTED_MEDIA_TYPE` |
| 422 | `VALIDATION_ERROR` |
| 429 | `RATE_LIMITED` |
| 502 / 503 | `UPSTREAM_UNAVAILABLE`, `SERVICE_UNAVAILABLE` |

### 1.2 Role matrix (public API)

| Endpoint group | student | teacher | admin |
|---|---|---|---|
| Auth, `/v1/me`, courses/topics | ✔ | ✔ | ✔ |
| Documents (own) | ✔ | — | ✔ (read) |
| Doubts (own sessions) | ✔ | — | ✔ (read) |
| Progress (own) | ✔ | — | ✔ (any) |
| Teacher escalation endpoints | — | ✔ (matched/assigned only) | ✔ (read) |
| Admin analytics / traces | — | — | ✔ |

---

## 2. `core-api` public endpoints

### 2.1 Auth and catalog

| Method & path | Purpose |
|---|---|
| `POST /v1/auth/login` | Exchange demo credentials for a JWT |
| `GET /v1/me` | Current user |
| `GET /v1/courses` | Courses the caller may see (enrolled for students) |
| `GET /v1/courses/{course_id}/topics` | Topics with prerequisite IDs |

```json
// POST /v1/auth/login
{ "email": "student1@demo.local", "password": "…" }
// 200
{ "access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600,
  "user": { "id": "u_s1", "role": "student", "display_name": "Demo Student 1" } }
```

### 2.2 Documents

| Method & path | Purpose |
|---|---|
| `POST /v1/documents` *(idem)* | Multipart upload (`file`, `course_id`, `title`) |
| `GET /v1/documents` | Own documents, with status |
| `GET /v1/documents/{document_id}` | Status detail |
| `DELETE /v1/documents/{document_id}` | Delete document, chunks and embeddings |

```json
// POST /v1/documents  → 202
{ "document_id": "d_12", "status": "UPLOADED", "course_id": "c_cn", "title": "Transport layer notes",
  "created_at": "2026-10-09T09:00:00Z" }

// GET /v1/documents/d_12 → 200
{ "document_id": "d_12", "status": "READY", "page_count": 14, "chunk_count": 61,
  "ocr_pages": [3, 4], "error_code": null }
```

Validation: size and MIME limits configurable; MIME type sniffed server-side, not trusted from the client. Status values: `UPLOADED, PARSING, INDEXING, READY, FAILED, DELETED`. Duplicate content (same SHA-256, same owner) returns the existing `document_id` with `200`.

`DELETE` returns `204`; the document is excluded from search immediately (status `DELETED`), and chunks/embeddings are removed in the same transaction in `knowledge-svc`.

### 2.3 Doubts

| Method & path | Purpose |
|---|---|
| `POST /v1/doubts` *(idem)* | Create session and start a workflow run |
| `GET /v1/doubts/{session_id}` | Session state and latest intervention (poll; supports `?since=<ISO ts>` for incremental messages) |
| `GET /v1/doubts` | Own sessions |
| `POST /v1/doubts/{session_id}/messages` *(idem)* | Student reply (clarification reply or follow-up) |
| `POST /v1/doubts/{session_id}/ack` *(idem)* | After an explanation: `understood`, `still_confused`, or `check_me` |
| `POST /v1/doubts/{session_id}/answers` *(idem)* | Submit practice answer |
| `POST /v1/doubts/{session_id}/request-teacher` *(idem)* | Student explicitly requests a teacher (sets `explicit_teacher_request`) |
| `POST /v1/doubts/{session_id}/cancel` *(idem)* | Cancel |

```json
// POST /v1/doubts
{ "course_id": "c_cn",
  "text": "Why does TCP slow start double the window every RTT, but then stop doubling?",
  "client_ref": "optional-opaque-string" }
// 202
{ "session_id": "s_88", "run_id": "r_55", "status": "RUNNING", "created_at": "2026-10-09T10:15:00Z" }
```

```json
// GET /v1/doubts/s_88  (run status AWAITING_STUDENT after an explanation)
{
  "session_id": "s_88",
  "status": "AWAITING_STUDENT",
  "course_id": "c_cn",
  "topic": { "id": "t_tcp_congestion", "name": "TCP congestion control" },
  "hypotheses": [
    { "id": "g_3", "topic_id": "t_tcp_congestion", "description": "May confuse slow-start threshold with congestion window", "status": "proposed" }
  ],
  "latest_intervention": {
    "id": "i_21",
    "action": "GENERATE_EXPLANATION",
    "rule_id": "R6_low_evidence_explain",
    "explanation": {
      "text": "Slow start grows the congestion window exponentially until it reaches ssthresh … [1]",
      "citations": [
        { "n": 1, "chunk_id": "ch_412", "document_id": "d_12", "page": 6,
          "quote": "…the window doubles each RTT until ssthresh is reached…", "verified": true }
      ],
      "follow_up_check_offered": true
    },
    "created_at": "2026-10-09T10:15:07Z"
  },
  "messages": [ { "id": "m_1", "role": "student", "content": "Why does TCP slow start…", "created_at": "2026-10-09T10:15:00Z" } ]
}
```

`rule_id` is shown to the student only if the deployment enables "explain my path" (demo setting); it is always returned to admins.

```json
// POST /v1/doubts/s_88/ack
{ "ack": "check_me" }          // understood | still_confused | check_me
// 202 { "run_id": "r_55", "status": "RUNNING" }

// GET /v1/doubts/s_88 when AWAITING_ANSWER
"latest_intervention": {
  "action": "GENERATE_PRACTICE",
  "rule_id": "R7_check_after_explanation",
  "practice": {
    "items": [
      { "item_id": "pi_7", "kind": "mcq", "prompt": "After a timeout, TCP Reno sets cwnd to …",
        "options": ["1 MSS", "ssthresh", "half of cwnd", "unchanged"] }
    ]
  }
}
```

The practice item response **never** includes `answer_key` or `rubric`.

```json
// POST /v1/doubts/s_88/answers
{ "item_id": "pi_7", "answer": "1 MSS", "hints_used": 0 }
// 202 { "attempt_id": "at_31", "run_id": "r_55", "status": "RUNNING" }
```

The grading result appears on the next `GET`:

```json
"last_attempt": { "attempt_id": "at_31", "item_id": "pi_7", "correct": true, "partial_credit": null,
                  "error_tags": [], "feedback": "Correct: after a timeout Reno restarts from 1 MSS." }
```

### 2.4 Progress and analytics

| Method & path | Purpose |
|---|---|
| `GET /v1/learners/me/progress` | Per-topic mastery summary, hypotheses, recent evidence |
| `GET /v1/admin/learners/{student_id}/progress` | Admin read |
| `GET /v1/admin/analytics/overview` | Aggregates |
| `GET /v1/admin/runs/{run_id}/trace` | Proxy to orchestrator steps and decisions |

```json
// GET /v1/learners/me/progress
{ "topics": [
  { "topic_id": "t_tcp_congestion", "name": "TCP congestion control",
    "status": "emerging",
    "mastery_mean": 0.58, "evidence_count": 3, "distinct_items": 2,
    "last_evidence_at": "2026-10-09T10:20:00Z",
    "open_hypotheses": [ { "id": "g_3", "status": "proposed" } ],
    "recent_evidence": [ { "source": "attempt", "polarity": "positive", "weight": 1.0, "at": "2026-10-09T10:20:00Z" } ] }
] }
```

Numbers above are placeholders to illustrate shape only.

### 2.5 Teacher endpoints

| Method & path | Purpose |
|---|---|
| `PUT /v1/teacher/availability` *(idem)* | Replace the teacher's future availability slots |
| `GET /v1/teacher/escalations?status=OPEN` | Escalations matched to or open for this teacher |
| `GET /v1/teacher/escalations/{escalation_id}` | Brief, thread, learner context |
| `POST /v1/teacher/escalations/{escalation_id}/accept` *(idem)* | First accept wins; optional `slot_id` |
| `POST /v1/escalations/{escalation_id}/messages` *(idem)* | Thread message (student or accepted teacher) |
| `POST /v1/teacher/escalations/{escalation_id}/resolve` *(idem)* | Submit feedback and resolve |

```json
// GET /v1/teacher/escalations/e_9
{
  "escalation_id": "e_9", "status": "ACCEPTED", "topic_id": "t_tcp_congestion",
  "brief": {
    "doubt": "Why does slow start stop doubling?",
    "reason_rule_id": "R5_repeated_failure",
    "attempts_summary": [
      { "item_prompt": "After a timeout, TCP Reno sets cwnd to …", "correct": false, "error_tags": ["confuses_ssthresh_cwnd"] }
    ],
    "open_hypotheses": [ { "id": "g_3", "description": "…", "status": "proposed" } ],
    "mastery": { "status": "emerging", "evidence_count": 4 },
    "prerequisites_weak": [ { "topic_id": "t_tcp_handshake", "status": "unknown" } ]
  },
  "match": { "rank": 1, "components": { "topic_proficiency": 0.9, "past_feedback": 0.7, "availability": 1.0, "load": 0.8 }, "score": 0.87 },
  "expires_at": "2026-10-10T10:15:00Z"
}
```

```json
// POST /v1/teacher/escalations/e_9/resolve
{
  "notes": "Walked through ssthresh vs cwnd with a worked example.",
  "topic_assessments": [
    { "topic_id": "t_tcp_congestion", "level": "emerging" },
    { "topic_id": "t_tcp_handshake", "level": "solid" }
  ],
  "hypothesis_decisions": [ { "hypothesis_id": "g_3", "decision": "confirmed" } ]
}
// 200 { "escalation_id": "e_9", "status": "RESOLVED", "resumed_run": true }
```

`level ∈ {struggling, emerging, solid}`. Validation: the teacher must be the accepting teacher; topic IDs must belong to the course; `409 ESCALATION_ALREADY_ACCEPTED` when another teacher accepted first. A second `resolve` with the same idempotency key replays the first response.

---

## 3. `core-api` internal endpoints (called by `orchestrator`)

Base `/internal/v1`. All writes *(idem)*.

| Method & path | Purpose |
|---|---|
| `GET /learners/{student_id}/state?topic_ids=a,b` | Learner state for topics + weak prerequisites + open hypotheses + recent error tags |
| `POST /evidence` *(idem)* | Append evidence events (used for non-attempt sources) |
| `POST /attempts/{attempt_id}/grade` *(idem, key = attempt_id)* | Record grading result; core derives evidence, updates state and hypotheses atomically |
| `GET /practice-items/{item_id}` | Item including `answer_key`/`rubric` (internal only) |
| `POST /practice-items:batch` *(idem)* | Save validated generated items |
| `POST /interventions` *(idem)* | Persist an intervention (and assistant message) to the session |
| `POST /gap-hypotheses` *(idem)* | Create `proposed` hypotheses |
| `POST /escalations` *(idem, key = run_id + sequence)* | Create escalation; runs deterministic matching |
| `GET /topics?course_id=` | Topic taxonomy for `understand` |

```json
// GET /internal/v1/learners/u_s1/state?topic_ids=t_tcp_congestion
{
  "student_ref": "sr_4a1f",
  "topics": [
    { "topic_id": "t_tcp_congestion", "status": "emerging", "alpha": 2.4, "beta": 1.9,
      "mean": 0.56, "evidence_count": 3, "distinct_items": 2, "last_evidence_at": "2026-10-08T12:00:00Z",
      "recent_error_tags": [ { "tag": "confuses_ssthresh_cwnd", "count": 2, "window_days": 14 } ],
      "weak_prerequisites": [ { "topic_id": "t_tcp_handshake", "status": "unknown" } ],
      "open_hypotheses": [ { "id": "g_3", "status": "proposed" } ] }
  ]
}
```

```json
// POST /internal/v1/attempts/at_31/grade   (Idempotency-Key: at_31)
{ "correct": false, "partial_credit": null, "error_tags": ["confuses_ssthresh_cwnd"],
  "grader": "exact", "hints_used": 0, "item_difficulty": "medium" }
// 200
{ "evidence_event_id": "ev_88",
  "topic_state": { "topic_id": "t_tcp_congestion", "status": "emerging", "mean": 0.45, "evidence_count": 4, "distinct_items": 3 },
  "hypothesis_updates": [ { "id": "g_3", "status": "confirmed", "reason": "2 failed distinct targeting items" } ] }
```

```json
// POST /internal/v1/escalations
{ "run_id": "r_55", "session_id": "s_88", "student_id": "u_s1", "topic_id": "t_tcp_congestion",
  "reason_rule_id": "R5_repeated_failure",
  "brief": { "doubt": "…", "attempts_summary": [], "open_hypotheses": [], "mastery": {}, "prerequisites_weak": [] } }
// 201
{ "escalation_id": "e_9", "status": "OPEN",
  "candidates": [ { "teacher_id": "u_t3", "rank": 1, "score": 0.87, "components": { "topic_proficiency": 0.9, "past_feedback": 0.7, "availability": 1.0, "load": 0.8 } } ],
  "expires_at": "2026-10-10T10:15:00Z" }
```

If there are no candidates, `candidates` is empty and the escalation stays `OPEN` and visible to all teachers of the course until `expires_at`.

---

## 4. `orchestrator` internal endpoints

| Method & path | Purpose |
|---|---|
| `POST /internal/v1/runs` *(idem)* | Create a run (idempotent on `session_id + trigger_id`) |
| `GET /internal/v1/runs/{run_id}` | Run status and non-sensitive state |
| `GET /internal/v1/runs/{run_id}/steps` | Steps and decision records (admin trace) |
| `POST /internal/v1/runs/{run_id}/events` *(idem, key = event id)* | Deliver student events |
| `POST /internal/v1/runs/{run_id}/resume` *(idem, key = escalation_id)* | Resume after teacher outcome |
| `POST /internal/v1/runs/{run_id}/cancel` *(idem)* | Cancel |

```json
// POST /internal/v1/runs
{ "session_id": "s_88", "trigger_id": "m_1", "student_id": "u_s1", "student_ref": "sr_4a1f",
  "course_id": "c_cn", "allowed_course_ids": ["c_cn"], "doubt_text": "Why does TCP slow start…" }
// 202 { "run_id": "r_55", "status": "CREATED" }
```

`allowed_course_ids` is the enrollment-derived ACL passed to retrieval; the orchestrator never derives access itself.

```json
// POST /internal/v1/runs/r_55/events
{ "event_id": "ev_5", "type": "practice_answer",
  "payload": { "attempt_id": "at_31", "item_id": "pi_7" } }
```

Event `type ∈ {student_message, student_ack, practice_answer, student_requests_teacher, cancel}`. Payload schemas: `student_message {message_id}`, `student_ack {ack: understood|still_confused|check_me}`, `practice_answer {attempt_id, item_id}`, `student_requests_teacher {}`. Responses: `202 {"status": "RUNNING"}`; wrong state → `409 INVALID_RUN_STATE`; unknown event ID replays stored result.

```json
// POST /internal/v1/runs/r_55/resume   (Idempotency-Key: e_9)
{ "escalation_id": "e_9", "outcome": "RESOLVED",
  "teacher_evidence_event_ids": ["ev_90", "ev_91"],
  "hypothesis_updates": [ { "id": "g_3", "status": "confirmed" } ] }
// outcome ∈ RESOLVED | DECLINED | EXPIRED | CANCELLED
// 202 { "run_id": "r_55", "status": "RUNNING" }
```

Teacher evidence is written to the ledger by `core-api` **before** resume is sent; the orchestrator re-reads learner state after resuming.

```json
// GET /internal/v1/runs/r_55/steps  (admin trace via core-api proxy)
{ "steps": [
  { "seq": 1, "node": "understand", "provider": "fake", "model": "fake-1", "prompt_version": "understand@1",
    "input_hash": "sha256:…", "latency_ms": 12, "output": { "topic_id": "t_tcp_congestion", "intent": "conceptual", "clarity": "clear" } },
  { "seq": 3, "node": "decide", "output": { "rule_id": "R6_low_evidence_explain", "action": "GENERATE_EXPLANATION" } }
],
  "decisions": [ {
    "rule_id": "R6_low_evidence_explain", "action": "GENERATE_EXPLANATION",
    "inputs_snapshot": { "clarity": "clear", "classification_confidence": 0.9,
      "retrieval_support": { "n_chunks_above_threshold": 4, "top_dense_similarity": 0.71, "lexical_hits": 6 },
      "mastery": { "status": "emerging", "mean": 0.56, "evidence_count": 3 },
      "counters": { "actions_used": 0, "clarify_rounds": 0, "explain_attempts": 0 } },
    "advisor": { "provider": "jev", "mode": "shadow", "proposal": "GENERATE_PRACTICE", "confidence": 0.62, "fallback": false },
    "overridden": false } ] }
```

Numeric values are illustrative.

---

## 5. `knowledge-svc` internal endpoints

| Method & path | Purpose |
|---|---|
| `POST /internal/v1/documents` *(idem)* | Multipart forwarded by `core-api` after authorization; creates document + ingestion job |
| `GET /internal/v1/documents/{document_id}` | Status |
| `DELETE /internal/v1/documents/{document_id}` *(idem)* | Delete document, chunks, embeddings |
| `POST /internal/v1/search` | Hybrid retrieval (read-only) |
| `GET /healthz`, `GET /readyz` | Health |

```json
// POST /internal/v1/search
{ "query": "TCP slow start window growth ssthresh",
  "allowed_course_ids": ["c_cn"],
  "owner_ids": ["u_s1"],           // documents owned by the student (+ course-shared documents if enabled)
  "top_k": 6,
  "mode": "hybrid" }               // hybrid | dense | lexical (for evaluation ablations)
// 200
{ "results": [
    { "chunk_id": "ch_412", "document_id": "d_12", "page": 6, "section_path": ["Transport", "Congestion control"],
      "text": "…the window doubles each RTT until ssthresh is reached…",
      "scores": { "dense": 0.71, "lexical": 0.33, "fused": 0.031 } }
  ],
  "support": { "n_chunks_above_threshold": 4, "top_dense_similarity": 0.71, "lexical_hits": 6 },
  "index_version": "…" }
```

The ACL filter (`course_id ∈ allowed_course_ids`, `owner/visibility`, `status = READY`, `deleted_at IS NULL`) is applied in SQL before ranking. `support` uses the configured `RET_MIN_SIM`; `n_chunks_above_threshold` counts results meeting it.

---

## 6. Events

Transport: Redis Stream `eduos.events`, one consumer group per consuming service; at-least-once delivery, consumers dedupe on `event_id`.

| Event | Producer → consumer | Payload |
|---|---|---|
| `document.ready` | `knowledge-svc` → `core-api` | `{event_id, document_id, chunk_count, page_count, ocr_pages, at}` |
| `document.failed` | `knowledge-svc` → `core-api` | `{event_id, document_id, error_code, at}` |
| `document.deleted` | `knowledge-svc` → `core-api` | `{event_id, document_id, at}` |

Escalation resumption is **not** a stream event; it is the outbox-relayed HTTP call in §4 so that failures are retried by the relay with the stored idempotency key.

---

## 7. Agent output contracts

Pydantic definitions for `DoubtAnalysis`, `Explanation`, `PracticeItemDraft`, `EvaluationResult`, `PolicyInput`, `Decision`, `DecisionRequest/Result` and `WorkflowState` are in [AGENT_SPECIFICATIONS](AGENT_SPECIFICATIONS.md). All carry `schema_version`; any change that removes or retypes a field increments it and is a breaking change requiring contract tests to be updated in the same change.

## 8. Compatibility and testing rules

- Contract tests (Phase 1 onward) assert: OpenAPI generated from FastAPI matches the models in `libs/contracts`; every state-changing endpoint rejects a missing `Idempotency-Key`; `answer_key` and `rubric` never appear in any public response; internal endpoints reject user JWTs; public endpoints reject service tokens.
- Cross-student access tests exist for every endpoint that takes an ID.
- Idempotency tests replay each *(idem)* endpoint with the same key (same response) and with a changed body (409).


---

## 9. Phase 2 as implemented (authoritative where it differs from sections 2.2 and 5)

Everything here exists and is covered by tests; see [PHASE2_ACCEPTANCE](PHASE2_ACCEPTANCE.md).

### 9.1 Documents (asynchronous by default, `INGESTION_MODE=async`)

| Method & path | Behaviour |
|---|---|
| `POST /v1/documents` (multipart `file`, `course_id`, `title`) | Roles `student` (own course, private document) and `admin` (course-shared). **202** `QUEUED` with the document and its job; **200** if the same bytes were already uploaded by this owner in this course (no second document or job). `INGESTION_MODE=sync`: **201** only once `READY`, or **422** `INGESTION_FAILED` for an unreadable file. 413 `FILE_TOO_LARGE`, 415 `UNSUPPORTED_MEDIA_TYPE`, 403 not enrolled / teacher. |
| `GET /v1/documents`, `GET /v1/documents/{id}` | Document with the latest job. 404 for documents the caller may not see. |
| `PUT /v1/documents/{id}/file` (multipart `file`) | Replace the content. Owner or admin. **202**; the current version stays searchable until the new one is fully indexed. |
| `POST /v1/documents/{id}/reindex` | Rebuild chunks from the stored file (e.g. after enabling OCR). Owner or admin. **202**. |
| `DELETE /v1/documents/{id}` | **204**. Chunks, embeddings, jobs and every stored file version are removed in the same operation. |
| `GET /v1/admin/documents/{id}/events` | Admin only. Lifecycle audit trail (metadata only; survives deletion). |

Document object: `document_id, course_id, title, filename, status (QUEUED | PROCESSING | READY | FAILED), indexed (true only when READY), visibility, page_count, chunk_count, error_code, version, extraction, created_at, updated_at, job`.
`extraction` (after indexing): `{ocr_engine, ocr_pages[], empty_pages[], failed_pages[{page,error}], ocr_budget_exceeded_pages[]}`.
`job`: `{job_id, status (QUEUED | PROCESSING | DONE | FAILED), stage, attempts, max_attempts, error_code, last_error, queue ("notified" | "deferred"), created_at, finished_at}`; `deferred` means Redis could not be notified and a worker will find the job by polling.

A failed *replace* or *re-index* sets `job.status = FAILED` but leaves the document `READY` and serving its previous version.

Ingestion `error_code` values: `UNREADABLE_PDF, ENCRYPTED_PDF, TOO_MANY_PAGES, NO_EXTRACTABLE_TEXT, FILE_MISSING, PARSER_TIMEOUT, PARSER_CRASHED, PARSER_RESOURCE_LIMIT, DUPLICATE_CONTENT, MAX_ATTEMPTS_EXCEEDED, WORKER_LOST, DOCUMENT_MISSING`.
Lifecycle request errors (all **409**): `DOCUMENT_NOT_READY`, `JOB_IN_PROGRESS`, `UNCHANGED_CONTENT`, `DUPLICATE_CONTENT`.

### 9.2 Search

`GET /v1/search?q=&course_id=&top_k=` returns `{query, method, n_above_threshold, min_terms, results[{chunk_id, document_id, document_title, course_id, page, text, rank, matched_terms}]}`.
`method` is `postgres_fts`, `hybrid_rrf` or `dense`. Hybrid retrieval, semantic scores and fallbacks are reported in the **admin trace**, not the public search response. Only `READY` documents and active versions are searched; access control is applied in SQL before ranking.

### 9.3 Acknowledgment and evidence

| Method & path | Behaviour |
|---|---|
| `POST /v1/doubts/{session_id}/ack` | Body `{"ack": "understood" \| "still_confused" \| "check_me"}` (no other fields). Student only; `Idempotency-Key` required. **202** `{run_id, status, ack, mastery_credit: 0.0}`. 404 for another student's session, 403 for other roles, 409 `INVALID_RUN_STATE` unless the run is waiting for an acknowledgment, 409 `IDEMPOTENCY_KEY_REUSED`, 409 `DUPLICATE_ACK`, 422 for an invalid value. A repeated delivery with the same key replays the first response and applies nothing. |
| `GET /v1/learners/me/evidence` | Student's own ledger rows `{id, evidence_type, weight, topic_id, source_run_id, provenance, created_at}` plus a note. |
| `GET /v1/admin/learners/{student_id}/evidence` | Admin read. |

Evidence types today: `self_report_understood`, `self_report_confused`, `check_requested`, all with **weight 0** (enforced in code and by database constraints). The ledger is append-only (database trigger).
An acknowledgment never changes mastery. After `understood` / `check_me` the policy selects practice, which does not exist yet, so the run completes with `outcome = "UNVERIFIED"` and the intervention `{practice: {available: false}, message, outcome}`.

### 9.4 Admin trace additions

`retrieval[]` entries now include `mode: {requested, used, fallback_reason, degraded, missing_embeddings, embedding_model, min_sim}`, and each result has `dense_similarity`, `fused_score` and `sources` (`["fts"]`, `["dense"]` or both). `used` reports what actually ran; a requested hybrid search that degraded to full text says so and why (`embeddings_not_configured`, `pgvector_unavailable`, `no_active_embedding_model`, `embedding_model_mismatch`, `dense_error`, `empty_query`).

### 9.5 Readiness

`GET /readyz` adds an informational `retrieval` object (`configured_mode, embedding_provider, pgvector, effective_mode, fallback_reason, embedding_model, dims, missing_embeddings`). It does not affect the ready/not-ready decision.

### 9.6 Not implemented

The `/internal/*` endpoints and Redis Stream events of sections 3–6 do not exist (monolith). Practice, attempts, grading, teacher/escalation endpoints, `request-teacher` resume and learner progress are still future work.


## 10. Phases 3–5 as implemented

All under `/v1`, bearer-authenticated, errors in the standard envelope. Writes that can be retried require `Idempotency-Key`.

| Method and path | Role | Purpose |
|---|---|---|
| `POST /doubts/{id}/answers` `{item_id, answer, hints_used}` | student | One scored attempt per item; the reference answer is revealed only in this response (409 `ITEM_ALREADY_ANSWERED` afterwards) |
| `GET /doubts/{id}/decisions` | student (own), admin | Per-decision plain-language explanation: rule, reasons, hard-rule precedence, evidence used, provider/model, fallbacks (admin also sees model calls and errors) |
| `GET /learners/me/gap-map` | student | Per-topic status, reason, linked evidence with timestamps, hypotheses (suspected vs confirmed), prerequisite suggestions |
| `GET /learners/me/passport` | student | Deterministic record with a SHA-256 digest: topics, answers, doubts and outcomes, teacher feedback, history, retention note |
| `GET /learners/me/{evidence,progress,history}` | student | Ledger rows (acknowledgements have weight 0), per-topic mastery, change history |
| `GET /escalations/{id}` | student (own) / teacher (authorised) / admin | Role-specific view; others get 404 |
| `POST /escalations/{id}/messages`, `POST /escalations/{id}/rate` | student, teacher | Asynchronous thread (idempotent); one helpfulness rating |
| `GET /teacher/escalations?status=`, `POST /teacher/escalations/{id}/{accept,release,resolve}` | teacher | Inbox, race-safe accept, idempotent resolve (writes bounded evidence, resumes the workflow) |
| `GET/PUT /teacher/availability`, `GET /teacher/profile` | teacher | Validated slots (aware datetimes, ≤ 12 h, within 90 days) |
| `GET /admin/escalations`, `GET /admin/teachers`, `POST /admin/escalations/{id}/assign`, `POST /admin/escalations/reconcile` | admin | Oversight, assign/reassign/override (audited), apply pending resumes and expiries |
| `GET /admin/system`, `GET /admin/ingestion/jobs`, `GET /admin/runs`, `GET /admin/runs/{id}/trace` | admin | Provider health, retrieval mode in effect, counters, jobs, traces (with decision ids and evidence references) |
| `POST /admin/students/{id}/anonymize` `{confirm: true}` | admin | ADR-013 |
