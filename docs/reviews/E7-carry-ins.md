# E7 — carry-ins

The RC1 security round ran seven agents in seven worktrees. Six of them reported
inside their own surface; the seventh's report, and a handful of findings the
others raised about code that was not theirs, land here. Same discipline as the
rest of the round: **every finding is re-derived independently, by a route the
reporter did not use, before it is accepted.**

## Status

| | Finding | Outcome |
|---|---|---|
| S1 | A manifest the gate cannot read opts its generation out of the seam scan | fixed — `UnreadableManifest`, `sessions(strict=)` |
| S1b | The refusal, and every other exception, prints an unmasked path | fixed — `_safe_exc` |
| S2 | The owner-data scan reads the working tree; a push sends the object graph | fixed — `_history_scan` over `gitrepo.pushable_objects` |
| S4 | A FIFO planted in the store hangs `verify` and adoption, under the lock | fixed — `store._regular` before every segment `open` |
| S6 | A rejected manifest takes the stray-file sweep down with it | fixed — `_verify_one` composes; the sweep always runs |
| S7 | The shim strips C0 from the one value it echoes, and claims more | fixed — `LC_ALL=C tr -cd` over printable ASCII |
| S8 | The shell's own job report escapes the shim's redirects | fixed — a brace group around the write |
| S9 | `home` and `.git` are re-chmodded on every start; the spool is not | fixed — `init` makes and repairs `spool/` |
| S10 | Every write failure is silent, and nothing said what that costs | accepted — the asymmetry is deliberate; `hook/README.md` states the cost |
| S11 | `find_session` defaults to searching the real `~/.claude/projects` | fixed — required `projects_root`, plus an autouse home-isolation fixture |
| S12a | The gate opens what a tracked symlink points at, and blocks on a FIFO | fixed — `redact.contents`, used by the file scan and the seam scan |
| S12b | The owner-data *test* scanner does the same | fixed — `os.readlink`, so the link text is what is scanned |
| S13 | 23 of 27 evasions walk past the owner-data patterns | fixed — trailing `/` dropped, `re.I` everywhere, the file name scanned |
| F1 | `.git` pointing at a repository the store did not make | fixed — `_assert_own_git_dir`, in `init` **and** `commit` |
| — | The mutation harness scored a mutant that does not parse as CAUGHT | fixed — `verdict` reads both runs; a compile check on every row |
| — | Two tests proved "this does not block" by blocking, wedging the pass | fixed — `_deadline`, and the `WEDGED` verdict |

---

## S1 — a `continue` in the gate's feed is not a skip, it is a bypass

`store.sessions` skips a manifest it cannot read. That skip is right, and there
is an E2 regression test for it: a reader that raised would make one
half-written manifest — what a full disk leaves behind — un-indexable for the
whole store.

`segment_groups` is not a reader. It is the gate's feed, and the *only* thing
that produces the ordered segment runs `redact.scan_group` joins across. A
credential cut in half by a segment boundary is invisible to every per-file
scan; the seam join is what sees it. So a skip there does not lose a row, it
loses exactly the class of secret the group scan exists for — while `push` goes
on printing `gate passed`.

One byte does it, by anything that can write inside the store:

```
honest                       groups=1 per-file-high=[]        seam-high=['openai_api_key']
start is a string            groups=0 per-file-high=[]        seam-high=[]
segments is a dict           groups=0 per-file-high=[]        seam-high=[]
manifest not json            groups=0 per-file-high=[]        seam-high=[]
```

Reproduced through `segment_groups`/`scan_group` directly rather than the
reporter's route through the CLI and a `config.toml`, so that the demonstration
does not depend on the push path being set up at all. `verify` reports the same
manifest as broken; `_push` does not call `verify`.

The same round's `pushable_objects` (F1) does **not** close it. That change scans
the object graph, and blobs are scanned individually — the straddling secret is
still in two halves.

**The fix is the strict/lenient split, not a third code path.** `sessions` grew
`strict: bool = False`; `segment_groups` is the only caller that passes `True`.
A reader wants as much of the store as it can get and the gate wants all of it
or nothing, and those are the two settings.

