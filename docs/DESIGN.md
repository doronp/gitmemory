# gitmemory — locked design (v1)

**Status:** LOCKED. Reviewed adversarially by Claude Opus 5 and Gemini 3.1 Pro over two rounds.
Supersedes `DESIGN-v0.md` (kept for the audit trail).
Changes from v0 are marked **[R1]** (Gemini round 1) / **[R2]** (round 2) / **[C]** (Claude rebuttal).

---

## 0. Thesis

> A memory system is only as trustworthy as its source. Every other agent-memory tool distills
> first and stores the distillate; when the distillate is wrong you cannot tell, because the
> source is gone. gitmemory versions the **source** — as append-only byte segments that tile the
> transcript with no holes — and treats every derived artifact as a **rebuildable function of
> committed bytes**.

Two properties, and they are the whole product:

1. **Deterministic.** Same committed bytes → byte-identical derived artifacts. `git diff` on
   `derived/` is a semantic diff of the agent's understanding.
2. **Cryptographically contiguous.** **[R1 — downgraded from "provably complete"]** We prove the
   captured byte stream tiles `[0, EOF)` with no holes or overlaps and hashes to a recorded
   digest. We do **not** prove the model emitted nothing further — a process killed before flush
   leaves a stream that is contiguous and short. **This limitation is stated in the README, not
   buried.**

### Hard constraints

- Touches **nothing** existing on this machine. `~/.claude/settings.json`, `~/memory`,
  `~/work/memory-daemon` and napkin were read as *reference architecture* — system shape only.
  **No data from this machine is ingested, copied, or committed. Ever.**
- Test corpora are synthetic or public-benchmark-derived.
- Userspace only: no `crontab`, no `sudo`, no Docker. LaunchAgent is the keep-alive.
- Local-first. No network by default. No remote configured by default.
- Apache-2.0.

---

## 1. Language — LOCKED

**POSIX sh for the hook shim. Python 3.13 (uv, `pyproject.toml`) for everything else. No new
Rust. No hybrid.** Both reviewers agreed after argument.

Reasons that survived review:
- Datasette + ~50 lines of YAML is an unmatched dashboard-per-LOC ratio; the Rust equivalent
  (axum+maud+rust-embed+rusqlite) is the highest-LOC option on the table.
- The derivation layer has no Rust equivalents (sumy, KeyBERT, networkx, GLiNER, sqlite-utils, ranx).
- Full-corpus rebuild measures ~8 s, which kills the case for a resident Rust indexer.
- Go loses both ends: no Datasette equivalent; SQLite needs cgo or `modernc.org/sqlite`.
- A Rust-core/Python-sidecar hybrid means two toolchains forever — worst of both.

**[R1] Struck from v0:** the claim that tantivy's non-deterministic segment merge disqualifies
it. `index/` is gitignored, so index-byte determinism is irrelevant. A good conclusion does not
get to keep a bad argument.

The shim touches no interpreter. Everything else is one toolchain.

---

## 2. Architecture

### 2.1 Canonical records + native passthrough **[C — both, not either]**

Adapters do one thing: agent-native bytes → canonical records. They may not touch git, the
index, or derivation. Canonical JSON: sorted keys, no floats, UTF-8, `\n`.

```
Session { session_id, agent, agent_version, cwd, git_branch, started_at, ended_at,
          parent_session_id?, native{} }
Turn    { turn_id, session_id, seq, role, model, ts?, request_id?, usage{}, is_sidechain, native{} }
Block   { block_id, turn_id, kind: text|thinking|tool_use|tool_result, content_sha256, text,
          tool_name?, native{} }
Event   { event_id, session_id, seq, kind: session_start|session_end|compaction|fork, meta{} }
```

`native{}` is the verbatim provider object. Gemini argued for a fully generic
`content: list[dict]` because Hermes/Kimi/opencode are not Claude-shaped; a fully generic blob
destroys cross-agent queryability, which is the point of a canonical model. So: the typed
projection is what you **query**, `native{}` is what you **fall back to** when an adapter is
imperfect. Costs one field.

