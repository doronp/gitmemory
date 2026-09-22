# E7 — the pair review of the security round

The round's own rule, turned on the round. Six agents reviewed the code; this
is what happened when an independent reviewer was given the *fixes* and asked
to break them.

Two runs, each in its own git worktree, each opening with an attestation naming
the files it read, the commands it ran, and the sentence that it read nothing
outside this repository — in particular nothing under the user's `~/.claude/`
or `~/memory/`. Neither run was shown the other's report.

Twelve items. **Four were real and are fixed. Four did not reproduce. Two were
declined with a reason. One was a duplicate of another run's finding, which is
the most useful thing in this document. One was not a finding at all.**

And one the review did not make, which the suite made for it.

| # | Run | Claim | Disposition |
|---|---|---|---|
| A1 | A | a placeholder span is not a placeholder path | **fixed** — `a591644` |
| A2 | A | the object graph cannot see through a gitlink or an LFS pointer | **fixed** as a tripwire — `a591644` |
| A3 | A | `_verify_one`'s blanket `except Exception` hides logic errors | **refuted** |
| A4 | A | `clean_env`'s `HOME` is redundant with the conftest's | **refuted** |
| B1 | B | owner-data scanner blinded under username `x` | **duplicate of A1**, arrived at independently |
| B2 | B | `pushable_objects` misses a detached `HEAD` | **refuted by its own script** |
| B3 | B | `g-1.json` hides a generation from both sweeps | **confirmed smaller than reported**, fixed — `6f879e6` |
| B4 | B | `g001.json` leaves the real generation directory unswept | **refuted** |
| B5 | B | an inherited `xtrace` traces the shim into the agent's stderr | **confirmed worse than reported**, fixed — `6f879e6` |
| B6 | B | the spool's mode should be re-applied per tick, not per start | **declined with reasoning** |
| B7 | B | a `/tmp` sentinel buys a once-per-boot write warning for free | **declined with reasoning** |
| B8 | B | a symlink-isolation test was vacuous before `798ab48` | not a finding — it agrees with the fix |
| — | suite | a `verify` that gave up on the lock swept anyway | **fixed** — `a591644` |

## The two that were real and are in the code

### B3 — two sweeps, two definitions of a generation directory

`_GEN_RE = re.compile(r"^g(\d+)\.json$")` is the single definition of "this is a
generation manifest". `verify`'s main loop used it. `_verify_unattested` — the
sweep that asks which files in this store no manifest speaks for — did not: it
built the manifest path from the directory name and asked `os.path.exists`.

`g-1.json` is a file and is not a manifest, because a hyphen is not a digit. So
`verify` skipped it, and `_verify_unattested` marked `raw/…/g-1` attested on the
same file, and the bytes inside that directory were reported by neither. Both
sweeps use the one regex now.

**Reported as larger than it is, and that matters for the next one.** The report
says "Run `verify()`. It will report zero problems, leaving the stray files
entirely unchecked." Measured, `verify` reported one problem —
`sessions/claude-code/sess/g-1.json: not a manifest` — and left the planted
bytes unnamed. The store was not silent; it named the odd manifest and not the
transcript beside it. A reporting gap in a command whose whole value is being
believed is worth fixing on its own, and it is still worth fixing after the
overstatement is stripped out. But "reports zero problems" and "reports the
wrong one of two problems" are different findings, and only one of them was
there.

### B5 — the shim traced into somebody else's agent

`/bin/sh` here is bash, and bash honours an exported `SHELLOPTS`. That is not a
new fact in this file: the `:-` default on every parameter exists because
`SHELLOPTS=nounset` killed the shim on line 27 during E4. `xtrace` is the same
door, one option over.

**Worse than reported.** The report describes leaked "internal script paths and
lines". Measured with `SHELLOPTS=xtrace`, it is 700 bytes into the agent's
stderr *on every compaction*, and among them:

```
+ H='/tmp/gm-xt/h2\033[2K\rHIJACKED'
```

`od -c` on the captured stderr, with the ESC intact. That is a direct bypass of
E7 S7 — the allowlist filter that exists so this exact variable cannot edit the
terminal when the shim refuses a relative path. S7 strips the value in the one
message that echoes it; `xtrace` printed the same value raw, three lines
earlier, without the message.

