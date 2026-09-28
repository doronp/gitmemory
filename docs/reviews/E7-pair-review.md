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
| A1 | A | a placeholder span is not a placeholder path | **fixed** — `7622748` |
| A2 | A | the object graph cannot see through a gitlink or an LFS pointer | **fixed** as a tripwire — `7622748` |
| A3 | A | `_verify_one`'s blanket `except Exception` hides logic errors | **refuted** |
| A4 | A | `clean_env`'s `HOME` is redundant with the conftest's | **refuted** |
| B1 | B | owner-data scanner blinded under username `x` | **duplicate of A1**, arrived at independently |
| B2 | B | `pushable_objects` misses a detached `HEAD` | **refuted by its own script** |
| B3 | B | `g-1.json` hides a generation from both sweeps | **confirmed smaller than reported**, fixed — `d842723` |
| B4 | B | `g001.json` leaves the real generation directory unswept | **refuted** |
| B5 | B | an inherited `xtrace` traces the shim into the agent's stderr | **confirmed worse than reported**, fixed — `d842723` |
| B6 | B | the spool's mode should be re-applied per tick, not per start | **declined with reasoning** |
| B7 | B | a `/tmp` sentinel buys a once-per-boot write warning for free | **declined with reasoning** |
| B8 | B | a symlink-isolation test was vacuous before `266d1e3` | not a finding — it agrees with the fix |
| — | suite | a `verify` that gave up on the lock swept anyway | **fixed** — `7622748` |

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

## The three rows that were not attribution problems

The same full pass produced three `MISSED` verdicts — mutant caught, but not by
the test named for it. `MISSED` normally means the intended test is decorative
and some other test is doing the work. All three meant something else, and
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

With that removed, all three rows turned out to be **a later fix having
quietly taken over**, which is a different and more interesting thing than a
weak test.

- `_fill` fed the digest twice for a skipped generation. An E3 loop at the end
  of the function wrote `{"skipped": "<key>: <repr(exc)>"}`; the E6 generation
  row writes the same two facts as `session_key` and `skip_reason`. Deleting the
  loop leaves its named test green and the whole suite green. It was worth one
  careful look first — `skip_reason` goes through `_encodable`, which replaces
  what sqlite3 cannot encode, while `canonical_json` escapes it — but `repr`
  turns a lone surrogate into six ASCII characters before either sees it, so
  there is nothing for `_encodable` to lose. The loop is gone; the row anchors
  on the line that does the work.
- `os.path.abspath` on the index target claimed, in its own comment, to be what
  made `--db out.db` work: `dirname("out.db")` is `""` and `makedirs("")` is
  `ENOENT`. Removed, because reverting it left the named test green and the
  suite green.
- `bench/gate.py`'s `hasattr(derive, "decisions")` check — the one that must not
  turn an ImportError from inside `derive` into "no decisions yet" — was never
  reached by the test named for it. The test replaces `derive` with an object
  that raises ImportError from *every* attribute, and the E5:R8 line above the
  check, `print(f"scoring {derive.__file__}")`, was added afterwards. It trips
  first, the test's `pytest.raises(ImportError)` is satisfied, and the check is
  never executed. Unlike the two above, the behaviour here is real and is in one
  line: `__file__` is a plain string on the stub now and only `decisions`
  raises, so the test exercises what it is named for.

The digest row re-anchored onto the line that had been doing its work since E6.
The `--db` row did not, and that is the more interesting half. Re-anchored onto
index-F9's `realpath` — `realpath("")` being the working directory — it scored
MISSED again. So the reasoning was walked instead of assumed, and none of it
held: this code does not call `makedirs`, `_mkdir`'s walk is `while path:` so an
empty parent is zero levels to create rather than an error, `os.path.join("",
name)` is `name`, and a relative path resolves against the working directory
like any other. The flag works because three separate things tolerate the empty
string. There is no line to revert, so **the row was deleted** and both
`tests/mutate_index.py` and the test's own docstring say why — a row that can
only ever score MISSED sends the next reader to audit an attribution that was
never wrong.

None of the three was a bug, and that is what makes them hard to find. Each was
a claim — in a comment, or in a test's choice of what to break — that had been
true when it was written and was quietly made false by a later fix landing
above or beside it. The `--db` comment had been wrong about *two* mechanisms,
the one it named and the one that replaced it, and a passing test said nothing
about either because the behaviour was never in one place to begin with.

A review that reads code for defects will not find any of this. A green test
suite will not either: all three tests were green throughout, and two of them
were green for reasons their authors would not have recognised. Only asking
*"does this test fail when I remove the thing it is named for"* separates a
test from a decoration, and only asking it of every row, on a schedule, finds
the ones that were fine when written.

## The full pass, and the three rows that held nothing at all

With `SURVIVED` reachable, every row was run: **422 rows, 415 caught by their
intended test, 6 `MISSED`, 1 `BROKEN`.** Three of the six are the section above.
The other three were not `MISSED` — they were `SURVIVED`, and the pass could not
say so, for a second reason with the same shape as the first.

