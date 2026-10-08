# EduOS Agent and Component Specifications

| | |
|---|---|
| **Status** | Design. **No component below is implemented.** Code blocks are proposed type definitions, not existing code. |
| **Version** | 0.1 (2026-10-09) |
| **Related** | [ARCHITECTURE](ARCHITECTURE.md), [API_CONTRACTS](API_CONTRACTS.md), [EVALUATION_PLAN](EVALUATION_PLAN.md) |

## 1. Component roster

Only four components call an LLM. Everything else is deterministic code. Components are named for what they do; "agent" is reserved for LLM-backed steps.

| # | Component | Kind | LLM | Deployed in |
|---|---|---|---|---|
| 1 | Doubt Understanding Agent | Agent | Yes | `orchestrator` |
| 2 | Learner State Service (was "Learner Modeling Agent") | Deterministic service | No | `core-api` (module `learner`) |
| 3 | Retrieval (was "Knowledge Retrieval Agent") | Tool | No | `knowledge-svc` |
| 4 | Intervention Policy Engine (was "Intervention Decision Agent") | Deterministic engine with optional advisor | No (advisor optional, shadow) | `orchestrator` |
| 5 | Explanation Agent | Agent | Yes | `orchestrator` |
| 6 | Practice Generation Agent | Agent | Yes | `orchestrator` |
| 7 | Answer Evaluator | Hybrid | Only for `short_text` rubric grading | `orchestrator` |
| 8 | Teacher Matcher (was "Teacher Matching Agent") | Deterministic scorer | No (optional brief summary, enhancement) | `core-api` (module `teaching`) |
| 9 | Learning Analytics | Queries/views | No | `core-api` (module `analytics`) |

Cross-cutting: `LLMProvider` (`fake`, plus a real provider once configured), `DecisionProvider` (`rules` default, `llm`, `jev`), citation verifier, practice validators.

## 2. Shared conventions

- Every output model has `schema_version: Literal["1"]` and `model_config = ConfigDict(extra="forbid")`.
- Every LLM call goes through `LLMProvider.generate_structured(schema, messages, prompt_version, max_tokens, timeout_s)`; the provider returns the validated model or raises `LLMError(kind=…)`.
- Failure policy for LLM nodes: validate → on schema failure one repair retry with the validation error appended → on second failure or provider error use the node's fallback (below). Provider errors get bounded backoff retries (timeouts, 429/5xx).
- Each execution is recorded in `orch.workflow_steps` (node, input hash, output, provider, model, prompt version, latency, tokens, error).
- Prompts live in versioned files (`prompts/<node>@<n>.md`); `prompt_version` is recorded per step.
- Untrusted text (doubts, retrieved chunks, student answers) is always inserted inside delimited data blocks; the system prompt states that text inside the blocks is data, never instructions. No generation node is given tools.

### 2.1 `LLMProvider` and `FakeLLM`

```python
class LLMProvider(Protocol):
    name: str
    async def generate_structured(
        self, *, schema: type[BaseModel], system: str, user: str,
        prompt_version: str, max_tokens: int, timeout_s: float,
    ) -> StructuredResult: ...   # .value (validated model), .tokens_in, .tokens_out, .model

class FakeLLM:
    """Deterministic: returns canned, schema-valid fixtures keyed by (prompt_version, input hash prefix or scenario tag).
    Supports fault modes: timeout, invalid_json, schema_violation, slow, flaky(n)."""
```

`FakeLLM` makes the system runnable and testable end-to-end without credentials (open item O-1). Quality of real LLM outputs cannot be claimed until a real provider is configured and evaluated.

### 2.2 `WorkflowState` (persisted in `orch.workflow_runs.state`)