Two candidate fixes were measured rather than reasoned about. Bare `set +xv`
leaves ten bytes, because the shell traces the command that turns tracing off.
`{ set +xv; } 2>/dev/null` leaves zero and costs no fork — a brace group is not
a subshell, which is S8's trick for S8's reason. It is the first command in the
file, because anything above it is traced before it runs.

## The four that did not reproduce

**A3.** The claim was that a blanket `except Exception` in `_verify_one` would
swallow a logic error. It does not: the guard puts `{exc!r}` into the problem it
reports, and every healthy-path assertion in the suite is "no problems". An
injected `AttributeError` took the suite red at test 457.

**A4.** Removing `clean_env`'s `HOME` fails
`test_with_no_variable_set_the_spool_lands_under_the_documented_default`, which
asserts on the specific directory the fixture names. Not redundant.

**B2 — refuted by the reviewer's own reproduction script.** The claim is that
`git rev-list --objects --all` omits a detached `HEAD`, so a secret committed
there bypasses the egress gate. Run the script in the report verbatim, on git
2.50.1:

```
--- rev-list --all ---
97038efaac58d6386681f5cfcd135cfdc21e697d
cb6888a22a19b0d0e053b2f0394340ed3c275610
--- rev-list --all HEAD ---
97038efaac58d6386681f5cfcd135cfdc21e697d
cb6888a22a19b0d0e053b2f0394340ed3c275610
```

Identical, and the "missing" detached commit is the first line of both. The
report's own stated output — "completely absent from `git rev-list --all`" — is
not what the command prints. `git-rev-list(1)` documents `--all` as "all the
refs in `refs/`, along with `HEAD`". Checked at the level that matters as well:
`gitrepo.pushable_objects` saw a planted `leak.txt` both with a branch and with
no branch at all.

**B4.** The claim is that a manifest named `g001.json` leaves the real
generation directory unswept. The reproduction gives three problems, including
`raw/claude-code/sess/g00/planted.jsonl: no manifest speaks for these bytes`.
The unattested sweep is exactly the thing that covers this: a directory no
manifest reconciles with is a directory whose bytes nobody has vouched for, and
that is the sentence it prints.

## The two declined, with the reason

**B6 — re-`chmod` the spool on every tick.** Declined. `gitrepo.init` re-applies
`0o700` on every watcher start, not once — that is finding 3 of the round's
three cross-surface truths, and it was fixed there. Within a run, `drain_spool`
already refuses anything that is not a regular file (fs F3) and treats every
record as untrusted input regardless of who wrote it. The residual case is a
process on this machine that can `chmod` your spool, and a process that can do
that can read the store; the mode is not what is protecting you. This is fs F8's
reasoning (`PATH`) applied to a directory instead of a binary.

**B7 — a `/tmp` sentinel for a once-per-boot write warning.** Declined, and the
proposed implementation is the reason rather than the policy. The report's case
rests on `/tmp` being "RAM-based and cleared on boot"; on macOS it is neither —
it is a symlink to `/private/tmp` on the boot volume, and its clearing is a
periodic-task matter, not a boot guarantee. More to the point, `:>
/tmp/gitmemory_spool_warned` writes to a fixed name in a world-writable sticky
directory, on a path touched at every compaction. Anyone on the machine can
pre-create that name as a symlink and turn a documented silence into a
write primitive. S10's silence is a cost this project has already priced and
written down where a user meets it; the remedy is worse than the limitation.

## The duplicate, which is the most useful line in the table

A1 and B1 are the same finding, reached by two runs that could not see each
other: a placeholder span (`/Users/x`) is not a placeholder path, so a key file
*under* the placeholder home matched the placeholder itself, compared equal to
the excuse, and went through.

Corroboration is the only thing in this document that is evidence about the
*method* rather than about the code. Four of twelve items did not reproduce;
against that base rate, a claim two independent runs make is worth more than a
claim either makes alone. Both proposed fixes were read and neither was taken:
scoping the excuse to two filenames still excuses a leak planted in
`tests/mutate_index.py`. `_hits` looks at the character after the span instead,
which is safe against the whole history because no blob in the object graph puts
a path separator after the placeholder.

