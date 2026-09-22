# E5 probe E — does fix 2 generalise?

**19 of 32**, scored once, on 2026-09-22 at `53c6464`. The number is not the
finding. The split inside it is:

| | probe E — COBOL batch on an IBM mainframe |
|---|---|
| **total** | **19 / 32** |
| items that are not decisions | **12 / 12** |
| directives | 5 / 8 |
| reversals (assistant abandoning its own position) | **2 / 12** |
| *aside* — items the author could not settle | 3 / 8 |

Ten of those twelve non-decisions carry a substitution frame — *instead of*,
*replace … with*, *switch to*, *rather than*, *in place of* — and were written
to be non-decisions anyway. Nine of the twelve reversals carry no
self-correction marker at all. The probe is built so that a rule keying on the
frame fails the first group and a rule keying on the marker fails the second,
and the result says precisely which of those the extractor is.

The items, the author's labelling conventions, the ten items they expect to be
disputed, and their attestation are in
[`bench/probe_e_cases.py`](../../bench/probe_e_cases.py) — their file, copied in
whole and byte-for-byte rather than transcribed
(`e327c0050b3320b1f92cf96acd1efa2da65a2d9c97c526d414bc28202c6cf028`).

## Why it was written

Fix 2 is the largest single behavioural change the extractor has had: a bare
substitution in the assistant role stopped counting as a reversal, and the
assistant-side output on real third-party sessions went from 61 nodes to 9. It
was tuned against 61 items adjudicated by the same hand that wrote the fix, on
one corpus. Probes A and B are spent, C and D are user-side, and the synthetic
gate turns out to contain none of the vocabulary this branch is built on
(`docs/reviews/E5-fix2-derive-standalone-review.md`, F4). So fix 2's central
claim — *that a substitution frame from the assistant is ordinary narration and
not a course change* — had never been tested anywhere except where it was
written.

Probe E is that test, written blind, in a register with no HTTP surfaces, no
containers, no package managers and no test runners: JCL steps and DD
statements, copybooks, packed decimal and sign nibbles, OCCURS DEPENDING ON,
abend codes, tape mounts, generation data groups.

## What it found

**Fix 2's claim holds, and holds cleanly. 12 of 12.** Ten of the twelve
non-decisions are substitution narration in a domain the extractor has never
seen, and not one of them was emitted:

- *"Per your note I'm switching the sort step to OUTREC…"* — origin is the user.
- *"…the version the shop has been running since 1998"* — the mask was never the
  assistant's to put down.
- *"…two ways to do the totals…"* — deliberation that commits to nothing.

The narrowing is not an overfit to the corpus it was adjudicated on. This is the
question the probe was written to settle and the answer is unambiguous.

**It is bought at a recall cost that is much worse off-corpus than on.** 2 of 12
here, against 7 of 7 on the secondary set's adjudicated assistant reversals. The
two that landed both carry a marker *and* a substitution frame:

- *"**I was wrong** about the copybook being safe to extend at the end … The new
  fields go into a separate redefined region **instead**."*
- *"**On second thought** the 88-levels I added to the copybook are the wrong
  home for this … **it needs to be** a small file the job reads into a table."*

The other ten are retreats signalled by **stance**: a constraint discovered that
invalidates the earlier choice, a cost that has flipped, resignation after
repeated attempts, a flat new direction that is unintelligible except as a
withdrawal from the old one.

- *"That intermediate work file I put between the two steps is doing nothing for
  us except costing a second mount and forty minutes of elapsed time. Step two
  reads the master directly now."*
- *"Keeping the reformat inside the program made sense while I believed the sort
  card couldn't express the packed-decimal rounding. It can."*
- *"Fourth attempt at getting the dynamic CALL to resolve at run time, and the
  linkage editor still won't see the module … Static CALL it is."*

Nothing fires on any of them but `_CLAUSE`. This is the declared shape of the
assistant branch rather than a defect in it — the branch is a *marker* detector,
and the source says so — but it had never been measured against writing that
deliberately withholds the marker, and now it has: **the assistant branch
recovers a change of course only when the author announces one.**

**Three directive misses, all the same already-known hole.** Plain positive
standing rules with nothing to match on: *"From now on any copybook you touch
gets a change line at the top with the ticket number and the date"*, *"Put the
region on the JOB card once and keep it off the individual steps"*, *"Treat
every numeric field coming off that tape as unsigned display unless the record
layout explicitly says packed"*. Probe C missed seven of these, probe D missed
seven, E misses three; three probes in three unrelated domains now say the same
thing, which is that the user branch has no rule claiming a declarative standing
rule.

