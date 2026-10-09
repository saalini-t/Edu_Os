# Phase H2 baseline: large-PDF ingestion, measured before anything is changed

Date: 2026-10-09. **Nothing in the application was changed in this step** (no limits, chunking, embedding, OCR or ingestion code). All numbers come from runs made in an isolated Docker project (`eduos_h2`, own database volume, own port 18000). The development stack and its demo history were not touched: afterwards the dev database still held 1 document and 4 doubt sessions, and Git showed the same 39 uncommitted entries as before this step.

Measured results are marked **measured**. Anything extrapolated is marked **estimate**. Most figures are single runs (n = 1); where a case was run twice the spread was about ±20 % (section 3).

## 1. Environment and fixtures

| Item | Value |
|---|---|
| Host | Windows 11 Home, Intel Core i5-13450HX (10 cores / 16 threads), 16.9 GB RAM |
| Docker | Engine 29.1.3, Compose 5.0.1; Docker VM 8 CPUs, 8.3 GB RAM |
| Container limits | none (`HostConfig.Memory = 0`, `NanoCpus = 0` on worker and database) |
| Image | `edu_os-backend` (2.8 GB): Python 3.12, torch CPU, sentence-transformers, RapidOCR, pypdfium2 |
| Stack | PostgreSQL 16 + pgvector, Redis 7, API, one worker (all recreated in the isolated project) |
| Settings (mirror the dev `.env`) | `CHUNK_WORDS=120`, overlap 20, `EMBEDDING_BATCH_SIZE=16`, MiniLM-L6-v2 (384-d), `RET_MIN_SIM=0.6`, `OCR_ENGINE=rapidocr`, `OCR_MAX_PAGES=20`, `PARSER_TIMEOUT_S=120`, `JOB_LEASE_SECONDS=120`, `MAX_PDF_PAGES=200`, `MAX_UPLOAD_BYTES=10 MB`, `PARSER_ISOLATION=subprocess` |
| LLM | not involved (retrieval and ingestion only) |

**Fixtures (real, supplied by you; kept in the scratchpad, not in the repository, not committed).** Two full textbooks from your Downloads folder:

| Book | Size | Pages | Text layer |
|---|---|---|---|
| *Computer Networks, 5th ed.* (Tanenbaum) | 8.57 MB | 962 | digital |
| *Data Communications and Networking, 5e* (file name) | 66.94 MB | 1,269 | digital, many figures |

Neither is a 200-page, 30 MB document. I therefore **cut real page ranges out of them** (these are excerpts of real textbook pages, not synthetic text):

| Excerpt | Pages | Size |
|---|---|---|
| **A** = Tanenbaum pp. 40–239 | 200 | 2.51 MB |
| **B** = Data Communications pp. 100–299 | 200 | 6.54 MB |
| F = Data Communications pp. 300–499 | 200 | 7.94 MB |
| smaller ranges (40–100 pages) | | used only for failure cases |
| scan5 / scan25 | 5 / 25 | **synthetic scanned-like**: image-only renderings of real Tanenbaum pages 60–64 / 60–84 at 150 dpi (no text layer), made inside the container |

Not available and therefore **not measured**: a real 200-page, ~30 MB single document. Note that a real **100-page** slice of the second book (pp. 500–599) is already **15.6 MB**, so a 200-page image-heavy slice is plausibly ~30 MB (**estimate**).

## 2. Current ingestion architecture (from the code)

`POST /v1/documents` (`knowledge/router.py`) → reads at most `MAX_UPLOAD_BYTES + 1` bytes of the spooled upload, checks the `%PDF-` header and SHA-256 duplicates (`Ingestion.register_upload`) → writes the file, creates `Document` (QUEUED) and a durable `IngestionJob` → wakes the worker through Redis (or the worker polls). The worker (`app/worker.py`) claims the job (`FOR UPDATE SKIP LOCKED`, lease 120 s), then `process_job`:

