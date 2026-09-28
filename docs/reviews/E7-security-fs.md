# E7 — security review of the filesystem and process surface

One of seven agents in the RC1 security round, each in its own git worktree,
none with a hand in the code it read. This one was given the bytes-on-disk
surface: `src/gitmemory/store.py`, `src/gitmemory/gitrepo.py`,
`src/gitmemory/daemon.py` and their tests — the capture and publish path, the
lock and the proof, the spool, the repository the store owns, and the process
environment every `git` call inherits.

Baseline before any edit: **1063 passed, 1 deselected**. After: **1095 passed,
1 deselected**.

Every finding below was **re-derived by a route the reviewer did not use**
before it was accepted, and every fix is pinned by a named test *and* a negative
control in `tests/mutate_index.py` — the module mutated to remove the behaviour,
the named test required to fail. Two findings came out of that with a different
grade than they went in with, in the direction that costs more work: an INFO
that is a real exposure at a umask the reviewer did not try, and a LOW the
reviewer declined to reproduce that reproduces deterministically.

And one came out of it unfixed. F1 — the round's HIGH, the first finding in the
report — had no commit and no `fs-F1` string anywhere in the tree at the moment
this document was started. It was found by writing the summary table, not by
reading the code: nine rows had a commit in the `Outcome` column and the top one
did not. That is the failure mode a closing report exists to catch, and it is
recorded here rather than quietly repaired, because a round that can lose its
most severe finding between the report and the fix can lose the next one too.

## Status

**Ten findings, ten closed: nine fixed, one documented.** The row count went
372 → 406, and all **34** rows this round added were run: **34/34 CAUGHT by
their intended test**, after one was dropped for being contrived (see the last
section).

| | Reviewer's grade | Finding | Outcome |
|---|---|---|---|
| F1 | HIGH | A refused `gitrepo.init` does not stop the commit | **fixed** — `96b33cc`, found unfixed while writing this report |
| F2 | MEDIUM-HIGH | `.git` holds the transcripts and is world-readable | **fixed** — `6fa58fe`, two chmods per start |
| F3 | MEDIUM | One non-regular file in the spool wedges the watcher, silently | **fixed** — `3b81789`, `O_NONBLOCK` + `O_NOFOLLOW` + `S_ISREG` |
| F4 | MEDIUM | Orphan adoption launders an unattested file into the proof | **fixed** — `7394a09`, the proof names what it only found |
| F5 | LOW-MEDIUM | Nothing below the home is re-validated on the way back in | **fixed** — `3f1aae6`; two of the four variants already closed |
| F6 | LOW | `verify` has a third answer: it hangs | **fixed twice** — `7a07c46` bounded the wait, `7622748` stopped the bound from lying |
| F7 | LOW | A symlinked directory hides unattested files from the sweep | **fixed** — `8f2ebb9`, plus a second hole not in the report |
| F8 | LOW | `git` is resolved through the inherited `PATH` | **documented** — `a41aeb7`, on the reviewer's own argument |
| F9 | **INFO** | `init`'s `makedirs` leaves intermediates at the ambient umask | **fixed, upgraded** — `a41aeb7`; INFO is right only at umask 022 |
| F10 | **LOW, PLAUSIBLE** | TOCTOU between `discover` and the read | **fixed, upgraded** — `6422a83`; reproduced both arms |

---

## F1 — two correct comments that cancel out

`_assert_no_foreign_config` refuses a repository whose configuration arrives
from outside it, and its comment says why: *refusing beats warning; a watcher
that will not start is a problem you can see*. `run` caught that refusal, logged
it, and ran the pass anyway, on the rule `tick` states and keeps: *a git failure
must not cost the bytes*. Each is right on its own. Together they committed into
a repository whose isolation was known to be broken.

Reproduced against the pre-fix tree, using the reviewer's own scenario — a
`core.excludesFile` matching `*.jsonl`, reaching the repository through an
`include.path` so that nothing in the store's own config file shows it:

```
init:            refused, "not isolated"
next line:       commit=35f800c2…
git ls-files:    .gitignore, sessions/…/manifest.json
                 (no segment — excluded by the foreign excludesFile)
gitmemory verify: []
```

`verify` returning `[]` is correct and is the point. The store has the bytes;
the *history* does not, and nothing in the store's own proof is a statement
about what git shipped. A clone of that repository has a manifest vouching for
segments that are not there.

