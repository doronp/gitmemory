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

```
gitmemory capture ~/.claude/projects/<proj>/<session>.jsonl
gitmemory verify
```

`verify` returns an empty list or a list of problems. There is no third answer.

## Status

Under construction, epoch by epoch. E2 (the segment store and its proof) is
complete: 487 tests, and every check in `verify` has a test that fails when
that check is deleted. Index, derivation, hooks, and the dashboard follow.

See `docs/DESIGN.md` for the locked decisions and `docs/reviews/` for the
review record.

## Licence

Apache-2.0. Third-party code and its licences are listed in `THIRD_PARTY.md`.
