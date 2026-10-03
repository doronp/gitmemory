# E16 — Full-history control arm on LongMemEval-S

**Status:** Completed.
**Date:** 2026-10-03.

## Why

Honcho's benchmark post reports a 92.0 QA accuracy on LongMemEval-S using Gemini 3 Pro with full history and no memory component. Our published `rerank12` arm achieves 94.2. To measure the exact contribution of our retrieval system, we ran a control arm (`full_history`) through the exact same reader and judge pipeline as our other benchmark runs, passing the entire untruncated history of each instance (sorted by date) directly to the reader model.

## Setup

- **Harness:** `bench/qa.py` was extended to support the `full_history` arm, which bypasses the retrieval step and passes all `haystack_session_ids` to `lme_history`.
- **Reader & Judge:** Both models set to `gemini-3.1-pro-preview` (temperature 0), identical to the 94.2 run.
- **Dataset:** LongMemEval-S (500 instances).
- **Cost control:** Billable Vertex AI project `spending-tokens-for-devetc`.

## Smoke Run Numbers

A smoke run on 5 instances from the dev split showed:
- **Token size:** Each prompt averaged ~125,000 tokens (~500 KB).
- **Accuracy:** 100% on the small sample.

## Full Run Results (n=500)

| Type | Accuracy | n |
|---|---|---|
| **overall** | **0.9460** | 500 |
| abstention | 0.9000 | 30 |
| knowledge-update | 0.9861 | 72 |
| multi-session | 0.8843 | 121 |
| single-session-assistant | 1.0000 | 56 |
| single-session-preference | 0.9000 | 30 |
| single-session-user | 0.9844 | 64 |
| temporal-reasoning | 0.9606 | 127 |

- **Failure count:** 0 instances failed completely (any temporary failures were recovered on retry).
- **Cost / Wall time:** ~62.5M total input tokens processed in ~5 minutes using 64 parallel Vertex AI workers (cost ~$80-150 depending on tier).
- **Interpretation:** The full history control (94.60) and the `rerank12` arm (94.20) are identical within statistical noise (n=500). Retrieval therefore provides massive token savings (from ~125k to ~5k per request) while sacrificing virtually zero accuracy. Honcho's lower baseline (92.0) is likely due to their prompt engineering or model differences.

