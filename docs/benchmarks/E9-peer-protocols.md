# E9 — The field's protocols, run as the field runs them

**Who we compare against.** The headline peers are memory systems that are
**open source** (an OSI licence, the memory engine itself in a public repository)
**and** shipped as products: Mem0, Zep (its engine is open as Graphiti), Letta,
Honcho, Hindsight, Mastra, EverOS, MemOS and Memobase. On no-LLM retrieval the
only open-source systems that publish comparable numbers are MemPalace and
agentmemory: widely used, local-only, no hosted offering. Closed or
source-available systems that publish only their own numbers (Total Recall,
Recallium, Backboard, ByteRover) are listed once, [at the end](#closed-and-source-available-systems-listed-for-completeness),
and are not ranked against.

Every figure is a self-report, ours included; each links to the page it was
read from, checked 2026-09-28. "No-LLM" means no model call anywhere between
the question and the ranked list; a local cross-encoder counts as no-LLM, an API
reranker does not.

| Benchmark (metric) | Class | gitmemory | Rank among open-source peers | Best open-source peer |
|---|---|---|---|---|
| LongMemEval-S, session R@5 | no-LLM retrieval | **99.2** | **1st of 3** | MemPalace 98.4 (450 held-out) |
| LongMemEval-S, session R@10 | no-LLM retrieval | **99.8** | **1st of 3** (tied) | MemPalace 99.8 (450 held-out) |
| LongMemEval-S, all-evidence@10 | retrieval | 96.0 | no open-source peer publishes it | — |
| LoCoMo, session R@10 (MemPalace protocol) | no-LLM retrieval | 91.3 | 2nd of 2 | MemPalace 92.4 |
| LongMemEval-S, QA accuracy | reader + judge | 94.2 | 4th of 8 | Mastra OM 94.87 |
| LoCoMo cats 1–4, QA accuracy | reader + judge | 85.5 | 7th of 9 | Zep 94.7 |

Retrieval is where it leads. The two QA rows are where it is not competitive,
and they are also the rows whose judges differ from everyone else's, so read
those ranks as approximate.

**What the gitmemory column is.** The retrieval numbers are the `rerank12` bench
arm: the shipped BM25 index retrieves 200 turns, a local MiniLM-L-12
cross-encoder (ONNX, via flashrank) reorders them, turns are collapsed to
sessions in rank order. **The product ships BM25 alone** — its row is in every
table below, and it is not first anywhere. `rerank12` was chosen on the LoCoMo
dev half and then run unchanged on everything else, including all of
LongMemEval, which it never saw during selection.

*Revised 2026-09-28.* The first version of this page ranked every system
together, closed ones included, and read "first on one benchmark and second on
two others" — the stop condition set for the epoch, which still holds on the
retrieval rows above. Three citations were also wrong against their sources and
are corrected here: Total Recall's 97.73 is labelled Recall@10 on its page, not
all-evidence@10, and its page shows no 99.4; its LoCoMo 91.17 is retrieval
Recall@10, not QA accuracy; agentmemory's run is n = 500, not 470.

## Table 1 — LongMemEval-S, session retrieval, no LLM

MemPalace protocol: all 500 questions (abstention included), gold = every
answer session id, `recall_any@k` over sessions. Our harness reproduces their
scorer (`bench/peer.py --protocol mempalace`, the NDCG ideal included).

| Rank | System | Licence | R@5 | R@10 | all@10 | NDCG@10 | n | Source |
|---|---|---|---|---|---|---|---|---|
| 1 | **gitmemory `rerank12`** | Apache-2.0 | **99.2** | **99.8** | **96.0** | 0.961 | 500 | this run |
| 1 | MemPalace hybrid v4 | MIT | 98.4 | 99.8 | — | — | 450 held-out | [BENCHMARKS.md](https://github.com/MemPalace/mempalace/blob/main/benchmarks/BENCHMARKS.md) |
| — | gitmemory `rerank` (TinyBERT@50) | | 98.0 | 99.4 | 95.8 | 0.940 | 500 | this run |
| 3 | agentmemory BM25 + vector | Apache-2.0 | 95.2 | 98.6 | — | 0.879 | 500 | [LONGMEMEVAL.md](https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md) |
| — | gitmemory `hybrid` (RRF) | | 97.4 | 98.8 | 94.6 | 0.923 | 500 | this run |
| — | **gitmemory `candidate` (shipped)** | | 95.8 | 97.2 | 86.4 | 0.896 | 500 | this run |
| — | *MemPalace, LLM rerank* | | *99.2–100* | — | — | — | 500 | same; LLM-assisted, other class |

Ranked by R@10, then R@5; our other arms are shown unranked. MemPalace's 98.4
R@5 on the full 500 came from a configuration tuned on those same questions, so
we cite its held-out figure instead; its untuned raw run reads 96.6. No
open-source system publishes all-evidence@10, the strict metric (every answer
session in the top 10).

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
| — | MemPalace hybrid v5 | 88.9 | 83.7 | same |
| — | gitmemory `rerank` | 88.69 | 84.22 | this run |
| — | **gitmemory `candidate` (shipped)** | 86.48 | — | this run |
| — | MemPalace raw | 60.3 | — | same |

Ranked by each system's best configuration. On the held-out test half
(n = 1,127, conversations never used for selection): `rerank12` 91.84 / 88.51,
`rerank` 89.30 / 84.94, `candidate` 88.36 / 82.61. The dev half read
90.6 / 86.6, so selection did not inflate the test figure. agentmemory
publishes no LoCoMo run.

## Table 3 — LongMemEval-S, QA accuracy

Reader and judge are both `gemini-3.1-pro-preview`. The reader prompt is
`run_generation.py`'s and the judge prompts are `evaluate_qa.py`'s, byte for
byte (pinned by sha256 in `bench/test_qa.py`). The official judge is gpt-4o;
ours is not, so **this row is not directly comparable** to any other.

| Rank | System | Licence | Accuracy | Reader | Source |
|---|---|---|---|---|---|
| 1 | Mastra Observational Memory | Apache-2.0 (bar `ee/`) | 94.87 | gpt-5-mini | [mastra.ai](https://mastra.ai/research/observational-memory) |
| 2 | Mem0 Platform (top-50) | Apache-2.0 | 94.8 | not stated | [memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) |
| 3 | Hindsight (AMB run) | MIT | 94.6 | a Gemini model | [benchmarks.hindsight.vectorize.io](https://benchmarks.hindsight.vectorize.io/) |
| 4 | **gitmemory `rerank12`, top 10 sessions** | Apache-2.0 | **94.20** | gemini-3.1-pro-preview | this run |
| — | gitmemory `full_history` (control) | | 94.60 | gemini-3.1-pro-preview | this run |
| — | gitmemory `rerank`, top 10 sessions | | 93.60 | gemini-3.1-pro-preview | this run |
| 5 | Honcho | AGPL-3.0 | 92.6 | Gemini 3 Pro | [plasticlabs.ai](https://plasticlabs.ai/blog/research/Benchmarking-Honcho) |
| 6 | Zep (Cloud) | engine open as Graphiti, Apache-2.0 | 90.2 | gpt-5.4 | [getzep.com/research](https://www.getzep.com/research/) |
| 7 | MemOS | Apache-2.0 | 89.20 | not stated | [MemOS README](https://github.com/MemTensor/MemOS) |
| 8 | EverOS (EverMemOS) | Apache-2.0 | 83.00 | GPT-4.1-mini | [arXiv 2601.02163](https://arxiv.org/html/2601.02163v2) |

How far apart the top four really are depends on details the sources do not
share. Mastra's 94.87 is the mean of six per-type scores; pooled over the 500
questions, as ours is, it reads 93.6, which would put gitmemory third. Mem0's
figure is its hosted Platform, not the open-source library. AMB is run by
Vectorize, which makes Hindsight; Hindsight's own paper reports 91.4.

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

| Rank | System | Licence | Accuracy | Source |
|---|---|---|---|---|
| 1 | Zep (Cloud, multi-scope) | engine open as Graphiti, Apache-2.0 | 94.7 | [getzep.com/research](https://www.getzep.com/research/) |
| 2 | EverOS (EverMemOS) | Apache-2.0 | 93.05 | [arXiv 2601.02163](https://arxiv.org/html/2601.02163v2) |
| 3 | Mem0 Platform (top-200) | Apache-2.0 | 92.5 | [memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) |
| 4 | Hindsight (AMB run) | MIT | 92.01 | [benchmarks.hindsight.vectorize.io](https://benchmarks.hindsight.vectorize.io/) |
| 5 | Honcho | AGPL-3.0 | 89.9 | [plasticlabs.ai](https://plasticlabs.ai/blog/research/Benchmarking-Honcho) |
| 6 | MemOS | Apache-2.0 | 88.83 | [MemOS README](https://github.com/MemTensor/MemOS) |
| 7 | **gitmemory `rerank`, top 10 sessions** | Apache-2.0 | **85.52** | this run |
| 8 | Memobase | Apache-2.0 | 75.78 | [locomo experiment](https://github.com/memodb-io/memobase/tree/main/docs/experiments/locomo-benchmark) |
| 9 | Letta | Apache-2.0 | 74.0 | [letta.com](https://www.letta.com/blog/benchmarking-ai-agent-memory) |

Mastra declined to publish a LoCoMo figure. Letta's run used gpt-4o-mini and
does not state n.

By category: multi-hop (1) 79.1, temporal (2) 84.7, open-domain (3) 61.5,
single-hop (4) 90.7. Dev half 84.9, test half 86.7 (n = 664 / 869, excluding the
failed calls, which carry no id). Arm `rerank`, the arm the dev choices were made with; the
`rerank12` swap was not re-tuned for QA here.

Dev-half choices (n = 665): top 5 sessions 80.75, top 10 sessions 84.66, top 30
turns 75.94. Top 10 sessions was kept.

Two published findings about this row's yardstick, relayed rather than
adopted: [locomo-audit](https://github.com/dial481/locomo-audit) reports 99 of
the 1,540 gold answers wrong (a 93.57% ceiling for a perfect system) and a
Mem0-style judge accepting 62.81% of vague-but-topical wrong answers. Zep's
row above sits over that ceiling and EverOS's within a point of it.

## Closed and source-available systems, listed for completeness

These publish only their own numbers, with no engine under an OSI licence to
inspect or rerun, so they are not ranked above. Figures as their pages showed
them on 2026-09-28.

| System | Licence | LongMemEval-S | LoCoMo | Source |
|---|---|---|---|---|
| Total Recall | closed, no repository | 98.0 QA (gpt-5.4, "earlier retrieval snapshot; rerun pending"); 97.73 "Recall@10" | 91.17 retrieval "Recall@10", "older snapshot" | [total-recall.dev/benchmarks](https://total-recall.dev/benchmarks) |
| Recallium | Elastic-2.0, not OSI | 93.4 QA; retrieval hit@10 98.4, all-evidence@10 93.6 | — | [recallium.ai/benchmarks](https://recallium.ai/benchmarks) |
| Backboard | closed | 93.4 QA | 90.0 QA, cats 1–4 | [LongMemEval results](https://github.com/Backboard-io/Backboard-longmemEval-results), [LoCoMo](https://github.com/Backboard-io/Backboard-Locomo-Benchmark) |
| ByteRover | Elastic-2.0, repository archived | 92.8 QA | 96.1 QA, 1,982 questions | [arXiv 2604.01599](https://arxiv.org/html/2604.01599v1) |

On all-evidence@10, Recallium's 93.6 is below gitmemory's 96.0; Total Recall
does not say whether its "Recall@10" is any-session or all-sessions, so it has
no column here. On QA, Total Recall's 98.0 and ByteRover's 96.1 are the highest
figures anyone publishes, and neither can be checked against a public artifact.

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

- **That the shipped product is first.** It is not. BM25 alone trails both
  open-source peers on LongMemEval R@10 (97.2, against agentmemory's 98.6 and
  MemPalace's 99.8) and MemPalace's best on LoCoMo (86.5 against 92.4). First
  place belongs to a bench arm that adds a 22 MB local cross-encoder; shipping
  it as an option is not done.
- **That we beat the closed systems.** Total Recall publishes higher QA figures
  on both benchmarks and a retrieval figure whose metric it does not define;
  none of it can be rerun, and we have not tried.
- **That the QA rows are ranked fairly.** Every QA number is a self-report with
  its own reader and judge. Ours uses one Gemini model for both, which may be
  more or less lenient than gpt-4o; we have not measured which.
- **That LoCoMo's numbers are clean.** LoCoMo is CC BY-NC 4.0. It is downloaded
  at run time by `bench/fetch_locomo.sh` and never committed; only scores
  computed from it appear here, which is the use the owner's ruling for E9
  permits.