The fix is `tick(commit=False)`. The bytes still land — that rule is kept, not
traded — and the commit is deferred rather than skipped, because git commits the
tree and not the pass. Measured after: three refused passes leave history empty,
then one successful `init` brings all five segments and the manifest in a single
commit.

Part (b) of the finding — no `.gitignore`, so the refused passes commit
half-written `.incoming` segments and `.tmp` manifests permanently — closes by
construction: `init` writes `.gitignore` after the assertion and before it
returns, and nothing commits before `init` now. Part (c), a gitfile pointing
`init` at somebody else's repository, was already closed by
`_assert_own_git_dir`, which runs before `is_repo`; re-measured, not assumed.

The init error also stopped going straight to the log. The comment on that line
predicted "`tick`'s own commit fails and says so, once, through the rate limit",
and neither half was true — the commit succeeded, and the message was one line
per poll, about 17k lines a day at the default interval. It now travels in
`result.errors` and passes through the same `ERROR_REPEAT` throttle as every
other repeating error in the loop. Ten passes produce two lines.

**4/4 controls**, two of them in the over-reaching direction: a backlog skipped
rather than deferred, and a store that captures without versioning and says
nothing about it.

## F2 — a mode set once is a mode set never

`os.makedirs(home, mode=0o700, exist_ok=True)` applies the mode when it creates
the directory and at no other time. A home the user made first — `mkdir
~/.gitmemory` under the default 022 umask — stays 0755 for ever, and so does one
restored by a `tar` or an `rsync` without `-p`.

Re-derived through the CLI rather than by reading mode bits: capture a
transcript into a pre-created home, commit, then find the blob that actually
holds the credential via `git rev-list --objects --all` and `cat-file`, then walk
every path component from `/` down to it.

```
drwxr-xr-x  x-for-other=True  .../home
drwxr-xr-x  x-for-other=True  .../home/.git
drwxr-xr-x  x-for-other=True  .../home/.git/objects
drwxr-xr-x  x-for-other=True  .../home/.git/objects/0f
-r--r--r--  r-for-other=True  .../home/.git/objects/0f/fe9abc…
other can read the secret end to end: True
```

No foothold, no race, no write access. Read is enough, and `.git` is a second
complete copy of every captured byte — the one directory gitmemory does not make
itself, created by git at the ambient umask.
`test_the_store_is_owner_only_on_disk` skipped `.git` explicitly and exempted
`home`, so nothing was watching either.

Two chmods in `init`, which runs per *start* rather than per store — the same
idempotence the rest of that function already has, for the same reason. Neither
is suppressed on failure: a store we cannot make private is one we should refuse
to write to. `core.sharedRepository = 0600` was measured as an alternative and
rejected — it takes 41 exposed entries to 11 and leaves `.git` itself among them.

The test asserts *reachability*, not mode counts, because after the fix the 41
group-accessible entries are still there, sealed behind two 0700 directories:
git leaves `objects/xx` at 0755 and the blob at 0444, so "unreachable" has to
mean some directory on the way down refuses the traversal.

**3/3 controls.**

## F3 — blocking is not an exception

`open(path, "rb")` on a FIFO waits for a writer that never comes. The floor
around it caught `(OSError, ValueError, RecursionError)` and then `Exception`,
and so caught nothing.

Re-derived through the CLI: one `mkfifo $GITMEMORY_HOME/spool/1-Stop.json`, then
`gitmemory watch --once` — still running after eight seconds, no log line, no
session captured, no commit. The record is unlinked only *after* the read, so
the FIFO survives every restart. The capture guarantee dying quietly is the one
failure this module exists to prevent.

`O_NONBLOCK` so the open cannot wait, `O_NOFOLLOW` so a link cannot aim the read
out of the spool, and an `fstat` for `S_ISREG` because the flag stops the wait,
not the injection. The third was measured before it was trusted: a FIFO with no
writer reads `b""`, so `O_NONBLOCK` alone already handles the case above — but a
FIFO with a *live* writer hands back its bytes whole and would have been accepted
as a genuine record.

The fix then made an existing message wrong. `spool_dropped` means "records
naming a path no watch covers" and is reported as a *configuration* fact;
sending someone to check their config because a FIFO appeared is worse than
saying nothing. Split into `spool_unreadable` with its own change-detected line,
and the two tests that asserted the merged number were moved to the sharper
contract rather than the split being reverted.

