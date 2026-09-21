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

- **Hook (latency, best-effort).** `PreCompact` + `SessionEnd` + `Stop`. 112 lines of POSIX sh,
  53 of them not comments. **[E4, review: docs 9 — the estimate was "~20 lines" and it stayed in
  the document while the file grew. What it grew for is in the file: every guard has a comment
  naming the failure it was written against.]**
  Reads hook JSON on stdin, writes **one file** to `spool/`, `exit 0`. No git, no Python, no
  network on the hot path. Target <50 ms; **p50/p99 measured and published** — 7.43 ms and
  10.48 ms, in `hook/README.md`, against a control that times spawning `true` the same way.
  **[E4, review: docs — this said "Budget is 10 s", which is not the budget for any event this
  shim binds. Claude Code gives a `command` hook 600 s by default, and `SessionEnd` hooks *share a
  1.5-second budget* (code.claude.com/docs/en/hooks, read 2026-09-21). 1.5 s shared is the binding
  constraint and it is 6.7× tighter than the number we published, so the figure was not just
  unsourced, it was slack in the wrong direction — the shim's 10.48 ms p99 is 0.7% of a budget it
  does not get to itself. It also said "which no tool in the field does", which is a claim about
  every tool that exists and cannot be checked. Narrowed to what §6 actually read: none of the four
  publishes percentiles for the hook it puts in the agent's critical path. Two publish *retrieval*
  latency — `ccf/agentcairn@8fd534b:benchmarks/` and `grooverLab/fable@c6d8c86:scripts/benchmark.py`
  — and neither harness mentions the hook. That is a different path, off the one the user waits
  on.]**
- **Watcher (the guarantee).** Tails transcript dirs by `(path, inode, byte-offset, mtime)`.
  Append-only makes this cheap and exact. Catches crashed sessions, non-compacting sessions, and
  sessions where the hook was never installed.

If the hook is never installed the system is still correct. That is what makes it safe to ship.

**Spool concurrency [R1 raised, C resolved]:** each hook invocation writes its **own** file
`spool/<pid>-<event>[-<n>].json`. No shared file → no torn writes → no `flock` to get wrong.
Fewer moving parts than a lock. **[E4, review: docs 2 — this said `<epoch_ns>-<pid>.json`. The
leading timestamp was removed in E4: no reader ever read it, it cost a third `date` fork on the
one path a user waits for, and a clock set before 1970 made it negative and shifted every
field. `<n>` is the collision suffix when one pid fires the same event twice.]**

**Hook/watcher double-write [R1 raised, C resolved]:** both derive the same next offset from the
same committed state, so the loser writes an identical segment or none. Idempotent by
construction; no lock needed for correctness.

### 2.4 Storage — append-only segments **[C — the central correction]**

v0 committed whole-file snapshots; Gemini correctly smelled pack explosion. Its proposed fix
(content-addressed whole-file blobs outside git) is **worse**: CAS writes a new immutable blob
each time the file grows, and if those blobs are zstd-compressed git cannot delta them at all —
turning O(N) storage into O(N²). **Gemini verified and conceded this in R2.**

JSONL is append-only. So store **segments**, not files:

**[E4, review: docs 5 — the diagram below is a `find` over a real store. The one it replaces had
a `<date>` level nothing writes, no `g<NN>/` level, a `manifest.json`, and three files that do
not exist.]**

```
$GITMEMORY_HOME/                      # default ~/.gitmemory
  raw/<agent>/<session_id>/g<NN>/
      000000000000-000000131072.jsonl     # bytes [0, 131072) of generation NN
      000000131072-000000164000.jsonl     # bytes [131072, 164000)
  sessions/<agent>/<session_id>/
      g00.json                             # canonical JSON + contiguity proof, one per generation
      g01.json                             # written only when the source diverges — see §2.5a
  index/                                   # .gitignored — SQLite is not diffable
  spool/                                   # .gitignored — one file per hook fire
  .locks/                                  # .gitignored — one per session, capture serialisation
  config.toml
```

