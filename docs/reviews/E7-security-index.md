# E7 — security review of the index and derivation surface

One of seven agents in the RC1 security round, each in its own git worktree,
none with a hand in the code it read. This one was given `src/gitmemory/index.py`,
`derive.py` and `graph.py`: SQL construction and the FTS5 query builder,
resource exhaustion from transcript content, the artifact write door,
`_sweep_partials`, content-derived identity, and the schema check against a
hostile SQLite file.

Every finding below is **re-derived independently before it is accepted**, by a
route the reviewer did not use, and every fix is pinned by a named test *and* a
negative control in `tests/mutate_index.py` — the module mutated to remove the
behaviour, the named test required to fail.

## Status

**Ten findings; six closed so far.** This document grows as the round does.

| | Finding | Outcome |
|---|---|---|
| F1 | `derive.build` writes outside the store through a symlinked leaf | **fixed** — `lstat` in `store._mkdir`, and a rollback that reports |
| F2 | Two concurrent builds destroy each other's temp | **fixed** — one build at a time per directory |
| F3 | `_sweep_partials` deletes files it does not own | **fixed** — the name the build writes, and `<` not `!=` |
| F4 | A SQLite file the user did not create is trusted completely | **fixed** — read-only by default, and a step budget |
| F5 | Lone surrogates reach the committed artifacts | **fixed** — one transform, at the one door |
| F6 | One oversized block aborts the whole build | open |
| F7 | Control characters and bidi overrides reach `graph.json` labels | **fixed** — same transform, and it was not only `graph.json` |
| F8 | `derive.ideas` costs ~32 s of CPU for 284 KB of crafted text | open |
| F9 | `index/` is created 0755 where the store is 0700 | open |
| F10 | The truncation warning is announced once per process | open |

---

## F1 — a symlink was already a directory, everywhere

`os.path.isdir` follows symlinks. `store._mkdir` asked `isdir` before creating
each component, so a symlink anywhere in the chain was already "a directory":
`_mkdir` stopped there, every write below it went to the link's target, and no
call reported anything. The reviewer found it on the derived leaf. It is not
confined to the derived leaf.

Reproduced in three parts before anything was changed:

| Planted | Before | After |
|---|---|---|
| `derived/<agent>/<session>/g00` → `/tmp/victim` | `graph.json`, `ideas.json`, `timeline.json` written into the victim; `stats.skipped == []` | victim empty; the skip names "a symbolic link" |
| the same, then the rollback | `rmtree(ignore_errors=True)` refuses a symlink and says nothing; all three artifacts stay | the refusal is appended to `stats.skipped` |
| `raw/<agent>` → `/tmp/victim2` (**not in the report**) | a whole session's segments written outside `$GITMEMORY_HOME` | `NotADirectoryError` before the first byte |

The third row is why the fix is not where the report put it. Its smallest
acceptable fix was "in `_write`, refuse a parent that is a symlink" — which
guards one caller, and only the *immediate* parent, so a link one level higher
still redirects the write. Every one of these names is predictable from the
store's own contents and none of them exists before the first capture, so any
process running as the user can plant one and wait. The check belongs in the
one function that makes directories: `_mkdir` now `lstat`s each component,
walks up past what is missing, and raises `NotADirectoryError` naming whether
it found a symlink or a plain file. `derive`, the store and the index all
inherit it.

The rollback is the second half, and it failed in exactly the way the comment
above it asserted it would not. `ignore_errors=True` was doing two jobs and
only one was wanted: most skips happen before anything is written and `rmtree`
on an absent path raises, which the flag correctly swallowed — and `rmtree` on
a symlink refuses outright (`Cannot call rmtree on a symbolic link`), which it
also swallowed, leaving artifacts on disk while the run reported the generation
skipped. `FileNotFoundError` is now the ordinary skip; any other `OSError`
becomes a `rollback left artifacts behind: …` line in `stats.skipped`.

**What this does not fix.** `_mkdir` checks and then creates, so a swap between
the `lstat` and the `mkdir` still wins. Closing that means `O_NOFOLLOW` on
every final open; it is worth doing when something can show the race is
reachable, and the shortcut is marked `ponytail:` in the source.