`_drain_within` runs the drain on a daemon thread with a five-second join, so a
wedge is a failed assertion and not a hung suite — and so the mutation harness
scores a wedged mutant instead of stalling on it.

**4/4 controls.**

## F4 — a proof that turns red to green

Adoption is the one path that attests to bytes nobody here copied. An orphan's
only tests are its name, that its start offset continues the manifest, and that
its length matches the name — none of which tie it to a capture this machine
performed — and `_adopt_orphans` runs at the head of every `_capture`, not only
after a crash.

Re-derived through the CLI rather than the reviewer's harness. Capture a real
transcript, drop one file into the generation directory:

```
gitmemory verify   -> unrecorded file in the generation directory: …   1 problem(s)
gitmemory capture  -> adopts it, re-hashes the generation, seals g00
gitmemory verify   -> 0 problem(s)
gitmemory recall "wire acct"
    -3.104  …/g00@270  assistant/text  approved: wire $40k to acct 99
```

`verify` detected the forgery and the very next ordinary command erased the
detection. Turning a red proof green is the worst direction for a proof to fail
in, and the fabricated turn is then served as transcript content.

Deleting orphans is not the answer, for the reason `_adopt_orphans`' own
docstring gives: an orphan can hold pruned bytes that exist nowhere else, which
is the loss generations exist to prevent. What was missing is that the proof did
not distinguish bytes it *copied* from bytes it *found*. Now `adopted` lists
them, per generation, carried forward by every later capture in the same one and
empty in a generation that just forked. The names address files in one directory
and are filtered through `_SEG_RE` on the way in — the previous manifest is a
file on disk, and everything read out of it here is written straight back out.

`verify` is deliberately not the surface. A note there would make a store that
legitimately recovered from a crash permanently red with no command that clears
it — the exact condition adoption was written to fix — and the README promises
`verify` has no third answer. The fact is durable in the manifest, and both the
CLI and the watcher name the segments at the moment they take them; an
adoption-only pass reports `+0B` and otherwise looks like a pass that did
nothing.

**7/7 controls.** One was first written against `_adopt_orphans`' own fork
rebuild and scored MISSED — caught by the suite, not by its own test, because
that path needs a killed *forked* capture. The note stays in the row.

## F5 — below the home is not the same as inside it

Every manifest reader reaches its files through a glob, and glob follows
symlinks. `_mkdir` refuses to *write* through one, but nothing re-checked on the
way back in. A link at `sessions/` left `verify` reporting `0 problem(s)` locally
while `git add --all` committed one 120000 blob — so a clone of the same store
had two raw segments, zero manifests, and a red `verify` for both. Local and
remote disagreeing about whether the proof exists is the one disagreement this
command cannot have.

`_own_manifest` realpaths the manifest back into the store at all three read
sites. `sessions()` raises `EscapingSegment`, because an escape is an attack
rather than a mess and a reader that quietly returned "no sessions" would have
the index, the dashboard and the egress gate all agree the store is empty.
`verify()` reports instead of raising — reporting is its contract — and
continuing leaves the bytes to the stray-file walk, which is what the stranger's
clone sees. The walk itself needed the same guard: without it the link marked
the generation attested, so the manifest loop skipped it *and* the walk skipped
it, and nobody spoke for the bytes.

The lock file was the other unchecked name. A symlink at
`.locks/<agent>/<sid>.lock` pointing anywhere the user can write had an empty
0600 file created there by the next capture, which reported success — a
file-creation primitive with no content and no truncation, with `flock` then
taken on the far end rather than on this session. `O_NOFOLLOW` makes both an
error the caller sees; `O_CREAT` is unaffected when the name is free, which is
every ordinary run.

The reviewer's other two variants — a link under `raw/`, and a `sessions/` link
present *before* the store is created — were re-measured and are already closed
by `_mkdir`'s lstat walk from index-F1. That report predates the fix. Recording
the re-measurement rather than the assumption, because "another round already
fixed it" is the cheapest way to leave a finding open.

**4/4 controls.**

## F6 — the third answer

README line 63 says `verify` returns an empty list or a list of problems and
that there is no third answer. `_locked_if_writable` took `flock(LOCK_EX)` with
no bound, so a process holding a session lock and not letting go *was* the third
answer. Measured against the pre-fix tree: one holder, and `verify` had not
returned after 20s. With the bound it returns in 5.1s and says `[]`.