```python
class Counters(BaseModel):
    actions_used: int = 0
    clarify_rounds: int = 0
    explain_attempts: int = 0
    practice_sets: int = 0
    failed_checks: int = 0
    llm_calls: int = 0
    tokens: int = 0

class Flags(BaseModel):
    explicit_teacher_request: bool = False
    safety_flag: bool = False
    teacher_unavailable: bool = False

class Pending(BaseModel):
    kind: Literal["student_message", "student_ack", "practice_answer", "teacher", "none"] = "none"
    item_ids: list[str] = []
    escalation_id: str | None = None

class WorkflowState(BaseModel):
    schema_version: Literal["1"] = "1"
    run_id: str; session_id: str; student_id: str; student_ref: str; course_id: str
    allowed_course_ids: list[str]
    doubt_text: str
    status: RunStatus                      # CREATED RUNNING AWAITING_STUDENT AWAITING_ANSWER WAITING_HUMAN COMPLETED FAILED CANCELLED
    current_node: str
    counters: Counters = Counters()
    budgets: Budgets                       # from config at run creation (snapshotted for reproducibility)
    analysis: DoubtAnalysis | None = None
    context: Context | None = None         # mastery summary + retrieval summary (chunk ids, support)
    explained: bool = False                # an explanation delivered in this run
    acked: Literal["none", "understood", "still_confused", "check_me"] = "none"
    last_check: CheckResult | None = None  # last practice outcome
    last_decision: Decision | None = None
    pending: Pending = Pending()
    flags: Flags = Flags()
    outcome: Literal["RESOLVED", "UNRESOLVED"] | None = None
    version: int = 0
```

---

## 3. Doubt Understanding Agent

| | |
|---|---|
| **Responsibility** | Infer topic, intent, clarity, and plausible gap hypotheses from the doubt (plus prior messages in the session). Does not retrieve, decide, or explain. |
| **Inputs** | `doubt_text`, session messages (clarification replies), course topic taxonomy (`GET /internal/v1/topics`), optional learner summary |
| **Tools** | None (the taxonomy is passed in the prompt) |
| **Reads** | Topic taxonomy; session messages |
| **Writes** | Step record; `gap_hypotheses` as `proposed` via `core-api` (after validation) |
| **LLM** | Yes (small model tier) |
| **Deployed in** | `orchestrator` |

```python
class GapHypothesisDraft(BaseModel):
    topic_id: str                       # must be in taxonomy
    description: str                    # max length enforced
    # no status here: always stored as "proposed"; never confirmed by this agent

class DoubtAnalysis(BaseModel):
    schema_version: Literal["1"] = "1"
    topic_id: str | None                # must exist in taxonomy else None
    secondary_topic_ids: list[str] = []
    intent: Literal["conceptual", "procedural", "error_diagnosis", "fact_lookup", "other"]
    clarity: Literal["clear", "ambiguous", "off_topic"]
    clarification_question: str | None  # required iff clarity == "ambiguous"
    gap_hypotheses: list[GapHypothesisDraft] = []   # max 3
    safety_flag: bool = False           # distress, academic-integrity, or out-of-policy signal
    explicit_teacher_request: bool = False
    classification_confidence: float    # 0..1, model self-report, NOT mastery
```

**Post-validation (deterministic):** unknown `topic_id` → `None` and `clarity="ambiguous"`; `clarification_question` required when ambiguous; hypotheses capped at 3; `safety_flag`/`explicit_teacher_request` OR'ed with deterministic keyword/pattern checks so the rules R1/R1b never depend only on the model.

**Failure/fallback:** invalid after repair → `DoubtAnalysis(topic_id=None, clarity="ambiguous", clarification_question=<generic>, classification_confidence=0.0)`.

**Evaluation:** topic accuracy and macro-F1, intent macro-F1, ambiguity precision/recall, safety-flag recall on the labeled doubt set ([EVALUATION_PLAN §3.1](EVALUATION_PLAN.md)).

---

## 4. Learner State Service

| | |
|---|---|
| **Responsibility** | Maintain the evidence ledger, derive topic state, manage hypothesis lifecycle, answer state queries. Deterministic. |
| **Inputs** | Graded attempts, teacher feedback, hypotheses from `understand` |
| **Reads/Writes** | `core.evidence_events` (append), `core.learner_topic_state` (derived), `core.gap_hypotheses`, `core.audit_events` |
| **LLM** | No |
| **Deployed in** | `core-api` module `learner` |