IDs are content-derived and stable across rebuilds:
`turn_id = sha256(session_id, seq, role, content_sha256)`. Unstable IDs churn the committed
tree and destroy diffability, which is the entire point.

Adapter contract is a pytest conformance suite. An agent is "supported" when it passes.

### 2.2 Claude Code adapter — the traps, each a test case

Every item below was found by recon on real transcripts and is a required passing test:

| Trap | Handling |
|---|---|
| `parentUuid` is `null` at every `compact_boundary` | walk `parentUuid // logicalParentUuid` |
| usage over-counts **2.79×** (one assistant line per content block, each repeating cumulative usage) | dedup by `requestId`; drop `<synthetic>` model rows. **Settled from data [E1]:** across 1,223 requests in claude-code-log's corpus, usage within a `requestId` is monotonically non-decreasing in **0 counter-examples** — it is cumulative, so last-write-wins is exact (identical to max-wins, verified). Independent over-count on that corpus: **2.46×**. |
| no `costUSD` field | tokens × a **vendored, dated** price snapshot; tile says "estimated" + shows date |
| subagents are 92% of files; one session had 78% of cache-read tokens in `subagents/**` | per-session rollup must include them |
| ~78% of sessions never compact | watcher, not hook, is the capture guarantee |
| `sessionId` is not a stable conversation key (fork-from-compaction observed) | dedup on message `uuid` |
| schema drifts across CC versions; `sessionId` sometimes `session_id` | every field optional; never `KeyError` |
| six line types have no uuid and no timestamp | **byte position is the only ordering authority; timestamps are never a sort key [R1]** |
| worktrees move the project dir | glob `*/<session_id>.jsonl`, take newest mtime — never compute the path |
| PreCompact **cannot** inject context, only block; a block-shaped error leaves the conversation uncompacted | **shim exits 0 unconditionally** |
| sidechain interleaving | anchor on the parent's `toolUseId`, **not** on time **[R1]** |

**Departures from this table made while building [E1, round 2]** — recorded
rather than made silently:

1. **A line is a turn iff it carries identity *or content*.** The identity-only
   rule dropped `type: summary` (which carries `leafUuid`, never `uuid`) and
   `queue-operation`/`remove` (which carries real human steering text and no id
   at all). Text is also read from `content`, `summary`, `attachment` and
   `toolUseResult`, not only `message.content`: on the MIT corpus reading only
   `message` lost 122 of 133 `system` lines and all 54 attachments.
2. **`leafUuid` is a pointer, not an identity** (`Turn.ref_uuid`). Folding it
   into `uuid` made a summary collide with the turn it summarises and lose the
   dedup race — three summaries silently dropped, counted as `duplicate_uuid`.
3. **Sidechain anchoring splits into two fields.** `sourceToolAssistantUUID` is
   a turn uuid and `toolUseID` is a content-block id; merging them into one
   `anchor_uuid` made joins silently wrong. `agentId` is now projected too
   (`Turn.agent_id`) — it covers the sidechains the other two miss.
4. **`compact_boundary` is a turn as well as an event.** It carries a uuid and
   parents the summary that follows; emitting only the event deleted the node
   and left a dangling DAG edge.
5. **Ids key on `anchor`/`byte_offset`, never `seq`.** `Event.event_id` still
   keyed on `seq` after `turn_id` stopped doing so, so a skip-rule change
   churned every later event.
6. **Image payloads are projected, not inlined.** Base64 was 61.7% of all
   indexed text; the bytes stay in `native` and in the raw segment.

### 2.3 Triggers — hook AND watcher

- **Hook (latency, best-effort).** `PreCompact` + `SessionEnd` + `Stop`. ~20 lines POSIX sh.
  Reads hook JSON on stdin, writes **one file** to `spool/`, `exit 0`. No git, no Python, no
  network on the hot path. Budget is 10 s; target <50 ms; **p50/p99 measured and published**,
  which no tool in the field does.
- **Watcher (the guarantee).** Tails transcript dirs by `(path, inode, byte-offset, mtime)`.
  Append-only makes this cheap and exact. Catches crashed sessions, non-compacting sessions, and
  sessions where the hook was never installed.

If the hook is never installed the system is still correct. That is what makes it safe to ship.

