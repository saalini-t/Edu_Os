# EduOS Evaluation Plan

| | |
|---|---|
| **Status** | Design. **No dataset, harness or result exists yet.** No performance numbers are stated or implied anywhere in this document; thresholds are chosen by the team before running and recorded in the report. |
| **Version** | 0.1 (2026-10-09) |
| **Related** | [ARCHITECTURE](ARCHITECTURE.md), [AGENT_SPECIFICATIONS](AGENT_SPECIFICATIONS.md), [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md) |

## 1. Principles

1. **Offline metrics ≠ learning outcomes.** Everything here measures whether the system behaves as designed on a small labeled set. It does not show that students learn better. Learning claims need real students over time (§8).
2. **Pre-register thresholds.** Before any run, the team writes acceptance thresholds into `eval/THRESHOLDS.md` (committed). Results are reported against them; thresholds are not edited after seeing results without a recorded reason.
3. **Reproducible.** One command (`make eval`) regenerates the report from committed fixtures, with fixed seeds, pinned model identifiers and recorded prompt versions.
4. **Small-sample honesty.** Report n, confidence intervals (bootstrap), and per-class counts. Treat results from ~100-item sets as indicative.
5. **Separate dev and held-out splits.** Thresholds (`T_CLARIFY`, `RET_MIN_SIM`, …) are tuned on `dev`; reported on `test`. Test labels are not inspected during tuning.
6. **Synthetic or team-authored data only.** No real student data. Jev receives synthetic data only (D-07).

## 2. Datasets (planned layout)