`derived/` is E5 and does not exist yet; §2.6 describes what will land there.

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

The `derived/` boundary acts on the **HIGH tier only** — the nine issuer-anchored shapes, not the
three SUSPECT ones. A sentence carrying a high-confidence key is dropped before ranking and
counted in `sentences_redacted`; `derive._write` then refuses outright to publish a payload that
still contains one, because an artifact reaching that door with a key in it is a bug upstream, not
a sentence to quietly lose. SUSPECT is excluded deliberately: it is `password: "..."`-shaped and
fires on prose *about* configuration, so acting on it would gut legitimate content to suppress a
shape the raw copy carries anyway. **`raw/` stays unredacted by design; the push gate is what
holds it** **[E5:3]**.

`gc.auto` is configured and the daemon periodically runs `git gc --auto`: `raw/` scales O(N),
but `derived/` is rewritten on every rebuild and orphans blobs **[R2]**. A generation that fails
mid-rebuild is *removed*, not left behind: a stale artifact beside a skip line is a `derived/`
tree that has quietly stopped being a function of the committed bytes, and `git diff` shows clean
while it happens **[E5:2, E5:7]**.

### 2.5 The contiguity proof

A manifest is `sessions/<agent>/<session_id>/g<NN>.json` — one per generation, `g00` first. It
carries eleven fields **[E4, review: docs 1/4 — "exactly six" and `manifest.json` were both
wrong, and they were wrong in the recipe below, which is the product's central claim]**:

```
schema, session_id, agent, generation, size, source_path,
file_sha256, prev_manifest_sha256, diverged_from,
segments: [{path, start, end, sha256}, ...],
compact_boundaries: [byte_offset, ...]
```

`segments[].path` is relative to `$GITMEMORY_HOME`, and `compact_boundaries` holds byte offsets
into this generation, not `seq` numbers.

**Verification, by a stranger with only the repo and no access to this machine.** Run from the
root of the checkout; this is copy-pasteable and was run to write it down:

```sh
M=sessions/claude-code/s-403bcc5f6f7814d3/g00.json          # any manifest

# 1. the bytes are the bytes
jq -r '.segments|sort_by(.start)|.[].path' "$M" | tr '\n' '\0' | xargs -0 cat | shasum -a 256
jq -r .file_sha256 "$M"                                      # the two must match

# 2. the segments tile [0, size) with no hole and no overlap
jq -e '. as $m
       | ($m.segments|sort_by(.start)
          | reduce .[] as $s (0; if $s.start == . then $s.end else null end)) == $m.size' "$M"
```

Zero LLM calls, zero Python, zero host dependency. `gitmemory verify` does the same over every
manifest in the store and exits non-zero on any hole.

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

This is not hypothetical. Verified at `grooverLab/fable@c6d8c86:docs/ARCHITECTURE.md:18` — *"The live
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

| Artifact | Method | Deterministic | State |
|---|---|---|---|
| Key ideas | `sumy` LexRank over prose blocks, each with its `block_id` | yes | shipped, E5 |
| Timeline | fold over `Event` + `Turn`, with a tail so every turn is in a span | yes | shipped, E5 |
| Decision graph | structural → **graphify** (`build_from_json` → cluster → export) | yes, **gated** | E5, gated |
| Titles/labels | optional local model at an **explicit gate only**, named in config | no — marked | not built |

**[E5 — the `Key phrases` row is gone.** It named `KeyBERT` + a Model2Vec backend. KeyBERT
pulls torch and transformers whatever backend it is handed: 42 packages, five of them heavy,
for a keyphrase list nothing downstream reads. Deleted rather than deferred, because a row in
this table is a promise. `docs/tasks/E5-dependency-verification.md` has the closure counts.
The declared extra is `sumy>=0.13, numpy>=2.0, graphifyy>=0.9.65` — `numpy` because LexRank
hard-requires it and the old table did not say so, and `networkx` is graphify's to pin, not
ours.**]**

**[E4, review: docs 10 — the row above used to name `qwen3.5:35b` and quote "46 tok/s measured". No such model exists; nothing in this repository measured it. A fabricated number in the table about *not trusting generated text* is the worst place in the document for one, so the model is now config and the number is gone. E5 names whatever is actually run and publishes what it actually measures.]**

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

#### The graph layer is graphify, not ours **[E2]**

The original plan — `networkx` plus a hand-rolled transitive reduction plus a `D2`/`dot`
emitter — was going to reimplement a tool that already exists under a compatible licence.
Verified before deciding, not assumed:

| Question | Answer |
|---|---|
| Licence | Apache-2.0 (`LICENSE`, `license = "Apache-2.0"`), inbound-compatible with ours |
| Distribution | PyPI `graphifyy` 0.9.53 at E2; **0.9.65 installed and re-checked at E5**, which is what the `derive` extra pins |
| Runtime deps | `networkx`, `numpy`, `rapidfuzz`, tree-sitter grammars — no service, no key. `openai`/`anthropic`/`boto3` exist only as extras we do not take |
| LLM needed? | No, for the path we use. Structural extraction is tree-sitter AST |
| Deterministic? | Yes. `cluster.py` pins `seed=42` / `random_seed=42` / `randomness=0.001` and explicitly stabilises for Louvain's order-sensitivity (`cluster.py:117`) |
| Input seam | `build_from_json(extraction, *, directed, root)` takes a plain nodes/edges dict |

The seam's field names were read off the installed `validate.py` rather than inferred, because
the emitter has to satisfy them exactly: a node is `{id, label, file_type, source_file}` and an
edge is `{source, target, relation, confidence, source_file}`. Two of those vocabularies happen
to be the ones we needed anyway, which is the second reason this fits:

- `file_type` admits **`rationale`** alongside the file-ish kinds, so a decision node does not
  have to masquerade as a document.
- `confidence` is **`EXTRACTED` | `INFERRED` | `AMBIGUOUS`**, which is the distinction this
  project already refuses to blur. A node lifted verbatim from a block is `EXTRACTED`; an edge
  asserting that one decision supersedes an earlier one is `INFERRED`, because nothing in the
  bytes says so and the reader is entitled to know which is which.

`source_file` is where the `source_ref` travels, so the provenance rule above survives the
hand-off instead of stopping at our boundary. **[E5]**

That last row is what makes it fit rather than merely adjacent. graphify's own corpus model
is *files*; ours is *decisions inside transcripts*. We do not point it at a directory. We
derive decision nodes ourselves — deterministically, each with its `source_ref`, behind the
precision gate above — emit them as an extraction dict, and hand that to `build_from_json`.
graphify supplies the graph, the community detection, the HTML and the GraphRAG JSON;
gitmemory supplies the truth claims and stays answerable for them.

What it does **not** give us, and what the gate is therefore still about: the extraction
itself. Deciding that a span of transcript *is* a decision, with a rejected alternative, is
the part that can be wrong, and no graph library can be right on our behalf.

Not wired in yet — this records the decision and its evidence at E2. The attribution goes in
THIRD_PARTY.md when the first line of code depends on it, not before.

### 2.7 Retrieval

SQLite FTS5 with **separate columns** and per-column `bm25()` weights. Flat indexing is a known
failure: tool output is the bulk of a transcript and the least of its signal, so flat BM25 returns
pasted logs.

A dense arm (Model2Vec, RRF at k=60) and a rerank arm (FlashRank on top-50) are planned, shipped as
the **optional `hybrid` extra**, and **have never been run** — `docs/benchmarks/E3-longmemeval.md`
records both as *"arm not available"* against the 470-instance gate. Until they run, FTS5 alone is
the measured system and they are a hypothesis.

**[E4, review: docs — this paragraph used to give the bulk as "~79% of text volume" and to settle
ANN with "at ~144 k vectors numpy brute force is 5.8 ms and *exact*". Neither number had a source
and neither is reachable from here: the 79% was an E0 estimate over Claude Code transcripts, and the
only ones on this machine are the operator's, which this project does not read; the 5.8 ms describes
a vector search that does not exist, in a dependency the environment does not install. The measured
sibling that does survive is §2.2's — base64 image payloads were **61.7%** of all indexed text — and
it is the one that justifies projecting rather than inlining. The column split stands on its own
argument, which never needed a percentage: whatever the exact share, tool output is bulky and
low-signal and a flat index drowns in it. `bench/` is where the share and the arms both get
settled.]**

**Built with four columns, not three [E3].** This section originally named `prose`,
`tool_result`, `paths`. The implementation adds **`tool_use`** as its own column, weighted
above `tool_result`: `prose` 4.0 · `paths` 3.0 · `tool_use` 2.0 · `tool_result` 1.0. A tool
*call* is short and high-signal — `Read(file_path=…)`, `Bash(command=…)` — and sharing a column
with bulk tool *output* buries it under exactly the volume the column split was invented to
separate. Two tests fail if the split is collapsed: `test_a_tool_argument_outranks_tool_output`
and `test_weights_are_what_decides_the_order`.

The index carries **no dependency**: FTS5 ships with CPython. The `hybrid` extra
(`model2vec`, `numpy`, `flashrank`) exists so `bench/` can score the dense and rerank arms
*against* BM25 — a dependency each of them has to earn on the benchmark, not one the index
takes on faith.

**Four things the implementation settled that this section did not say [E3]:**

- **A hit is scored per block, returned per turn**, and the partition is
  `(turn_id, agent, session_id)` with `ORDER BY score, generation DESC, block_seq`. Each half of
  that is load-bearing. `MIN()` with bare columns does pick the winning row but does not say
  *which* winning row on a tie, so the representative followed insert order and the same content
  indexed differently answered differently. Partitioning on `turn_id` alone collapsed a turn in
  session A with a verbatim-identical turn in session B and silently under-delivered `k` —
  `turn_id` is derived over the record's `sessionId`, which §2.2 records as reused across a fork.
  Generations of *one* session do collapse, newest first, because a pruner rewrite copies a turn
  forward unchanged and returning it per generation spends the budget on one text.
- **`Hit.byte_offset` addresses the generation, not a file.** The parser is handed the
  concatenation of a generation's segments, so that is what every offset in the system means.
  `store.span(stored, offset, length)` is the resolver, and deliberately *not* a
  `(path, offset)` pair: no such pair exists for a turn the store cut in half, and seeking with
  one returns whatever the next segment begins with — plausible bytes from the wrong place.
  While a generation is still one segment the two coincide, which is why every single-segment
  test passes either way.
- **`_PATH` is bounded per component, not just possessive.** The `paths` column is fed from
  `native["input"]` — a tool call the *model* wrote — so the regex runs on attacker-choosable
  text. A possessive `+` stops backtracking *within* a component; what caps the work per start
  position is the explicit `{1,255}` bound. Unbounded, a long run of path characters is a
  quadratic scan on the index path.
- **The derived copy is sanitised; raw is not.** `jsonl` decodes with `surrogateescape` and
  `records` hashes with `surrogatepass`, so a transcript that caught a binary `cat` keeps those
  bytes exactly. sqlite3 encodes strict UTF-8 and raised from inside the row loop, so one bad
  byte cost the whole store its index. `_encodable` replaces on the way *into* the index only —
  the loss is in the derived copy, and raw, which is what the offsets point at, still has it.

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

**As built [E6, reconciled after the review].** The paragraphs above are the design; this is
what exists, because a design document that describes panels nobody built is a second thing to
verify claims against. Seven SQL views *inside the database*, so `sqlite3` and the next
dashboard get the same definitions: `dash_corpus` · `dash_growth` · `dash_contiguity` ·
`dash_requests` · `dash_unbilled` · `dash_spend` · `dash_unmeasured`. Deviations:

- **Quality and Health are not built.** Quality needs the three retriever arms the environment
  cannot install (§3) — a panel reading one arm would claim a comparison that has not happened.
  Health needs daemon liveness and hook latency, which are not in the index.
- **No dollar figure, deliberately.** "Estimated $ + price-snapshot date" above would make this
  repository a price list with an expiry date nobody watches, and a stale one is worse than
  none. Tokens are what we measure; the multiplication is the reader's.
- **Injection cost is not built either** — `dash_unmeasured` carries it as a row reading
  `NOT BUILT`, alongside net-saving-vs-no-memory and the two invisible tails. The frugality
  refusals ship as rows on a page rather than as prose in this file.
- **`dash_unbilled` is new** and has no paragraph above. `dash_spend` bills one turn per
  `(agent, request_id)`; everything with usage that it excludes — a non-assistant turn, a
  `<synthetic>` model, a missing request id — is itemised there, so the spend panel is an
  admitted floor with its remainder beside it rather than a number that quietly disagrees
  with `billable_usage`.

---

## 3. Evaluation

Public + synthetic only. **Never this machine's history.**

- **LongMemEval-S** — primary, via **`xiaowu0162/longmemeval-cleaned`** (MIT), file
  `longmemeval_s_cleaned.json`, pinned at revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`,
  sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`, 277,383,467 bytes.
  The original release is deprecated in favour of the cleaned one; `bench/fetch_longmemeval.sh`
  pins the revision and verifies the hash before the file is usable, so a wrong constant fails
  closed rather than silently scoring against the wrong corpus.
  Mutation: replay through a synthetic Claude-Code-shaped transcript
  generator so the *input* is our real format, then inject synthetic compaction boundaries at
  controlled positions to measure **recall across the compaction wall** — which nobody else measures.
- **Not LoCoMo** — CC-BY-NC-4.0, 99 documented ground-truth errors, unmaintained since 2024-08.
- **`thedotmack/membench` (MIT) ablation design**, borrowed: 4 arms (candidate / none / shuffled /
  reference), item-paired, Bonferroni-corrected, calibration gate. The `none` arm is a floor **by
  construction** (it returns nothing), so the comparison against it is one-sample, not paired.
  The `reference` oracle must score exactly 1.00 recall and 1.00 MRR in every compaction mode —
  that is the check that the generator's ground-truth offsets and the adapter's turn boundaries
  agree, and a harness that fails it is measuring its own bugs [E3, verified].
- **Arm seam:** `retrieve_factory(instance, transcript_bytes) -> retrieve(query, k) -> [byte offset]`.
  A factory rather than a bare `retrieve`, because each of the 500 instances has its own synthetic
  transcript and a pre-built retriever cannot know which one a query refers to [E3].
- **Retriever arms:** BM25 · Model2Vec · RRF hybrid · RRF+FlashRank. Public evidence is genuinely
  contradictory (BM25 *beats* bge-base on LongMemEval knowledge_update 88.0 vs 81.3, loses on
  MembBench 39.45 vs 60.29), so we measure rather than cite. **Of those four, only BM25 has been
  measured**: the E3 gate ran 470 instances against FTS5 and skipped the other three, which need the
  `hybrid` **extra** the environment does not install — the report says so, in the arm list, as
  *"arm not available"*. Planning to measure is not measuring, and this bullet read as though the
  comparison had happened. [E4, review: docs — the unrun arms]
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
| E3 | ~~Index + retrieval + CLI~~ **PASSED 2026-09-20** | Benchmark arms scored on mutated LongMemEval-S — 470 instances, [gate report](benchmarks/E3-longmemeval.md) |
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

Four tools read at code level, then a three-lens panel (`kill` / `build` / `neutral`) ruled
independently. **2 BUILD / 1 PR_TO_fable.**

Read at these commits, so every claim in the table below is fetchable — the clones were scratch and
the scratch is gone, which is why the citations are not paths. **[E4, review: docs — E0 citations]**

| Tool | Read at |
|---|---|
| agentcairn | `ccf/agentcairn@8fd534b` |
| fable | `grooverLab/fable@c6d8c86` |
| continuity-v2 | `Haustorium12/continuity-v2@4e98d46` |
| claude-code-log | `daaain/claude-code-log@6ad029e` |

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
