# E4 round 3 — Gemini review of Claude's latest commits

You are the second pair-programmer on `gitmemory`. Claude wrote the three commits
below; your job is to review every line and every decision in them as an equal,
not to approve them.

Write your review to `docs/reviews/E4-gemini-round3.md`. Open it with an
attestation naming exactly which files you read and which commands you ran, and
stating that you did not read, list, or reference anything outside this
repository — in particular nothing under `~/.claude/` or `~/memory/`. That
constraint is absolute: this product must never touch its author's own machine
history, and `tests/test_no_owner_data.py` enforces it.

## What to review

```
b1a5954 daemon: a rewrite between the parse and the copy loses the boundaries
5f0a3ee gitrepo: the user's global git config cannot reach a store
e58037f daemon: discover by inode, and fix the CLI-9 flake at its real cause
```

Read them with `git show <sha>` and read the surrounding code, not just the diff.
The five changes are:

1. **`daemon.discover`** now deduplicates on `(st_dev, st_ino)` instead of on
   `os.path.realpath`, because `realpath` resolves symlinks but does not correct
   case, so on APFS two spellings of one path are two keys. This also folds hard
   links. Question worth asking: is inode identity right for *discovery*, given
   the store's session identity is still the path?

2. **The CLI-9 test flake.** `tests/test_daemon.py` drives one `daemon.run` across
   two passes by monkeypatching `time.sleep`. It flaked about one run in twenty.
   Two theories were written into the test as comments and both were wrong — a
   `.git/config` lock collision, then the `interval` gate. Instrumentation found
   the real cause: `subprocess.Popen._wait` busy-polls with `time.sleep` whenever
   a timeout is set, and `gitrepo._git` always sets one, so a slow `git` looked
   like a pass boundary and the test deleted `.git` between two `git config`
   calls inside `gitrepo.init`. The fix discriminates on the sleep duration and
   forwards everything that is not `run`'s `poll` to the real sleep. Judge
   whether that discriminator is sound, and whether the same pattern is used
   unsafely elsewhere in the suite.

3. **`gitrepo._env`** now sets `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` to
   `os.devnull`. The scrub previously dropped every `GIT_*` variable and stopped
   there, while git reads `~/.gitconfig` and `/etc/gitconfig` with no variable's
   help. Reproduced: a global `core.excludesFile` matching `*.jsonl` makes
   `git add --all` stage the manifest and skip every segment it references —
   `commit` returns a sha, the working tree is untouched so `verify` stays clean,
   and the history silently stops containing the bytes it is the record of.
   Judge: is `/dev/null` the right mechanism, does it break anything a user
   needs, what does it do on a git older than 2.32, and what else in the
   ambient environment can still change a commit?

4. **`daemon.capture_one`** now stats the source before and after the adapter
   parse and drops the boundaries if the file changed, because the parse and the
   copy are two reads and a rewrite between them attaches the old content's
   offsets to the new content's bytes — offsets that are then carried forward
   into every later manifest of that session. Judge the stat tuple chosen
   (`st_dev`, `st_ino`, `st_size`, `st_mtime_ns`), the residual window between
   the second stat and `store.capture`'s own read, and whether the store should
   be validating boundaries against the size they index into regardless.

5. **Two ceilings written down instead of built**, in `store.session_id_for`: a
   32-bit collision tag (refused loudly by `_capture` rather than duplicating
   silently), and a moved transcript becoming a new session copied again whole.
   Judge whether "document it" is the right call for each, or whether one of
   them is a defect wearing a comment.

## The standard

- **Verify before reporting.** Reproduce each finding with the smallest script
  you can write. Say what you ran and what it printed. Label a finding
  **CONFIRMED** only if you reproduced it, **PLAUSIBLE** if you reasoned it out,
  and never blur the two. The previous round's findings were almost all
  PLAUSIBLE; this round should do better.
- **Negative control.** If you claim code is load-bearing, delete it and show the
  suite notices. If you claim a test holds a property, break the property in the
  source and show that test fails. A fix nothing distinguishes is a fix nothing
  is holding in place.
- **A guarantee needs a floor, not a list of the ways people have fallen through
  so far.** Prefer a finding that names a missing floor to one that adds another
  name to a denylist.
- Disagreement is the point. If one of these five decisions is wrong, say so and
  say what you would have done.

## Running things

- Python is `.venv/bin/python`, from the repo root — an absolute path here would
  name the author's home directory, which is the one thing this repo may not
  record. There is no bare `python` on this machine and no `timeout` command.
- `.venv/bin/python -m pytest -q` from the repo root: 739 tests, ~15s, all green.
- `/bin/sh` here is bash 3.2.57 in posix mode, not dash. Measuring the hook shim
  under `/bin/dash` has misled this project before.
- No `sudo`, no `crontab`, no network installs. Do not modify tracked files other
  than the review you are writing.
