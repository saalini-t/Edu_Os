# Retrieval evaluation: det-final-fts-a

- generated: 2026-10-09T02:52:15+00:00  |  git: af1c6fb+worktree  |  mode: `fts`
- dataset: `retrieval.jsonl` (54 questions)  |  corpus chunks: 25
- thresholds used: `{'min_terms': 4, 'min_chunks': 1, 'min_sim': None}`
- embedding: `None`

## Split: dev

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 0.9688 | 0.9062 to 1.0 |
| hit@3 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@3 | 16 | 0.9388 | 0.8477 to 1.0 |
| recall@6 | 16 | 0.9688 | 0.9062 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.9388 | 0.8477 to 1.0 |
| mrr | 16 | 0.9271 | 0.8229 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 0.875 (CI [0.6875, 1.0], n=16)  
FPR unanswerable supported: 0.0 (CI [0.0, 0.0], n=8)  
Youden J: 0.875

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 0.875 |
| out_of_corpus | 4 | 0.0 |
| in_domain_missing | 4 | 0.0 |
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
  "n_citations": 38,
  "relevant": 27,
  "value": 0.7105
 },
 "unanswerable_answered_with_citation": {
  "n": 8,
  "mean": 0.0,
  "ci95": [
   0.0,
   0.0
  ]
 },
 "unanswerable_first_action": {
  "ASK_CLARIFICATION": 5,
  "ESCALATE_TO_TEACHER": 3
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
 "answerable_missed": [
  {
   "id": "q01",
   "question": "What are the three steps of the TCP handshake?",
   "reason": "not_supported_by_threshold"
  },
  {
   "id": "q13",
   "question": "How many subnets do you get by dividing a /24 network into /26 blocks?",
   "reason": "not_supported_by_threshold"
  }
 ],
 "answerable_relevant_not_first": [
  {
   "id": "q05",
   "question": "When the sender sees three repeated ACKs for the same data, what does Reno do?",
   "first_relevant_rank": 2
  },
  {
   "id": "q13",
   "question": "How many subnets do you get by dividing a /24 network into /26 blocks?",
   "first_relevant_rank": 3
  }
 ],
 "unanswerable_falsely_supported": [],
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

## Split: test

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 0.9375 | 0.8125 to 1.0 |
| hit@3 | 16 | 0.9375 | 0.8125 to 1.0 |
| ndcg@3 | 16 | 0.9062 | 0.75 to 1.0 |
| recall@6 | 16 | 1.0 | 1.0 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.9332 | 0.8308 to 1.0 |
| mrr | 16 | 0.9115 | 0.776 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 0.875 (CI [0.6875, 1.0], n=16)  
FPR unanswerable supported: 0.0 (CI [0.0, 0.0], n=8)  
Youden J: 0.875

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 0.875 |
| out_of_corpus | 4 | 0.0 |
| in_domain_missing | 4 | 0.0 |
| ambiguous | 3 | 0.6667 |

### Workflow level (fake provider, full pipeline)

```json
{
 "answerable_answered_with_citation": {
  "n": 16,
  "mean": 0.8125,
  "ci95": [
   0.625,
   1.0
  ]
 },
 "answerable_has_relevant_citation": {
  "n": 16,
  "mean": 0.75,
  "ci95": [
   0.5,
   0.9375
  ]
 },
 "citation_precision_micro": {
  "n_citations": 34,
  "relevant": 20,
  "value": 0.5882
 },
 "unanswerable_answered_with_citation": {
  "n": 8,
  "mean": 0.0,
  "ci95": [
   0.0,
   0.0
  ]
 },
 "unanswerable_first_action": {
  "ASK_CLARIFICATION": 7,
  "ESCALATE_TO_TEACHER": 1
 },
 "ambiguous_asked_clarification": {
  "n": 3,
  "mean": 0.6667,
  "ci95": [
   0.0,
   1.0
  ]
 },
 "all_emitted_citations_verified": true
}
```

### Failure cases

```json
{
 "answerable_missed": [
  {
   "id": "q06",
   "question": "What happens to the congestion window after a retransmission timeout?",
   "reason": "not_supported_by_threshold"
  },
  {
   "id": "q12",
   "question": "How many usable hosts does a /26 subnet have?",
   "reason": "not_supported_by_threshold"
  }
 ],
 "answerable_relevant_not_first": [
  {
   "id": "q06",
   "question": "What happens to the congestion window after a retransmission timeout?",
   "first_relevant_rank": 4
  },
  {
   "id": "q32",
   "question": "Why does TCP retransmit a segment?",
   "first_relevant_rank": 3
  }
 ],
 "unanswerable_falsely_supported": [],
 "answerable_not_answered_by_workflow": [
  {
   "id": "q02",
   "action": "ASK_CLARIFICATION",
   "rules": [
    "R3_needs_clarification"
   ],
   "fallback": null
  },
  {
   "id": "q22",
   "action": "ESCALATE_TO_TEACHER",
   "rules": [
    "R4_no_grounding"
   ],
   "fallback": null
  },
  {
   "id": "q32",
   "action": "ASK_CLARIFICATION",
   "rules": [
    "R3_needs_clarification"
   ],
   "fallback": null
  }
 ]
}
```

## Limitations

- Small corpus (tens of chunks); many candidates are trivially separable and metrics saturate easily.
- Labels written by a single author, not double-annotated; evidence phrases favour lexical overlap with the questions.
- Dev/test samples are small (see n); confidence intervals are wide, differences smaller than the CI are not evidence of improvement.
- Workflow-level numbers use the deterministic FAKE provider, so they measure retrieval, policy and citation plumbing, not LLM quality.
