# E5 — standalone review of fix 2, the assistant reversal narrowing

One reviewing agent in its own worktree, asked for one commit: `d97e6ab`, the
fix that stopped a bare substitution from counting as a reversal in the
assistant role. It is the largest single behavioural change the extractor has
had — 61 assistant nodes on real third-party sessions down to 9 — and it was
written against an adjudicated set by the same hand that wrote the adjudication
harness, which is exactly the shape that wants an outside reader.

**This file is a disposition register, not the reviewer's report.** The report
itself was never written to disk, and by the time that was noticed the session
holding it was gone. Rather than reconstruct its prose from memory — which is
the failure F7 below is about — every entry here is stated from the commit,
test, and code comment that closed it, all of which are in the tree and
checkable. Finding numbers are given where the tree cites them
(`[E5 fix 2 review, Fn]`) and omitted where it does not. Anything the reviewer
said that did not end in a commit is gone; that is the cost, and the reason
`docs/reviews/` gets the file at the time from here on.

Baseline at the start of the round: 847 tests offline, 462 mutation rows. At
the end of the seven items: 854 tests offline, 1176 with the conformance
corpus, 474 mutation rows. Four more entries were added to this register after
that, each with its own commit, and the round closes at **864 tests offline,
1186 with the corpus, 486 mutation rows** (`7283a08`), `ruff check` clean.

## Status

Seven items closed, each by its own commit with a reproduction, a re-measurement
and — where one can exist — a mutation row. Every new row **CAUGHT by its
intended test**. One item open, listed at the bottom; four more entries added
after the round, in their own section; and seven recorded and not fixed.

| # | what it was | closed by | pinned by |
|---|---|---|---|
| — | fix 2 moved held-out recall 1.0000 → 0.7428 and no test scored the extractor against the gate at all, so five write-ups still quoted the old figure | `12f6de1` | `test_the_shipped_extractor_still_scores_what_the_write_ups_claim` |
| F3 | the conjunction was scoped to the block, so a concession three paragraphs above an unrelated `instead of` read as a reversal | `d419187` | 3 rows; `test_a_concession_three_paragraphs_from_a_substitution_licenses_nothing`, `…_offered_with_the_substitution_is_still_a_reversal`, `test_cutting_an_attitude_out_does_not_reflow_the_message` |
| F5 | `_RECANT` listed `my (mistake|bad|error)`, which `_REPAIR` shadows — no input could reach it | `e03447d` | no row is possible; the test says why |
| F7 | two recorded diagnoses named the wrong mechanism | `22703b7` | 4 assertions naming which predicate is responsible |
| F6 | `_ABANDON`'s object list admitted the `scratches the surface` idiom and had silently lost `them/those/these` | `4de1f57` | 2 new rows + 1 repointed |
| F4 | eight `_RECANT` alternatives were held by no test; the synthetic corpus contains none of the class | `38ca928` | 8 rows, `test_every_recant_alternative_is_held_by_something` |
| F9 | the commit's "eight hits" for the two deleted `_ABANDON` branches is not a number any population gives | this file | — (see below) |

The three that are worth reading past the table are F7, F4 and F9, because none
of them is a code defect.

## F7 — a recorded diagnosis is a claim, and two of them were wrong

Both were written down as settled and neither was.

*"Probe A's ceiling item fails in `_REPAIR`."* It fails in `_ABANDON`. The two
imply different repairs — reordering the guards would have moved nothing — and
the test now asserts which predicate fires and which does not, rather than
asserting the empty result that held under either story.

*"Bare `scratch` matched the filename `main.py.oldscratch`."* The token really is
in the corpus and the branch really did fire five times, but never on that: the
alternative sits inside `\b(?:…)\b`, and `old` and `scratch` have no word
boundary between them. An example that cannot occur, attached to a defect that
did, costs the next reader the half hour it took to notice.