```python
def apply_evidence(ev: EvidenceIn, *, now: datetime) -> TopicState: ...   # idempotent on (source, ref_id)
def rebuild_state(student_id: str, topic_id: str) -> TopicState: ...      # replay ledger; used by tests and repair
def topic_state(student_id, topic_ids) -> list[TopicStateView]: ...       # includes weak prerequisites via recursive CTE
```

Rules are in [ARCHITECTURE §8.3 and §9](ARCHITECTURE.md). Key invariants (each has a test):
1. Applying the same `(source, ref_id)` twice changes nothing.
2. `rebuild_state` equals incrementally maintained state.
3. A single correct attempt never yields `demonstrated`; a single failed attempt never yields a `confirmed` hypothesis.
4. `self_report` and `explanation_ack` evidence weigh 0.
5. Recency decay: with no new evidence, state moves toward the prior as `Δt` grows.
6. Teacher `emerging` assessment writes no evidence.

Failure handling: DB errors roll back the whole grade call; the caller retries with the same idempotency key.

Evaluation: invariant tests, simulated-student tests (consistent learners converge; one-off slips do not flip status), replay equality.

---

## 5. Retrieval (tool)

| | |
|---|---|
| **Responsibility** | Authorized hybrid retrieval and support signals. Not an agent: a function with no autonomy. |
| **Inputs** | `query`, `allowed_course_ids`, `owner_ids`, `top_k`, `mode` |
| **Output** | `SearchResponse {results[], support{n_chunks_above_threshold, top_dense_similarity, lexical_hits}}` ([API_CONTRACTS §5](API_CONTRACTS.md)) |
| **Reads** | `know.chunks` (ACL filter in SQL first) |
| **Writes** | None |
| **LLM** | No (query is built deterministically from the analysis: doubt text + topic name + key terms) |
| **Deployed in** | `knowledge-svc` |

Failure: timeout/5xx → `retrieval_support = unavailable`; the policy treats this as R4 (never answers as if grounded). Evaluation: Recall@k, MRR, nDCG, ablation across `dense`/`lexical`/`hybrid` ([EVALUATION_PLAN §3.2](EVALUATION_PLAN.md)).

---

## 6. Intervention Policy Engine and `DecisionProvider`

| | |
|---|---|
| **Responsibility** | Choose exactly one action per `decide` step using ordered rules; record the rule and inputs. |
| **LLM** | No. An optional advisor (`DecisionProvider`) can propose within a bounded set; default provider is rules. |
| **Reads** | `WorkflowState` and the learner state snapshot |
| **Writes** | `orch.decision_records`; updates `state.last_decision` |
| **Deployed in** | `orchestrator` |

```python
Action = Literal["ASK_CLARIFICATION", "GENERATE_EXPLANATION", "GENERATE_PRACTICE",
                 "ESCALATE_TO_TEACHER", "COMPLETE"]

class RetrievalSupport(BaseModel):
    available: bool
    n_chunks_above_threshold: int = 0
    top_dense_similarity: float = 0.0
    lexical_hits: int = 0

class MasterySummary(BaseModel):
    status: Literal["unknown", "hypothesis", "emerging", "demonstrated"]
    mean: float | None
    evidence_count: int
    distinct_items: int
    repeated_error_tag_max: int            # max count of one error tag in the recent window
    weak_prerequisite_ids: list[str] = []

class PolicyInput(BaseModel):
    schema_version: Literal["1"] = "1"
    clarity: Literal["clear", "ambiguous", "off_topic"]
    classification_confidence: float
    retrieval: RetrievalSupport
    mastery: MasterySummary
    counters: Counters
    budgets: Budgets
    flags: Flags
    explained: bool
    acked: Literal["none", "understood", "still_confused", "check_me"]
    last_check: CheckResult | None
    thresholds: Thresholds                 # T_CLARIFY, RET_MIN_CHUNKS, N_MIN, T_MASTER … snapshot from config

class Decision(BaseModel):
    schema_version: Literal["1"] = "1"
    action: Action
    rule_id: str                           # R1_explicit_teacher_request … R10_default_clarify
    outcome: Literal["RESOLVED", "UNRESOLVED"] | None = None   # only with COMPLETE
    reasons: list[str]                     # human-readable, generated from inputs by code, not by a model
    advisor: AdvisorTrace | None = None
    overridden: bool = False

def decide(inp: PolicyInput, advisor: DecisionResult | None = None) -> Decision: ...
```