## The finding nobody reported

Running the full mutation pass saturates the machine, and under that load
`test_verify_does_not_report_a_live_capture_as_corruption` failed twice: **71 of
a healthy store's live segments reported as litter.**

That is the E4 store-5 false positive, back again and now gated behind E7
fs-F6's five-second bound. fs-F6's own docstring had said so — a holder that
outlasts the wait "gets the treatment a read-only store already gets: the check
runs unlocked, which can be wrong". Nobody read that sentence as a finding,
including the person who wrote it, until a test made it happen.

The fix is in the fs report under F6. The part that belongs here is what it says
about reviewers: a bounded wait looks like a fix and reads like a fix, and six
agents plus two independent runs all looked at it and none of them said "and
then it lies". A test running on a loaded machine did.

## The two rows that were not attribution problems

The same full pass produced two `MISSED` verdicts — mutant caught, but not by
the test named for it. `MISSED` normally means the intended test is decorative
and some other test is doing the work. Both of these meant something else, and
finding out which required a third harness fix.

**`SURVIVED` was unreachable.** The suite the harness runs includes
`test_every_mutation_row_anchors_exactly_once`, which reads the file the harness
has just mutated and asserts every row's anchor is present. Under a mutant the
anchor has been replaced, so it is absent, so that test fails — for every
mutant, on every row, whether or not one line of the product is pinned by
anything. The harness's first question ("revert one behaviour; does the suite go
red?") was being answered by the harness's own bookkeeping, and had been for as
long as that bookkeeping existed. It is deselected in the suite run now, and
only there: outside a mutation the anchor is supposed to be present, and that
test has caught a row broken by an ordinary product edit twice.

With that removed, both rows turned out to name **code a later fix had made
dead**, which is a different and more interesting thing than a weak test.

- `_fill` fed the digest twice for a skipped generation. An E3 loop at the end
  of the function wrote `{"skipped": "<key>: <repr(exc)>"}`; the E6 generation
  row writes the same two facts as `session_key` and `skip_reason`. Deleting the
  loop leaves its named test green and the whole suite green. It was worth one
  careful look first — `skip_reason` goes through `_encodable`, which replaces
  what sqlite3 cannot encode, while `canonical_json` escapes it — but `repr`
  turns a lone surrogate into six ASCII characters before either sees it, so
  there is nothing for `_encodable` to lose. The loop is gone; the row anchors
  on the line that does the work.
- `os.path.abspath` on the index target was what made `--db out.db` work:
  `dirname("out.db")` is `""` and `makedirs("")` is `ENOENT`. Then index-F9's
  fix added `realpath` on the caller-supplied parent, and `realpath("")` is the
  working directory. Dead on the other branch too, since `resolve_home` already
  returns a realpath. Removed, and the row re-anchored onto the `realpath` line
  — deliberately sharing an anchor with the row for the symlink behaviour. One
  line, two behaviours, two rows, two named tests.

Neither line was wrong and neither was a bug. Both were comments claiming to be
the reason something worked, three fixes after they had stopped being the
reason, which is the kind of thing that survives every review that reads code
for defects.

## What it cost

**1121 → 1127 tests, 423 → 431 negative controls**, for four fixes and the
harness repair below.

Every one of the four has a row in `tests/mutate_index.py` that reverts it, and
the named test must go red when it does. Two of the rows are the two ends of one
wire: for the lock timeout, one deletes the branch that declines the sweep, and
the other leaves the branch in place and never arms it. A fix with one control
is a fix you have checked from one side.

## The overstatement problem

Both runs claimed more verification than they did, in the same shape, and it is
worth naming because it is the failure mode of a reviewer that writes well.

Run B's attestation lists "Git experiments in `/tmp` to verify `git rev-list`
behavior on a detached HEAD" among the commands it ran. Those experiments
disprove its own finding 2. The command was run; the output was not read, or was
read as confirming what the reviewer already believed. Run A overstated in the
same direction twice, in findings it also got right.

This is the whole reason the round's rule is *re-derived*, not *re-run*. Reading
a reviewer's script and watching it print the same thing checks the script. Four
of these twelve items survive only because someone ran the claim by a different
route and looked at what came back.
