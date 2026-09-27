# E9 — The field's protocols, run as the field runs them

**Rankings first.** Every row is a self-report, ours included; sources are in
the tables. "No-LLM" means no model call anywhere between the question and the
ranked list; a local cross-encoder counts as no-LLM, an API reranker does not.

| Benchmark (metric) | Class | gitmemory | Rank | Leader |
|---|---|---|---|---|
| LongMemEval-S, session R@5 | no-LLM retrieval | **99.2** | **1st** | — (next: MemPalace 98.4) |
| LongMemEval-S, session R@10 | no-LLM retrieval | **99.8** | **1st** (tied) | MemPalace 99.8 on its 450 held-out |
| LongMemEval-S, all-evidence@10 | retrieval | 96.0 | **2nd** | Total Recall 97.73 |
| LoCoMo, session R@10 (MemPalace protocol) | no-LLM retrieval | 91.3 | **2nd** | MemPalace 92.4 |
| LongMemEval-S, QA accuracy | reader + judge | 94.2 | 5th of 10 | Total Recall 98.0 |
| LoCoMo cats 1–4, QA accuracy | reader + judge | 85.5 | 8th of 8 | ByteRover 96.1 |

That is first on one benchmark and second on two others, which was the stop
condition set for this epoch. The two QA rows are where we are not competitive,
and they are also the rows whose judges differ from everyone else's.

**What the gitmemory column is.** The retrieval numbers are the `rerank12` bench
arm: the shipped BM25 index retrieves 200 turns, a local MiniLM-L-12
cross-encoder (ONNX, via flashrank) reorders them, turns are collapsed to
sessions in rank order. **The product ships BM25 alone** — its row is in every
table below, and it is not first anywhere. `rerank12` was chosen on the LoCoMo
dev half and then run unchanged on everything else, including all of
LongMemEval, which it never saw during selection.

## Table 1 — LongMemEval-S, session retrieval, no LLM

MemPalace protocol: all 500 questions (abstention included), gold = every
answer session id, `recall_any@k` over sessions. Our harness reproduces their
scorer (`bench/peer.py --protocol mempalace`, the NDCG ideal included).