The ordered rules R1–R10 and the termination argument are in [ARCHITECTURE §8.2](ARCHITECTURE.md). `decide` is a **pure function**: no I/O, no clock, no randomness.

### 6.1 `DecisionProvider`

```python
class DecisionRequest(BaseModel):
    question_id: Literal["next_intervention", "difficulty_route"]
    state: dict                            # sanitized: enums and numbers only, opaque student_ref
    allowed_options: list[str]
    timeout_ms: int

class DecisionResult(BaseModel):
    option: str | None
    probabilities: dict[str, float] = {}
    confidence: float | None
    provider: Literal["rules", "llm", "jev"]
    model_version: str | None
    latency_ms: int
    status: Literal["ok", "timeout", "error", "invalid", "disabled"]
    fallback: bool = False

class DecisionProvider(Protocol):
    async def decide(self, req: DecisionRequest) -> DecisionResult: ...
```

Advisor rules (enforced in `decide`, not in the provider):
1. Consulted only if R1–R5 did not fire.
2. Option must be in `allowed_options`, which is computed from remaining budgets and the current state (e.g., `GENERATE_PRACTICE` not allowed before an explanation unless the rules would allow it).
3. Applied only when `JEV_MODE=advisory` (or the LLM/other advisor is explicitly set to advisory) **and** `confidence ≥ JEV_CONFIDENCE_MIN`. In `shadow` mode the result is logged and ignored.
4. Any non-`ok` status → rules decision, `fallback=True`.
5. The advisor can never produce `COMPLETE` or `ESCALATE_TO_TEACHER` through this path in this build.

Provider specifics for `JevDecisionProvider` and its configuration are in [ARCHITECTURE §13](ARCHITECTURE.md). It is disabled by default (`JEV_MODE=off`) and uses synthetic data only until the experiment and privacy review complete.

**Tests:** table-driven rule tests (one row per rule, plus precedence and boundary cases); termination property test (random states with decreasing budgets always reach escalate/complete within `MAX_ACTIONS`); advisor-veto tests; determinism (same input → same output).

---

## 7. Explanation Agent

| | |
|---|---|
| **Responsibility** | Produce a level-appropriate explanation grounded in retrieved passages, with verifiable citations. |
| **Inputs** | `doubt_text`, `DoubtAnalysis`, retrieved chunks (id, page, text), mastery summary, recent `error_tags`, explanation attempt number, optional `previous_explanation_summary` (for a new angle) |
| **Tools** | None |
| **Reads** | Inputs only |
| **Writes** | Step record; intervention via `core-api` after verification |
| **LLM** | Yes (larger model tier, or tier chosen by an approved difficulty route) |
| **Deployed in** | `orchestrator` |

```python
class Citation(BaseModel):
    chunk_id: str
    quote: str                    # exact span from the chunk

class Explanation(BaseModel):
    schema_version: Literal["1"] = "1"
    text: str                     # contains [n] markers mapped to citations
    citations: list[Citation]
    uses_general_knowledge: bool  # if true, those parts must be labelled "not from your materials"
    follow_up_check: str | None
```

**Citation verifier (deterministic, not an LLM):** for each citation, `chunk_id ∈ retrieved_ids` and `normalize(quote)` is a substring (or ≥ configured similarity match) of `normalize(chunk.text)`. Result per citation: `verified: bool`. Unverified citations are removed along with their `[n]` markers. If zero verified citations remain and `uses_general_knowledge` is false → regenerate once → else fallback.

