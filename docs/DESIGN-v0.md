# gitmemory — design v0 (for adversarial review)

**Status:** SUPERSEDED by [`DESIGN.md`](DESIGN.md). Kept verbatim as the audit trail of the adversarial review, so nothing here is corrected — several decisions below were overturned, and reading it as current will mislead you. **[E4, review: docs]**

**Status when written:** proposal. Nothing built. Every decision below is contestable.
**Reviewers:** Claude Opus 5 (author of this draft), Gemini 3.1 Pro (adversarial reviewer + co-author).

---

## 0. What this is

A **standalone, generic, local-first memory system for coding agents.** It is a product, not a
personal script. It ships with a Claude Code adapter first; Hermes, Kimi and opencode adapters
follow on the same seam.

**Thesis (the one sentence that justifies the project):**

> A memory system is only as trustworthy as its source. Every other agent-memory tool distills
> first and stores the distillate; when the distillate is wrong you cannot tell, because the
> source is gone. gitmemory versions the source — content-addressed, gap-checked, and
> provably complete across the compaction boundary — and treats every derived artifact as a
> **rebuildable function of committed bytes**, not as the record itself.

Two properties follow, and they are the whole product:

1. **Deterministic.** Given the same committed raw bytes, every derived artifact reproduces
   byte-for-byte. `git diff` on the derived tree is a semantic diff of the agent's understanding.
2. **Provably complete.** Each snapshot carries a manifest asserting coverage of
   `[session_start, snapshot_point]` with no holes, and the assertion is checkable offline by
   anyone with the repo.

### Hard constraints (from the owner)

- **Does not touch any existing system on this machine.** Not `~/.claude/settings.json`, not
  `~/memory`, not `~/work/memory-daemon`, not napkin. Those were read as *reference
  architecture* — system shape only. **No data from this machine is ingested, copied, or
  committed. Ever.** Test corpora are synthetic or public-benchmark-derived.
- Userspace only. No `crontab`, no `sudo`, no Docker.
- Local-first. Network is optional and off by default.
- Apache-2.0. Private repo initially.

---

## 1. Prior art — what we are NOT rebuilding

| Tool | License | What it does | Why we still build |
|---|---|---|---|
| `ccf/agentcairn` | Apache-2.0 | PreCompact→distill into a markdown vault; DuckDB cache; LongMemEval harness | Distills rather than snapshots; no git substrate; no completeness proof |
| `grooverLab/fable` | MIT | Verbatim recall thesis, SQLite, recall@1 76.7% | Right thesis, no git, no attestation; PreCompact wiring UNVERIFIED at code level |
| `Haustorium12/continuity-v2` | MIT | SQLite+FTS5, PreCompact **and** PostCompact, MCP recall | Same architecture minus git and minus completeness |
| `nixfred/lmf4.1` PreCompact hook | **no LICENSE** | rsync jsonl → git commit → push | Exactly our capture step, in 40 lines of bash. **Read the pattern, never copy the bytes.** |
| `claude-mem` | — | 5 hooks, none of them PreCompact | Not our problem space |

**Honesty gate (carried from recon, and I am adopting it as a real gate):** before writing code
we install agentcairn and fable and read `claude-code-log`'s parser. If the only delta we can
articulate is "git instead of DuckDB", we send agentcairn a PR and close this project.
The delta has to be *git-as-substrate + provable completeness*, which is an architecture.

### Dependency policy — do not build these

SQLite **FTS5 + `bm25()`** (public domain) · **Model2Vec `potion-retrieval-32M`** (MIT, CPU
static embeddings, ~22k docs/s) · **FlashRank** (Apache-2.0, rerank, no torch) · **ranx** (MIT,
eval metrics) · **sqlite-utils** (Apache-2.0) · **Datasette 0.65.x + datasette-dashboards**
(Apache-2.0, Vega vendored in the wheel = genuinely offline) · **sumy** (Apache-2.0, extractive
summarization) · **KeyBERT** (MIT) · **networkx** (BSD-3, incl. transitive reduction) ·
**GLiNER v2.1** (Apache-2.0) · **D2** (MPL-2.0) + **Graphviz** (EPL-2.0) for rendering ·
**claude-code-log** (MIT) as parser reference.