## Two defects, found by tracing the misses

Neither is the recall ceiling above; both are cases where a rule that claims the
shape did not deliver it.

**1. `_DELIBERATION` fires on the bare noun `options` — measured, and not
fixed.** This item has `_ABANDON` and `_ADOPT` in it and reaches the stop/start
pair, which would have labelled it correctly:

> *"I've spent the whole morning trying to get the OCCURS DEPENDING ON table to
> behave under the compiler **options** this shop still uses … I'm **dropping**
> the variable-length layout. Fixed 250 entries and an explicit occurrence
> counter."*

The guard for *weighing alternatives* matched `options` at offset 102, inside
*compiler options*, and returned None before the assistant branch ran. `options`
is an ordinary technical noun on a coding transcript — compiler options, mount
options, command-line options — and this is the third time a bare word in a
guard's alternation has swallowed a real decision (round 4's `same request`,
F6's `scratches the surface`).

The narrowing is nonetheless **blocked**, and the measurement is why. Replacing
the bare set-nouns with a token that cannot match:

| | blocks containing one | verdict changed |
|---|---|---|
| 559 real assistant blocks | 12 | **0** |
| 89 real user blocks | 6 | **3**, all `None` → `directive` |

Zero cost on the side that matters, and three *new* false positives on the other
side — which turn out not to be about this guard at all. The three are pasted
machine output, and what labels them is `_PROHIBIT`: `disallowed` in a browser's
*"blocked because of a disallowed MIME type"*, and `no arguments` in a pasted
`gh pr view` man page. `_DELIBERATION` is masking them by accident, on an
unrelated word, which is the worst kind of green. So the order of work is fixed
by the measurement: `_PROHIBIT`'s descriptive matches first — they are the
already-open *bare negation* item, now with real-corpus instances — and the
`options` narrowing after, or the board goes from 12 false positives to 15.

The larger half of that bare-negation item is since closed — the contracted
`don't`/`doesn't`, which took the user side from 12 false positives to 6 — and
the narrowing was re-measured on top of it: still 3, still the same three
blocks, so the board would now go 6 to 9 and the order of work is unchanged.
`docs/benchmarks/E5-secondary-set.md`, fix 6.

**2. `_PIVOT`'s cancel marker was pinned to a pronoun — fixed.** It listed
`scratch that` and nothing else, so *"Scratch the SEARCH ALL."* fell through, as
would *scratch the plan* or *scratch the two-pass design*. The deictic is the one
form the guard `_REPAIR` also claims and runs first on, so the alternative was
**dead** in F5's sense rather than merely unused: no input could reach it. The
reachable form, the one that names its object instead of pointing at it, was
claimed by nobody.

Replaced with the complement of `_REPAIR`'s deictics, carrying `_ABANDON`'s
`surface` exclusion so one rule governs the idiom. Measured across 726 real
blocks in both roles, all five probes and both gate splits: **nothing moves
except probe E's own item and probe A's ceiling item**, which the same change
lifts — A's aside goes 2/5 to 3/5 and E goes 19 to 20. The recorded diagnosis
that probe A's item was a guard-ordering casualty (F7) is what made it possible
to see that the repair belonged in `_PIVOT`.

**Probe E's headline stays 19.** 20 is what it scores after being shown its own
miss list, which is training data, and the floor in `bench/test_probes.py` is
re-pinned to 20 with that said out loud.

## What it does not measure

- **It is spent.** These thirteen misses are now the brief for a round, which is
  the definition. A future score against probe E is a regression floor and not
  evidence of anything.
- **The eight `hard` items are scored aside**, the same treatment probe C's
  unsettled items get, and 3 of 8 is not comparable to anything. The author
  flagged them because they could not settle the label — whether adopting a
  user's suggestion transfers ownership of it, whether an announced-but-unbuilt
  plan is a position, whether abandonment without a named successor is a course
  change. A miss there may be the label's fault.
- **No prevalence claim.** Probe E is 32 hand-written sentences with a
  composition chosen to attack, not a sample. That nine of twelve reversals are
  unmarked here says what unmarked retreat looks like; it says nothing about how
  often real transcripts write that way. The real-corpus number for that is 7
  adjudicated assistant reversals in 559 blocks, and it is a different question.
- **One author, one domain, one sitting.** D exists because C's number needed a
  replication. E has none yet.