**Spool concurrency [R1 raised, C resolved]:** each hook invocation writes its **own** file
`spool/<epoch_ns>-<pid>.json`. No shared file → no torn writes → no `flock` to get wrong.
Fewer moving parts than a lock.

**Hook/watcher double-write [R1 raised, C resolved]:** both derive the same next offset from the
same committed state, so the loser writes an identical segment or none. Idempotent by
construction; no lock needed for correctness.

### 2.4 Storage — append-only segments **[C — the central correction]**

v0 committed whole-file snapshots; Gemini correctly smelled pack explosion. Its proposed fix
(content-addressed whole-file blobs outside git) is **worse**: CAS writes a new immutable blob
each time the file grows, and if those blobs are zstd-compressed git cannot delta them at all —
turning O(N) storage into O(N²). **Gemini verified and conceded this in R2.**

JSONL is append-only. So store **segments**, not files:

```
$GITMEMORY_HOME/                      # default ~/.gitmemory
  raw/<agent>/<session_id>/
      000000000000-000000131072.jsonl     # bytes [0, 131072)
      000000131072-000000164000.jsonl     # bytes [131072, 164000)
  sessions/<agent>/<date>/<session_id>/
      manifest.json                        # canonical JSON + contiguity proof
      turns.jsonl                          # normalized projection
      snapshots/000.json …                 # append-only; writing an existing N raises
  derived/<session_id>/ …                  # rebuildable, committed so diffs are reviewable
  index/                                   # .gitignored — SQLite is not diffable
  spool/                                   # .gitignored — one file per hook fire
  config.toml
```

Each capture commits **only the bytes appended since the last recorded offset**. Consequences:

1. **Zero duplication.** Stored bytes == bytes the agent ever wrote, not a multiple.
2. **Plain text, never pre-compressed.** Let git's zlib + delta work on text it understands.
   Pre-compressing defeats git's native packing.
3. **Reconstruction is `cat` in filename order**, checked against a recorded whole-file sha256.
4. **Contiguity becomes arithmetic, not a heuristic** — see §2.5.

**Segment coalescing [R2]:** the shim never writes to the worktree. The watcher coalesces spool
records into a segment on one of: a `compact_boundary` crossed · session end · max once per hour.
Bounds a session to typically <10 segments, so tree objects stay small. Tiling is unaffected —
coalescing only changes where the cut points are, never whether they tile.

**Raw is committed locally, always** — that is the thesis. Redaction applies at exactly two
boundaries: anything written to `derived/`, and anything that leaves the machine. **`push` is
opt-in per remote and refuses unless the redaction gate passed.** Default config has no remote.
A startup assertion refuses to run if `$GITMEMORY_HOME` is inside another work tree, and every
git call is explicit `git -C $GITMEMORY_HOME` — never cwd-inherited **[R1]**.

`gc.auto` is configured and the daemon periodically runs `git gc --auto`: `raw/` scales O(N),
but `derived/` is rewritten on every rebuild and orphans blobs **[R2]**.

### 2.5 The contiguity proof

`manifest.json` carries exactly **[R2]**:

```
session_id, agent, file_sha256, prev_manifest_sha256,
segments: [{path, start, end, sha256}, ...],
compact_boundaries: [seq, ...]
```

**Verification, by a stranger with only the repo and no access to this machine:**

```sh
cat $(jq -r '.segments|sort_by(.start)|.[].path' manifest.json) | shasum -a 256
# must equal .file_sha256; and segments must tile [0, EOF) with no hole and no overlap
```

Zero LLM calls, zero Python, zero host dependency. `gitmemory verify` does the same and exits
non-zero on any hole.

**What it detects:** in-place mutation (someone `sed`s a leaked key to an equal-length string —
offsets unchanged, but `hash(concat) != file_sha256`), truncation, rotation, inode reuse,
mid-session deletion, and the worktree duplicate-session case. In each the daemon sees diverged
history and **refuses to append** rather than silently corrupting.

**What it does NOT prove, stated plainly:** that the agent wrote everything it generated. A
`kill -9` before flush yields a stream that is contiguous and short. Hence *contiguous*, not
*complete*.