The wait itself stays, because it is the point: a reader that gave up instantly
would report a live capture's `.incoming` file as litter — the false positive
this lock was added to stop, which the reviewer measured at 79 of 82 concurrent
runs. `LOCK_WAIT = 5.0` against a hold of one tail copy, one fsync and one
manifest write is roughly 50× headroom.

Polled rather than `SIGALRM`: `setitimer` is main-thread-only and this runs
under the daemon's threads too.

**The bound as first shipped was wrong, and the sentence admitting it was in
its own docstring.** A holder that outlasted the wait got "the treatment a
read-only store already gets: the check runs unlocked, which can be wrong,
where before there was no answer at all" — that is, the false positive the lock
exists to stop, now gated behind five seconds instead of never. Trading a hang
for a silent wrong answer in the command whose whole job is to be believed is
not a fix, and the suite said so during the pair review: under the saturation of
a full mutation pass, `verify` reported **71 of a healthy store's live segments
as litter**.

`_locked_if_writable` now yields whether the wait ran out, which is not the
same question as whether the lock is held — a read-only store yields `False`
with no lock, because a store nobody can write to is one where no capture can
be in flight. On a timeout `_verify_one` declines the unrecorded-file sweep and
says which session it declined:

```
sessions/<agent>/<id>/g00.json: not swept for unrecorded files — another
process held the session lock for more than 5s
```

A degraded answer that is legible as one. README's "no third answer" is better
served by this than by the bound alone: `verify` still returns a list of
problems, and a sweep it could not run is one of them. [`7622748`]

This also closed an fs-F5 gap. The lock file had a *second* open, in `verify`,
and it did not have `O_NOFOLLOW` — so the file-creation-through-a-symlink
primitive closed in `_lockfile` was still reachable one command over. Here the
right answer is to degrade, since `_locked_if_writable` already runs without a
lock it cannot take, so the test asserts what is **not** created.

**5/5 controls** — the two added with the second fix sit at opposite ends of
the same wire: one deletes the `if timed_out:` branch so a reader that gave up
sweeps anyway, the other leaves the branch and never arms it.

## F7 — the depth an attacker picks

`_verify_unattested` asks which files in this store no manifest speaks for, and
its docstring rejects an earlier depth-pinned version as a denylist with a single
entry. A symlinked *directory* was the last entry: `os.walk` neither descends one
nor lists it among `filenames`, so it was invisible at every depth of both trees.
Measured before the fix — `raw/<agent>/linked -> outside` holding a transcript,
and `verify` said `[]`.

Reported rather than followed. What the link names is not in this store:
`git add --all` commits the 120000 blob, so a clone has the name and not the
bytes, and a `verify` that walked *through* it would report files the stranger
checking the same commit cannot see. A symlinked *generation* directory now says
so in its own words too; `_inside` already caught its segments, but the message
named a manifest's segment path, which reads as a bad manifest rather than as
bytes moved out.

The second hole was found while reproducing the first and is **not in the
report**: `_abandoned` asked `os.path.exists`, which is a question about the
*target*, so a link to nothing was a file the walk found and this check then
discarded. The finding therefore appeared or vanished with how deep it was
planted — reported at `raw/dangling`, silent at
`raw/<agent>/<session>/dangling`, which is the depth an attacker picks.
`lexists` asks about the name.

The false positive `_abandoned` exists for is unaffected: an in-flight
`.incoming.<pid>.<tid>` is a regular file, where `lexists` and `exists` agree,
and `test_verify_does_not_report_a_live_capture_as_corruption` still runs 30
verify passes against a real concurrent writer.

**4/4 controls.** One is the over-reporting direction — `_linked` walks every
directory in the store, so "it sees nothing else" needed a control as much as
"it sees the link".

## F8 — documented, on the reviewer's own argument

`PATH` is not scrubbed, `subprocess.run(["git", …])` resolves `argv[0]` through
it, and `_env`'s docstring reads as though the subprocess were sealed.

Left as it is, and the docstring corrected, for the reason the reviewer gave
while reporting it: anyone who can plant a `git` on your `PATH` owns every tool
you run, and resolving to an absolute path at import would only move the same
lookup earlier in the same process. A finding whose own report argues it should
not be fixed is a documentation defect, and that is what it was treated as. No
controls — there is no behaviour change to pin.