Two details the tests pin:

- `_Skip`, a private exception, so the two hand-written rejections (`segments`
  is not a list; a segment entry names no usable path) leave by the same door as
  a `json.JSONDecodeError`. Two ways out of one loop body is how the `strict`
  check gets added to one of them and forgotten on the other — there is a
  mutation row that makes one of them a bare `continue` again.
- A file matching `g*.json` that is not a generation name is still skipped under
  `strict`. `gfoo.json` claims no segments; refusing over it would be F4a's
  over-refusal one directory down. `verify` reports it and the file walk scans
  it.

`_push` catches `UnreadableManifest` next to `EscapingSegment` and prints the
refusal with the reason and the command that explains it. Uncaught it would
reach the top-level handler and print `error:` with a traceback's vocabulary —
the store still does not push, but "error" and "refusing to push" are different
sentences to the person reading them at 3am.

## S1b — the message carries the path, and the path can be the credential

Found by S1's own mutation run rather than by a reviewer. The last mutant's
captured stderr read, in full:

```
error: /…/sessions/claude-code/sess/g00.json: Expecting property name … line 1 column 2
```

The path, unmasked, out of the top-level handler. F3 established that the gate
must not publish what it caught, and fixed the channel F3 was looking at. This
is the same shape on a channel nobody had looked at: a generation is named after
a session id, a session id is chosen by whoever calls `capture`, and an `OSError`
carries whatever path it failed on. Reproduced with a missing source file named
`ghp_AAAA…`, which printed it twice — once from the parse-failure warning, once
from the handler.

`_safe_exc` masks it at all three sites. The path stays readable either side of
the mask, which matters here more than anywhere: the refusal's whole job is to
send you to `verify` knowing which file to look for. A
`json.JSONDecodeError` says "line 1 column 2" and names nothing, so the strict
raise always prefixes the path — that prefix is what there was to mask.

**Deliberately not in the `print` shim**, whose docstring otherwise makes exactly
the centralising argument ("the one that gets forgotten is the vulnerability").
Escaping is lossless; masking is not. `recall` prints transcript content because
that is what it is for, and a store on the owner's own disk is not an egress —
masking there would make the tool withhold the user's own bytes from them.
Egress is `push` and `serve`. An exception is neither: it is the third case,
nobody asked to see it, and it is the one that leaks by accident.

The `{home}` and `{path}` in `not a store:`, `no index at`, and `is not a git
repository` are left alone. Those echo an argument the invoking command line
already recorded, so masking them removes nothing from the transcript.

## S4 — `open` on a FIFO does not fail, it never returns

`verify` and orphan adoption both read every segment a manifest names, and both
do it under the session's exclusive `flock`. Neither asked what the path *was*.
A FIFO named as a segment by anything that can write inside the store therefore
takes the store hostage: `open` blocks for ever, the lock is never released, and
every later command on that store blocks behind it. Not a crash — no output, no
exit, nothing in a log.

The first reproduction **did not reproduce**, and the reason is worth keeping. A
FIFO dropped loose in the store is *reported*, not opened: the file walk stats
it and moves on. It has to be named by a manifest, as a segment, for anything to
try to read it. So the finding is narrower than "a FIFO in the store" and the
test had to be written to the narrower shape.

The fix is `_regular`, a four-line `lstat`/`S_ISREG` predicate, checked **before**
the `open` and not inside its `except` — a FIFO does not raise, so an `except`
never sees it. Three call sites: adoption's pending-segment test, adoption's
whole-run rehash, and `_verify_one`.

The gate had the same shape twice over, on paths it takes from
`git ls-files`. Two facts decide it: **`git ls-files` does not list a FIFO or a
socket, but it does list symlinks**, and **git stores a symlink as mode `120000`
with the link text as blob content and never follows it**. So the gate was
scanning the wrong bytes for every tracked symlink — the target's contents
rather than the path that is actually committed — and would block on a FIFO
reached *through* one. `redact.contents()` reads link text for a symlink, `b""`
for anything else irregular, and the file otherwise; `_edge` (the seam scan, a
second reader with its own `open`) goes through it too.