All under `eval/data/`, JSONL, versioned with a `dataset_version` field. Authors: at least two team members annotate; disagreements adjudicated; inter-annotator agreement (Cohen's κ) recorded.

| File | Contents | Planned size* | Used by |
|---|---|---|---|
| `corpus/` | Seeded Computer Networks material (team-authored or openly licensed), ~5 topics: layering/OSI-TCP-IP, IP addressing & subnetting, TCP (handshake, reliability, flow control, congestion control), routing (distance-vector / link-state), DNS/HTTP | ~15–30 pages | §3.2, §3.3 |
| `doubts.jsonl` | `{id, text, gold_topic, gold_intent, gold_clarity, gold_safety_flag, gold_teacher_request, split}`; includes ambiguous, off-topic, multi-topic, misconception-laden and adversarial doubts | ~100 | §3.1 |
| `retrieval.jsonl` | `{query, relevant_chunk_ids (graded 0/1/2), split}` | ~50 | §3.2 |
| `poison/` | Documents/chunks containing instruction-like text ("ignore previous instructions…", exfiltration attempts, fake citations) | ~20 | §4 |
| `states.jsonl` | Synthetic learner/workflow states (the `PolicyInput` fields) with `gold_action` (and `gold_rule_family`), annotated by two teachers/team members; oversampling ambiguous cases | ~150 | §3.4, §7 |
| `difficulty.jsonl` | `{doubt, gold_difficulty 1–5}` | ~100 | §7 |
| `students.jsonl` | Simulated learner profiles (consistent, slow learner, lucky guesser, forgetful, sloppy) generating attempt sequences | ~10 profiles | §3.8 |
| `teachers.jsonl` + `match_cases.jsonl` | Seeded teachers (topics, proficiency, availability, ratings) and escalation cases with `gold_best_teacher_ids` (top-3 acceptable) | ~10 teachers, ~30 cases | §3.7 |
| `items.jsonl` | Hand-authored gold practice items incl. subnetting arithmetic with solver outputs | ~40 | §3.5, §3.6 |

\*Planned sizes are working estimates for effort planning, not requirements.

## 3. Metrics by component

### 3.1 Doubt classification (`understand`)

| Metric | Definition |
|---|---|
| Topic accuracy / macro-F1 | Predicted `topic_id` vs `gold_topic` (None counts as a class) |
| Intent macro-F1 | 5 classes |
| Clarity precision/recall | Positive class = `ambiguous` (cost of missing ambiguity ≠ cost of over-asking, report both) |
| Safety-flag recall | Share of `gold_safety_flag` doubts flagged (by model OR deterministic patterns); false-positive rate reported separately |
| Confidence calibration | Reliability table of `classification_confidence` vs correctness |
| Schema-validity rate | Share of outputs valid without repair; share needing repair; share falling back |

Baselines: majority class; keyword/TF-IDF topic matcher. With `FakeLLM` these measure the harness, not model quality; real-model numbers require a configured provider (O-1).

### 3.2 Retrieval

Recall@k (k ∈ {3, 6}), MRR, nDCG@k, against graded relevance. Ablations: `dense`, `lexical`, `hybrid` (RRF), and `hybrid + rerank` only if a reranker is added. Also: ACL correctness (§4) and latency p50/p95. Decision rule: add reranking only if hybrid shows a ranking gap that a reranker closes on `dev` by a pre-registered margin.

### 3.3 Citation correctness

| Metric | Method |
|---|---|
| Citation-verification rate | Automatic: share of emitted citations passing the verifier (chunk in retrieved set, quote found) |
| Fabrication rate | Share of citations failing verification before stripping (measures the generator, not the system) |
| Support rate (entailment) | Human, sampled: does the cited chunk actually support the sentence it is attached to? (supported / partially / not) |
| Coverage | Share of factual sentences with ≥1 citation (human sample) |

The verifier proves a quote exists, not that it supports the claim; the human-sampled support rate is the honest measure of that gap.

### 3.4 Explanation quality

Blind pairwise human comparison on ~30 doubts: EduOS (retrieval + citations + learner context) vs a plain-LLM baseline without retrieval. Rubric (each 1–5, anchored descriptions in `eval/rubrics/explanation.md`): technical correctness, groundedness, relevance to the doubt, level-appropriateness for the learner's mastery, clarity. Report win/tie/loss and inter-rater agreement. An LLM judge may be added only after it is calibrated against the human ratings; its agreement is reported and it is never the sole evidence.

### 3.5 Practice generation

Validator pass rate; human spot-check of key correctness and distractor plausibility on a sample; for solver-backed templates, recomputation agreement (should be exact); duplicate rate; share of items whose `source_chunk_ids` support the item.

### 3.6 Assessment correctness

- Exact graders (MCQ/numeric): unit-tested against `items.jsonl` gold answers, including unit/format variants — expected to be exact by construction; any miss is a bug.
- LLM-rubric grader (`short_text`): agreement (Cohen's κ, accuracy) with human grades on a sample; false-accept rate reported separately (accepting a wrong answer is the harmful error).

### 3.7 Teacher matching

Precision@1 and @3 vs `gold_best_teacher_ids`; hard-constraint violations (wrong topic, student-as-teacher, inactive) must be **zero** (a bug if not); component-ablation (drop each score component, report change); stability under tie-breaking.

### 3.8 Learner model and learning progress (simulated)

Using `students.jsonl`:
- **Replay equality:** `rebuild_state` == incremental state (exact).
- **Single-answer safety:** one correct attempt never yields `demonstrated`; one failed attempt never confirms a hypothesis (exact property tests).
- **Convergence:** consistent-learner profile reaches `demonstrated` after sufficient distinct evidence; lucky-guesser profile does not at the same evidence count; forgetful profile decays per half-life.
- **Predictive calibration:** predicted P(correct) from the posterior vs next-attempt correctness on held-out simulated sequences (Brier score vs a constant-prior baseline). Simulated results validate mechanics only.

### 3.9 Intervention selection

| Metric | Definition |
|---|---|
| Agreement | Policy action vs `gold_action` on `states.jsonl` (accuracy, macro-F1, confusion matrix) |
| **Escalation recall** | Share of gold-escalate states where the system escalates (safety-critical; also report precision) |
| Rule coverage | Distribution of fired `rule_id`s; any rule never exercised in tests is flagged |
| Termination | Property test: no simulated run exceeds `MAX_ACTIONS` without escalating or completing |
| Disagreements | Reviewed manually; each is either a policy bug, a label error, or a documented judgment call |

Because the policy is rule-based, gold labels measure whether the *rules encode the intended pedagogy*, and tune thresholds; they do not measure learning.

### 3.10 Latency and reliability

Per-node and end-to-end latency p50/p95 from `workflow_steps` (with `FakeLLM` the numbers measure plumbing only and are labelled so); error, repair and fallback rates; fault-injection suite (§5). No targets asserted before measurement.

## 4. Security evaluation

| Test | Pass criterion |
|---|---|
| Cross-student isolation | For every endpoint taking an ID, a second student's token gets 404/403 and no data |
| Document ACL | Search as student B never returns student A's chunks; after deletion, zero hits for the deleted document |
| Prompt injection corpus (`poison/`) | Generated outputs never follow embedded instructions (rate reported over the corpus), never change policy action, never call tools (none exist), never emit secrets |
| Answer-key leakage | No public response contains `answer_key`/`rubric` |
| Internal endpoint exposure | User JWTs rejected on `/internal/*`; service tokens rejected on `/v1/*` |
| Secret leakage | Grep of logs and API responses for configured secret values finds none |

## 5. Fault injection (reliability acceptance)

Scenarios run against the workflow with `FakeLLM` fault modes and service kill/restart: LLM timeout; invalid JSON; schema violation; `knowledge-svc` down; duplicate `resume`; duplicate student event; worker crash mid-run (heartbeat expiry → retry); resume before checkpoint persisted (reconciler); `core-api` unavailable during grade (retry with same key). Each scenario asserts: terminal or resumable state, no duplicate evidence, audit rows present, and no unbounded retry.

## 6. Threshold tuning procedure

1. Freeze `test` splits; tune only on `dev`.
2. Sweep each threshold (`T_CLARIFY`, `RET_MIN_SIM`, `RET_MIN_CHUNKS`, `T_MASTER`, `N_MIN`, `HALF_LIFE_DAYS`) one at a time on `dev`, plotting the relevant trade-off (e.g., clarification precision/recall; escalation recall vs unnecessary escalations).
3. Choose by the pre-registered criterion in `eval/THRESHOLDS.md` (e.g., "maximize X subject to escalation recall ≥ Y" with X/Y set by the team).
4. Record chosen values and the dev curves; report once on `test`.

## 7. Jev experiment (ADR-001)

**Purpose:** establish whether Jev provides measurable value as an *advisor* over the deterministic rules and over a structured-output LLM. Runs in `shadow` mode with **synthetic data only**; it is not on the critical path.

**Preconditions:** `DecisionProvider` implemented; a Jev API key obtained legitimately; `JEV_MODEL` pinned; privacy review of Jev's retention/training terms *before any non-synthetic data* (not needed for this experiment, required before adoption).

**Questions and datasets**

| Use case | Question | Answer space | Dataset |
|---|---|---|---|
| 1. Intervention | `choice` over {clarify, explain, practice, escalate} | 4 options | `states.jsonl`, restricted to states where R1–R5 do not decide (the only states where an advisor can matter) plus a separate set of R1–R5 states to confirm the advisor is never consulted |
| 5. Route | `score` difficulty 1–5 (or `choice` small/large) | ordered levels | `difficulty.jsonl` |
| 3. Grounding (optional, second) | `noul` "does the explanation address the question" / "are claims supported by the quotes" | yes/no | Pairs of (explanation, quotes) with human labels, including deliberately unsupported explanations |

Use cases 2 and 4 are out of the experiment by decision (kept deterministic).

**Arms**
- A: rules only (`RulesDecisionProvider`; for use case 5 a simple heuristic baseline, e.g., doubt length / keyword count).
- B: structured-output LLM via `LLMDecisionProvider` (requires O-1; if no LLM is configured, arm B is reported as not run and the Jev comparison is against A only).
- C: Jev via `JevDecisionProvider`, pinned model.

**Metrics:** accuracy and macro-F1; **recall on escalation-worthy states** (and false-escalation rate); calibration (reliability table / ECE) of Jev probabilities and LLM confidence; coverage-vs-accuracy curve over confidence thresholds (supports choosing `JEV_CONFIDENCE_MIN`); agreement with rules; latency p50/p95; cost per 1,000 decisions from provider-reported usage (`creditsUsed` / `usage.cost`, recording which billing path was used); invalid/unparseable rate; **repeat-call stability** (same input N times → identical option rate); sensitivity to paraphrase and field-order changes in `state`; performance on non-English or code-mixed doubts is reported but not required (Jev documents English as primary).

**Decision rule (set before running; written into ADR-001 and `eval/THRESHOLDS.md`):** adopt Jev as a vetoable advisor only if, on the held-out ambiguous subset, it meets the team's pre-registered criterion — for example, "at least matches arm B's macro-F1 at lower p95 latency or cost, with escalation recall not worse than arm A and stable repeat calls." The actual margins are the team's to set; none are proposed here. Otherwise: **reject for this build** (default if the time-box is exceeded).

**Safety checks regardless of outcome:** advisor never consulted when R1–R5 fire; non-`ok` provider status always yields the rules decision; shadow mode never alters user-visible behavior (test compares full run outputs with and without the advisor).

**Report contents:** dataset versions, model identifiers, dates, raw confusion matrices, calibration plots, cost and latency tables, and the ADR-001 decision update. Findings are labelled by source (our measurement vs vendor documentation).

## 8. Real learning outcomes (not claimed)

What would be needed, and what we will say instead:
- A controlled pilot with real students, pre/post assessments, a comparison condition, adequate sample, and ethics/consent review. Not feasible in the hackathon window.
- If a small anecdotal pilot (e.g., a handful of volunteers on Computer Networks) is run, it is reported only as qualitative feedback and a pre/post mini-quiz with explicit "no causal claim" language.
- Demo claims are limited to: decisions are traceable, evidence-based mastery behaves as specified in simulations, escalation works end-to-end.

## 9. Demo scenarios (acceptance-linked)

| Scenario | Shows |
|---|---|
| S1: Grounded explanation | Upload notes → doubt → cited explanation; citation panel shows verified quotes |
| S2: Ambiguous doubt | Clarification → refined understanding → explanation |
| S3: No evidence of mastery from one right answer | Correct answer → status `emerging`, not `demonstrated`; hypothesis still `proposed` |
| S4: Repeated failure → teacher | Two failed checks → R5 → escalation → teacher resolves → run resumes → progress updated with teacher-sourced evidence |
| S5: Document deletion | Delete → search no longer returns it; old citations show "source removed" |
| S6: Fault tolerance | Kill `knowledge-svc` mid-demo → graceful R4 behavior → restart → recovers |
| S7: Decision trace | Admin opens run trace: rule fired, inputs, (shadow) advisor proposal, override flag |

## 10. Reporting

`make eval` writes `eval/reports/<timestamp>/report.md` + CSV/JSON: dataset versions, git commit, config snapshot (thresholds, budgets), model/provider identifiers (including `fake` when applicable — runs with `FakeLLM` are stamped "harness-only"), per-section metrics with n and CIs, threshold pass/fail, failure examples. A report is the only source of any number quoted in the demo.
