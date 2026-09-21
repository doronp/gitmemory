# gitmemory

Git-versioned, contiguity-checked memory for coding agents.

An agent transcript is an append-only byte stream that nothing durably keeps.
Compaction discards it, pruners rewrite it in place, and what the agent
remembers afterwards is a summary nobody can check against the thing it
summarised. gitmemory keeps the source instead: raw bytes, copied out at every
compaction, tiled by byte offset so the record either proves it is contiguous
or names the hole.

Everything derived — indexes, decision graphs, key ideas — is rebuilt from
those bytes, so a derivation is never the only copy of anything.

**Contiguous, not complete.** The proof is that the captured bytes tile
`[0, size)` with no hole and no overlap and hash to a recorded digest. It is
*not* a proof that the agent wrote everything it generated: a process killed
before its flush leaves a stream that is contiguous and short, and nothing on
disk can tell you about bytes that never reached the disk. The design document
has said this limitation is stated here since v0; until E4 it was not.

```
gitmemory watch                 # tail the configured roots, capture, commit
gitmemory capture <transcript>  # or copy out one file's new bytes, by hand
gitmemory verify                # prove the bytes tile the range they claim
gitmemory index                 # rebuild the retrieval index from the store
gitmemory recall "the question" # search it
```

`verify` returns an empty list or a list of problems. There is no third answer.

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
spool and exits 0; its cost in the agent's critical path is measured, not
asserted.

## Status

Under construction, epoch by epoch. 893 tests, plus one gated on the
LongMemEval download (`-m corpus`).

| | | |
|---|---|---|
| E1 | Canonical records + Claude Code adapter | passed |
| E2 | Segment store, contiguity proof, `verify`, redaction gate | passed |
| E3 | Index + retrieval + CLI | passed — 470 LongMemEval instances, [gate report](docs/benchmarks/E3-longmemeval.md) |
| E4 | Hook shim + watcher + git daemon | in review |
| E5 | Derivation and decision graph | extractor passed — 1.0000/1.0000 on a blind split, [gate report](docs/benchmarks/E5-decision-gate.md); graph not wired |
| E6 | Dashboard | |
| E7 | RC1: security review, private repo | |

**Read the E5 number carefully.** 1.0000 precision and recall is the score on a
held-out split the extractor's author could not see, and it is the gate that was
pre-registered before anything was measured. It is *not* evidence the extractor
reads English: a probe in a third vocabulary, written by the reviewer, scores the
same code **23/32**. The corpus is two samples of the language, and passing on
the second one is the weaker claim of the two. Both numbers are in the report.

The discipline the test count does not show: every fix is pinned by a negative
control — mutate the module to remove the behaviour, confirm the named test
fails — because a fix nothing distinguishes is a fix nothing is holding in
place. Three of this epoch's fixes were refuted that way and rewritten.

See `docs/DESIGN.md` for the locked decisions and `docs/reviews/` for the
review record, including the findings that were disputed and why.

## Licence

Apache-2.0. Third-party code and its licences are listed in `THIRD_PARTY.md`.