**Banned:** YAKE (AGPL-3.0) · `leidenalg`/`python-igraph` (GPL) · `basic-memory` (AGPL-3.0) ·
`adr-tools` (GPL-3.0) · LoCoMo dataset (CC-BY-NC-4.0, 99 known ground-truth errors) · Langfuse
(Docker+Postgres+ClickHouse+Redis) · Arize Phoenix (Elastic 2.0, telemetry-on-by-default).

**Budget:** if glue exceeds ~1,500 LOC we are rebuilding something on the do-not-build list.
Target 400–700.

---

## 2. Architecture

```
                 ┌──────────── adapters (per agent) ─────────────┐
  Claude Code ──▶│ claude_code.py   hermes.py  kimi.py  opencode │──┐
  hook / watcher └───────────────────────────────────────────────┘  │
                                                                    ▼
      hot path (ms)                                     canonical records
  ┌────────────────┐        ┌──────────────────────────────────────────┐
  │ shim → spool/  │───────▶│ ingest → normalize → dedup → redact-gate  │
  │ append 1 line  │        └──────────────────────────────────────────┘
  └────────────────┘                          │
                                              ▼
                         ┌─────────────── store (git) ───────────────┐
                         │ raw/  (CAS, verbatim, never pushed w/o opt-in)
                         │ sessions/<agent>/<date>/<sid>/
                         │     manifest.json   turns.jsonl   snapshots/NNN.json
                         │ derived/<sid>/  key-ideas.md  decisions.d2|svg  timeline.json
                         └───────────────────────────────────────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
              index (SQLite,            derive (deterministic)     attest
              NOT committed,            sumy/KeyBERT/networkx      manifest +
              rebuilt in ~8s)           GLiNER → D2                gap check
                    │                         │                         │
                    └─────────────┬───────────┴─────────────────────────┘
                                  ▼
                  recall (CLI / MCP / optional injector hook)
                                  │
                                  ▼
                  serve — Datasette + dashboard.yml @ 127.0.0.1
```

### 2.1 The adapter seam (the thing that makes it generic)

An adapter's **only** job: agent-native bytes → canonical records. It may not touch git, the
index, or derivation. Four record types, canonical JSON (sorted keys, no floats, UTF-8, `\n`):

```
Session { session_id, agent, agent_version, cwd, git_branch, started_at, ended_at, parent_session_id? }
Turn    { turn_id, session_id, seq, role, model, ts, request_id?, usage{...}, is_sidechain }
Block   { block_id, turn_id, kind: text|thinking|tool_use|tool_result, content_sha256, text, tool_name? }
Event   { event_id, session_id, seq, kind: session_start|session_end|compaction|fork, meta{...} }
```

`turn_id`/`block_id` are **content-derived and stable across rebuilds** (`sha256` over
`(session_id, seq, role, content_sha256)`), because unstable IDs make the committed tree churn
and destroy the diffability that is the entire point.

Adapter contract is a pytest conformance suite. A new agent is "supported" when it passes it.

### 2.2 Claude Code adapter — the specifics that matter

These are the traps recon found. Every one is a test case:

- **`parentUuid` is `null` at every `compact_boundary`.** Walk `parentUuid // logicalParentUuid`
  or silently truncate the thread (one observed file: 3.9 M cumulative dropped tokens).
- **Usage over-counts 2.79×.** One API request emits one assistant line *per content block*,
  each repeating cumulative usage. Dedup by `requestId`; drop `<synthetic>` model rows.
- **No `costUSD` field.** Dollars = tokens × a *vendored, dated* LiteLLM price snapshot. The
  dashboard tile says "estimated" and shows the snapshot date. Non-negotiable.
- **Subagents are the majority.** 2,353 of 2,564 files. Per-session cost must include
  `<sid>/subagents/**` — one observed session had 78% of cache-read tokens there.
- **~78% of sessions never compact.** PreCompact alone is not a capture strategy.
- **`sessionId` is not a stable conversation key** (fork-from-compaction observed). Dedup on
  message `uuid`.
- **Schema drifts across CC versions** in a single corpus; `sessionId` is sometimes `session_id`.
  Treat every field as optional; never `KeyError`.