### 2.5a Generations — what happens *after* divergence **[E0]**

"Refuses to append" is not a resting state; a system that stops capturing on first divergence is
broken. Divergence forks a **generation**:

```
raw/<agent>/<session_id>/g00/000000000000-000000131072.jsonl
raw/<agent>/<session_id>/g01/000000000000-000000098304.jsonl   # source was rewritten; g00 is sealed
```

`manifest.json` gains `generation: int` and `diverged_from: {generation, at_byte, prev_file_sha256}`.
g00 is never rewritten or deleted — the pre-rewrite bytes stay committed, which is the one thing
every other tool in this space loses. Contiguity is asserted *within* a generation; across them the
manifest chain records the break honestly instead of hiding it.

**Two layout changes made while building this [E2]:**
- **One manifest per generation**, `sessions/<agent>/<session_id>/g00.json`, not one per session.
  A single per-session manifest describes only the *current* generation, so a stranger with a
  checkout could verify g01 and not g00 — the sealed bytes would be provable only by digging
  through git history, which defeats "third-party checkable with `cat` and `shasum`". Flat files
  also mean a sealed generation's manifest is never rewritten, matching the bytes it describes.
- **Dropped the `<date>` path tier.** It keys a committed path on a value we may not have — a
  transcript whose first line carries no timestamp has no date — and a path that moves churns the
  tree. The manifest carries the timestamps; the dashboard groups by them.
- `compact_boundaries` holds **byte offsets**, not turn `seq`. The store holds no record model and
  must not import an adapter to get one, and the E4 coalescer cuts on byte positions. The CLI
  passes offsets the adapter found; `--no-parse` records bytes with none.

This is not hypothetical. Verified at `/tmp/gm-e0/fable/docs/ARCHITECTURE.md:18` — *"The live
transcript is REWRITTEN by the pruner (52 generations, 3GB of backups exist)"*. The rewriter there
is **fable's own `prune.py`** (`prune_file(..., replace=True)`, auto-fired from its hook), not
Claude Code. So: a memory tool installed on the same machine destructively rewrites the source
gitmemory reads. Generations are how we survive a neighbour like that, and the contrast is the
README's sharpest line — *we never write to the source; the tool that does has 3 GB of backups
proving why you would want that.*

**Correcting two E0 claims that did not survive checking:**
- *"claude-code-log refuses its prefix proof on 49 of 185 files, so the source is not append-only"* —
  **wrong reason.** `converter.py:392` refuses because *rows* ≠ *lines* (subagent blocks splice into
  the middle of the row list), not because bytes changed. Its byte-prefix check
  (`_begin_byte_parse`) is described in-tree as "a *stronger* check" and does resume. Byte
  contiguity is unaffected.
- *"agentcairn's redact-before-write is the safer default"* — it is the *lossier* default. Its own
  reading shows redaction rewrites text before hashing, so the field labelled `verbatim` is not the
  bytes. We keep raw local-only and redact at the two egress boundaries (§2.4). Same safety, no
  silent loss.

### 2.5b Vendored, not written **[E0]**

Owner's instruction: do not write what already exists under Apache/MIT. E0 read four tools at code
level; these are the pieces worth taking rather than re-deriving. `NOTICE` + `THIRD_PARTY.md` carry
attribution; MIT and Apache-2.0 both inbound-compatible with our Apache-2.0.

| From | What | Why not write it |
|---|---|---|
| fable `fable/jsonl.py` (MIT, 71 LOC) | `iter_records` / `read_span` | `surrogateescape` so offsets stay byte-true on invalid UTF-8 (`errors="replace"` inflates: U+FFFD is 3 bytes), plus `raw_decode` looping for concatenated objects on one physical line. Both are real corpus facts. This is the artifact we would get subtly wrong. |
| claude-code-log `converter.py:115` `SILENT_SKIP_TYPES` (MIT) | the frozenset of entry types carrying no DAG fields | Field knowledge bought with someone else's crashes. |
| claude-code-log `dev-docs/dag.md:628-634` (MIT) | the table of every shape that legitimately lands as a DAG root | Read before writing a line of DAG code. |
| claude-code-log `test/test_data/` (MIT, 162 `.jsonl`) | conformance corpus | **Resolves the test-data problem directly**: third-party, redistributable, real-shaped — and not one byte of this machine's history. |
| agentcairn `ingest/harness/claude_code.py:103` `classify_claude_code` (Apache-2.0) | fail-closed structural event taxonomy | The best classifier of the four; we take the taxonomy, not the discard-everything pipeline around it. |
| agentcairn `encode_cwd` (Apache-2.0) | Claude's project-dir encoding incl. the base-36 JS string hash for >200-char paths | Undocumented, and wrong-by-default. |

