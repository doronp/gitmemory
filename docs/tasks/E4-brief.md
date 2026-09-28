# E4 brief — hook shim, watcher, git daemon

**Ship gate (DESIGN.md §4):** hook p50/p99 measured and published · `kill -9`
mid-write loses nothing · concurrent spool proven.

## The seam, fixed before anyone writes code

Two halves meet at one directory and one filename convention. Neither half may
change it without the other.

```
$GITMEMORY_HOME/spool/
    .tmp-<pid>-<n>            # being written; readers ignore any name starting with a dot
    <pid>-<event>[-<n>].json  # complete; content is the hook's stdin, verbatim
```

- `<event>` is the shim's **argv[1]**, not something parsed out of stdin: one of
  `PreCompact`, `SessionEnd`, `Stop`. The shim matches it against that list and
  substitutes `unknown` for anything else. A hook script that parses JSON in
  POSIX sh is a hook script with an escaping bug.
- The file's **content is stdin byte-for-byte**. The shim constructs no JSON.
- `-<n>` is a collision suffix, appended only if the name already exists.
  Uniqueness is the pid plus that suffix; **there is no timestamp in the name.**
  It carried an `<epoch_s>` prefix at first, on the theory that the spool wanted
  ordering. It does not — the watcher's fold over records is order-independent,
  the arrival time is the file's own mtime, and nothing ever read the field. The
  `date` it cost was one of three forks on the one path a user waits for, worth
  2.0 ms of 7.6, and it was the grammar's only signed field: a clock set before
  1970 made `date +%s` negative and shifted every index in the name. So the
  watcher *finds* the event in the name rather than counting to it, which reads
  both grammars and is what let the prefix go.
- Completion is `rename(2)` from the dot-name. A `kill -9` mid-write leaves a
  `.tmp-` file, which the watcher ignores and sweeps when it is older than an
  hour. That is the whole durability argument for the shim.

**The spool is a doorbell, not data.** It says *something happened to this
transcript*; the watcher re-derives everything else from the source file's bytes.
Nothing downstream may treat a spool record as a source of truth, and no spool
timestamp is ever a sort key (DESIGN.md §2.2: byte position is the only ordering
authority).

## Split

| Half | Files | Owner |
|---|---|---|
| A — the shim and its latency proof | `hook/gitmemory-hook.sh`, `hook/README.md`, `tests/test_hook.py`, `tools/hook_latency.py` | **Gemini** |
| B — watcher, git layer, CLI | `src/gitmemory/daemon.py`, `src/gitmemory/gitrepo.py`, `src/gitmemory/__main__.py`, `tests/test_daemon.py`, `tests/test_gitrepo.py` | **Claude** |

Disjoint file sets, on purpose: the last time two workers shared a tree, one
clobbered the other's edit and the review that followed cited a mutant as
production code.

## Half A — the shim (Gemini)

`hook/gitmemory-hook.sh`, POSIX `sh` (`#!/bin/sh`, no bashisms — it will be run
under `dash` in the test).

Requirements, each of which is a test:

1. **Exits 0 unconditionally.** DESIGN.md §2.2: `PreCompact` cannot inject
   context, only block, and a block-shaped error leaves the conversation
   uncompacted. Full disk, unwritable spool, missing `$GITMEMORY_HOME`, stdin
   closed — all exit 0. The user's session is never collateral.
2. **Touches no interpreter, no git, no network.** `mkdir`, `cat`, `mv`, `date`.
   Nothing else. This is checkable by reading it and the test should read it:
   assert the script body contains no `python`, `git`, `curl`, `nc`, `ssh`.
3. **Writes the payload verbatim.** A test feeds bytes that are not valid UTF-8
   and not valid JSON and asserts the spooled file is identical.
4. **Never writes outside the spool.** `$GITMEMORY_HOME` unset defaults to
   `~/.gitmemory`; the test sets it to a tmp dir and asserts nothing else
   changed anywhere under it.
5. **Concurrent fires do not collide.** The test launches 50 shims at once with
   distinct payloads and asserts 50 distinct complete files with all 50 payloads
   intact and zero `.tmp-` leftovers.
6. **A `kill -9` mid-write leaves no complete file.** Simulate by making the
   shim's temp file a FIFO or by killing between write and rename — however you
   can do it deterministically. What must hold: a partial payload never appears
   under a complete name.
7. **Unknown argv[1] becomes `unknown`** and still spools. A hook we do not
   recognise is still evidence that something happened.
8. **`$GITMEMORY_HOME` containing spaces, and a relative one, both work.**

`tools/hook_latency.py`: runs the shim N times (default 1000) against a realistic
payload, reports p50/p95/p99/max in milliseconds, and prints the machine and OS
it measured on. Budget is 10 s; the target is <50 ms. **Report the number you
measure, including if it misses.** Its output is quoted in the README, so it
prints a table that can be pasted.

`hook/README.md`: what to put in `settings.json` to install it, as a copyable
block, and one paragraph stating that installing it is the user's action — the
project never edits anyone's agent config.

**Hard constraints, unchanged from every prior round:**
- Never read, copy, or reference `~/.claude/projects`, the author's private notes, or any path
  under `$HOME` outside this repo and a tmp dir you create. Fixtures are
  synthetic. If you find code that violates this, that is a BLOCKING finding.
- Do not run `tests/mutate_index.py` — it rewrites source files in place.
- Do not touch any file in half B's list. If you believe one needs changing, say
  so in your report instead of changing it.
- Do not commit. Claude commits after reviewing.
- Every claim in your report carries either a command you ran and its output, or
  the word PLAUSIBLE.

## Half B — watcher and git (Claude)

`gitrepo.py`: `init` (repo-local `user.name`/`user.email`, `gc.auto`, the store's
own `.gitignore` for `index/` and `spool/` — E3 left `index.py`'s "is gitignored"
comment a lie), `commit`, `gc`. Every call is explicit `git -C <home>`; nothing
inherits cwd (DESIGN.md §2.4 [R1]).

`daemon.py`: drain the spool, tail configured transcript roots by
`(path, inode, size)`, coalesce per DESIGN.md §2.4 — a compaction boundary
crossed, session end, or once an hour — and call `store.capture`, then commit.

**Watch roots have no default.** The watcher watches exactly what
`config.toml` names. A product default of `~/.claude/projects` would make the
first run on this machine ingest the owner's corpus, which is the one thing this
project must never do. The Claude Code adapter may *document* the conventional
path; the config must name it.

## Then

Cross-review every line of the other half, by the same rules as E3: BLOCKING /
MAJOR / MINOR, file:line, a reproduction or the word PLAUSIBLE. Then a standalone
reviewer-agent round on the whole of E4, then mutants, then the gate.