Neither changed behaviour. Both changed what the next reader will believe, which
is the whole reason the comments are there.

## F4 — the gate cannot see the branch these fixes are about

The review asked how much of `_RECANT` any test holds. Swept one alternative at
a time — each replaced by a token that cannot match, never deleted, since a
deleted alternative leaves an empty branch that matches everywhere and measures
nothing — **nine of twelve survived the whole suite**.

Counting before writing nine tests turned up the larger finding. Neither
synthetic split contains a single alternative of this class, including the three
that tests already held. So every *"no change on the gate"* line recorded for
fixes 2, 3 and 4 is true and vacuous for the assistant substitution branch: the
gate is blind to it, and it has only ever been measured by the real-corpus
adjudication and one probe case. Recorded in
`docs/benchmarks/E5-secondary-set.md` under both fix 5 and *What it does not
measure*.

All eight remaining alternatives are pinned rather than five of them deleted.
The deleted one was **dead** — a guard returned before it could be read — and
these are merely **unobserved**; a mutant of dead code survives because the code
cannot run, a mutant of unobserved code survives because nobody wrote the input,
and only the first is a fact about the program.

## F9 — "eight hits", measured

`d97e6ab`'s message says the two deleted `_ABANDON` branches had *"eight hits on
real sessions and never an abandonment"*. Re-measured on the pinned corpus, over
the 559 distinct assistant prose blocks the secondary set uses:

| | `no longer [\w-]+` | bare `scratch(es\|ed\|ing)?` | both |
|---|---|---|---|
| blocks containing it | 5 | 4 | **9** |
| blocks whose verdict it changed | 5 | 2 | **7** |

Eight is neither, which is the signature of a number remembered rather than
measured. The defensible figure is **7** — blocks the branches actually changed
— and 9 is the count of blocks that merely contain the vocabulary. The claim
attached to it survives intact: all seven went `reversal` → nothing, so all
seven were false positives and none was an abandonment.

Reproduced by restoring both branches into `_ABANDON` and re-scoring the same
population through `derive._decision_kind`. No test pins this. The number
describes a state of the code that no longer exists and can only be recovered by
reconstructing two deleted regexes, so a test would assert a fixture of history
and would break the next time `_ABANDON` changes shape for an unrelated reason.
A commit message cannot be edited either; this paragraph is where the correction
lives.

## Still open

- **`_PROHIBIT` reads pasted machine output as a rule — three blocks left.**
  The contracted-negation half of this is closed below; what survives is
  *"blocked because of a disallowed MIME type"* twice, in a browser console
  error, and a `gh pr view` man page's *"no arguments"*. All three are invisible
  only because `_DELIBERATION` catches the same blocks on the bare noun
  `options`, and they are exactly what the `options` narrowing costs: measured
  on top of fix 6, dropping the bare set-nouns takes the board from 6 false
  positives to 9 and these are the three. Two alternatives, two lookbehinds,
  each fitted to one block — the shape this class is built against, which is why
  they are written down rather than guessed at. Three further blocks carry a
  `_PROHIBIT` hit under the same guard and would stay masked because each has a
  second `_DELIBERATION` hit; that is luck, and it is tabulated with the rest in
  `docs/benchmarks/E5-secondary-set.md`, fix 6.

## Closed after this file was written