Not taken: every storage layer, every index, every pipeline. Those are where the delta is.

### 2.6 Derivation — deterministic, no LLM in the automatic path

| Artifact | Method | Deterministic |
|---|---|---|
| Key ideas | `sumy` LexRank/TextRank over prose blocks | yes |
| Key phrases | `KeyBERT` + Model2Vec backend, fixed seed | yes |
| Timeline | fold over `Event` + `Turn` | yes |
| Decision graph | structural → `networkx` → transitive reduction → `D2`/`dot` | yes, **gated** |
| Titles/labels | optional local `qwen3.5:35b` (46 tok/s measured) at an **explicit gate only** | no — marked |

**No LLM anywhere automatic.** 4B-class models hallucinate rationale that was never in the
transcript — the worst possible failure mode for a decision record.

**Decision extraction ships only if it clears a pre-registered gate [R1 challenged, R2 + C].**
Gemini's objection is correct: a naive redirect-after-tool-result heuristic will flag routine
linter retries as "rejected alternatives."

- **Labeled set:** primary = synthetic sessions from our own generator, which emits ground truth
  **by construction** (we plant the decisions, so labels are free and exact). Secondary = 30
  hand-labeled real-shaped sessions to confirm the synthetic set isn't too easy.
  *(This replaces Gemini's "50 SWE-bench sessions hand-labeled by the owner" — that is not the
  owner's job, and SWE-bench trajectories are not Claude-Code-shaped.)*
- **Metric:** strict node-level precision and recall.
- **Pre-registered bar, declared before measuring: precision ≥ 0.85, recall ≥ 0.60.**
- **Below the bar it does not ship,** and the README states the measured number and says so.

Every node carries a `source_ref` to a committed block hash. **No node may exist without one.**
That rule is what stops a decision diagram from becoming fiction.

### 2.7 Retrieval

SQLite FTS5 with **separate columns** — `prose`, `tool_result`, `paths` — and per-column
`bm25()` weights. Flat indexing is a known failure: tool results are ~79% of text volume, so flat
BM25 returns pasted logs. Hybrid BM25 ⊕ Model2Vec fused by RRF (k=60, ~6 lines). FlashRank on
top-50. Skip ANN: at ~144 k vectors numpy brute force is 5.8 ms and *exact*.

### 2.8 Recall surface

`gitmemory recall "<query>"` (CLI) · MCP server · an **optional, off-by-default**
`UserPromptSubmit` hook with its own kill-switch file, short-prompt guard, query cap and soft
deadline. Beyond filename hits it injects relevant prior **decisions**, prior **dead-ends**
("tried X at `<ref>`, abandoned after Y") and file-touch history, each with a `source_ref`.

*(Gemini argued to cut this. Rejected: the owner named it explicitly. Off-by-default fully
answers the token-burn objection.)*

**UNVERIFIED, must be tested before claiming coexistence:** whether multiple `UserPromptSubmit`
hooks' `additionalContext` outputs concatenate or last-wins.

### 2.9 Dashboard

`uvx datasette --immutable $GITMEMORY_HOME/index/gitmemory.db -m dashboard.yml --host 127.0.0.1`
— immutable so the dashboard can never mutate the store. Vega is vendored in the wheel, so it is
genuinely offline.

Panels: **Corpus** (sessions, turns, tokens, growth, unique-vs-total bytes) · **Contiguity**
(% sessions verifying, holes found, compactions captured, spool backlog) · **Spend** (tokens by
model/day/session, requestId-deduped; cache-hit share; estimated $ + price-snapshot date) ·
**Quality** (benchmark recall@k / MRR / nDCG per retriever arm) · **Frugality** · **Health**
(daemon state, last commit, index freshness, hook p50/p99).

**Frugality — two exact numbers and one refusal [R1: the third was dropped]:**
1. **Injection cost** — exact, and it is a *cost*. Tokens added per session, % of context. Shown first.
2. **Cache-served share** — exact. A real saving, but versus *"same injection, uncached"*, **not**
   versus *"no memory."* Never conflated on the tile.
3. **"Net saving vs no-memory: UNMEASURED"** until the A/B harness runs. No "tokens saved" tile
   ships before then.

**Dropped in review:** "avoided rediscovery." It was a counterfactual dressed as a measurement —
you cannot prove an agent *would* have run `rg` just because comparable sessions did.

Also stated on the dashboard: whether an injected memory *influenced* the answer is **not
observable** (attention leaves no trace — the panel says **REFERENCED**, not USED), and both
tails are invisible (a prevented dead-end is an unbounded unmeasured saving; a stale memory that
misled is an unbounded unmeasured cost). Any single frugality number assumes both tails are zero.

---

## 3. Evaluation

Public + synthetic only. **Never this machine's history.**

- **LongMemEval-S** — primary. Mutation: replay through a synthetic Claude-Code-shaped transcript
  generator so the *input* is our real format, then inject synthetic compaction boundaries at
  controlled positions to measure **recall across the compaction wall** — which nobody else measures.
- **Not LoCoMo** — CC-BY-NC-4.0, 99 documented ground-truth errors, unmaintained since 2024-08.
- **`thedotmack/membench` (MIT) ablation design**, borrowed: 4 arms (candidate / none / shuffled /
  reference), item-paired, Bonferroni-corrected, calibration gate.
- **Retriever arms:** BM25 · Model2Vec · RRF hybrid · RRF+FlashRank. Public evidence is genuinely
  contradictory (BM25 *beats* bge-base on LongMemEval knowledge_update 88.0 vs 81.3, loses on
  MembBench 39.45 vs 60.29), so we measure rather than cite.
- **Contiguity fuzzing** (Hypothesis): injected truncation, interleaving, duplicate replay, schema
  drift, mid-write crash, in-place mutation, inode reuse. The claim must be fuzzed, not asserted.
- **Determinism:** build twice from the same raw, `git diff --exit-code` on `derived/`.
- **End-to-end:** an isolated Claude Code instance (own `CLAUDE_CONFIG_DIR`, throwaway project)
  driven to a real compaction. Proves the loop on the real binary without touching the owner's config.

---

## 4. Epochs

**[R1 reordered: retrieval before the watcher daemon; E1 is the riskiest.]**

| # | Goal | Ship gate |
|---|---|---|
| E0 | ~~Honesty gate~~ **PASSED 2026-09-20** — four tools read at code level, 3-lens panel | 2 BUILD / 1 PR_TO_fable. Dissent recorded in §6. |
| E1 | ~~**(riskiest)** Canonical records + CC adapter + conformance suite~~ **PASSED 2026-09-20, on the second attempt** | Every §2.2 trap is a passing test; determinism test green. First attempt was signed off wrongly — see below. |
| E2 | ~~Segment store + contiguity proof + `verify` + redaction gate~~ **PASSED 2026-09-20** | 11 mutation classes + 150 seeded fuzz rounds, zero undetected; `push` refuses with no config |
| E3 | Index + retrieval + CLI | Benchmark arms scored on mutated LongMemEval-S |
| E4 | Hook shim + watcher + git daemon | p50/p99 published; `kill -9` mid-write loses nothing; concurrent spool proven |
| E5 | Derivation (+ decision graph behind its gate) | Every node has a `source_ref`; build twice = identical bytes |
| E6 | Dashboard | Runs offline from one command |
| E7 | RC1: full security review, README + graphic, Apache-2.0, private repo | No unresolved findings |

**Process:** per-module code review after each module's commit (skilled reviewer agents on
distinct lenses, findings adversarially verified). One full security review at RC1. Claude and
Gemini pair-review each other's modules; neither merges the other's code unreviewed.

