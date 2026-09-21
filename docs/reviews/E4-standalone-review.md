# E4 standalone review round

The cross-review in `E4-cross-review.md` is Gemini and me reading each other's
half. This is the other thing: **independent review agents**, dispatched per
module against the committed tree, told to find defects and not to fix them.
Four reports came back — store, watcher, CLI/git layer, documentation — with
thirty-four findings between them.

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
| git layer + CLI | 6 | 5 | 1 stale, pinned instead | `a9ad664` |
| store | 8 | 8 | — | `201bda2` |
| watcher | 10 | 9 | 1 already closed by a store fix | `5d38582` |
| documentation | 16 | 16 | — | `b28c5ad`, `ad14979` |

Negative controls: **24 tests were written against unfixed production code and
21 of them failed.** The three that passed are each accounted for —
`[file_sha256-12]`, where the existing hash comparison already caught the defect;
the no-op counterpart of store finding 7, which has to pass on both sides of the
change by construction; and daemon 5, which is the interesting one and is below.

---

## The findings that did not survive verification

**gitrepo 1 — pinned, not fixed.** The report described a `.gitignore` pattern
problem that the boundary round (`a9ad664`) had already closed two commits
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
why*. Reverting `store.sessions()` to its pre-`201bda2` form made the test fail,
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
