# EduOS API Contracts

| | |
|---|---|
| **Status** | Design. **No endpoint below is implemented yet.** JSON examples are illustrative contracts, not captured responses. |
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