Four tests carry this — two in `test_derive.py` for the leaf and the rollback,
two in `test_store.py` for the capture path and for the ordinary case a real
daemon produces, because a check that refuses everything would pass the first
three.

---

## F2 — the sweep cannot tell a live build from a dead one

`build` sweeps `.building-*.db` before it starts, because `except BaseException`
covers a crash and cannot cover SIGKILL. The docstring said the sweep was "safe
to do unconditionally", on the grounds that "a concurrent build holds its own
`mkstemp` name that this pass has not seen yet". That is exactly backwards: the
concurrent build's name is on disk by the time it matters, it matches the
pattern, and **the name is the only evidence there is**. A partial index left by
a kill and one a live build is still writing are the same string.

Reproduced with two builds in one directory, the first paused inside `_fill`
with its transaction open:

```
sqlite3.OperationalError: disk I/O error
```

— from the first build, when the second swept its temp and journal out from
under it. An error that names a failing disk when the disk is fine. Two
`gitmemory index` runs in two terminals is the ordinary way to get there, and
the report's own symptom (`attempt to write a readonly database`) is the same
race landing on a different SQLite code path.

The fix is an exclusive `flock`, held across the sweep and the build. It sits in
the **target's own directory** rather than under `$GITMEMORY_HOME`, because the
directory is the contended thing: `--db` points the sweep wherever the caller
says, so a home-keyed lock would not serialise two homes writing into one
directory. `store._locked` was already five lines of `os.open` plus `flock`
around a session path; those five lines are now `store._lockfile`, and the
session lock is one call to it.

Pinned by `test_a_second_build_waits_instead_of_sweeping_the_first_one_away`,
which asserts the *outcome* — neither build raises, and the index still answers
— rather than that a lock file exists.

## F3 — the sweep deleted files it did not write

Two over-matches in one condition:

| On disk | Before | After |
|---|---|---|
| `.building-manifest.yaml` (the user's) | deleted | kept |
| `.building-notes` (the user's) | deleted | kept |
| `.building-abc123.db` (ours, from a kill) | deleted | deleted |
| `.building-abc123.db-journal` | deleted | deleted |
| `gitmemory-v1.db` (an older schema) | deleted | deleted |
| `gitmemory-v3.db` (a **newer** schema) | deleted | kept |

The prefix on its own is not the name of anything gitmemory wrote, and `--db`
points the sweep at a directory the *user* chose. Every temp this module makes
ends `.db`, because `mkstemp` is called with `suffix=".db"` eight lines away, so
`_PARTIAL_RE` matches that and the journals SQLite hangs off it — and nothing
else.

The second half is the schema test. It was `!=`, which reads "not the current
version" and means "not *this* version" — so an index written by a **newer**
gitmemory looked exactly like one written by an older one. Run both against the
same home and each deletes the other's index on every build, each rebuilds from
raw, and neither says a word. Forward is not the same direction as stale; the
test is now `<`.

## F4 — a SQLite file the user did not create was trusted completely

`open_db` was a bare `sqlite3.connect(path)`. Two consequences, both measured:

**It creates the file.** `sqlite3.connect` opens for writing and makes the
database if it is absent, so `gitmemory recall --db typo.db` left a 0-byte
database at the typo — a file that answers nothing, forever, with no sign of
where it came from. Every caller but `build` is a reader.

**It runs whatever the file says.** `_check_schema` does
`SELECT value FROM meta WHERE key = 'schema'`, and in a file gitmemory did not
write, `meta` can be a **view** — arbitrary SQL, executing inside that
innocuous-looking SELECT. A view over a recursive CTE counting to 4×10⁸:

| | Before | After |
|---|---|---|
| Wall clock | **29.7 s**, and rising with the constant in the file | 0.0 s |
| Interruptible | no — SQLite is executing in C, so `signal.alarm` does not fire and neither does Ctrl-C | n/a |
| Result | `ValueError: index is schema 400000000` | `ValueError: not a gitmemory index: interrupted` |

`open_db(path, *, write=False)` now opens `file:…?mode=ro` by default and
`build` passes `write=True`; a step budget of ~10⁶ VM instructions wraps the
schema SELECT, against a real query that is an index seek on a two-row table —
about six orders of magnitude of headroom. The budget raising
`OperationalError` needed no new error path: `_check_schema` already turns a
`DatabaseError` into "not a gitmemory index".

**What this does not fix, and cannot.** An attacker who can write the `--db`
file chooses what `recall` prints — `session_key`, `byte_offset` and `byte_len`
are a seek instruction an agent may follow into the real store, and no open mode
changes that. Read-only removes the *accident*: the stray file, and a reader
scribbling on an index it only meant to query. The reviewer's own table already
showed `load_extension` blocked by SQLite and every other hostile shape landing
on a clean `sqlite3.Error`.

Two tests carry this: `test_a_reader_cannot_create_or_scribble_on_an_index` and
`test_a_meta_that_is_not_a_table_cannot_run_forever`, the second of which
asserts a wall-clock bound, because the defect is duration.

## F5 and F7 — the artifacts had no door, and the terminal's door was a copy

Two findings, one cause. `derived/` is committed to git *and* read by a second
tool that draws it, so it is a rendering surface with the same problem the
terminal has — and `index._encodable` and `__main__._safe_str` each solve half
of it, for their own surface only.

Reproduced together, from one capture: `\xff\xfe` in a user turn and an ANSI
erase-line, a bidi override pair, NUL, DEL and a zero-width space in an
assistant block.

| In the artifact | Before | After |
|---|---|---|
| `graph.json` node label | `Never use \udcff\udcfe pickle…` | `Never use ?? pickle…` |
| `ideas.json` idea text | `We decided \x1b[2K to \u202eesrever\u202c … \x00 … \x7f … \u200b` | `We decided \x1b[2K to \u202eesrever\u202c …` (literal, six characters) |
| A consumer re-encoding either file | `UnicodeEncodeError: surrogates not allowed` | clean |
| `stats.skipped` | `[]` — nothing reported, either way | `[]` |

**The report's fix location was wrong, and the reproduction is what showed it.**
F7 asked for `graph._WS` to be widened. The control characters landed in
`ideas.json`, which never passes through `_label`; in this run they did not
reach `graph.json` at all, because that block was not extracted as a decision.
Fixing `_label` would have produced a clean `graph.json`, a still-hostile
`ideas.json`, and a third artifact next month with neither. The transform goes
in `derive._write`, which the docstring already calls the single door, and it
runs **before** the redaction scan so the bytes scanned are the bytes written.

The transform itself is `records.safe_text`: lone surrogates replaced, then
every control, bidi and separator character spelled out as `\xNN` or `\uNNNN`.
It is `__main__._safe_str` moved down a layer and given the surrogate half —
`__main__` now imports it and its own copy is gone, so there is one character
class in the codebase instead of two that can drift. Two incidental
corrections came with the move: the class gained `\u200b`–`\u200d` (zero-width
characters, which hide a difference between two labels that render identically),
and U+202E now prints as `\u202e` rather than `\x202e`, which reads as a space
followed by `2e`.

**What this does not fix.** `safe_text` is not a redaction gate and does not
pretend to be one — a ZWSP planted inside a credential still breaks the
detector's `\b` anchor both before and after, because the scan runs on text in
which the ZWSP has become six ASCII characters rather than none. Closing that
means normalising *before* scanning, which is a different change with a
different risk, and nothing in this round showed it reachable.

## Negative controls added this round

Nine rows, all run, **9/9 CAUGHT by their intended test**.

| Mutant | Verdict |
|---|---|
| two builds in one directory sweep each other's temp away | CAUGHT |
| the sweep matches the prefix, not the name the build writes | CAUGHT |
| the journals beside a partial index are left behind | CAUGHT |
| a newer schema's index is swept as a leftover | CAUGHT |
| every reader opens the index for writing, creating it if absent | CAUGHT |
| a hostile `meta` runs for as long as it likes | CAUGHT |
| a symlink in the chain is already a directory | CAUGHT |
| a symlinked derived leaf is followed, not refused | CAUGHT |
| a rollback that could not run is reported as if it had | CAUGHT |

Two existing rows — "an index from a superseded schema is left on disk forever"
and "a skipped generation loses its stale artifacts" — had their anchors
re-pointed at the rewritten lines and were re-run: both still CAUGHT.