- **Six line types have no uuid and no timestamp** — orderable only by byte position.
- **Worktrees move the project dir**, so a *computed* transcript path misses worktree sessions.
  Glob `*/<session_id>.jsonl` and take newest mtime.
- **PreCompact cannot inject context** — it can only block. A hook that errors in a block-shaped
  way leaves the conversation uncompacted. **The shim exits 0 unconditionally.**

### 2.3 Trigger architecture — hooks AND watcher

- **Hook (low latency, best-effort):** `PreCompact` + `SessionEnd` + `Stop`. ~20 lines of POSIX
  sh. Reads hook JSON on stdin, appends **one line** to `spool/`, `exit 0`. No git, no Python,
  no network on the hot path. Single-digit ms. Budget is 10 s; we will spend <50 ms and we will
  *measure and publish* p50/p99, which no tool in the field does.
- **Watcher (completeness, authoritative):** tails the agent's transcript dir by
  `(path, inode, byte-offset, mtime)` — JSONL is append-only, so this is cheap and exact. This
  is what makes completeness *provable* rather than hoped-for, and it catches crashed sessions,
  non-compacting sessions, and sessions where the hook never fired.

The hook is an optimization. The watcher is the guarantee. If the hook is never installed the
system is still correct — which is also what makes it safe to ship.

### 2.4 Store layout and the raw/push split

```
$GITMEMORY_HOME/            # default ~/.gitmemory — configurable, never inside another repo
  .git/
  raw/<sha256[:2]>/<sha256>.jsonl.zst    # verbatim agent bytes, content-addressed
  sessions/<agent>/<YYYY-MM-DD>/<session_id>/
      manifest.json                       # canonical JSON + completeness proof
      turns.jsonl                         # normalized, deduped
      snapshots/000.json, 001.json …      # append-only; writing an existing N raises
  derived/<session_id>/ …                 # rebuildable; committed so diffs are reviewable
  index/                                  # .gitignored — SQLite is not diffable
  config.toml
```

**Raw verbatim is always committed locally** — that is the thesis; a memory system whose source
is redacted is not a source. Redaction is applied at **two boundaries only**: (1) anything
written to `derived/`, (2) anything that leaves the machine. `push` is **opt-in per remote** and
refuses unless the redaction gate has run and passed. Default config has no remote at all.

### 2.5 Completeness proof (the differentiator)

Per snapshot, `manifest.json` records: the raw blob hashes covered, first/last `seq`, a
`prev_manifest_sha256` chain, the set of `compact_boundary` events crossed, and
`gaps: []` — computed, not asserted, by walking the normalized `seq` space and the
`parentUuid // logicalParentUuid` chain. `gitmemory verify` re-derives all of it from committed
bytes and exits non-zero on any hole. A third party with the repo and no access to this machine
can run it.

### 2.6 Derivation — deterministic by default, LLM never automatic

| Artifact | Method | Deterministic? |
|---|---|---|
| Key ideas | `sumy` LexRank/TextRank over prose blocks | yes |
| Key phrases | `KeyBERT` + Model2Vec backend, fixed seed | yes |
| Entities/relations | `GLiNER` v2.1 (zero-shot, pinned checkpoint) | yes |
| Decision graph | structural extraction → `networkx` → transitive reduction → `D2`/`dot` | yes |
| Timeline | fold over `Event` + `Turn` | yes |
| Titles/labels | optional, local `qwen3.5:35b` (46 tok/s measured), **at an explicit gate only** | no — marked |

**Decision extraction is the one genuinely novel piece.** The signal is structural, not lexical:
a user turn that redirects after a tool result; a plan revised; an edit path abandoned; a test
failure followed by a *different* approach. Rejected alternatives are defined by the **absence**
of a following tool call — no NER model or summarizer surfaces that. Output is a DAG:
nodes = `{decision, alternative-considered, alternative-rejected, outcome}`,
edges = `{led_to, rejected_for, superseded_by}`. Rendered via D2. Every node carries a
`source_ref` back to a committed block hash — **no node may exist without one.** That rule is
what stops a decision diagram from becoming fiction.

**No LLM anywhere in the automatic path.** 4B-class models hallucinate rationale that was never
in the transcript, which is the single worst failure mode for a decision record.

### 2.7 Retrieval

