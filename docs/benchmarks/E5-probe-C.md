# Probe C — the first generalisation number, and it is 14/32

The decision extractor has read **1.0000 precision / 1.0000 recall** on its
held-out gate split through five rounds of real defects. `docs/DESIGN.md`
pre-registered that gate at ≥ 0.85 / ≥ 0.60 before anything was measured, and it
has never moved, in either direction, for any reason. This document is the
measurement that says why.

**Probe C scores the same code 14 of 32.**

> *Since scored, and not part of this measurement:* C read 13 after fix 6 and
> reads **16** after fix 7, which closed one corner of the positive-standing-rule
> hole this page reports. Both moves and their costs — including one gain that
> fires on the rejected half of its sentence, and one user-reversal item that
> goes from a silent miss to `directive` — are in
> `docs/benchmarks/E5-secondary-set.md`. The 14 below is the blind number and
> stays the number this page is about.

| probe | class | directive | reversal | None | aside |
|---|---|---|---|---|---|
| A | 26/32 | 6/6 | 5/5 | 15/21 | 2/5 |
| B | 27/32 | 5/5 | 3/4 | 19/23 | 1/3 |
| **C** | **14/32** | **4/11** | **0/11** | **10/10** | **2/8** |

## Why C is the only one of the three that counts

A and B were written by a reviewer, in a third vocabulary, and both were
adversarial and honest. But each one *set the brief for a revision round and
then scored the result of that round*. That makes them training data. The floor
in `bench/test_probes.py` keeps a regression red; it is not evidence about
unseen text, and this project has said so in `bench/probes.py` since they were
written.

C was written to close that hole:

- The author read **only** `README.md` and `docs/DESIGN.md`. Not
  `src/gitmemory/derive.py`, not `bench/`, not any gate report, not A or B.
- They never ran the extractor, the gate, or the existing probes against their
  sentences. The only code they executed was a shape assertion over their own
  file.
- The set was **frozen unscored** and handed over. It was scored once, here, by
  someone else, and the number in the table is that first score.
- Domain: theatrical show control — cue stacks, DMX universes, sACN/ArtNet, fade
  curves, grandmaster, followspots, house lights. Chosen so that lexical overlap
  with the corpus (parsers, tokenizers, buckets, credentials, workers) is
  impossible by construction. Verified: none of those five corpus words appears
  in any case sentence.
- 32 scored items, balanced 11 directive / 11 reversal / 10 None, plus 8 `hard`
  items scored apart.

The author also committed, in writing and before scoring, to three labelling
conventions and named the seven items they thought most likely to be disputed.
Those are in the docstring at the head of `CASES_C`.

## What the number decomposes into

The 18 class misses are not eighteen separate problems. They are two.

### 1. Every reversal missed — 0 of 11 — and all eleven are the user's

`_decision_kind` reads role as definitional, and says so in its own docstring:
a directive is something the *user* imposes, a reversal is the *assistant*
changing course. There is a `ponytail:` comment in `src/gitmemory/derive.py`
naming the exact consequence:

> so a user who reverses their own earlier instruction, and an assistant that
> records a standing rule for itself, are both missed.

Probe C put all eleven of its reversals in the user's mouth. A and B put theirs
in the assistant's. That is the whole of the 0/11, and it is a declared limit
behaving exactly as declared.

Three things follow, and only the first is comfortable:

- **The probe did not find a bug here.** It found a documented ceiling.
- **The ceiling is documented in one place: a source comment.** It is not in
  `README.md` and not in `docs/DESIGN.md`. A reader of the public documents —
  which is precisely what C's author was — has no way to know that
  "Cue index zero is no longer reserved" will be silently dropped. If the
  convention for probes A and B had been available to them, these eleven would
  have gone in the aside group and C would read 14/21. **It is reported as
  14/32 because that re-denomination is only available with hindsight the
  author did not have**, and a ceiling nobody can read is not a disclosed
  ceiling.