1. `parse_document` runs `app.knowledge.parse_child` in a **child process** (timeout `PARSER_TIMEOUT_S`, optional `RLIMIT_AS`). The whole file is read into the worker and piped to the child. Page count, encryption and per-page text/OCR happen here, so **page and encryption limits are only discovered after the upload was accepted**.
2. `chunk_pages` (sentence-greedy, 120 words, 20-word overlap, never across pages).
3. One transaction: delete old chunks, insert new chunks, set `READY`, finish the job.
4. **After** that commit, `KnowledgeService._embed_document` runs **inline in the same worker call**: MiniLM batches of 16 with a commit per batch, `ON CONFLICT DO NOTHING`. Failures are swallowed (a log line) and the document stays `READY`.

Nothing re-embeds chunks that missed their vectors, except `python -m app.knowledge.reindex`, which the backend runs only at its own start.

## 3. Measured results

**End to end through the real API and worker (OCR on, `RET_MIN_SIM=0.6`):**

| | A (digital, 200 pp, 2.5 MB) | B (figure-heavy, 200 pp, 6.5 MB) |
|---|---|---|
| Upload response | 0.19 s (HTTP 202) | 0.14 s (202) |
| Queue delay (first PROCESSING seen) | ≤ 0.24 s | ≤ 0.18 s |
| Parsing | 6.4 s | 29.6 s |
| Chunking + insert + READY | 0.3 s | 0.3 s |
| **READY at** | **6.9 s** | **30.1 s** |
| Embeddings complete after READY | +27.9 s | +16.6 s |
| **Total to fully embedded** | **34.8 s** | **46.7 s** |
| Peak CPU / memory (container) | worker 375 % / 489 MB; API 430 MB; DB 75 MB | worker 361 % / 673 MB; API 418 MB; DB 76 MB |

Repeat runs with OCR off (first pass): A READY 6.5 s (total 33.4 s); B READY 23.5 s (total 39.0 s), so run-to-run and configuration spread is roughly ±20 %. Page-level parse speed is **~29 pages/s for A and ~7 pages/s for B**: figures dominate.