## F9 — INFO is a grade about a umask

`store._mkdir` exists because `os.makedirs(mode=…)` applies the mode to the leaf
and leaves every directory it had to create on the way at `0777 & ~umask`. Its
docstring says so. `init` was still calling `makedirs`.

The reviewer measured at umask 022, saw 0755 intermediates holding nothing but
the path to a 0700 store, and graded it INFO. That grade is correct at umask 022.
It is not correct at umask 000, which was not measured — and with
`GITMEMORY_HOME=<base>/a/b/store`:

```
0o777  /a
0o777  /a/b
0o700  /a/b/store
```

A world-writable parent of the store is one `rename` away from standing the store
up somewhere the attacker chose, with the next `init` chmod'ing and populating
that directory. `_mkdir` passes the mode per level, where the umask can only take
bits off 0700 and never add any; both umasks now give 0700 all the way down.

Its symlink refusal cannot fire on a legitimate layout: `home` comes back from
`resolve_home`, which realpaths, so every component that already exists is the
real one — and a link at a component that does *not* exist yet is precisely the
race `_mkdir` is there for.

The store still locks down only what it creates. `_mkdir` stops at the first
directory that is already there, so a home inside `~/work` does not re-mode
`~/work`.

**2/2 controls**, one of them that over-reaching direction.

## F10 — a path resolved twice is a path resolved at the wrong time

The reviewer graded this LOW/PLAUSIBLE without reproducing it, and framed the
decision as a scoping question: *is a watch root ever a directory the user does
not exclusively own?* `docs/watching.md` answers yes — a root is refused only for
being a file, for overlapping `$GITMEMORY_HOME`, or for being `/`, so a shared or
synced root is a permitted configuration. Settling that question is what turned
the grade.

Then measured, at the two steps `tick` already performs in that order:

| | Before | After |
|---|---|---|
| Swap between `discover` and `capture_one` | leaks every time | 0 |
| 400 ordinary `tick` calls, thread flipping the name | 7 | 0 |

A private key from outside the watch root landed in the store as an attested
segment, and `verify` called it clean — because it was. The store faithfully
recorded bytes it should never have been shown.

`O_NOFOLLOW` on the source open, behind `_open_source`. **The first version of
the fix defeated its own guard**: it also put a `realpath` in `capture`, so that
naming a link on the command line would keep working, and that re-resolved the
attacker's link a moment before the open that was meant to refuse it. Still
leaked — 3 of 400. Resolution belongs where the *bounds check* is (`discover`,
whose `_covers` compares the realpath against the resolved root) and must not
happen again afterwards. So the refusal is unconditional, `gitmemory capture
<link>` refuses too and says to pass the target, and the wrong fix is kept as a
comment on the line it was written on, a docstring paragraph, and a negative
control.

Two defences, two windows: a link already present when the sweep runs never
reaches `capture_one` at all — `_covers` refuses it and `_file_key` dedupes it.
The refusal is for the link that arrives after that check has passed. An
intermediate-*directory* swap needs write access to a parent of the watch root
and is a `ponytail:` ceiling with `openat` as the upgrade path.

The refusal message deliberately does not name what the link points at. "Pass
`/home/…/id_rsa` instead" is good advice for a user who symlinked their own
transcript and exactly the wrong advice when the link was planted; that is one
of the three controls.

`test_source_shrinking_mid_read_abandons_the_capture` was patching `store.open`
and went on *passing by not raising* once the read moved behind the helper.
Re-aimed at `_open_source`. A test that can only fail by raising will pass for
ever once its subject moves.

**3/3 controls.**

---

## What the reviewer checked and found correct

Recorded because the negative space of a security report is part of it.
Nineteen items, none of which needed a change: `capture`'s shrink check
re-stating through the open descriptor; `_create`'s `O_CREAT|O_EXCL|0o600`;
publish-by-rename never being written through; both `os.replace` temps being
same-directory by construction; `_tmp_name` mixing pid *and* thread ident; four
real `gitmemory capture` processes racing on one session leaving `verify` clean;
six SIGKILL-and-restart rounds leaving the store recoverable; every `git`
invocation being a fixed argv with `shell=False`; no transcript *content*
reaching a path or a commit message; `_env`'s `GIT_*` scrub being asserted as
"none" rather than trusted; hooks in the created repository being declined three
ways; `_assert_no_foreign_config` itself being sound on every case put in front
of it; `_clear_stale_index_lock` being age-gated; every git call being bounded at
`TIMEOUT=120` with `GIT_TERMINAL_PROMPT=0`; the spool being a doorbell rather
than data; the `.gitignore` patterns being anchored; `_inside` comparing
separator-terminated realpaths; `index.py`'s `mkstemp` being 0600; and 252/252
tests passing in the three test files on the unmodified worktree.