- **The contracted negation is no longer a prohibition on its own**, which is
  the larger half of the `_PROHIBIT` item still listed above. On the 89 real
  user prose blocks `don't`/`doesn't` occurs 21 times in 19 blocks: 18 report
  that something is broken and 3 are written as imperatives. (Written as one is
  not the same as being a standing rule, which this entry originally claimed:
  the annotators read all 3 as task-scoped and gold-labelled the blocks `none`.
  Corrected in the fix-7 review round.) So `don't` counts in imperative position
  only and `doesn't` counts nowhere — English has no
  third-person imperative, so no position rescues it, which is the call
  `cannot` already got. `never mind` is excluded by name as a formula for
  dropping a request. User-side false positives **12 → 6**, the declared
  `reversal-by-user` aside **2 of 3 labelled → 0 of 3**, everything else on
  every board unchanged except probe C, **14 → 13**. Four mutation rows, all
  CAUGHT. `docs/benchmarks/E5-secondary-set.md`, fix 6.

  Probe C's lost item was right for the wrong reason and the floor is re-pinned
  *down* on purpose: what scored it was a `don't` inside a parenthetical about
  somebody else's fixtures, while its actual directive is a positive standing
  rule — root cause 4, the hole probes C, D and E all report. Closing that
  restores the item to the rule that should always have held it, and the floor
  goes back to 14 then and not before. **Closed one fix later**, and the
  item came back exactly as predicted; C is re-pinned at 16.
- **`_PIVOT`'s cancel marker was dead**, in F5's sense and not F4's: the whole
  of it was `scratch that`, `_REPAIR` claims the demonstratives and runs first,
  so no input could reach it. The form that names its object rather than
  pointing at it — what you write when the thing is two turns back — was claimed
  by nobody. Replaced with the complement of `_REPAIR`'s deictics, carrying
  `_ABANDON`'s `surface` exclusion so one rule governs the idiom. Nothing moves
  on 726 real blocks in both roles or either gate split except the two items the
  change is about: probe E 19 → 20 and probe A's aside 2/5 → 3/5. F7's
  correction is what made it visible — while A's item was believed to fail in
  the guard order, the repair looked like a reordering. `409cb36`.
- **A rule can say that a thing stays as it is.** One corner of root cause 4,
  the largest item on the list and the one probes C, D and E all report: nothing
  in `derive.py` claimed a *positive* standing rule, because every user-side rule
  it can see names something rejected. `_PERSIST` claims the subset that carries
  the meaning in a verb — `stays`, `remains`, `keeps` — with three guards, each
  answering a sentence from the real corpus: a subject in front makes the verb
  aspectual (*"I keep getting build errors"*), `keep-alive` is a header value,
  and an objectless `keep … as` is an appraisal. Position, not vocabulary, the
  same discriminator fix 6 used. Probe C **13 → 16**, D **14 → 16**, E **20 →
  21**, and the real-text set produces **its first true positive in seven
  fixes** (0/6/2 → 1/7/1). Six mutation rows, all CAUGHT.

  Three costs, all written up rather than netted off: the new false positive is a
  lexical twin of the new true positive and no rule separates them; three
  user-reversal items (C ×1, D ×2) move from a silent miss to `directive`, which
  probe C's author called worse than a miss; and one of C's three gains fires on
  a `keep` inside the **rejected** half of the sentence. `pros or cons` would
  have taken the board back to 6 false positives and was declined for being the
  `options` mistake again. Root cause 4 stays open — this is a corner of it.
  `docs/benchmarks/E5-secondary-set.md`, fix 7.

  **It exposed two defects that are not it.** Both were unreachable until a rule
  read the word `keep`: `_BACKREF` listed `keep in mind` as a fixed string and
  the phrasal verb is separable, so *"Please keep this instruction in mind."*
  walked past the guard that claims exactly it — found by the held-out gate,
  where the first version of `_PERSIST` read 0.8595 test precision and every loss
  was one distractor of that shape. And `_DELIBERATION` had `on the one hand …
  on the other` but not `a case for X and a case for Y`, which is the same frame
  in other words.
- **Probe E is scored: 19 of 32**, on 2026-09-22, and 20 after the `_PIVOT`
  fix above. Fix 2's claim holds off the corpus it was tuned on — 12 of 12
  non-decisions, ten of them wearing the substitution frame the fix exists to
  ignore. The price is 2 of 12 reversals, because nine were written with no
  self-correction marker and the assistant branch is a marker detector.
  `docs/benchmarks/E5-probe-E.md`.

