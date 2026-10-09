# Retrieval evaluation: baseline-fts-untuned-defaults

- generated: 2026-10-09T01:44:24+00:00  |  git: af1c6fb+worktree  |  mode: `fts`
- dataset: `retrieval.jsonl` (54 questions)  |  corpus chunks: 25
- thresholds used: `{'min_terms': 2, 'min_chunks': 1, 'min_sim': None}`
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
  "mean": 0.5625,
  "ci95": [
   0.3125,
   0.8125
  ]
 },
 "answerable_has_relevant_citation": {
  "n": 16,
  "mean": 0.5625,
  "ci95": [
   0.3125,
   0.8125
  ]
 },
 "citation_precision_micro": {
  "n_citations": 25,
  "relevant": 17,
  "value": 0.68
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
 ]
}
```

## Split: test

### Ranking (answerable questions)

| metric | n | mean | 95% CI |
|---|---|---|---|
| recall@3 | 16 | 0.875 | 0.6875 to 1.0 |
| hit@3 | 16 | 0.875 | 0.6875 to 1.0 |
| ndcg@3 | 16 | 0.875 | 0.6875 to 1.0 |
| recall@6 | 16 | 1.0 | 1.0 to 1.0 |
| hit@6 | 16 | 1.0 | 1.0 to 1.0 |
| ndcg@6 | 16 | 0.9261 | 0.8139 to 1.0 |
| mrr | 16 | 0.9031 | 0.7562 to 1.0 |

### Support / insufficient-context (retrieval level)

TPR answerable supported: 1.0 (CI [1.0, 1.0], n=16)  
FPR unanswerable supported: 0.375 (CI [0.125, 0.75], n=8)  
Youden J: 0.625

| category | n | supported rate |
|---|---|---|
| answerable | 16 | 1.0 |
| out_of_corpus | 4 | 0.0 |
| in_domain_missing | 4 | 0.75 |
| ambiguous | 3 | 0.6667 |

### Workflow level (fake provider, full pipeline)

```json
{
 "answerable_answered_with_citation": {
  "n": 16,
  "mean": 0.625,
  "ci95": [
   0.375,
   0.875
  ]
 },
 "answerable_has_relevant_citation": {
  "n": 16,
  "mean": 0.5625,
  "ci95": [
   0.3125,
   0.8125
  ]
 },
 "citation_precision_micro": {
  "n_citations": 27,
  "relevant": 13,
  "value": 0.4815
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
  "GENERATE_EXPLANATION": 1
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
   "id": "q06",
   "question": "What happens to the congestion window after a retransmission timeout?",
   "first_relevant_rank": 4
  },
  {
   "id": "q32",
   "question": "Why does TCP retransmit a segment?",
   "first_relevant_rank": 5
  }
 ],
 "unanswerable_falsely_supported": [
  {
   "id": "m02",
   "category": "in_domain_missing",
   "question": "How does the TCP CUBIC window growth function work?",
   "top_matched_terms": [
    3,
    3,
    2
   ]
  },
  {
   "id": "m04",
   "category": "in_domain_missing",
   "question": "How many bits are in an IPv6 address?",
   "top_matched_terms": [
    2,
    2,
    2
   ]
  },
  {
   "id": "m08",
   "category": "in_domain_missing",
   "question": "What is the Spanning Tree Protocol used for?",
   "top_matched_terms": [
    2,
    2,
    2
   ]
  }
 ]
}
```

## Dev threshold sweep (pre-registered grid; dev split only)

Selected by max J, conservative tie-break: **{'min_terms': 4, 'min_chunks': 1, 'min_sim': None, 'J': 0.875, 'tpr': 0.875, 'fpr': 0.0}**

| min_terms | min_chunks | min_sim | J | TPR | FPR |
|---|---|---|---|---|---|
| 4 | 1 | None | 0.875 | 0.875 | 0.000 |
| 3 | 1 | None | 0.750 | 1.000 | 0.250 |
| 5 | 1 | None | 0.625 | 0.625 | 0.000 |
| 2 | 3 | None | 0.500 | 0.750 | 0.250 |
| 2 | 1 | None | 0.375 | 1.000 | 0.625 |
| 2 | 2 | None | 0.375 | 0.750 | 0.375 |
| 1 | 1 | None | 0.250 | 1.000 | 0.750 |
| 1 | 2 | None | 0.250 | 0.875 | 0.625 |
| 1 | 3 | None | 0.250 | 0.875 | 0.625 |
| 3 | 2 | None | 0.250 | 0.500 | 0.250 |
| 4 | 2 | None | 0.188 | 0.188 | 0.000 |
| 5 | 2 | None | 0.125 | 0.125 | 0.000 |
| 3 | 3 | None | 0.062 | 0.188 | 0.125 |
| 4 | 3 | None | 0.062 | 0.062 | 0.000 |
| 5 | 3 | None | 0.000 | 0.000 | 0.000 |

## Limitations

- Small corpus (tens of chunks); many candidates are trivially separable and metrics saturate easily.
- Labels written by a single author, not double-annotated; evidence phrases favour lexical overlap with the questions.
- Dev/test samples are small (see n); confidence intervals are wide, differences smaller than the CI are not evidence of improvement.
- Workflow-level numbers use the deterministic FAKE provider, so they measure retrieval, policy and citation plumbing, not LLM quality.
