# E4 sign-off

The [standalone review](E4-standalone-review.md) closed its fix list at
`f409ff8` but recorded no sign-off, because the round's own rule is that a
sign-off follows a clean pass, not a closed list. This is that pass, run on
2026-09-28 as part of the pre-release review, and the sign-off it supports.

## What was run

One independent review agent, told to find defects and not fix them, ran the hook
shim, the watcher and the git/store capture path end to end in a throwaway
directory, with `GITMEMORY_HOME` and `HOME` both pointed inside it, on a
synthetic Claude Code transcript. An adversarial verifier then tried to refute
each finding.

| Scenario | Result |
|---|---|
| First capture, then appends with a `compact_boundary` | Two contiguous segments; the recorded boundary is the byte offset of the boundary line; `verify` clean. Without `--interval 0` the second pass waits for the interval, as documented |
| `index`, `recall` | The right turns, with byte offsets |
| Prefix rewrite, truncation, truncation to 0 B | New generations g01, g02, g03 with `diverged_from`; g00 intact; `verify` clean throughout |
| Continuous watcher, `kill -9` at a random point, five rounds | No leftover `.incoming` or manifest temps, no `index.lock`, `verify` clean each time; after restart the segments' concatenation equals the source byte for byte (16,545,729 B, 47 segments) |
| Hook shim, `PreCompact` payload | Spool record written with mode 0600 and drained by the next pass even inside the interval; a `Stop` record drained without forcing a capture; a record naming a path outside every watch root dropped with a message |
| Symlinks | A link out of the root is never captured; a link to an in-root file is deduplicated; `capture <link>` is refused |
| Store hygiene | `config.toml`, `spool/`, `.locks/`, `index/` gitignored; `hooksPath=/dev/null`, signing off |
| `pytest -k "daemon or hook or watch or store or gitrepo"` | 412 passed, 1 skipped |

## Findings

One, low: a symlinked transcript printed "parse failed … capturing bytes without
boundaries" just before the store refused it. The verifier reproduced it. It is
fixed (the parse is skipped for a symlink) with a test that failed before the fix
(`test_a_symlinked_transcript_is_refused_without_a_parse_failure_line`).

No critical, high or medium finding.

## Sign-off

E4 is signed off: every scenario behaved as [watching.md](../watching.md) and
[hook/README.md](../../hook/README.md) say, and the only finding is fixed and
tested.