## Recorded, not fixed

- **M11 — a concession plus an abandonment is not a pair.** *"Good catch.
  Dropping the retry wrapper."* scores nothing: `_RECANT` fires, `_ABANDON`
  fires, and the assistant branch wants `_RECANT` with `_SWITCH` or
  `_CONTRAST`, neither of which is there. It is a reversal on any reading, and
  the stop/start pair cannot reach it because nothing replaces the thing.

  Priced rather than argued about — the disjunct was added and every board
  re-run: probes 23/25/14/14, secondary user side 0/12/2, assistant side 7 of 7
  with 54 withdrawn, gate dev 1.0000/0.9319, held-out 1.0000/0.7428. **All five
  identical**, because the construction occurs in neither the 559 real
  assistant blocks nor either synthetic split.

  Declined on that, and the reasoning is the mirror of F4's. Keeping `_RECANT`
  vocabulary nobody has observed is cheap because it sits inside a construction
  this corpus *does* show, seven times. This would be a new construction, and
  fix 2 itself was bought with 61 adjudicated items. Retaining an unobserved
  word and adding an unobserved rule are not the same act. One observed instance
  flips it; the sentence is kept in `derive.py` beside the branch so the next
  reader argues with it rather than with the abstraction.
- `_REPAIR` shadows `_PIVOT` on *"scratch that"* — a guard wins over a class
  that would have labelled it. Still true, and now known to have been the whole
  of `_PIVOT`'s cancel marker: the alternative was unreachable, so probe E's
  *"Scratch the SEARCH ALL"* had nothing to fall back on. The nominal form is
  `_PIVOT`'s as of `docs/benchmarks/E5-probe-E.md`; the deictic is still
  `_REPAIR`'s and the ordering question is still open.
- **`_DELIBERATION` fires on the bare noun `options`**, and narrowing it is
  blocked rather than declined. Zero verdict changes on 559 real assistant
  blocks, three new false positives on the user side — re-measured on top of
  fix 6 and still three, `_PROHIBIT` matching `disallowed` twice and
  `no arguments` once inside pasted machine output, which this guard is masking
  on an unrelated word. The rest of the `_PROHIBIT` repair comes first; the
  causal breakdown, including the three blocks a second `_DELIBERATION` hit
  would keep masked either way, is in
  `docs/benchmarks/E5-secondary-set.md`, *What is left of root cause 2*.
- The `over`-complement hole in `_CONTRAST`, declined twice; see
  `docs/reviews/E5-gemini-pair-review-derive.md`.
- A contrast between two **facts** reads as a contrast between two **plans**:
  *"You're right, the build is slow because of the cold cache rather than the
  linker"* is a corrected claim with no decision in it and is emitted. Affects
  every member of `_RECANT`, including the one with fourteen real hits.
- The paragraph-versus-line scope choice in F3 is unmeasured — both score
  identically on the nine items that could distinguish them.
- The `surface` object ceiling from F6: a real abandonment of a thing called
  *"the surface probes"* is declined along with the idiom, because telling them
  apart needs to know whether `surface` heads the noun phrase, which is a parse.

## What the round changed about how the next one runs

- **Reproduce by a route the reporter did not use.** Every claim here was
  re-derived through `derive._decision_kind` or the one-block session helper,
  never by re-running the predicate the reviewer named.
- **A red baseline invalidates a sweep.** The first F4 run had all twelve
  mutants "caught" by the same unrelated test. The tell is the sameness; the
  cause was an unmutated baseline that was already failing, twice over.
- **A body that exercises a branch is not a body that depends on it.** F4's
  `good catch` row came back MISSED because its sentence also contained the
  stop/start pair and reached `reversal` without the alternative. Every body is
  now checked by deleting its own alternative and confirming the block goes to
  nothing.
- **Write the review down when it arrives.** This file exists because that was
  not done.