**Fallback:** present the top retrieved passages (with page refs) and a notice that an explanation could not be generated; the policy still counts this as an explanation attempt.

**Safeguards:** retrieved text in data delimiters; no tools; output rendered as escaped text; if the model claims course grounding with no verified citation, the claim is stripped.

**Evaluation:** citation-verification rate (automatic); human-rated faithfulness/correctness/relevance on a sample; comparison to a plain-LLM-no-retrieval baseline ([EVALUATION_PLAN §3.3–3.4](EVALUATION_PLAN.md)).

---

## 8. Practice Generation Agent

| | |
|---|---|
| **Responsibility** | Generate a small set of items for a topic, a gap hypothesis, or a weak prerequisite. |
| **Inputs** | Target topic (and prerequisite if chosen), hypothesis description (if any), retrieved chunks, mastery summary, recent `error_tags`, desired `kind` mix, count |
| **Tools** | None |
| **Writes** | Draft items → validators → `POST /internal/v1/practice-items:batch` (only validated items) |
| **LLM** | Yes |
| **Deployed in** | `orchestrator` |

```python
class PracticeItemDraft(BaseModel):
    schema_version: Literal["1"] = "1"
    topic_id: str
    kind: Literal["mcq", "numeric", "short_text"]
    prompt: str
    options: list[str] | None       # mcq: 3–5 distinct options
    answer_key: str                 # mcq: exact option text; numeric: canonical number + unit; short_text: reference answer
    numeric_tolerance: float | None
    rubric: str | None              # required for short_text
    difficulty: Literal["easy", "medium", "hard"]
    targets_hypothesis_id: str | None
    source_chunk_ids: list[str]     # must be in the retrieved set
    distractor_error_tags: dict[str, str] = {}   # option → error tag it would indicate (mcq)
```

**Validators (deterministic):** MCQ options distinct and contain `answer_key`; numeric key parses and tolerance sane; `short_text` has a rubric; `source_chunk_ids ⊆ retrieved ids`; `topic_id` ∈ taxonomy; prompt length limits; no duplicate of a recently shown item (prompt hash). Computer Networks numeric items (e.g., subnet size, RTT/throughput arithmetic) additionally pass a **recomputation check** where a deterministic solver exists (subnetting calculator) *(Enhancement; implemented only for item templates that have a solver)*.

**Fallback:** if no valid item remains after one regeneration, skip practice; the policy re-decides (explanation or escalation) and the failure is audited. A small hand-authored seed bank for the demo course can serve as a last resort so the demo spine never depends on generation.

**Evaluation:** key-validity rate via validators + human spot-check of a sample; distractor plausibility rating; alignment of `error_tags` with confusions ([EVALUATION_PLAN §3.5](EVALUATION_PLAN.md)).

---

## 9. Answer Evaluator (hybrid)

| | |
|---|---|
| **Responsibility** | Grade an attempt; emit `error_tags`. Exact for MCQ/numeric; LLM rubric only for `short_text`. |
| **Inputs** | Item (including `answer_key`/`rubric`, fetched internally), student answer, hints used |
| **Writes** | `POST /internal/v1/attempts/{id}/grade` |
| **LLM** | Only for `short_text` |
| **Deployed in** | `orchestrator` (grading result is persisted by `core-api`) |

```python
class EvaluationResult(BaseModel):
    schema_version: Literal["1"] = "1"
    correct: bool | None             # None when ungradable
    partial_credit: float | None     # 0..1, short_text only
    error_tags: list[str]            # constrained vocabulary per topic; unknown tags dropped
    grader: Literal["exact", "llm_rubric"]
    grader_status: Literal["ok", "unavailable"]
    feedback: str                    # generated from templates for exact graders; LLM text only for rubric grading
```

