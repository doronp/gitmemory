# E4 standalone review round

The cross-review in `E4-cross-review.md` is Gemini and me reading each other's
half. This is the other thing: **independent review agents**, dispatched per
module against the committed tree, told to find defects and not to fix them.
Four reports came back — store, watcher, CLI/git layer, documentation — with
thirty-four findings between them. A fifth pass came later and asked a different
question: not *is this code wrong* but *is this test load-bearing*. It is
written up under "The vacuity audit" below, and it is the one that found things
no reviewer had.

Two rules governed the round, and both of them changed the outcome:

1. **Verify every finding against HEAD before acting on it.** Three did not
   survive that check, and one of the three was closed by a fix landed in the
   same session by a different report.
2. **Write the test before the fix and observe it fail.** A fix nothing
   distinguishes is a fix nothing is holding in place. Where a finding's test
   passed *before* the fix, that is recorded below and the finding was
   re-examined rather than shipped.

| Report | Findings | Fixed here | Not a fix | Commit |
|---|---|---|---|---|
| git layer + CLI | 6 | 5 | 1 stale, pinned instead | `ccfafdd` |
| store | 8 | 8 | — | `74626f1` |
| watcher | 10 | 9 | 1 already closed by a store fix | `38b1f59` |
| documentation | 16 | 16 | — | `a52b0b4`, `727789a` |
| vacuity audit | 24 | 7 | 15 mis-attributed, 1 equivalent, 2 false positives | `45362f3`, `004315d`, `f957657`, `018252c` |

Negative controls: **24 tests were written against unfixed production code and
21 of them failed.** The three that passed are each accounted for —
`[file_sha256-12]`, where the existing hash comparison already caught the defect;
the no-op counterpart of store finding 7, which has to pass on both sides of the
change by construction; and daemon 5, which is the interesting one and is below.

---

## The findings that did not survive verification

**gitrepo 1 — pinned, not fixed.** The report described a `.gitignore` pattern
problem that the boundary round (`ccfafdd`) had already closed two commits
earlier. The finding was real when the agent read the tree and stale by the time
it was reported. Closed as stale, with a test added so it stays closed.

**store 3 — the mechanism was wrong, the finding was right.** The report said a
`NaN` in a manifest field would wedge the store permanently. It does not:
`canonical_json` uses `allow_nan=False`, and `0 <= float('nan')` is `False`, so a
`NaN` is silently filtered rather than raised. The permanent-wedge vector is a
*string* — `0 <= "x"` raises `TypeError` — and that is what the fix and its test
address. Same defect, different door.

**daemon 5 — already closed by store fix 6.** Its test
(`test_an_empty_transcript_does_not_bypass_the_interval_gate`) passed against
unfixed daemon code, which is exactly the signal that says *stop and find out
why*. Reverting `store.sessions()` to its pre-`74626f1` form made the test fail,
which proves the finding was real and that the store round had closed it from
the other side. The test is kept as a pin and its docstring records the control.

---

## What the round was actually for

The individual fixes are in the commit messages. What the reports are worth
keeping for is the three shapes that recurred across modules:

**A guarantee needs a floor, not a list of the ways people have fallen through
so far.** `_verify_unattested` was pinned to `raw/*/*/g*`, the one depth an
incident had used. `tick`'s exception list was `(OSError, RuntimeError,
ValueError)`, three types someone had seen. Both are denylists wearing a
guarantee's clothes, and both were rewritten as floors. The negative control on
the second one is worth recording: the two extra exception types were **not
load-bearing** — the suite is green with `except OSError` alone. They were
guesses, and a `KeyError` from a damaged manifest walked past all three.

**One knob wearing two hats.** `--interval` set both the idle-capture period and
the error-log repeat rate, because the two defaults happened to be the same
number. `--interval 0` is a documented, parser-blessed setting meaning "capture
every pass"; it also set the log's rate limit to zero and put back the 2.06 M
lines a day the limit exists to prevent.

**A document nobody can run rots at the rate the code changes.** Sixteen of the
thirty-four findings were documentation, and the common shape was a number or a
path that was true when it was written. The headline verification recipe — the
"a stranger with only the repo can verify this" one, which is the product's
central claim — named a file the store has never written. `tests/test_docs.py`
exists so the checkable half cannot rot again: the test count, the recipe's
filenames, the manifest's fields, the shim's length, the scratch-path citations,
and whether an unbuilt component is described as built. It caught a stale claim
in §5 nobody was looking for on its first run.

The unmeasurable half got measurements instead of edits — `tools/fsync_cost.py`,
a load-stated re-run of the latency table, `repo@commit` citations — on the rule
that a number you cannot repeat is a number you should not print.

---

## The vacuity audit

`tests/mutate_index.py` asks one question: *does the named test catch its
mutation?* It cannot ask the other one — *is there a mutation nobody named?* —
because every entry in it was written by someone who already had a test in mind.
A test whose assertion cannot fail is invisible to it by construction.

So a separate pass went the other direction. Take a test, read the claim in its
name and its docstring, mutate the production code so that claim is false, and
run **the whole suite**. Three outcomes are interesting and one is not:

- the named test fails → the claim is pinned, nothing to do;
- **some other test fails** → the property is held, but not by the test that
  advertises it;
- **nothing fails** → the claim is decorative.

Two rules made the results usable. The audit ran in an isolated worktree pinned
at `ccfafdd`, three commits behind HEAD, so **every finding was re-verified
against HEAD before it was believed** — and two of them could not be, because
the code they mutated had since been rewritten and their anchors no longer
matched anything. Those two were re-derived by hand at HEAD and re-run. And
where a fix was made, the mutation went into `tests/mutate_index.py`, so the
second attempt at pinning a property is held the way the first one was not.

**Twenty-four mutations were not caught by the test that named the property.**
They are not twenty-four defects. They sort into four kinds, and only the first
two cost anything:

**Nothing at all failed — 7.** The suite went green with the behaviour removed.
Six are fixed: the manifest no-op (`45362f3`, an inode witness, because the byte
comparison it replaced was identical by construction); the never-seen-session
bypass, whose test used an interval smaller than the age of the Unix epoch so
the wrong clause answered; and three of `gitrepo`'s four hardening settings —
`--template=`, `--no-verify`, `commit.gpgSign = false` — each of which is
*unobservable while the config isolation in `_env` holds*, which it does in
every other test in the file (`f957657`). Defence in depth that nothing
distinguishes is defence in depth nobody will notice losing, so each is now
pinned by a test that switches the outer lock off first. The seventh is
`sqlite3.Error` in `main`'s handler (`018252c`): the test that claimed to pin it
corrupts the database file outright, and `_check_schema` converts that to a
`ValueError` several frames earlier, so the clause was never reached.

**The named test is decorative; a different test holds the property — 15.** One
was a real defect wearing this shape:
`test_compact_boundaries_accumulate_within_a_generation` passed `[512, 128]` to
its second capture, handing back the very offset it then asserted had been
carried forward. Fixed. A second,
`test_a_transcript_the_adapter_cannot_parse_is_still_captured`, rests on a false
premise: `iter_records` skips a line JSON refuses, so the adapter does not fail
on that input, it returns zero events. The remaining thirteen are the audit
guessing the wrong owner — the property is pinned, precisely, by a test named
for it (`test_the_tiling_check_is_not_deletable`,
`test_a_colliding_segment_is_never_overwritten`,
`test_an_unchanged_transcript_is_not_rehashed_on_every_pass`, and so on). No
code changed for those; their docstrings now name the sibling that does the
work, because the next person to read one will otherwise reach the audit's
conclusion and the audit was wrong.

**Equivalent mutant — 1.** `init`'s `if not is_repo(home):` deleted, so
`git init` re-runs on an existing repository. Checked rather than assumed:
reinit preserves history and `HEAD`, copies nothing (the template is empty),
warns on a stderr that `_git` captures, and the config loop that follows runs
either way. The cost is one subprocess per watcher start and the observable
state is identical. Ruled equivalent; no test can distinguish it and contorting
one to try would be worse than the mutant.

**False positive — 2.** The audit suffixed two mutation ids to disambiguate
(`…_FILEMODE`, `…_METADATA`) and then compared the suffixed name against pytest
node ids, which of course never match. Both were caught by exactly their own
test.

The shape worth keeping: **an inner lock is unpinnable while the outer lock
holds.** Four of the seven silent findings were that, and the mutation index
could never have found them, because whoever wrote the index also believed the
tests covered them.

### What the audit actually covered

The denominator, from the audit's own final accounting: **242 mutations, each
run against the whole suite**, producing 277 verdict rows over 263 distinct
tests. 252 rows CAUGHT, 24 VACUOUS — the same 24 triaged above, with nothing
found after the point this document was first written.

Two things in that accounting are worth more than the totals.

**One row was silently skipped, and the harness did not say so.** An anchor for
`test_synthetic_model_rows_are_not_billed` — `if t.role != "assistant" or
t.model == "<synthetic>":` at eight spaces of indent — is also a substring of the
twelve-space copy inside `rollup_usage`, so it matched twice and the mutation
never ran. The harness recorded BAD-ANCHOR and printed nothing. A skip that
looks like a pass is the one failure mode capable of hiding a finding from the
person reading the output, and it took the audit's own re-read of its results
file to surface it. Re-run with the loop header prepended to disambiguate:
CAUGHT.

**48 tests were never audited**, and they are uncovered rather than judged safe:
33 of 49 in `tests/test_claude_code.py`, all of `test_end_to_end.py` (9),
`test_no_owner_data.py` (4), and `test_conformance_can_fail.py` (3). Four files
were audited to completion — `test_store.py` 85/85, `test_daemon.py` 65/65,
`test_index.py` 48/48, `test_hook.py` 19/19 — and `test_gitrepo.py` 27/28.

Two tests were deliberately not mutated, and both rulings hold:
`test_gc_is_safe_on_a_fresh_repository` asserts only that a call does not raise,
which no mutation can falsify without breaking collection, and the call is
pinned by `test_gc_actually_invokes_git`; `test_corpus_is_present_or_explicitly_absent`
is about a third-party fixture's presence and has no production line to mutate.

> **Correction, vacuity pass 2 (D1).** The first half of that ruling is wrong as
> written. "No mutation can falsify it" is only true of mutations that leave
> `gc()` working: a one-character flag typo makes `gc()` raise, and this test
> fails along with ten others. What the ruling should say is that *this* test
> adds nothing the others do not already have — a no-op `gc()` is caught only by
> `test_gc_actually_invokes_git`, and that second half is confirmed. The
> distinction matters because "unfalsifiable" and "redundant" are different
> verdicts, and only one of them is an argument for leaving a test alone.

A second pass covers the 48, at HEAD rather than at `ccfafdd`. **E4 is not
signed off until it returns** — the audited files gave up seven silent findings,
so declaring the unaudited ones clean on the strength of the audited ones is the
inference this whole round exists to distrust.

---

## Rulings against Gemini, recorded because they went the other way

The cross-review is not a formality and it does not always end in a fix. Three
items from `E4-gemini-round3.md` were not taken, and the reasons are the
interesting part of the round.

**§1 — finding real, recommendation rejected.** `discover` deduplicates on
`(st_dev, st_ino)`. Gemini confirmed the consequence: two hard links to one
transcript keep one name, and which name survives shifts if the other is
deleted, so the store opens a second session and re-copies the file. Real.
The recommendation — case-normalise paths instead — cannot work:
`posixpath.normcase` is the identity function on macOS, and case sensitivity is
a per-volume property, not a platform one. And the pre-fix behaviour was
strictly worse: both names, always, two sessions, every pass. A rare
discontinuity traded for common duplication is the right trade, so the ceiling
went into `discover`'s docstring instead of a patch.

**§2 — critique refuted by one line of Python.** The claim was that
`monkeypatch.setattr(daemon.time, "sleep", ...)` is immune to
`subprocess.Popen._wait`'s busy-poll and that the global patch was a
self-inflicted wound. `daemon.time is time` → `True`. Same object, same patch,
no immunity — so the real finding is the inverse of the reported one: the two
tests written Gemini's way were the exposed ones, and the one with the duration
discriminator was safe. All three moved onto `_on_pass`. Measured on six busy
cores, 30 runs of the two affected tests: **17 failed without the
discriminator, 30 passed with it.**

One sighting after that fix went unexplained, so it was hunted rather than
assumed away: **36 full-suite runs and 40 isolated runs of
`test_a_repository_deleted_under_a_running_watcher_comes_back`, 76 in all, zero
failures.** Closed as unreproducible. That is not a proof of absence — a
one-in-twenty flake would have shown roughly four times in 76 — so it is
recorded here rather than forgotten, and the next sighting starts from this
number instead of from scratch. [E5]

**The `TZ` finding — confirmed as behaviour, ruled not a defect.** `git` bakes
the local UTC offset into commit metadata, so the same tree committed under
different `TZ` produces different commit SHAs. True, and not a claim this
project makes. Reproducibility here is a property of the *segments* — that they
tile `[0, size)` and hash to a recorded digest, checkable with `cat` and
`shasum` — and of `derived/`, which the determinism test rebuilds and diffs.
Commit identity is git's business. Scrubbing `TZ` would buy a property nothing
depends on and cost a difference between what the operator sees in `git log` and
what the daemon writes.

Recorded because a review record that lists only the fixes is a record of who
was persuasive, not of what was true.