SQLite FTS5 with **separate columns** — `prose`, `tool_result`, `paths` — and per-column
`bm25()` weights. Flat indexing is a known failure: tool results are ~79% of text volume, so a
flat BM25 returns pasted logs. Hybrid: BM25 ⊕ Model2Vec, fused with RRF (k=60, 6 lines).
FlashRank on the top-50 (measured elsewhere: top-20 failure 5.7%→1.9%). Measured on a
comparable corpus: 144 k messages index in ~8 s, queries 0.2–2.6 ms.

Skip ANN — at ~144 k vectors numpy brute force is 5.8 ms and *exact*.

### 2.8 Recall surface + injector

`gitmemory recall "<query>"` (CLI), an MCP server, and an **optional** `UserPromptSubmit` hook
the user installs themselves. The injector is **off by default**, ships with its own kill-switch
file, a short-prompt guard, a query cap and a hard soft-deadline. What it injects beyond
filename hits — the gap in filename-only recall — is: relevant prior **decisions**, prior
**dead-ends** ("tried X at <ref>, abandoned after Y"), and file-touch history, each with a
`source_ref`.

**UNVERIFIED and must be tested before claiming coexistence:** whether multiple
`UserPromptSubmit` hooks' `additionalContext` outputs concatenate or last-wins.

### 2.9 Dashboard

`uvx datasette --immutable $GITMEMORY_HOME/index/gitmemory.db -m dashboard.yml --host 127.0.0.1`.
Immutable so the dashboard process can never mutate the store. Panels:

- **Corpus** — sessions, turns, tokens captured, growth, unique-vs-total bytes
- **Completeness** — % sessions with a verified manifest, gaps found, compactions captured, spool backlog
- **Spend** — tokens by model/day/session (requestId-deduped), cache-hit share, estimated $ + price-snapshot date
- **Quality** — benchmark recall@k / MRR / nDCG per retriever arm, from `bench/`
- **Frugality** — see below
- **Health** — daemon state, last commit, index freshness, hook p50/p99

**Frugality, honestly.** Three numbers and one refusal:
1. **Injection cost** — exact, and it is a *cost*. Tokens added per session, % of context. Shown first.
2. **Cache-served share** — exact. A real saving, but versus *"same injection, uncached"*, **not**
   versus *"no memory"*. Never conflated on the tile.
3. **Avoided rediscovery** — a bounded, matched-comparison **estimate**, published as a range with
   its matching criteria printed beside it.
4. **"Net saving vs no-memory: UNMEASURED"** until the A/B harness runs. No "tokens saved" tile
   ships before then. The A/B is cheap: a `memory_enabled` flag, random ~20% of runs disabled,
   outcome-matched comparison.

We also state what is *not* measurable: whether an injected memory influenced the answer
(attention leaves no trace — the panel says **REFERENCED**, not USED), and both tails
(a prevented dead-end is an unbounded unmeasured saving; a stale memory that misled is an
unbounded unmeasured cost). Any single frugality number assumes both tails are zero.

---

## 3. Language — proposal, open to being overturned

**Proposal: POSIX sh for the hook shim; Python 3.13 (uv) for everything else; no new Rust.**

- The full-corpus rebuild measures ~8 s. That removes the case for a resident indexer, which was
  the strongest Rust argument.
- The derivation layer has no Rust story: sumy, KeyBERT, networkx, GLiNER, sqlite-utils, ranx —
  no equivalents. `text-splitter` is the one good Rust chunker and it is markdown-aware, not
  *conversation*-aware, which is the chunking that matters.
- Search is C either way — FTS5 is in the system sqlite.
- **tantivy is actively disqualifying**: its index is a *directory of segment files* whose merge
  policy makes bytes non-deterministic across rebuilds. For a git-versioned artifact that is fatal.
- The dashboard decides it: Datasette + ~50 lines of YAML vs. axum+maud+rust-embed+rusqlite,
  the highest-LOC option on the table, with `plotters` rendering static images.
- Go loses both ends: no Datasette equivalent; SQLite needs cgo or `modernc.org/sqlite`, which
  spoils the stdlib-only story that is Go's only advantage.

