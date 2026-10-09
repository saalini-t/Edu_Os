# Retrieval evaluation: hybrid-dev-sweep

- generated: 2026-10-09T01:53:51+00:00  |  git: af1c6fb+worktree  |  mode: `hybrid`
- dataset: `retrieval.jsonl` (54 questions)  |  corpus chunks: 25
- thresholds used: `{'min_terms': 2, 'min_chunks': 1, 'min_sim': None}`
- embedding: `sentence-transformers/all-MiniLM-L6-v2`

## Split: dev

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 0.9688 | 0.9062 to 1.0 |
| hit@3 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@3 | 16 | 0.9239 | 0.841 to 1.0 |
| recall@6 | 16 | 0.9688 | 0.9062 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.9239 | 0.841 to 1.0 |
| mrr | 16 | 0.9062 | 0.8125 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 1.0 (CI [1.0, 1.0], n=16)  
FPR unanswerable supported: 0.625 (CI [0.25, 0.875], n=8)  
Youden J: 0.375

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 1.0 |
| out_of_corpus | 4 | 0.25 |
| in_domain_missing | 4 | 1.0 |
| ambiguous | 3 | 0.3333 |

### Workflow level (fake provider, full pipeline)

```json
{
 "answerable_answered_with_citation": {
  "n": 16,
  "mean": 0.875,
  "ci95": [
   0.6875,
   1.0
  ]
 },
 "answerable_has_relevant_citation": {
  "n": 16,
  "mean": 0.875,
  "ci95": [
   0.6875,
   1.0
  ]
 },
 "citation_precision_micro": {
  "n_citations": 37,
  "relevant": 26,
  "value": 0.7027
 },
 "unanswerable_answered_with_citation": {
  "n": 8,
  "mean": 0.25,
  "ci95": [
   0.0,
   0.625
  ]
 },
 "unanswerable_first_action": {
  "ASK_CLARIFICATION": 5,
  "GENERATE_EXPLANATION": 3
 },
 "ambiguous_asked_clarification": {
  "n": 3,
  "mean": 1.0,
  "ci95": [
   1.0,
   1.0
  ]
 },
 "all_emitted_citations_verified": true
}
```

### Failure cases

```json
{
 "answerable_missed": [],
 "answerable_relevant_not_first": [
  {
   "id": "q03",
   "question": "Why does slow start double the window each round trip?",
   "first_relevant_rank": 2
  },
  {
   "id": "q05",
   "question": "When the sender sees three repeated ACKs for the same data, what does Reno do?",
   "first_relevant_rank": 2
  },
  {
   "id": "q13",
   "question": "How many subnets do you get by dividing a /24 network into /26 blocks?",
   "first_relevant_rank": 2
  }
 ],
 "unanswerable_falsely_supported": [
  {
   "id": "o07",
   "category": "out_of_corpus",
   "question": "How does the Linux scheduler pick the next process to run?",
   "top_matched_terms": [
    2,
    1,
    1
   ]
  },
  {
   "id": "m01",
   "category": "in_domain_missing",
   "question": "How does QUIC achieve zero round trip connection setup?",
   "top_matched_terms": [
    3,
    3,
    2
   ]
  },
  {
   "id": "m03",
   "category": "in_domain_missing",
   "question": "What is the difference between IPv4 and IPv6 header formats?",
   "top_matched_terms": [
    2,
    2,
    1
   ]
  },
  {
   "id": "m05",
   "category": "in_domain_missing",
   "question": "How does OSPF elect a designated router?",
   "top_matched_terms": [
    2,
    1,
    1
   ]
  },
  {
   "id": "m07",
   "category": "in_domain_missing",
   "question": "What is TCP BBR congestion control?",
   "top_matched_terms": [
    3,
    3,
    3
   ]
  }
 ],
 "answerable_not_answered_by_workflow": [
  {
   "id": "q13",
   "action": "ASK_CLARIFICATION",
   "rules": [
    "R3_needs_clarification"
   ],
   "fallback": null
  },
  {
   "id": "q19",
   "action": "ESCALATE_TO_TEACHER",
   "rules": [
    "R4_no_grounding"
   ],
   "fallback": null
  }
 ]
}
```

## Dev threshold sweep (pre-registered grid; dev split only)

Selected by max J, conservative tie-break: **{'min_terms': 4, 'min_chunks': 1, 'min_sim': 0.6, 'J': 0.875, 'tpr': 1.0, 'fpr': 0.125}**

| min_terms | min_chunks | min_sim | J | TPR | FPR |
|---|---|---|---|---|---|
| 4 | 1 | None | 0.875 | 0.875 | 0.000 |
| 4 | 1 | 0.6 | 0.875 | 1.000 | 0.125 |
| 3 | 1 | None | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.45 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.45 | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.5 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.5 | 0.750 | 1.000 | 0.250 |
| 4 | 1 | 0.55 | 0.750 | 1.000 | 0.250 |
| 3 | 1 | 0.6 | 0.750 | 1.000 | 0.250 |
| 5 | 1 | 0.55 | 0.688 | 0.938 | 0.250 |
| 5 | 1 | 0.6 | 0.688 | 0.812 | 0.125 |
| 5 | 1 | None | 0.625 | 0.625 | 0.000 |
| 4 | 2 | 0.35 | 0.625 | 0.875 | 0.250 |
| 5 | 2 | 0.35 | 0.625 | 0.875 | 0.250 |
| 4 | 1 | 0.4 | 0.625 | 1.000 | 0.375 |

## Limitations

- Small corpus (tens of chunks); many candidates are trivially separable and metrics saturate easily.
- Labels written by a single author, not double-annotated; evidence phrases favour lexical overlap with the questions.
- Dev/test samples are small (see n); confidence intervals are wide, differences smaller than the CI are not evidence of improvement.
- Workflow-level numbers use the deterministic FAKE provider, so they measure retrieval, policy and citation plumbing, not LLM quality.