| Rank | System | R@5 | R@10 | all@10 | NDCG@10 | n | Source |
|---|---|---|---|---|---|---|---|
| 1 | **gitmemory `rerank12`** | **99.2** | **99.8** | 96.0 | 0.961 | 500 | this run |
| 1 | MemPalace hybrid v4 | 98.4 | 99.8 | — | — | 450 held-out | [BENCHMARKS.md](https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md) |
| 3 | Total Recall | — | 99.4 | **97.73** | — | 500 | [total-recall.dev/benchmarks](https://total-recall.dev/benchmarks) |
| 4 | gitmemory `rerank` (TinyBERT@50) | 98.0 | 99.4 | 95.8 | 0.940 | 500 | this run |
| 5 | agentmemory BM25 + vector | 95.2 | 98.6 | — | 0.879 | 470 | [LONGMEMEVAL.md](https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md) |
| 6 | gitmemory `hybrid` (RRF) | 97.4 | 98.8 | 94.6 | 0.923 | 500 | this run |
| 7 | Recallium | — | 98.4 | 93.6 | — | 500 | [recallium.ai/benchmarks](https://recallium.ai/benchmarks) |
| 8 | **gitmemory `candidate` (shipped)** | 95.8 | 97.2 | 86.4 | 0.896 | 500 | this run |
| — | *MemPalace, LLM rerank* | *99.2–100* | — | — | — | 500 | same; LLM-assisted, other class |

Ranked by R@10, then R@5. Total Recall does not say whether its pipeline calls a
model, so it is listed here without a class claim; it leads all-evidence@10,
the strict metric, by 1.7 points. MemPalace's 98.4 R@5 on the full 500 came
from a configuration tuned on those same questions, so we cite its held-out
figure instead.

**Official protocol** (LongMemEval's own `eval_utils.py`: abstention skipped,
answer sessions with no user-side answer turn skipped, n = 419):

| Arm | any@5 | any@10 | all@10 | NDCG@10 |
|---|---|---|---|---|
| `rerank12` | 98.33 | 98.81 | 96.66 | 0.9475 |
| `rerank` | 97.14 | 99.05 | 95.94 | 0.9256 |
| `hybrid` | 96.66 | 98.33 | 95.94 | 0.9098 |
| `candidate` (shipped) | 94.51 | 96.18 | 87.35 | 0.8531 |

Nobody else publishes under this protocol, so it has no ranking; it is here so
the number under the protocol the benchmark's authors wrote exists somewhere.

## Table 2 — LoCoMo, session retrieval, no LLM (MemPalace protocol)

All 1,986 questions (category 5 included), gold = the session of the *first*
evidence id only, as `locomo_bench.py` reads it; `S-Recall@10`.

| Rank | System | R@10 | R@5 | Source |
|---|---|---|---|---|
| 1 | MemPalace bge-large hybrid | **92.4** | — | [BENCHMARKS.md](https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md) |
| 2 | **gitmemory `rerank12`** | **91.31** | 87.70 | this run |
| 3 | MemPalace hybrid v5 | 88.9 | 83.7 | same |
| 4 | gitmemory `rerank` | 88.69 | 84.22 | this run |
| 5 | **gitmemory `candidate` (shipped)** | 86.48 | — | this run |
| 6 | MemPalace raw | 60.3 | — | same |

On the held-out test half (n = 1,127, conversations never used for selection):
`rerank12` 91.84 / 88.51, `rerank` 89.30 / 84.94, `candidate` 88.36 / 82.61.
The dev half read 90.6 / 86.6, so selection did not inflate the test figure.

## Table 3 — LongMemEval-S, QA accuracy

Reader and judge are both `gemini-3.1-pro-preview`. The reader prompt is
`run_generation.py`'s and the judge prompts are `evaluate_qa.py`'s, byte for
byte (pinned by sha256 in `bench/test_qa.py`). The official judge is gpt-4o;
ours is not, so **this row is not directly comparable** to any other.

| Rank | System | Accuracy | Reader | Source |
|---|---|---|---|---|
| 1 | Total Recall | 98.0 | gpt-5.4 | total-recall.dev/benchmarks |
| 2 | Mastra OM | 94.87 | gpt-5-mini | [mastra.ai](https://mastra.ai/research/observational-memory) |
| 3 | Mem0 Platform (top-50) | 94.8 | gpt-4o | [memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) |
| 4 | Hindsight (AMB run) | 94.6 | gemini-3.1-pro-preview | [benchmarks.hindsight.vectorize.io](https://benchmarks.hindsight.vectorize.io/) |
| 5 | **gitmemory `rerank12`, top 10 sessions** | **94.20** | gemini-3.1-pro-preview | this run |
| — | gitmemory `rerank`, top 10 sessions | 93.60 | gemini-3.1-pro-preview | this run |
| 6 | Backboard | 93.4 | gpt-4.1 | backboard.io |
| 6 | Recallium | 93.4 | not stated | recallium.ai/benchmarks |
| 8 | ByteRover | 92.8 | Gemini 3.1 Pro | [arXiv 2604.01599](https://arxiv.org/html/2604.01599v1) |
| 9 | Honcho | 92.6 | Gemini 3 Pro | honcho.dev/evals |
| 10 | Zep (current) | 90.2 | gpt-5.4 | [getzep.com/research](https://www.getzep.com/research/) |

`rerank12` by type: knowledge-update 98.6, multi-session 88.4, single-session
assistant 100.0 / user 98.4 / preference 90.0, temporal 94.5, abstention 90.0.
Dev half 92.7, test half 96.0 (n = 247 / 252). Multi-session questions — the
answer spread over several sessions — are where it loses, as in every row above.

Dev-half choices (n = 247): top 10 sessions 94.33, top 20 92.71, the official
chain-of-thought reader at top 10 93.52. Top 10 without CoT was kept and run on
all 500.

## Table 4 — LoCoMo categories 1–4, QA accuracy

Reader and judge prompts are Mem0's `ANSWER_PROMPT` and `ACCURACY_PROMPT`
(mem0 `evaluation/`, pinned by sha256), the judge in JSON mode; both models are
`gemini-3.1-pro-preview`, not Mem0's gpt-4o-mini. n = 1,540.

| Rank | System | Accuracy | Source |
|---|---|---|---|
| 1 | ByteRover | 96.1 (states 1,982 Q) | [arXiv 2604.01599](https://arxiv.org/html/2604.01599v1) |
| 2 | Zep (current) | 94.7 | [getzep.com/research](https://www.getzep.com/research/) |
| 3 | EverMemOS | 93.05 | [arXiv 2601.02163](https://arxiv.org/html/2601.02163v2) |
| 4 | Mem0 Platform (top-200) | 92.5 | [memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) |
| 5 | Hindsight (AMB run) | 92.01 | [benchmarks.hindsight.vectorize.io](https://benchmarks.hindsight.vectorize.io/) |
| 6 | Total Recall | 91.17 | total-recall.dev/benchmarks |
| 7 | Backboard | 90.0 | [Backboard-Locomo-Benchmark](https://github.com/Backboard-io/Backboard-Locomo-Benchmark) |
| 8 | **gitmemory `rerank`, top 10 sessions** | **85.52** | this run |

By category: multi-hop (1) 79.1, temporal (2) 84.7, open-domain (3) 61.5,
single-hop (4) 90.7. Dev half 84.9, test half 86.7 (n = 664 / 869, excluding the
failed calls, which carry no id). Arm `rerank`, the arm the dev choices were made with; the
`rerank12` swap was not re-tuned for QA here.

Dev-half choices (n = 665): top 5 sessions 80.75, top 10 sessions 84.66, top 30
turns 75.94. Top 10 sessions was kept.

Two published findings about this row's yardstick, relayed rather than
adopted: [locomo-audit](https://github.com/dial481/locomo-audit) reports 99 of
the 1,540 gold answers wrong (a 93.57% ceiling for a perfect system) and a
Mem0-style judge accepting 62.81% of vague-but-topical wrong answers. Three rows
above sit within a point of that ceiling or over it.

## How the runs were made

```sh
sh bench/fetch_locomo.sh                        # pinned revision + sha256, into TMPDIR
python -m bench.peer --protocol mempalace --arms candidate,rerank,rerank12,hybrid --k 1,5,10
python -m bench.peer --protocol official  --arms candidate,rerank,rerank12,hybrid --k 5,10
python -m bench.locomo --protocol mempalace --arms candidate,rerank,rerank12 --k 5,10
python -m bench.qa --bench lme    --arm rerank12 --k 10 --split all
python -m bench.qa --bench locomo --arm rerank --unit session --k 10 --split all
```

*Corrected 2026-09-28:* the LongMemEval QA line read `--arm rerank`. The
published 94.20 is the `rerank12` row; `rerank` read 93.60.

- **Corpus.** LongMemEval sessions are rendered to Claude Code JSONL without the
  fabricated tool noise E3/E8 added (`to_transcript(..., plain=True)`), so the
  retriever sees the dataset's text. E8's numbers used the noisy corpus and a
  ten-*turn* budget; these use the field's ten-*session* budget. The two pages
  are not comparable to each other.
- **Splits.** Dev/test halves by sha256 parity of the question id (LME) or
  conversation id (LoCoMo). Every configuration choice on this page was made on
  a dev half; the dev choices are printed beside each table.
- **QA calls that fail.** A model call that fails all six attempts scores the
  question wrong and is counted in the run's output, rather than aborting.
  In the published runs: LoCoMo 6 of 1,540 (after one rerun, which recovered
  one), LongMemEval `rerank12` 1 of 500. The same prompts hang again on rerun,
  so they are a property of the service on those inputs, not bad luck; scored
  wrong, they cost at most 0.4 and 0.2 points.
- **QA is eval-only.** No model is called by gitmemory itself; `bench/qa.py`
  calls Vertex AI from the benchmark harness and caches every response by
  (model, prompt) hash so a rerun is free and exact.

## What this page does not let you say

- **That the shipped product is first.** It is not. BM25 alone is 8th of 8 in
  Table 1 and 5th of 6 in Table 2. First place belongs to a bench arm that adds
  a 22 MB local cross-encoder; shipping it as an option is not done.
- **That we beat Total Recall.** We lead it on R@10 by 0.4 on a different
  harness and trail it on all-evidence@10 by 1.7 and on both QA rows.
- **That the QA rows are ranked fairly.** Every QA number is a self-report with
  its own reader and judge. Ours uses one Gemini model for both, which may be
  more or less lenient than gpt-4o; we have not measured which.
- **That LoCoMo's numbers are clean.** LoCoMo is CC BY-NC 4.0. It is downloaded
  at run time by `bench/fetch_locomo.sh` and never committed; only scores
  computed from it appear here, which is the use the owner's ruling for E9
  permits.
