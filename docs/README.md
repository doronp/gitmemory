# Documentation

## Start here

| Page | Read it for |
|---|---|
| [README](../README.md) | What gitmemory is, the quickstart, results at a glance |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The shape of the system in about five minutes |
| [USAGE.md](USAGE.md) | Getting value out of it: the daily loop, reading `recall`, handing memory back to an agent |
| [RESULTS.md](RESULTS.md) | Every published result, including the ones it loses |
| [REPRODUCE.md](REPRODUCE.md) | The command behind each result, what it needs, and how closely a rerun matches |
| [watching.md](watching.md) | Configuring `gitmemory watch`: roots, flags, and telling a misconfigured watcher from an idle one |
| [../hook/README.md](../hook/README.md) | Installing the optional hook shim, and its measured cost |
| [agents.md](agents.md) | Which agents can be captured, which cannot, and why |

## Reference

| Page | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | The locked design decisions, each with the review that shaped it |
| [DESIGN-v0.md](DESIGN-v0.md) | The first design, kept verbatim as the audit trail of the review that replaced it. It is not current |
| [../SECURITY.md](../SECURITY.md) | Reporting a vulnerability; what to do when a credential lands in the store |
| [../ROADMAP.md](../ROADMAP.md) | Epoch status and the open work |
| [../CHANGELOG.md](../CHANGELOG.md) | What changed in each release |

## Benchmarks: the gate reports

| Report | Question it answers |
|---|---|
| [E3: LongMemEval](benchmarks/E3-longmemeval.md) | Does the store recover evidence that compaction dropped? (four modes, fourteen calibration gates) |
| [E5: decision gate](benchmarks/E5-decision-gate.md) | Does decision extraction clear its pre-registered bar on a held-out split? |
| [E5: probes C](benchmarks/E5-probe-C.md), [D](benchmarks/E5-probe-D.md), [E](benchmarks/E5-probe-E.md) | Does it generalise to vocabulary it has never seen? (written blind, scored once) |
| [E5: secondary set](benchmarks/E5-secondary-set.md) | Does it work on real sessions that nobody wrote for a benchmark? |
| [E8: where we stand](benchmarks/E8-where-we-stand.md) | How do we compare with other memory systems on E3's harness? |
| [E9: peer protocols](benchmarks/E9-peer-protocols.md) | How do we rank under the field's own protocols, retrieval and QA? |

## The record

The project keeps its working record in public, because a result is only as
credible as the process behind it.

- [`reviews/`](reviews/): every review round, including disputed findings, the
  findings that were refuted, and the reasons.
- [`tasks/`](tasks/): the briefs each round was given.