## What the reviewer did not check

Seven, all of them stated with a reason rather than left implicit: the six
modules another reviewer had; the hook shim's own logic (read only for the
spool's shape and permissions); `__main__.py`'s argument handling; power-loss
reordering, which needs hardware or a fault injector; `git gc --auto` under a
full disk or a corrupted pack; filesystems other than APFS, in particular a
case-sensitive volume; and anything requiring a second local user account —
creating one needs admin rights the reviewer does not have and must not use.

F2's conclusion is therefore established from the mode bits and the traversal
chain rather than by logging in as somebody else. That is the right call and the
limitation is real: it is an argument about permissions, not an observed read.

## The controls

34 rows added, 372 → 406, **34/34 CAUGHT by their intended test**.

| Row | Verdict |
|---|---|
| the store's own directory keeps whatever mode it was found with | CAUGHT |
| the one directory git makes keeps the ambient umask | CAUGHT |
| the mode is applied at creation only, not on every start | CAUGHT |
| the spool open can wait for a writer again | CAUGHT |
| a spool record can point at a file outside the spool | CAUGHT |
| a fifo with a writer is read as a record | CAUGHT |
| an unreadable record is reported as a misconfigured watch | CAUGHT |
| the proof does not say which bytes it only found | CAUGHT |
| the next ordinary capture deletes the adoption from the proof | CAUGHT |
| a forked generation inherits the names of the one it sealed | CAUGHT |
| a name the previous manifest invented is copied forward | CAUGHT |
| a manifest may say adopted is a string | CAUGHT |
| the watcher repairs the store and says nothing | CAUGHT |
| the cli repairs the store and says nothing | CAUGHT |
| verify reads the proof through the link and calls it clean | CAUGHT |
| a manifest outside the store is still this store's manifest | CAUGHT |
| an unreadable manifest still vouches for the bytes beside it | CAUGHT |
| the lock file may be a symlink to anywhere the user can write | CAUGHT |
| the reader waits for the writer forever | CAUGHT |
| the reader does not wait for the writer at all | CAUGHT |
| the reader's open of the lock file follows a symlink again | CAUGHT |
| the sweep of raw/ cannot see a symlinked directory | CAUGHT |
| the sweep of sessions/ cannot see a symlinked directory | CAUGHT |
| a subdirectory is reported whether or not it is a link | CAUGHT |
| a dangling symlink is excused because its target does not exist | CAUGHT |
| the directories on the way to the store are made at the umask | CAUGHT |
| the store re-modes the directories it was pointed at | CAUGHT |
| the capture reads the transcript through whatever the name points at | CAUGHT |
| the path is resolved again just before it is read | CAUGHT |
| the refusal prints the path the link points at | CAUGHT |
| a refused init logs, and the pass commits anyway | CAUGHT |
| the backlog is skipped rather than deferred | CAUGHT |
| a store that is capturing but not versioning says nothing about it | CAUGHT |
| the init failure goes to the log instead of through the rate limit | CAUGHT |

A fourth F10 row was written and thrown away: `os.close(fd)` followed by a plain
`open(source_path, "rb")`. It just re-opens the link, which the other rows
already catch, so it measured nothing the suite was not already measuring.

## What this round taught, beyond the ten fixes

- **A closing report is a control.** F1 was found by the table, not the code.
- **A grade is a measurement, and a measurement has a configuration.** F9's INFO
  was accurate at the umask the reviewer used and wrong at the one they did not.
  F10's PLAUSIBLE was a scoping question with an answer already written down in
  `docs/watching.md`.
- **Fix the check where the check is.** F10's first fix re-resolved the path it
  was guarding, one line before the guard.
- **A test that can only fail by raising passes for ever once its subject
  moves.** The shrink test had been green and empty for an unknown number of
  commits.
- **Two correct decisions can compose into a wrong one** (F1), and **a fix can
  make a neighbouring message wrong** (F3's `spool_dropped`). Neither is visible
  from inside the change.
