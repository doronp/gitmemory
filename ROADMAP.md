# Roadmap

gitmemory is built in epochs. Each epoch has a goal and a gate, and it closes
only when an independent review signs the gate off. Gate reports and review
records are linked from each row.

## Where each epoch stands

| | | |
|---|---|---|
| E1 | Canonical records + Claude Code adapter | passed |
| E2 | Segment store, contiguity proof, `verify`, redaction gate | passed |
| E3 | Index + retrieval + CLI | passed — [gate report](docs/benchmarks/E3-longmemeval.md) |
| E4 | Hook shim + watcher + git daemon | signed off 2026-09-28 — [end-to-end pass](docs/reviews/E4-signoff.md) after the [review record](docs/reviews/E4-standalone-review.md)'s fix list closed (`f409ff8`) |
| E5 | Derivation and decision graph | passed — [gate report](docs/benchmarks/E5-decision-gate.md); three artifacts per generation. Reopened by [probe C](docs/benchmarks/E5-probe-C.md) and held open by [probe D](docs/benchmarks/E5-probe-D.md), which replicates it in an unrelated domain: user-reverses-own-instruction is 0 of 11, twice. Then [the secondary set](docs/benchmarks/E5-secondary-set.md) scored it on real third-party sessions: **precision 0.0000 / recall 0.0000** on 140 human turns, 9 of 61 on the assistant side. Two fixes since: machine-injected blocks out of the prose stream, and the assistant substitution frame now needs a withdrawal beside it, in the same paragraph — 54 of the 61 withdrawn, the 7 left all reversals. By fix 7 the user side reads 0.1250 / 0.5000, on one true positive. [Probe E](docs/benchmarks/E5-probe-E.md), written blind to test fix 2: 19 of 32, with 12 of 12 non-decisions left alone and 2 of 12 assistant reversals caught |
| E6 | Dashboard | passed — seven views over the index, served read-only on loopback, [review record](docs/reviews/E6-standalone-review.md) |
| E7 | RC1: security review | review closed — six surfaces, 68 findings, [round record](docs/reviews/E7-security-round.md); the fixes then reviewed twice over, [pair review](docs/reviews/E7-pair-review.md); every row of the control index run, and the seven it broke on fixed. Apache-2.0 |
| E7b | The delta that review did not read | 4,966 lines landed after it, so four more lenses plus a pair review read the difference: 22 findings, 19 changed, 3 accepted with the measurement written down — [round record](docs/reviews/E7b-security-delta.md). Four High, one per lens, which is the argument for running four. The worst was a regex that could match one span two ways: 200 bytes in a transcript disabled `derive` for that store permanently |
| E8 | Where we stand against published memory systems | [report](docs/benchmarks/E8-where-we-stand.md), on E3's harness: ahead of agentmemory's BM25 run and behind its hybrid; behind the self-reported figures of two closed systems. [Review round](docs/reviews/E8-review-round.md): two blockers, both the page overclaiming the oracle, fixed; three items left open |
| E9 | The field's own protocols, retrieval and QA | [report](docs/benchmarks/E9-peer-protocols.md): the `rerank12` bench arm is first on LongMemEval-S session R@5 and R@10 among open-source no-LLM retrievers and second on LoCoMo R@10, the stop condition set for the epoch. QA 94.2 on LongMemEval-S (4th of 8 open-source products) and 85.5 on LoCoMo (7th of 9). The shipped BM25 arm is first on none |

## Open work

Everything listed here is written down somewhere in the repository as missing
or unbuilt. Nothing is promised by date.

| Item | State | Where it is recorded |
|---|---|---|
| `push` transport | `push` runs the redaction gate and does not send yet | [SECURITY.md](SECURITY.md), `gitmemory push --help` |
| Net saving against no memory | **unmeasured**, and there is no A/B harness | [Results: not measured](docs/RESULTS.md#what-is-not-measured-said-on-the-dashboard-itself) |
| Injection cost | not built | same |
| Retriever default | the shipped BM25 arm scores below the `rerank` and `rerank12` bench arms on every benchmark. Whether to ship a reranker by default is open | [E9](docs/benchmarks/E9-peer-protocols.md) |
| Decision extraction on real text | close to useless (user side 0.1250 precision); a user reversing their own instruction is 0 of 11, twice | [Results](docs/RESULTS.md) |
| Adapters: Kimi Code CLI, Codex CLI, Gemini CLI, DeepSeek Harness, Cline `hooks.jsonl`, Cursor Agent CLI | planned; each needs a corpus to conform against | [docs/agents.md](docs/agents.md) |
| A materializer for agents that fail P2 or P3 | not built; it would be a new ingestion path | [docs/agents.md](docs/agents.md) |
| Multi-file event streams (one global log for all sessions) | a store change, tracked as [issue #8](https://github.com/doronp/gitmemory/issues/8) | [docs/agents.md](docs/agents.md) |

To propose something new, open an issue with the **feature request** template.
If the proposal changes a locked decision in [DESIGN.md](docs/DESIGN.md), say
which decision it changes.