- **The ceiling is in the wrong place for this product.** A memory system's
  worst failure is a rule it still believes is in force after the user revoked
  it. `README.md` already names "a stale memory that misled" as an unbounded
  unmeasured cost. User-reverses-own-instruction is the single most direct route
  to that cost, and it is currently 0%.

Two of the eleven were not merely dropped but **relabelled as `directive`** —
"Cue index zero is no longer reserved" and "That rule about never setting the
grandmaster is lifted" both contain a prohibition the rule matches, so the
revocation of a rule is recorded as the rule. That is worse than a miss.

### 2. Seven of eleven directives missed, and the shape is consistent

The four that landed carry a prohibition or a substitution frame: *never*
seconds; *does not* set it; *No* sharing; *have to* be up within two seconds.
The seven that did not are plain positive standing rules:

| miss | shape |
|---|---|
| "Everything that speaks to the desk goes out through the sACN sender; nothing opens its own socket." | positive rule + `nothing`, outside the prohibition lexicon |
| "Patch file stays YAML." | bare positive standing rule |
| "I honestly don't care how the fade curve gets computed, as long as the interpolation happens on the show thread and not inside the renderer." | constraint in a subordinate clause |
| "While you're in the cue loader anyway, index zero stays reserved for blackout." | constraint in a subordinate clause |
| "Between ArtNet and sACN for the studio rig, we're going with sACN." | selection, alternatives named outside the frame |
| "I'd rather eat the slower timecode sync than keep the one that drifts, so take the slow one." | hedged selection |
| "Let's not grow a second dimmer curve table; fold the extra points into the one we already have." | negation outside the prohibition lexicon |

The user-role rule fires on a substitution frame or a narrow prohibition
vocabulary. Out of domain, that covers four cases in eleven. The same
`ponytail:` comment gives the reason for the conservatism — "loosening them
makes every polite suggestion a directive" — and C prices it: **7 of 11 real
rules dropped** to buy the precision.

### 3. What C confirms, loudly

**10 of 10 on the items that are not decisions.** Not one false positive, in a
vocabulary the guards have never seen, including three imperative-mood work
requests, two sentences carrying *must*/*requires* about somebody else's rule,
and three assistant turns that report a user decision without making one — one
of which reports a reversal.

That is the strongest single result in this document, and it is worth stating
next to the 14: the extractor's conservatism is not noise. It is a real,
generalising property. It simply costs more than the in-domain numbers implied.

## A and B point the other way, which is the actual finding

A and B are 11/11 and 8/9 on directives and reversals, and 15/21 and 19/23 on
the items that are *not* decisions. C is 4/22 and 10/10. The two sets fail in
opposite directions because they were written to attack opposite things — A and
B attack precision, C attacks recall — and neither author knew the other's
brief.

So the honest summary is not "the extractor scores between 14 and 27". It is:

> Out of domain, the extractor almost never calls a non-decision a decision, and
> misses most real decisions. The gate fixture cannot see either half, and reads
> 1.0000/1.0000 regardless.

## What was deliberately not done

**The extractor was not changed.** Not one rule, not one lexicon entry.

Editing `derive.py` against C's own miss list would raise C's score and destroy
the only unspent measurement this project has, exactly as it destroyed A and B.
The misses above are a brief for a later round; the round's result has to be
scored by a **probe D**, written by someone who has not read C, under the same
freeze. `FLOOR["C"] = 14` is pinned to catch a regression, and the comment above
it says not to tune against it.

Two documentation defects C exposed are fixable without touching the extractor,
and are queued rather than done here because the security round is in flight:

1. The user-role reversal ceiling and the positive-standing-rule ceiling belong
   in `docs/DESIGN.md`, in the section that describes the extractor, not only in
   a source comment.
2. `README.md` cites the probes; it now cites C as well, at 14/32.

## Reproducing

```
python -m bench.probes
```

Scored on `045d1b0` + the E6 round, 2026-09-21. The frozen probe is `CASES_C` in
`bench/probes.py`; the splice was verified byte-identical to the author's file
by `ast.literal_eval` round-trip before it was committed.