**The counter-argument Gemini must make properly:** this is a *product* meant to install on other
people's machines. "Requires uv + Python" is a worse distribution story than one static binary,
and the hot path (a hook with a 10 s ceiling that can block compaction if it misbehaves) is
exactly where a 5 ms static binary beats a 200 ms interpreter start. Does that flip the
recommendation to a Rust core (shim + spool + watcher + git) with a Python derivation sidecar?
Argue it, don't concede it. The cost of being wrong here is a two-toolchain project forever.

---

## 4. Evaluation — mutate a benchmark

Public + synthetic only. **Never this machine's history.**

- **LongMemEval-S** — primary. Multi-session QA, openly licensed, measures exactly our claim
  (retrieval over long agent history). Mutation: replay its sessions through a synthetic
  Claude-Code-shaped transcript generator so the *input* is our real format, then inject
  synthetic compaction boundaries at controlled positions to measure **recall across the
  compaction wall** — the thing nobody else measures.
- **Explicitly not LoCoMo** — CC-BY-NC-4.0, 99 documented ground-truth errors, unmaintained.
- **`thedotmack/membench` (MIT) ablation design**, borrowed: 4 arms (candidate / none / shuffled /
  reference), item-paired, Bonferroni-corrected, with a calibration gate.
- **Retriever arms**: BM25 · Model2Vec · RRF hybrid · RRF+FlashRank. Public evidence is
  genuinely contradictory (BM25 *beats* bge-base on LongMemEval knowledge_update 88.0 vs 81.3,
  loses badly on MemBench 39.45 vs 60.29) — so we measure on our own corpus rather than cite.
- **Completeness tests**: property-based (Hypothesis) over generated transcripts with injected
  truncation, interleaving, duplicate replay, schema drift and mid-write crashes. The claim is
  "no gaps"; it must be fuzzed, not asserted.
- **Determinism test**: build twice from the same raw, `git diff --exit-code` the derived tree.
- **End-to-end**: an isolated Claude Code instance (own `CLAUDE_CONFIG_DIR`, throwaway project)
  with the hook installed, driven to a real compaction. Proves the loop on the real binary
  without touching the owner's config.

---

## 5. Process

- **Epochs**, each with a ship/kill gate. No dates.
- **Per-module code review** after every module's commit — standalone round, skilled reviewer
  agents on distinct lenses (correctness · API design · concurrency · determinism · test
  coverage), findings adversarially verified before acting.
- **One full security review at RC1**: hook injection surface, git operations, path traversal,
  SQL, subprocess handling, dashboard bind/CSRF/SSRF/XSS, secret handling, dependency supply
  chain.
- **Pair review between Claude and Gemini on every module** — neither merges the other's code
  unreviewed.

### Epochs

| # | Goal | Ship gate |
|---|---|---|
| E0 | Honesty gate: install agentcairn + fable, read claude-code-log's parser | Delta is architectural, or we PR agentcairn and stop |
| E1 | Canonical records + Claude Code adapter + conformance suite | Every §2.2 trap is a passing test; determinism test green |
| E2 | Store + git daemon + completeness proof + `verify` | Fuzzer cannot produce an undetected gap |
| E3 | Hook shim + watcher | p50/p99 published; kill -9 mid-write loses nothing |
| E4 | Index + retrieval | Benchmark arms scored on mutated LongMemEval-S |
| E5 | Derivation + decision graph | Every node has a `source_ref`; build twice = identical bytes |
| E6 | Dashboard | Runs offline from one command |
| E7 | RC1: security review, README + graphic, Apache-2.0, private repo | No unresolved findings |

---

## 6. Open questions for the reviewer

1. **Language** (§3) — confirm or overturn, with the product-distribution argument taken seriously.
2. **Raw-in-git** (§2.4) — is committing verbatim raw locally right, or does CAS-outside-git with
   only hashes in git give the same guarantee at lower cost? Measure delta-compression on
   append-only JSONL before answering.
3. **Completeness proof** (§2.5) — is `gaps: []` computable soundly, or only detectably-unsound?
   Where does the proof actually break?
4. **Decision extraction** (§2.6) — is the structural signal real, or am I describing something
   that only works on transcripts I have already read?
5. **Scope** — what in here should be cut to hit 400–700 LOC? Name the thing you would delete.
