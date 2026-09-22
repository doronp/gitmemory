# E5 probe D — the replication

**14 of 32.** The same number probe C scored, with the same split inside it, in
a domain chosen without any knowledge of C's.

| | C — theatrical show control | D — aviation line maintenance |
|---|---|---|
| **total** | **14 / 32** | **14 / 32** |
| items that are not decisions | 10 / 10 | 10 / 10 |
| directives | 4 / 11 | 4 / 11 |
| reversals (user retracting their own instruction) | 0 / 11 | 0 / 11 |

The items are in [`E5-probe-D-items.md`](E5-probe-D-items.md), frozen and
committed before they were scored. `bench/probes.py` reads them out of that file
and refuses to run if its digest has changed, which is the difference between a
probe and a copy of one.

## Why it was written

C's 14/32 was one number from one domain, and a reader is entitled to ask
whether theatrical show control is simply hostile vocabulary. The way to find
out is another probe, written by an author who has not seen the first one, in a
domain picked to share nothing with it.

The author read exactly two files — `README.md` and `docs/DESIGN.md` — and ran
no shell command at all, so the extractor, probes A/B/C and the earlier reports
were all unread. Their attestation is at the top of the items file. The
composition was specified in the brief so the two probes would be comparable:
11 reversals, 11 directives, 10 non-decisions, shuffled.

## What it found

**The shape replicates exactly.** Not approximately — the same 4, the same 10,
the same 0.

**4 of 11 directives, and the four are the two constructions the user branch
covers.** Two prohibitions and two substitution frames:

- *Never write a task card without a tail number.*
- *A deferral never closes without a reference to the rectification work order.*
- *Use the station's local stores API, not the central one.*
- *Record releases to service on the EASA Form 1 fields, not the operator's
  internal release form.*

The seven misses are all plain positive standing rules with nothing to match on
— *Deferral records stay in UTC.*, *Part serial numbers keep their leading
zeros — store them as text.*, *Dispatch checks run the MEL first and the CDL
second, always in that order.* C's seven misses were the same sentence in
another vocabulary. This is not a lexical gap that a wider word list closes: the
extractor has no rule that claims a declarative standing rule, and two probes in
two domains now say so.

**0 of 11 reversals, which is a declared ceiling being confirmed for the second
time.** `_decision_kind` says it in a source comment: a reversal is *the
assistant* changing course, so a user retracting their own earlier instruction
is not a shape it claims. Both probes' authors wrote eleven of them anyway,
independently, which is worth noticing on its own — asked for "the user
reversing their own instruction", two writers with no contact produced the shape
the extractor cannot see, and in a real session it is the commonest correction
there is.

**Two of those eleven came back as `directive`, and that is the most useful
detail in the run.**

- *I told you earlier to block dispatch on an open category-A deferral; undo
  that — ops control wants a warning banner instead.*
- *Change of mind on station codes: go back to the IATA three-letter ones after
  all, not the ICAO four I asked you for.*

Both carry a substitution frame, so `_CONTRAST` fires and the user branch labels
what it found a directive. The *rule* is right — a memory that says "use the
IATA three-letter codes" is the memory you want — and the *kind* is wrong.
Scored kind-blind, D is 16/32. It is reported as 14 because the node kind is
what the graph edges are built from, but "we got the content and mislabelled it"
is a different defect from "we saw nothing", and the two are worth separating in
whatever fixes this.

## What was and was not changed

Nothing in the extractor was changed in response to D, and D was not shown to
anything that changed it. One extractor change landed between commissioning the
probe and scoring it — `_OPINION` became clause-scoped, which was round 4's
deferred F2 and was written before the items existed. The gate was re-scored
after it on all three splits at 1.0000 / 1.0000.

**D is spent the moment anyone tunes against it.** It is pinned as a floor in
`bench/test_probes.py` alongside A, B and C. A rise in that floor is evidence of
nothing unless it came from a change that was never shown D's miss list.

## What is still owed

A probe is still not a measurement on real text. Both C and D are hand-written
fiction by an agent given a brief, and the hand-labelled secondary set named in
`docs/DESIGN.md` §3 — real-shaped sessions, labelled by a human — remains the
binding gap. What D buys is narrower and worth having: the 14 is not an artefact
of one author's domain.