**A red baseline makes `SURVIVED` unreachable all over again.** The pass ran in
its own worktree, which has no clone of the conformance corpus, so
`test_the_readme_test_count_is_the_test_count` was already failing there before
any mutant was applied — that is the README defect, found by this pass and
written up in `tests/test_docs.py`. The suite run carries `-x`. It therefore
exited 1 on every row whatever the mutant did, and every unpinned row scored
`MISSED` — "caught, but not by its test" — which sends a reader to audit an
attribution when the behaviour is held by nothing. The three were re-derived in
a tree whose baseline was green, without `-x`: under each mutant the only red
test was the harness's own bookkeeping, 1126 passed, nothing else moved.

The three, and none of them was a weak test in the way `SURVIVED` usually means:

- **`for context|fyi|fwiw` is deletable.** The test's two framed examples were
  *"the old build never ran the tests"* and *"the previous team never wrote
  tests"* — reports of somebody else's past, which the extractor does not read
  as decisions at all. The guard was never what stopped them. Replaced with two
  framed *prohibitions*, which the directive class does claim and which the
  frame is the only thing declining. A guard can only be tested on an input it
  is the only thing stopping.
- **The perfect-tense hedge frame is load-bearing, and its test's example was
  not.** `I honestly have never needed that flag` is not read as a rule with the
  frame or without it. A first probe deleted the whole branch and labelled
  nothing, which nearly bought the conclusion that it was dead code; a second,
  written adversarially, showed what it is for: a participle spelled like a bare
  verb. *"We have never run migrations by hand"* without the frame is `never
  run` to the directive class, and a report of the past is filed as an order.
  `run`, `let`, `put`, `set` — the test now carries two of them, one in each
  adverb slot.
- **The dashboard row was mutating a column nothing reads.** It swapped
  `session_id` for `session_key` in the outer `SELECT` of `dash_requests`, on
  the theory that the per-generation key is what would double the bill. The
  dedup partitions on `agent, request_id` alone; the projected column plays no
  part in it. So the mutant renamed an output nobody asserts on and the test
  stayed green through a full pass — the *row* was wrong, not the test. It now
  puts `generation` into the window, which is the thing the test's own docstring
  describes: grouping per generation bills the conversation twice.

## `BROKEN` was a sentence about the wrong file

The seventh: `a non-list segments field skips without telling the gate`, reported
as *"the mutant does not import"*. The mutant imports. The row selected one
parametrisation by its prose id — `test_… and segments as a dict` — and pytest's
`-k` is identifiers joined by and/or/not with **no string literals**, so pytest
9.1.1 collected the whole suite and exited 4 having run nothing. Re-derived
directly from the command line, which is a route the harness does not use.

Three repairs, because the row was only the first of them:

- the row selects `… and dict`, the one word of that id no other case of the
  test contains;
- `verdict` keeps the `BROKEN` tag for exit 4 — it is still not evidence — but
  stops saying the mutant did not import, because that sentence sent this
  reader to the mutant;
- and a two-second pre-flight, `test_every_mutation_row_selects_with_an_expression_pytest_can_parse`,
  parses every row's expression with pytest's own parser. The hour-long pass
  should not be the thing that finds a typo in a row.

## The row that the workaround switched off

The flush repair above got the usual row — delete `flush=True`, expect
`test_a_verdict_reaches_the_log_before_the_next_row_runs` to go red — and it
scored **`SURVIVED`**, while applying the same mutant by hand and running the
same test made it fail in a tenth of a second. Two routes, two answers, and the
difference between them was the environment: the harness had been started with
`PYTHONUNBUFFERED=1`.

Of course it had. That is what you set when the verdicts interleave and you want
the log readable *now* — the workaround for the defect, applied by the person
about to check that the defect is fixed. The test spawned its child with
`{**os.environ, ...}`, so the flag reached the child, the child flushed whether
`say` asked or not, and the one test in the suite whose subject is buffering was
the one test the flag silently disarmed. It would have passed on a laptop and
failed in CI, or the reverse, and the mutation record would have said the
behaviour was pinned either way.

The test now pins the environment instead of inheriting it, and the pin has a
floor that is the mutant itself: an unflushed `print` in the same arrangement has
to come out *behind* the child, or there is no buffering here to get wrong and
the assertion that follows proves nothing. Re-measured under
`PYTHONUNBUFFERED=1` — the environment that hid it — the row now scores
`CAUGHT`.

The general shape is worth the paragraph: **a test that reads `os.environ` lets
the machine it runs on decide the verdict**, and the environments a harness is
run under are not a random sample. They are exactly the ones a tired reader
reached for.

## What it cost

**1127 → 1133 tests, 423 → 437 negative controls**, for four fixes, the harness
repair below, and the seven rows above. Eight rows added and one deleted — the
deletion is in the section above, and is the only row this project has ever
removed for being unanswerable rather than obsolete. Both test counts are
corpus-inclusive, and that unit is the last thing this round retired.

**The 1127 is retired, and finding out why is the last thing this round did.**
It was the count on a machine that had cloned claude-code-log's corpus; a fresh
checkout collected 811 at that point, and 812 once this round's last doc test landed. The test that exists to stop the README quoting a number
nobody else can reproduce was itself asserting one, and passed for two epochs
because the clone happened to still be in `/tmp`. The README now states the
offline count and names the corpus separately, and no environment default in
this repository points into a scratch directory.

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
