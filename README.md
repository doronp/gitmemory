<h1 align="center">gitmemory</h1>

<p align="center"><strong>Git-versioned, contiguity-checked memory for coding agents.</strong></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/contiguity-dark.svg">
    <img src="docs/assets/contiguity-light.svg" width="900"
         alt="A transcript's byte range tiled by three captured segments with no hole and no overlap, hashed to a recorded digest, committed to git, with derived artifacts rebuilt from those bytes.">
  </picture>
</p>

---

## The number this is built on

A coding agent's transcript is an append-only byte stream that nothing durably
keeps. Compaction discards it. So the case that matters is the one where the
evidence is *behind* the compaction boundary — where the live context window
has genuinely lost it.

On 470 LongMemEval instances, k = 10, seed 42, under that arrangement:

| Arm | Turn recall | Turn MRR |
|---|---|---|
| **gitmemory's index** | **0.7456** | **0.6209** |
| The live context window | 0.0000 | 0.0000 |

The zero is not a weak baseline. The bytes are gone; the window cannot reach
them at any k.

**Now the caveat, because it is the same size as the result.** Under the
*opposite* arrangement — evidence ahead of the boundary — the live window
**beats** the index, 0.7893 to 0.7456, because walling off the past also walls
off the distractors. The reported mode was pre-registered before anything was
measured, for exactly this reason. A benchmark that picked its own mode would
have published the 0.7893 as a gitmemory number.
[Full gate report](docs/benchmarks/E3-longmemeval.md) — four compaction modes,
fourteen calibration gates, every arm including the ones that lost.

## What it keeps

The raw bytes, copied out at every compaction, tiled by byte offset so the
record either **proves it is contiguous or names the hole**. Everything
derived — the index, decision graphs, key ideas — is rebuilt from those bytes,
so a derivation is never the only copy of anything.

**Contiguous, not complete.** The proof is that the captured bytes tile
`[0, size)` with no hole and no overlap and hash to a recorded digest. It is
*not* a proof that the agent wrote everything it generated: a process killed
before its flush leaves a stream that is contiguous and short, and nothing on
disk can tell you about bytes that never reached the disk.

```
gitmemory watch                 # tail the configured roots, capture, commit
gitmemory capture <transcript>  # or copy out one file's new bytes, by hand
gitmemory verify                # prove the bytes tile the range they claim
gitmemory index                 # rebuild the retrieval index from the store
gitmemory recall "the question" # search it
gitmemory dashboard             # serve the index, read-only, on loopback, behind a sign-in
```

`verify` returns an empty list or a list of problems. There is no third answer.

## The measurement board

| What | How it was measured | Result |
|---|---|---|
| Retrieval across a compaction boundary | 470 LongMemEval instances, 4 compaction modes, 14 calibration gates | **0.7456** turn recall, against **0.0000** for the live window |
| Decision extraction | held-out split, gate pre-registered at precision ≥ 0.85 / recall ≥ 0.60 | **1.0000 / 1.0000** — and see below |
| The same extractor, in a vocabulary its corpus does not contain | two adversarial probes hand-written by a reviewer, both since spent | 26/32 and 27/32 |
| The same extractor again, on a probe written blind and scored once | 32 items in a domain chosen to share no vocabulary with the corpus | **14/32** |
| Hook cost in the agent's critical path | timed against spawning `true` the same way | p50 **7.43 ms**, p99 **10.48 ms** |
| The suite | | **1034 tests**, plus one gated on the LongMemEval download and one on `pip install -e '.[serve]'` |
| Whether the tests hold anything | every fix mutated to remove the behaviour, the named test must fail | **340** negative controls |

**Read the extraction numbers carefully — the gap between them is the finding.**
1.0000 precision and recall is the score on a held-out split the extractor's
author could not see, against a bar pre-registered before anything was measured.
It is not evidence the extractor reads English. The gate has now read 1.0000
through five rounds of real defects, every one of which it scored identically
with and without.

Probe C is what that gate cannot see. It was written by an agent that read only
this README and the design document — never the extractor, never the other
probes — in a domain (theatrical show control) chosen so that lexical overlap
with the corpus is impossible. It was frozen unscored and scored once. **14 of
32**, against 26 and 27 for the two earlier probes, which are spent because each
set the brief for a revision and then graded it.

The 18 misses are two facts, and they point in opposite directions:

- **10 of 10 on the items that are not decisions** — no false positive anywhere
  in a vocabulary the guards have never seen, including three imperative work
  requests and two sentences carrying *must* about somebody else's rule. The
  conservatism is real and it generalises.