### E1 was signed off once before it was true

The first E1 sign-off claimed both halves of the gate and held neither:

- **"Every §2.2 trap is a passing test."** Two traps — per-session rollup
  including subagents, and cost estimated from a dated price snapshot — had
  neither a test nor an implementation. Both now exist (`session_files`,
  `rollup_usage`, `estimate_cost`, `PRICES_AS_OF`).
- **"Determinism test green."** The test compared `to_canonical()` against
  itself inside one interpreter, sharing interned strings, dict ordering and
  one `PYTHONHASHSEED`. It is now run in a fresh process under two different
  seeds, which is where a rebuild actually happens.

The standalone review round that caught this ran 42 agents over six lenses and
returned 65 finding-sets; 35 of 36 adversarial verdicts were confirmed by
execution, one refuted. The load-bearing finding was that **the conformance
suite could not fail**: an adapter whose `parse` returned an empty Session
passed all 162 corpus cases, because every rule quantified over records the
mutation had already removed. `tests/test_conformance_can_fail.py` is the
standing answer — 14 mutations, each one the old suite missed.

The lesson is recorded rather than tidied away: a gate checked only by the code
that has to pass it is not a gate, and a passing suite is evidence about the
suite until something has tried to break it.

---

## 5. Scope cuts taken

- Dropped: "avoided rediscovery" frugality estimate.
- Dropped: whole-file snapshots and the zstd CAS.
- Gated (may never ship): decision extraction, behind precision ≥ 0.85.
- Deferred: GLiNER entity/relation extraction — not needed to search a transcript.
- Kept against reviewer advice, because the owner named them: the injector hook (off by default),
  and remote push for the memory store (opt-in, redaction-gated).

