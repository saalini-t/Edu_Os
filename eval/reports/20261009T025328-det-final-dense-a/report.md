# Retrieval evaluation: det-final-dense-a

- generated: 2026-10-09T02:53:05+00:00  |  git: af1c6fb+worktree  |  mode: `dense`
- dataset: `retrieval.jsonl` (54 questions)  |  corpus chunks: 25
- thresholds used: `{'min_terms': 4, 'min_chunks': 1, 'min_sim': 0.6}`
- embedding: `sentence-transformers/all-MiniLM-L6-v2`

## Split: dev

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 0.9062 | 0.75 to 1.0 |
| hit@3 | 16 | 0.9375 | 0.8125 to 1.0 |
| ndcg@3 | 16 | 0.9266 | 0.7908 to 1.0 |
| recall@6 | 16 | 1.0 | 1.0 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.961 | 0.8864 to 1.0 |
| mrr | 16 | 0.9531 | 0.8594 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 1.0 (CI [1.0, 1.0], n=16)  
FPR unanswerable supported: 0.125 (CI [0.0, 0.375], n=8)  
Youden J: 0.875

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 1.0 |
| out_of_corpus | 4 | 0.0 |
| in_domain_missing | 4 | 0.25 |
| ambiguous | 3 | 0.3333 |

### Workflow level (fake provider, full pipeline)

```json
{
 "answerable_answered_with_citation": {
  "n": 16,
  "mean": 0.9375,
  "ci95": [
   0.8125,
   1.0
  ]
 },
 "answerable_has_relevant_citation": {
  "n": 16,
  "mean": 0.9375,
  "ci95": [
   0.8125,
   1.0
  ]
 },
 "citation_precision_micro": {
  "n_citations": 38,
  "relevant": 28,
  "value": 0.7368
 },
 "unanswerable_answered_with_citation": {
  "n": 8,
  "mean": 0.125,
  "ci95": [
   0.0,
   0.375
  ]
 },
 "unanswerable_first_action": {
  "ASK_CLARIFICATION": 5,
  "ESCALATE_TO_TEACHER": 1,
  "GENERATE_EXPLANATION": 2
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
   "first_relevant_rank": 4
  }
 ],
 "unanswerable_falsely_supported": [
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
  }
 ]
}
```

## Split: test

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 1.0 | 1.0 to 1.0 |
| hit@3 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@3 | 16 | 0.9226 | 0.837 to 1.0 |
| recall@6 | 16 | 1.0 | 1.0 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.9226 | 0.837 to 1.0 |
| mrr | 16 | 0.8958 | 0.7812 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 1.0 (CI [1.0, 1.0], n=16)  
FPR unanswerable supported: 0.0 (CI [0.0, 0.0], n=8)  
Youden J: 1.0

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 1.0 |
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
  "mean": 0.8125,
  "ci95": [
   0.625,
   1.0
  ]
 },
 "citation_precision_micro": {
  "n_citations": 35,
  "relevant": 23,
  "value": 0.6571
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
 "answerable_missed": [],
 "answerable_relevant_not_first": [
  {
   "id": "q04",
   "question": "What does ssthresh do in TCP?",
   "first_relevant_rank": 3
  },
  {
   "id": "q06",
   "question": "What happens to the congestion window after a retransmission timeout?",
   "first_relevant_rank": 2
  },
  {
   "id": "q14",
   "question": "How does a router choose among several matching forwarding table entries?",
   "first_relevant_rank": 2
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
