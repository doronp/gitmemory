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
| S4 | A FIFO planted in the store hangs `verify` and adoption, under the lock | fixed — `store._regular` before every segment `open` |
| S12a | The gate opens what a tracked symlink points at, and blocks on a FIFO | fixed — `redact.contents`, used by the file scan and the seam scan |
| S12b | The owner-data *test* scanner does the same | open |
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