**Budget: 400–700 LOC of glue. Past 1,500 we are rebuilding something on the do-not-build list.**

---

## 6. E0 honesty gate — result and recorded dissent

Four tools read at code level (clones under `/tmp/gm-e0/`), then a three-lens panel
(`kill` / `build` / `neutral`) ruled independently. **2 BUILD / 1 PR_TO_fable.**

| Tool | License | Retains raw bytes? | git substrate? | Contiguity proof? | No LLM in auto path? |
|---|---|---|---|---|---|
| agentcairn | Apache-2.0 | **No** — redacts *before* hashing | No (19 incidental `git` calls) | No (`doctor` = index-vs-index) | No — `CAIRN_JUDGE=anthropic` names files |
| fable | MIT | Per-record, but re-assembled `ORDER BY ts_epoch,lineno` | **No** — stated non-goal | No — `fat.verify()` compares fat to its own backup | No — 15s unattended loop |
| continuity-v2 | MIT | No — truncates `tool_result` to 500 chars | No — `.gitignore:11` *"NEVER commit JSONLs"* | No — mtime vs mtime | — (dormant since 2026-06-14) |
| claude-code-log | MIT | **No** — stores `zlib(json(model_dump()))`, `extra='ignore'` | Read-only, 2 call sites | Closest: `row_fingerprints` prefix proof — but over model dumps, in a disposable cache, never committed | **Yes** (by having no LLM) |

**The dissent, kept because it is the strongest argument against this project:** fable already has
the PreCompact hook, byte-exact span reads, an flock'd append-only per-session file, 499 tests, MIT,
no CLA. Its `fat.verify()` self-reference is a genuine bug we could fix as a PR. *"The whole project
is the PR."*

**Why it was not followed:** fable's fat is a re-assembly, not a copy — records are appended in
index order with `if not b.endswith(b"\n"): b += b"\n"`. Reproducing a source file's `file_sha256`
by `cat`-ing stored bytes is therefore not unimplemented in fable, it is *unrepresentable*. And
fable's pruner rewrites the user's transcripts in place (§2.5a). Inverting which layer is
authoritative, in someone else's repo, is not a PR.

**Reciprocity owed upstream** (not blocking, do it at RC1): report `fat.verify()`'s self-reference
to fable, and agentcairn's `cairn doctor --transcripts` gap. We are taking their code; we file what
we found.