- **4 of 22 on the items that are.** All eleven reversals were missed, and all
  eleven were the *user* reversing their own earlier instruction — a ceiling the
  extractor declares in a source comment and nowhere a reader of these documents
  could find it. Seven of eleven directives were missed, all plain positive
  standing rules ("Patch file stays YAML.") with no prohibition to match on.

The extractor was **not** changed in response, because editing it against C's
own miss list would turn the one unspent measurement into a fourth piece of
training data. [The full record](docs/benchmarks/E5-probe-C.md).

## What is *not* measured, said on the dashboard itself

A memory system that reports only its wins is a marketing surface. These ship
as rows on the page, not as omissions:

- **Net saving versus no memory — UNMEASURED.** No "tokens saved" tile exists,
  and none will before the A/B harness runs.
- **Injection cost — NOT BUILT.** It is a *cost*, it would be shown first, and
  it does not exist yet.
- **Whether an injected memory influenced an answer — not observable.**
  Attention leaves no trace. The panel can say REFERENCED; it cannot say USED.
- **Both tails are invisible.** A prevented dead-end is an unbounded unmeasured
  saving; a stale memory that misled is an unbounded unmeasured cost. Any single
  frugality number assumes both are zero.
- **Three of the four retriever arms have never run** — only BM25/FTS5 was
  measured. The gate report says so in the arm list.

## Getting started

Watch roots have no default. Name what to tail in
`$GITMEMORY_HOME/config.toml` (default `~/.gitmemory`):

```toml
[[watch]]
agent = "claude-code"
roots = ["~/.claude/projects"]
```

then run `gitmemory watch`. That is the whole install — see
[docs/watching.md](docs/watching.md) for the flags, the roots that get refused,
and how to tell a misconfigured watcher from an idle one.

The [hook shim](hook/README.md) is optional and makes a capture happen at the
compaction boundary rather than at the next sweep. **If you never install it
the system is still correct.** It is a POSIX `sh` script that writes stdin to a
spool and exits 0.

Claude Code is the first adapter, not the only intended one; the records are
agent-agnostic by construction.

## If a credential lands in the store

It will. A transcript is a recording of a terminal, and terminals print tokens.
The design answer is that `raw/` stays on your machine and the gate stands at
`push`; the operational answer is shorter:

1. **Rotate the credential.** Do this first and do not wait for anything below.
   The store is append-only and local, so the blob is in your history whatever
   you do next, and a rotated key is worth nothing to anyone holding it.
2. **`gitmemory push` will refuse, and that is working.** It scans the files git
   would ship, the segment seams, *and* the object graph a push transmits —
   deleting the file does not make the push clean, because `git push` does not
   send the working tree.
3. **If you must un-say it,** the store is an ordinary git repository, so the
   ordinary history-rewriting tools apply — `git filter-repo` is the usual one,
   and it is not vendored here. Run `gitmemory verify` afterwards: it will tell
   you whether the segments still tile. This is not automated and will not be —
   a memory system that silently edits its own history is not one you can quote
   from.

There is no override flag. A gate you can wave through on a deadline is a gate
that gets waved through on a deadline.

## Status

Under construction, epoch by epoch.

| | | |
|---|---|---|
| E1 | Canonical records + Claude Code adapter | passed |
| E2 | Segment store, contiguity proof, `verify`, redaction gate | passed |
| E3 | Index + retrieval + CLI | passed — [gate report](docs/benchmarks/E3-longmemeval.md) |
| E4 | Hook shim + watcher + git daemon | in review |
| E5 | Derivation and decision graph | passed — [gate report](docs/benchmarks/E5-decision-gate.md); three artifacts per generation. Reopened by [probe C](docs/benchmarks/E5-probe-C.md): user-reverses-own-instruction is 0 of 11 |
| E6 | Dashboard | passed — seven views over the index, served read-only on loopback, [review record](docs/reviews/E6-standalone-review.md) |
| E7 | RC1: security review, private repo | in progress |

## How this is built

Every module gets a standalone review round by an agent that did not write it,
in its own worktree, and every finding is re-derived by a route the reviewer did
not use before it is accepted — because a reviewer who is right for the wrong
reason is still being graded. Seven of the last round's tests were refuted that
way and rewritten: three the reviewer caught, four found while writing the tests
that answer them.

`docs/DESIGN.md` holds the locked decisions. `docs/reviews/` holds the record,
including the findings that were disputed and why, and the ones whose
demonstration was wrong while their conclusion held.

## Licence

Apache-2.0. Third-party code and its licences are listed in `THIRD_PARTY.md`.