- MCQ: option equality. Numeric: parse + tolerance + unit normalization. Both set `error_tags` from `distractor_error_tags` when an incorrect option is selected.
- `short_text`: LLM compares the answer to the rubric. Answer text is inserted as delimited data; instructions inside the answer are ignored. Result validated; on failure → `grader_status="unavailable"` and **no evidence is written** (the student is asked to retry or can request a teacher).
- Evidence: only `grader_status == "ok"` results reach the ledger. `llm_rubric` evidence carries a lower base weight than `exact` (config).

**Evaluation:** exact graders tested for correctness by unit tests; LLM-rubric agreement with human labels on a sample ([EVALUATION_PLAN §3.6](EVALUATION_PLAN.md)).

---

## 10. Teacher Matcher

| | |
|---|---|
| **Responsibility** | Rank teachers for an escalation; produce a structured brief. Deterministic. |
| **Inputs** | `topic_id`, student id, brief inputs from the orchestrator, `teacher_topics`, `availability_slots`, past `feedback` ratings, open load |
| **Reads/Writes** | Reads teacher tables; writes `escalations.candidates` |
| **LLM** | No. (Optional [Enhancement]: a one-paragraph natural-language summary of the brief, clearly labelled as AI-generated, with the structured fields remaining authoritative.) |
| **Deployed in** | `core-api` module `teaching` |

```python
class MatchComponents(BaseModel):
    topic_proficiency: float      # 0..1 from teacher_topics
    past_feedback: float          # smoothed rate of resolved escalations on this topic; prior-shrunk so new teachers are not zeroed
    availability: float           # 1 if a slot within MATCH_WINDOW, decaying with distance
    load: float                   # 1 - normalized open escalations

class Candidate(BaseModel):
    teacher_id: str
    rank: int
    score: float                  # Σ weight_i · component_i, weights from config
    components: MatchComponents
```

Hard constraints (filter before scoring): covers the topic; is not the requesting student; account active. Candidates with no availability remain eligible but score zero on availability (async thread still possible). Ties broken by `teacher_id` for determinism.

**Learning from previous interactions** *(Official area: "learning from previous student-teacher interactions")*: `past_feedback` uses resolved escalations and the teacher's structured outcomes (e.g., the share of hypotheses confirmed/refuted and subsequent student evidence on the topic after a resolution). This is a transparent aggregate, not a learned model; weights are config.

**Evaluation:** precision@1/@3 against teacher-labeled best fit; zero hard-constraint violations ([EVALUATION_PLAN §3.7](EVALUATION_PLAN.md)).

---

## 11. Learning Analytics

Deterministic SQL views in `core-api` (module `analytics`). No LLM, no writes.

| View | Content |
|---|---|
| `v_topic_mastery` | Per student/topic status, mean, evidence count |
| `v_hypothesis_funnel` | proposed → confirmed/refuted/expired counts |
| `v_intervention_mix` | Counts by action and rule_id over time |
| `v_escalation_stats` | Time to accept, time to resolve, expired count |
| `v_run_outcomes` | Completed (resolved/unresolved), failed, cancelled; actions per run |
| `v_cost_latency` | From `workflow_steps`: tokens, latency by node |

Student-facing progress and admin overview endpoints read these views. Evaluation: view results equal hand-computed values on the seeded fixture.

---

## 12. Cross-component testing strategy

| Level | What | Tooling |
|---|---|---|
| Unit | Policy table, learner invariants, validators, citation verifier, matcher scoring, graders | `pytest`, no network |
| Component | Each agent against `FakeLLM` fixtures including fault modes | `pytest` + fixtures |
| Contract | OpenAPI vs `libs/contracts`, idempotency and ACL tests | `pytest` + `httpx` |
| Workflow | Full runs with `FakeLLM`: clarify path, explain path, check pass, repeated failure → escalate → resume, budget exhaustion, duplicate resume | `pytest` against Compose or in-process |
| Evaluation | Offline metric runs over labeled datasets (`make eval`) | [EVALUATION_PLAN](EVALUATION_PLAN.md) |

Test results are reported by CI/`make` output only once the suites exist; this document asserts none.