## F1 — `_assert_no_foreign_config` proves the wrong thing

It proves no *setting* reaches this repository from outside. It cannot prove the
repository is the store's, and there are three ways for it not to be. All three
measured with the guard removed:

| `.git` is | `init` | gitmemory settings written elsewhere | `commit` | result |
|---|---|---|---|---|
| a gitfile (`gitdir: …`) | refused | **5** | **succeeded** | transcript in the foreign repo's history |
| a symlink to a foreign `.git` | **succeeded** | **5** | **succeeded** | same, with no error anywhere |
| a symlink to nothing | **succeeded** | — | — | a complete repository **created** at the attacker's path |

The reported finding is row one, and the reported fix — "`is_repo` should accept
a `.git` file" — does not touch rows two and three. Row two is the bad one:
`os.path.isdir` follows the link, so `is_repo` says yes and `git init` is skipped
entirely, and then `_assert_no_foreign_config` **passes**, because it compares
`realpath(home/.git/config)` against itself and a symlinked `.git` resolves to
the foreign config on both sides. Nothing refuses, nothing warns.

Row one's refusal was real but came *fourth*: `git init` adopted the foreign
repository and four `git config` calls wrote into it before the assertion ran.

`_assert_own_git_dir` is one `lstat` and covers all three, before the first
`git` call. `lexists`, not `exists`, because row three's link resolves to
nothing and `exists` answers False for it.

**It is called from `commit` as well as `init`, and that is the part worth
keeping.** `run` deliberately carries on after a failed `init` — two watchers
racing `init`'s four `git config` calls is an ordinary lock collision, and the
bytes are the part no later pass can recover — so a refusal in `init` alone is
followed immediately by `add --all` and `commit` into the foreign repository,
which is what row one actually did. Putting the check where the write is costs
one `lstat` and covers every caller, rather than threading a flag from `run`
through `tick` for the one caller that was reported.

The first reproduction of row one **did not reproduce**: with `.git` pointing at
a path that did not exist, `git init` failed `fatal: not a git repository`. The
foreign repository has to already be there. That corrects the report — `git init`
does not create the far end. Row three is the exception, and only because the
*parent* directory exists.

## The mutation harness wedged, on exactly the defect it was testing

The negative-control pass for the above stopped printing and sat there. `ps`
found the pytest child 33 minutes into
`test_a_fifo_named_as_a_segment_does_not_hang_orphan_adoption`; the live mutant
was "adoption rehashes a run it cannot read", which removes the guard, so
adoption opened the FIFO and blocked — under the lock — and the test
*demonstrated* the mutant by hanging with it.

**A test that proves "this does not block" by blocking is unusable exactly when
it matters.** It takes the whole pass down, prints nothing, and scores no row.
Two repairs:

- `_deadline`, a `setitimer`-based context manager in `tests/test_store.py`. An
  `open` on a FIFO *is* interruptible by a signal, so the alarm converts "never
  returns" into a named assertion and the mutant makes the test **fail**. It has
  its own positive control — a deadline that never fires is indistinguishable
  from one that is never needed, because every test using it passes either way.
- `WEDGED`, a verdict. `run` bounds each suite at 600 s and returns `-1`; the
  row is scored not-caught, and the note names *which* of the two runs hung.

Interrupting it needed `kill -INT`, not `TERM` or `KILL`: SIGINT raises
`KeyboardInterrupt`, so `main()`'s `finally: path.write_text(original)` runs and
the mutated source is restored.

## The harness credited a mutant that does not parse

Found by re-running the seven rows above and reading them rather than the exit
code. All seven said CAUGHT; one of them should not have.

`verify opens a segment before asking what it is` deleted an `if`'s two body
lines and left the `if` and its comment behind. That is an `IndentationError`:
the mutant was never a program, never ran, and demonstrated nothing. The
`BROKEN` verdict exists for precisely this and did not fire, because it read
only the suite run — and **the suite run carries `-x`, under which a collection
error exits 1 or 2 depending on collection order.** Measured, three ways:

```
bad sorts first    -x -> 1    -k -> 2
bad sorts second   -x -> 2    -k -> 2
bad alone          -x -> 2
```

`(1, 2)` fell through `suite > 1`, past `intended == 5`, past `intended == 0`,
and landed on CAUGHT. The meta-test named
`test_a_mutant_that_stops_the_module_importing_is_not_credited` passed
throughout, because it asserts on `(2, 2)` — a pair this harness never produces.
**A check that passes for a reason unrelated to the behaviour it is named for,
in the file whose entire job is to refuse that.**

Three repairs, and the order matters:

1. `verdict` reads the intended run too, which has no `-x` and reported 2 in all
   three arrangements. `intended == 5` is excluded — that is `NO TEST`, not a
   crash.
2. The row's anchor now takes the `if` and its comment with it, so the mutant is
   a program and the row is a real negative control.
3. `test_every_mutation_row_produces_a_program` compiles all 303 mutants (and
   `bash -n`s the three shim rows) in two seconds. `verdict` is the last line of
   defence against this class; a compile check is the first, and it runs on
   every commit instead of once an hour.

The sweep over the whole index after the repair: **0 offenders.**

## Negative controls

Ten rows, 296 total. **10/10 CAUGHT by their intended test.**

| Row | Test |
|---|---|
| an unreadable manifest goes back to being a skip | `…refuses_the_push_instead_of_passing_it` |
| the gate reads the store as leniently as a reader does | `…refuses_the_push_instead_of_passing_it` |
| a non-list segments field skips without telling the gate | `… and segments as a dict` |
| the readers become as strict as the gate | `test_the_readers_still_skip_what_the_gate_refuses` |
| the refusal stops naming which manifest it cannot read | `test_the_readers_still_skip_what_the_gate_refuses` |
| an unreadable manifest leaves by the traceback handler | `test_push_names_the_unreadable_manifest…` |
| the refusal prints a session id that is itself a credential | `test_the_refusal_masks_a_session_id…` |
| the top-level handler prints an exception's credential | `test_a_credential_in_an_exception_message_is_masked…` |
| exception text stops being masked at all | `test_a_credential_in_an_exception_message_is_masked…` |
| the parse-failure warning prints the path it could not read | `test_a_credential_in_an_exception_message_is_masked…` |

Seven more for S4/S12a, 303 total. **7/7 CAUGHT.** The first of these is the row
that scored CAUGHT while not being a program; the number above is the re-run
after both the row and `verdict` were repaired.

| Row | Test |
|---|---|
| verify opens a segment before asking what it is | `…does_not_hang_verify` |
| adoption rehashes a run it cannot read | `…does_not_hang_orphan_adoption` |
| the file-type check answers yes to everything | `…does_not_hang_verify` |
| the gate follows a symlink instead of reading its text | `test_the_gate_scans_a_symlinks_text_and_not_what_it_points_at` |
| the gate opens whatever it is handed again | `test_a_fifo_in_the_gates_file_list_does_not_block_it` |
| the seam scan opens what the file scan does not | `test_the_seam_scan_reads_a_symlink_the_same_way_the_file_scan_does` |
| the deadline leaks its timer into the next test | `test_the_deadline_fires_on_something_that_really_blocks` |

The last row is the only mutation of `_deadline` that is a program rather than
another wedge: removing the timer, lengthening it, or making the handler return
all end with the FIFO blocking and the row hanging on it. A leaked timer still
returns — and it is a real defect, because it fires inside whatever test runs
next, which reads as that test being flaky.

Four more for F1, 307 total. **4/4 CAUGHT.**

| Row | Test |
|---|---|
| a symlinked git dir is treated as a git dir | `…refused_before_anything_is_written[symlink]` |
| the ownership check follows the link before deciding | `test_a_git_dir_symlinked_to_nothing_does_not_make_a_repository_somewhere_else` |
| init refuses a foreign git dir only after writing to it | `…refused_before_anything_is_written[gitfile]` |
| the commit trusts that init already refused | `test_the_commit_refuses_the_same_git_dir_init_refused` |