**Per-stage decomposition** (the application's own functions run inside the worker container; OCR off; throw-away rows removed afterwards):

| Stage | A | B |
|---|---|---|
| Extracted text | 512,986 chars / 87,525 words | 385,253 chars / 67,441 words |
| Pages with < 20 non-space chars | 0 | 3 (pp. 146, 176, 196; OCR found no text on them) |
| Chunks | 946 | 741 |
| Chunk words: min / p5 / median / p95 / max / mean | 3 / 40 / 108 / 119 / 161 / 99.7 | 8 / 43 / 108 / 120 / 171 / 98.4 |
| Chunks < 20 words / < 40 words / empty | 10 / 45 / 0 | 10 / 34 / 0 |
| Pages that produced no chunk | 0 | 3 |
| Chunking time | 0.05 s | 0.03 s |
| DB insert of chunks (incl. full-text vectors) | 0.28 s | 0.41 s |
| Embedding model load + first query (once per worker process) | 9.4 s | 6.6 s |
| Embedding forward pass (batch 16) | 19.0 s = 49.7 chunks/s; batch mean 0.317 s, p95 0.51 s, max 0.61 s | 15.7 s = 47.1 chunks/s; mean 0.335 s, p95 0.51 s |
| DB insert of embeddings (one INSERT per chunk) | 1.66 s | 1.55 s |
| Embedding failures | none | none |
| Peak RSS: worker process / parser child | 704 MB / 80 MB | 700 MB / 102 MB |

So the pipeline time is dominated by **parsing figure-heavy pages** and by the **embedding forward pass**; chunking and database work are each ≤ 2 s.

**OCR (synthetic scanned-like fixtures, RapidOCR on CPU):**

| Fixture | Result |
|---|---|
| scan5 (5 pages, 1.26 MB) | READY at 31.3 s; 5 pages OCR'd; 8 chunks; worker peak 832 MB / 675 % CPU |
| scan25 (25 pages, 5.65 MB) | READY at 109.9 s; **20 pages OCR'd, pp. 21–25 skipped as `ocr_budget_exceeded`**; 31 chunks; worker peak 907 MB |

About **5.5 s per OCR page** (**measured**). A full 200-page scan would need roughly 200 × 5.5 ≈ **1,100 s ≈ 18 min** (**estimate**, if the budget and timeout were lifted). Today, 20 OCR pages already take 110 s against a 120 s timeout. OCR text accuracy was **not evaluated**.

**Retrieval and citation smoke test** (mixed corpus: A, B and the 6-page demo notes; hybrid retrieval, top 6, 16 questions I wrote after checking which terms occur in the excerpts, gold = a phrase present in a returned chunk; page reference = the returned chunk's page, checked against an independent per-page text extraction):

| Metric | Result |
|---|---|
| Questions with an answer in the corpus | 15 |
| Relevant chunk in top 6 | **15 / 15** (rank 1: 13, rank 3: 1 [CRC], rank 4: 1 [misspelled "cheksum"]) |
| Returned page's own text contains the gold phrase | **15 / 15** |
| Workflow sufficiency gate (`n_above_threshold ≥ 1`) | **14 / 15** with `RET_MIN_SIM=0.6`; only the misspelled query failed. Without the similarity threshold: 11 / 15 (bit stuffing, Manchester, misspelling and a two-concept question failed on term-count alone) |
| Out-of-domain question ("capital of France") | 6 results returned but supported = 0 (correct abstention) |
| Latency, top 6 | median 0.07 s (0.04–0.12 s) |

Caveats: 16 questions, written by me, gold is lexical, not human relevance judgements; it shows the pipeline retrieves and cites correct pages, **not** retrieval quality in general. Page numbers are PDF page indexes of the excerpt (1–200), not printed book page numbers.

## 4. Failure and recovery (isolated stack)

| Case | Outcome | State afterwards |
|---|---|---|
| Not a PDF | 415 `UNSUPPORTED_MEDIA_TYPE` at upload (0.01 s) | nothing stored |
| `%PDF-` header + random bytes | 202, then `FAILED UNREADABLE_PDF` in 0.6 s | no chunks, job FAILED (attempts 1/3, not retried: deterministic) |
| Encrypted PDF | 202, then `FAILED ENCRYPTED_PDF` in 0.6 s | consistent |
| 11 MB file | 413 `FILE_TOO_LARGE` in 0.08 s | nothing stored |
| Real 15.6 MB, 100-page slice | **413** | nothing stored |
| Real full book, 66.9 MB | 413 in 0.32 s (local loopback; /tmp 1 MB afterwards) | nothing stored |
| Real full book, 962 pages (8.6 MB) | 202, then `FAILED TOO_MANY_PAGES` after 1.1 s | consistent |
| Parser timeout (2 s set) | `FAILED PARSER_TIMEOUT` at 2.1 s, no retry | consistent |
| Parser memory limit | Limit enforced: 64 / 40 MB passed for a 20-page text PDF; 32 MB → `FAILED PARSER_CRASHED` (the same condition can be reported as `PARSER_RESOURCE_LIMIT` depending on where the allocation fails) | consistent |
| OCR disabled, image-only PDF | `FAILED NO_EXTRACTABLE_TEXT` with a message naming `OCR_ENGINE` | consistent |
| OCR budget exceeded (25-page scan) | READY; pages 21–25 listed in the extraction report | consistent, partial text |
| Embedding model unavailable at the worker | **Document `READY` with 0 / 201 vectors; never backfilled** (checked again after > 5 minutes) | consistent in the database but silently degraded |
| Worker container restarted during embedding (my test harness did this to one document) | **READY with 208 / 280 vectors, never completed** | partial vectors |
| Worker killed mid-parse (`docker kill`) | Job stayed PROCESSING for the lease length, re-queued after ~116 s, attempt 2 succeeded: READY at 153 s | 798 chunks, 798 distinct indexes, 798 vectors: no duplicates |
| Same bytes uploaded again | 200, same document, no second job | 1 document, 1 job |
| Reindex while searching | 19 searches during 6.9 s, **0 empty results**; all 946 chunk ids replaced; version 1→2; 946 vectors; 0 orphan embeddings; 0 chunks of an old version | consistent |
| Two uploads back to back | second job started 10.1 s after the first was READY (waiting behind the first job's inline embedding) | consistent |

## 5. Findings (each with its evidence)

1. **The 10 MB upload limit rejects real textbook slices.** A 100-page slice of a real book is 15.6 MB; the full books are 8.6 MB / 66.9 MB. (Section 4.)
2. **The 200-page limit rejects whole textbooks, and only after the upload is accepted.** The 962-page book is accepted with 202 and fails ~1 s later in the worker.
3. **Embedding is not durable.** It runs inline after `READY`, errors are swallowed, and nothing resumes or backfills it: one document has 0 %, another 74 % of its vectors (section 4). Dense retrieval silently covers only part of the corpus; only `/admin/system` shows `missing_embeddings`.
4. **Embedding blocks the worker.** The next job waited ~10 s behind the previous job's embedding. At 50 chunks/s a 2,000-chunk book blocks it for ~40 s per document (**estimate**).
5. **Parsing time depends on figures, not pages**: 29 vs 7 pages/s. A heavier or longer book will approach `PARSER_TIMEOUT_S=120` (B took 30 s for 200 pages; a 1,269-page version of B is **estimated** at ~190 s).
6. **OCR is slow and tightly coupled to the timeout**: ~5.5 s/page; a budget of 20 pages already consumes ~110 s of the 120 s timeout.
7. **Crash recovery works but is slow**: lease length (120 s) is the recovery delay; the lease is not extended while a long stage runs.
8. **No ingestion progress is visible to a user** and there is **no upload UI** (only the admin job list); the document status offers `QUEUED/PROCESSING/READY/FAILED` and a coarse job stage.
9. **Chunk quality is acceptable but not tuned**: ~1 % of chunks have < 20 words and the longest have 161–171 words (single long sentences). Running headers are merged into the first sentence (see the earlier audit).
10. **The retrieval sufficiency gate depends heavily on the similarity threshold** (11/15 vs 14/15 answerable questions pass); misspellings are not handled.
11. **Upload body handling**: the API reads at most limit+1 bytes into Python memory (the multipart body is spooled by the framework), but the whole request body is received before the size check (no early `Content-Length` rejection). A 66.9 MB request was received and rejected in 0.32 s locally. Peak memory during the upload was **not captured reliably** (one sample).

## 6. Current limits and where they come from

| Limit | Value | Source |
|---|---|---|
| Upload size | 10 MB | `Settings.max_upload_bytes` (`config.py`) |
| Pages | 200 | `Settings.max_pdf_pages`; enforced in `extract_document` (worker) |
| Parser wall time | 120 s | `parser_timeout_s` |
| Parser memory | off (0); `RLIMIT_AS` when set, POSIX only | `parser_memory_mb`, `parse_child._limit_memory` |
| OCR pages / DPI / pixels | 20 / 150 / 6 MP | `ocr_max_pages`, `ocr_dpi`, `ocr_max_pixels` |
| Job lease / attempts / backoff | 120 s / 3 / 5 s × 2ⁿ⁻¹ | `job_lease_seconds`, `job_max_attempts`, `job_retry_backoff_seconds` |
| Chunking | 120 words, 20 overlap, per page | `chunk_words`, `chunk_overlap_words`, `knowledge/chunker.py` |
| Embedding batch | 16 | `embedding_batch_size` |
| Container resources | unlimited | `docker-compose.yml` |

## 7. Recommended limits (proposals; **not applied**; each needs your approval)

| Setting | Proposal | Evidence / reasoning |
|---|---|---|
| Upload size | 50 MB default, rejected on `Content-Length` before the body is read | a real 100-page slice is 15.6 MB, so 200 pages of figure-heavy content is ~31 MB (estimate); the 66.9 MB full book would still be refused |
| Page budget | keep 200 for the demo; decide a higher value only together with a per-page time budget | whole books are 962–1,269 pages; the time budget (finding 5) must scale first |
| Check order | open the PDF header and page count at upload and answer 422 immediately | today the same answer arrives asynchronously after a 202 |
| Parser timeout | scale with pages: about 30 s + 1 s/page for digital text (A took 6.4 s, B 30 s for 200 pages, so 230 s is ~7× margin), OCR counted separately | fixed 120 s is only ~4× margin for B |
| OCR | treat as its own bounded stage with a per-page timeout and a page budget chosen from the time budget (about 12 pages per 120 s at 5.5 s/page, or a longer stage timeout) | measured 5.5 s/page |
| Lease | heartbeat every ~15 s during long stages and a shorter base lease (30–45 s) | a killed worker currently blocks a job for the whole 120 s |
| Embedding | separate durable stage/job with progress, batch-level resume, a sweep that backfills missing vectors, and a model warm-up at worker start | findings 3, 4; model load costs 6–9 s per worker start |
| Embedding batch | keep 16 until a sweep (16/32/64) is measured | only batch 16 was measured |
| Container limits | worker 2 GB memory limit and a CPU cap so torch (375–675 % CPU) cannot starve the API | observed worker peaks 489–907 MB |

## 8. Proposed plan for the next H2 step (not started; waiting for review)

1. **Cheap early rejection**: `Content-Length` check and page-count/encryption precheck at upload; keep the worker checks as defence in depth. *Acceptance:* 66.9 MB and 962-page files answer 4xx immediately with a clear code; no body read beyond the limit; existing tests still pass; a new test for each.
2. **Durable embedding stage**: new job kind or stage, resumable by chunk, backfill sweep, progress counters, worker warm-up. *Acceptance:* kill the worker during embedding and observe 100 % of vectors afterwards without manual action; a failed provider recovers on its own; the next job is not blocked by a previous document's embedding (or the queue is documented as serial).
3. **Heartbeat lease and scaled timeouts**. *Acceptance:* a killed worker's job resumes in ≤ 45 s; a 200-page figure-heavy document no longer risks the timeout.
4. **Progress and upload UI**: pages parsed / OCR pages / chunks / embedded %, error text, retry button, upload form for students and admins. *Acceptance:* browser test of a real 200-page upload.
5. **Re-run this harness** (kept in the scratchpad: `compose.h2.yml`, `h2_measure.py`, `stage_probe.py`, `h2_fail.py`, `h2_kill.py`, `h2_ask.py`) against the changed code and compare every number in section 3.

Only after steps 1 to 4: decide on a real 30 MB, 200-page fixture and the structure-aware chunking experiments from the audit.

## 9. Not run / limitations

- A real 200-page, ~30 MB document (none available); the nearest real cases are 200 pp / 7.9 MB and 100 pp / 15.6 MB (the latter refused by the limit).
- Full-book ingestion (962 / 1,269 pages) beyond the page limit; OCR of a 200-page scan; OCR text accuracy.
- Peak memory during a 60 MB+ upload (one sample only).
- The transient-error retry path in Docker (covered by `tests/test_async_ingestion.py`, not re-run in Docker); multiple workers contending for jobs; a partially corrupt PDF with a mix of good and bad pages.
- Embedding batch-size sweep; HNSW / query-plan behaviour at scale (the index is not used at ~3,000 chunks and was not inspected).
- CPU/memory sampling is `docker stats` at ~2 s intervals (container-level, coarse); short peaks can be missed.
- All numbers are from one Windows laptop under Docker Desktop (WSL2 VM); a Linux server will differ.
- The retrieval smoke test is tiny and self-authored; it is not an evaluation.
- The isolated stack was stopped (not deleted) afterwards; its compose file and scripts remain in the session scratchpad.