The third row is the one that shows why the ordering is a property and not
tidiness: with the call removed from `init`, `_assert_no_foreign_config` still
refuses the gitfile, so a test asserting only "it raises" stays green. What goes
red is the assertion that the foreign repository has none of gitmemory's
settings in it.

## S2, S12b, S13 — three holes in the test that stops this repo shipping its author's machine

`tests/test_no_owner_data.py` is the one gate whose failure mode is a public
repository containing somebody's private paths. All three findings are against
it, and they compose: S2 says it looks in the wrong place, S12b says it reads
the wrong bytes, S13 says its patterns miss.

**S2 — the working tree is not what a push sends.** The scan walked tracked and
untracked files. A file committed and then deleted is gone from `git ls-files`
and present in every clone for ever. `_history_scan` now asks the object graph,
through `gitrepo.pushable_objects` — the same feed the egress gate already uses,
so "what would leave this machine" has one definition and both gates read it.
It streams blobs, trees **and** commits through one `git cat-file --batch` pipe,
which is what makes commit *messages* scannable, and that mattered immediately:
**the first thing it caught was the commit message of the commit that
introduced it.** Two paths naming an account, written by the author of the
scanner, in the same change. A gate is only real if it can catch you.

**S12b — `read_text()` on a symlink reads the target.** Git stores a symlink as
mode `120000` with the link text as the blob; the target is not in the
repository at all. So the scanner opened files outside the tree — which is
itself the thing this test exists to prevent — and never read the bytes that
ship. On the fixture, the only hit the old scanner produced was a read it should
not have made, and it found none of the six planted leaks. `os.readlink` reads
what is committed. Same finding as S12a, one directory over; they were fixed
separately because the products are different (`redact.contents` is the gate's,
this is the test's) and sharing code between them would put the test's
correctness downstream of the code it is checking.

**S13 — 23 evasions, 4 caught.** The trailing `/` on the two home patterns is
the whole finding: it required a path *component* after the home, so the two
most likely pastes — an `export HOME=` line and a "run it from" line — went
straight through. It bought nothing the character class did not already buy. The
private-tree pattern was the one without `re.I`, so a capitalised directory name
walked past it. And the file *name* was never scanned, though it ships with the
content.

Dropping the trailing `/` made the scanner fail on its own history: `/Users/x`
appears as a regex sample in ~20 old blobs of `mutate_index.py`, and history
cannot be edited. `PLACEHOLDERS` excuses that exact span and only that span. An
earlier draft excused the *sample* spans too, which would have blinded the
positive control — those are precisely what the control plants.

Two mutation rows had to be written as string concatenations, broken across a
`+`. The scanner reads `mutate_index.py`, so a row cannot spell out a span its
own patterns match; both halves rejoin before the mutant is applied.

**1100 tests, 414 negative controls. 13/13 of the affected rows CAUGHT.**

## S6 — the one check that finds unattested bytes, switched off from inside the file under suspicion

`_verify_one` ran ten checks with an early `return` on each, and the stray-file
sweep — the check whose entire job is to find bytes no manifest attests — was
the last statement after all of them. Any earlier failure took it down, and the
attacker picks which failure to show.

Measured end-to-end through `verify()` rather than by reading the function,
which is what made the second half of the finding visible: **`_verify_unattested`
does not cover for it.** That sweep marks a generation directory attested when a
manifest for it *exists*, not when it verifies, so it skips the directory as
somebody else's job. Nobody reports the bytes. Reading `_verify_one` alone would
have suggested a duplicated message; running the whole command showed a hole.

Plant a transcript in a generation directory and break the manifest: **eight of
eight tamperings hid the plant**, each reporting one innocuous-looking line
instead. After the fix, eight of eight are seen — and a ninth case with it, a
NUL in a segment path, which is a `str` that passes every type check and then
raises out of `os.lstat`. That one only surfaces because the checks are now
wrapped for the same reason `verify` wraps them: a manifest is untrusted data,
and "the directory is always swept" is only worth having if nothing in the file
being checked can switch it off.

Three details the tests pin:

- The sweep says a **different sentence** when the manifest was rejected.
  `listed` is empty or partial then, so a sound generation's own segments land
  in the sweep too; calling those "unrecorded" would point at the directory when
  the finding is in the manifest.
- The directory name comes off the manifest's **filename**, not its declared
  `generation` — the same "trust the location, not the declaration" move
  `_verify_manifest` already makes for agent and session, and the only way the
  sweep can run at all when `generation` is what failed.
- The blanket guard is a mutation row of its own, because a fix that holds only
  for failures somebody predicted is not the fix.

**1109 tests, 417 negative controls. 3/3 new rows CAUGHT.**

## S7, S8, S9, S10 — the shim, which runs inside somebody else's program

Four findings against `hook/gitmemory-hook.sh` and the one directory it writes
to. All four reproduced against the shipped file before anything was edited.

**S7 — a denylist that claimed parity with an allowlist.** The refusal message
is the only place the shim puts an environment variable's value into an agent's
transcript, and it stripped control characters with `tr -d '\000-\037'` under a
comment saying it held itself to `records.safe_text`. That standard also spells
out DEL, the C1 block, the bidi marks and U+2028/9. Measured: **DEL, U+009B (a
working CSI wherever C1 is decoded), U+202E and U+2028 all reached stderr
intact.** The denylist could not be extended, either — those are multi-byte in
UTF-8 and `tr` deletes bytes, so removing the lead byte of an override leaves
the other two and the terminal sees a mangled sequence rather than nothing. It
is now `LC_ALL=C tr -cd '\040-\176'`, and the message says `(printable ASCII
only)`, because a value written in a non-Latin script comes out empty and an
empty value must not read as unset.

The test is parametrised over all five classes but **asserts the allowlist**
rather than the five — every byte of the message is printable ASCII — so a sixth
way in fails here without anyone having thought of it first.

**S8 — the shell speaks after the redirect is over.** The shim's `2>/dev/null`
is on the write; the shell's job report is printed when it *reaps* the child,
which is later. With `ulimit -f` set in the environment, a payload over the
limit killed `cat` with SIGXFSZ and `/bin/sh` announced it — this script's path
and a line number — straight into the agent's stderr. The failure itself was
always handled correctly; the noise was the whole symptom, which is the same
shape as the earlier `set -C` finding. A brace group with its own redirect
catches it, and a group is not a subshell, so it costs no fork on the one path
in this system a user waits for.

**S9 — the third directory that holds transcript bytes.** `home` and `.git` are
re-chmodded on every start, for the fs-F2 reason: a mode set once is a mode set
never. `spool/` holds whole unredacted payloads under a naming grammar anyone
can guess, and nothing ever looked at it. The shim creates it correctly, under
`umask 077` — but `mkdir -p` sets a mode only when it creates, and the `[ -d ]`
fast path means an existing spool is never looked at again. Measured, one left
at 0777 stayed 0777 across every fire. Fixed in `gitrepo.init` rather than the
shim: there it is free and idempotent and settled by the same argument as the
other two, while in the shim it would be a fork on the hot path. The records
inside are 0600, which is not the point — a traversable directory with
predictable names is enough.

**S10 — accepted, not fixed.** Two refusals are loud and every write failure is
silent. That asymmetry is deliberate: a refusal is a configuration you fix once,
and a message on the write path would fire at every compaction for as long as a
disk stayed full, which is the noise this shim exists not to make. The finding
was right that nothing said so. `hook/README.md` now does, including the two
things a broken run costs beyond the one capture — the single line of shell
noise above, and the fact that a record is as large as its payload. **The size
cap is not coming:** `head -c` costs the same one fork but closes the pipe
early, and an agent that does not handle `EPIPE` on its own hook would die.
Trading a large file for a killed session is the wrong way round for a program
whose first rule is never to disturb the caller.

## S11 — a default that reads the developer's own machine

`find_session(session_id)` defaulted to searching the real
`~/.claude/projects`. No caller in the tree used it that way, which is exactly
why it survived: it changed nothing today. The watcher's stated rule is that
watch roots have **no** default and a misconfigured run is loud rather than
guessing; a helper over the same data with a silent default is that rule with
one exception nobody chose, waiting for a second caller.

The signature alone does not close it, because the other route to somebody's
transcripts is the environment. `$HOME` feeds `expanduser` and `$GITMEMORY_HOME`
feeds `resolve_home`, so a test that omits a root gets the real one and passes.
`tests/conftest.py` — the repository's first — points `HOME` at a directory
under `tmp_path` for every test and drops `GITMEMORY_HOME`. **Set, not deleted:**
an unset `HOME` sends `expanduser` to the password database, which finds the
real home anyway.

This is the fix `clean_env` already carried for the hook tests, after a mutation
run wrote 74 files into a developer's home, applied once instead of per file. An
autouse fixture is invisible at every call site it protects, so the one thing
that can go wrong with it — somebody deletes a line and everything still passes
— is the thing nothing would catch. It gets its own negative control:
`tests/test_isolation.py` compares `$HOME` against the password database's
answer, which is the copy the fixture cannot reach.

The signature is pinned by an `inspect` test rather than a behavioural one, and
the docstring says why: with the default restored, a test that omits the root
reads whatever home it is pointed at and passes. The behaviour being removed is
not observable from outside.

## Negative controls, the rest of the round

Seventeen more rows: eight across S2/S12b/S13, three for S6, four for S7/S8/S9
(one of those a repair to the row S7 invalidated), two for S11. Three existing
rows were repaired rather than added: one whose anchor the S6 refactor moved,
and two in the S2 group rewritten as string concatenations, because the scanner
reads `mutate_index.py` and a row cannot spell out a span its own patterns
match. **422 rows in total.**

| Row | Test |
|---|---|
| the scan sees the checkout and not the object graph | `test_the_history_scan_finds_leaks_the_checkout_no_longer_has` |
| the history scan skips the blobs it cannot decode | `test_the_history_scan_finds_leaks_the_checkout_no_longer_has` |
| the owner-data scan follows a link out of the repository | `test_the_scanner_finds_leaks_that_are_really_there` |
| the owner-data scan ignores the file name | `test_the_scanner_finds_leaks_that_are_really_there` |
| a placeholder span excuses more than the one it names | `test_the_scanner_finds_leaks_that_are_really_there` |
| the home patterns need a trailing separator again | `test_the_patterns_would_actually_catch_something` |
| the private-tree pattern is case-sensitive again | `test_the_patterns_would_actually_catch_something` |
| a tracked file the scan cannot read is not reported | `test_nothing_tracked_is_a_file_the_scanner_cannot_read` |
| a rejected manifest takes the generation sweep down with it | `test_a_rejected_manifest_does_not_hide_a_plant_beside_it` |
| a crash in the manifest checks switches the generation sweep off | `test_a_rejected_manifest_does_not_hide_a_plant_beside_it` |
| the sweep calls a rejected manifest's segments unrecorded | `test_a_rejected_manifest_does_not_hide_a_plant_beside_it` |
| the shim's allowlist goes back to the C0 denylist it was | `test_a_refusal_cannot_rewrite_the_agents_terminal` |
| the shim echoes an environment variable's control characters | `test_a_refusal_cannot_rewrite_the_agents_terminal` |
| the outer redirect goes, so the shell's own job report reaches the agent | `test_a_file_size_limit_does_not_put_the_shells_own_noise_in_the_transcript` |
| init makes the spool but never repairs one that already exists | `test_the_spool_is_owner_only_whatever_it_was` |
| find_session gets its real-home default back | `test_the_adapter_has_no_default_place_to_look_for_transcripts` |
| the suite-wide home isolation is switched off | `test_no_test_can_see_the_account_that_is_running_it` |

Every one CAUGHT by its intended test. The suite stands at **1120 tests**.

The last row is the one worth reading twice. Its mutant removes the home
isolation and then runs the **whole** suite against a real home — which is
acceptable only because that is precisely the state the suite was in one commit
earlier, green, with `clean_env` already carrying the hook tests. Checked
afterwards: no `~/.gitmemory`, nothing of gitmemory's in the developer's home.
