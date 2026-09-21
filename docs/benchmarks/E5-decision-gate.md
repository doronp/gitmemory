# E5 ship gate — decision extraction

**Pre-registered before anything was measured** (`docs/DESIGN.md`): strict
node-level precision ≥ 0.85 and recall ≥ 0.60, and *below the bar it does not
ship, and the README states the measured number and says so*.

Every run against the held-out split is recorded here, pass or fail, in the
order it happened. A gate you can re-roll until it passes is not a gate, so the
rule is: the split a number came from is named, a split is used once, and what
crossed the wall between two runs is written down before the next one starts.

---

## Run 1 — FAILED

| | precision | recall | matched | predicted | gold |
|---|---|---|---|---|---|
| **held-out (test split, 100 sessions)** | **0.7012** | **1.0000** | 352 | 502 | 352 |
| directives | 0.5441 | 1.0000 | 179 | 329 | 179 |
| post-failure reversals | 1.0000 | 1.0000 | 81 | 81 | 81 |
| other reversals | 1.0000 | 1.0000 | 92 | 92 | 92 |

For contrast, the same extractor on the dev split it was developed against —
367 gold, **1.0000 / 1.0000**, every slice perfect.

### How the extractor was kept blind

The author worked in a git worktree with `bench/decisions.py`, `bench/fixture.py`
and `tests/test_decisions_bench.py` **absent from the tree**, and
`bench/test_gate_fixture.py` pins that the scorer runs with `bench.decisions`
blocked at the import hook — blindness that is structural rather than promised.
What they had was a frozen dump of the dev split and `bench/gate.py`, which reads
a directory and has no idea a generator exists.

Checked after the fact, by a route the author did not use: every string constant
of 20 characters or more in their tests, parsed out with `ast`, searched verbatim
against every line of the dev dump. **Zero hits** — the fixtures are hand-written,
not lifted.

The held-out split was dumped separately (`python -m bench.fixture … --split test
--seed 20042`) to a directory outside both git trees, and scored by
`bench.gate.score_fixture` — the same scorer, six defects lighter after its own
standalone review (`docs/reviews/E5-gate-standalone-review.md`). The dev number
was re-computed under the fixed scorer too, and is unchanged; none of the six
defects move it.

### What failed

**150 false positives. Zero false negatives.** Every miss is a user block in the
directive slice; both reversal slices are perfect on vocabulary the author never
saw.

All 150 are the same family: a user **restating a directive they already gave**.
The generator plants these as distractors on purpose — only the first statement
of a constraint is gold, because a memory system that opens a second decision
node every time someone repeats themselves is a memory system that cannot
deduplicate. The extractor has a guard for exactly this family, and the guard is
right about what it is for. It did not fire because its alternatives enumerate
the phrasings that appear in the one split the author could see.

That is the benchmark doing its job, and it is worth being precise about how: the
two splits are **lexically disjoint by construction**. The generator draws the
restatement prefixes — and the headers, and the template wordings — from
different pools for `dev` and `test`, so a rule stated as a class of word
survives the split and a rule stated as the strings one sample happens to
contain does not. Of the four restatement prefixes in the held-out pool, the
extractor's guard covered one.

So the held-out score here measures **lexical generalisation** specifically, and
should be read that way. It does not measure whether the extractor works on real
transcripts; the secondary set of hand-labelled real-shaped sessions named in
`docs/DESIGN.md` is still owed, and nothing in this file substitutes for it.

Perfect recall on unseen vocabulary is the strongest signal in the run, and it is
a real one: the pair-detector design — a decision is a sentence that names both
what is taken up and what is put down, never one side alone — found all 352 gold
decisions in words it had not seen. The failure is in the guards, not the rules.

### Disclosure ledger

Between run 1 and run 2 the author was told, and nothing else:

1. The four figures above and the per-slice breakdown.
2. That the errors are 150 false positives and 0 false negatives, all in the
   directive slice.
3. That every false positive is a block one of their guards exists to suppress,
   missed because the lexicon enumerates phrasings rather than the class its own
   comment names — plus the instruction to re-derive every lexicon as a class,
   and the warning that widening a guard risks the recall they have.

No sentence, phrase, template or word of the held-out split was disclosed, and
they were told not to try to infer any. **Run 2 will be scored on a freshly
generated split at a seed they have never been scored on**, because seed 20042
has now informed a revision and is spent.

---

## Run 2 — PASSED

A freshly generated held-out split (`--split test --seed 31337`, 100 sessions,
346 gold), dumped and smoke-checked against a perfect and an empty extractor
*before* the revised extractor was run against it.

| | precision | recall | matched | predicted | gold |
|---|---|---|---|---|---|
| **held-out (fresh split)** | **1.0000** | **1.0000** | 346 | 346 | 346 |
| directives | 1.0000 | 1.0000 | 169 | 169 | 169 |
| post-failure reversals | 1.0000 | 1.0000 | 103 | 103 | 103 |
| other reversals | 1.0000 | 1.0000 | 74 | 74 | 74 |

Dev is unchanged at 1.0000 / 1.0000 — the author re-scored it after every single
widening, and no widening ever caught a dev gold. The spent split from run 1 also
goes to 1.0000 / 1.0000, which is what the revision was aimed at and is not
evidence of anything by itself.

**The gate is met: 1.0000 ≥ 0.85 and 1.0000 ≥ 0.60.**

### What changed between the runs

The guards were rewritten as classes. The one real bug: `_REPAIR` — "that's a
typo, not a decision" — sat in the *assistant* branch, so a user correcting a
misspelling could not be suppressed at all, and every one of run 1's false
positives was a user block. The rest is class widening: a citation frame
`as <subject>? <slot>* <saying-verb>` over ~45 saying verbs in place of two
out-of-sync verb lists, an adverb slot inside the frames, the polite and
imperative variants of the same speech act, a perfect-tense branch separating
"we have never used pickle" (a memory) from "we never use pickle" (a rule), and
a whole-word requirement in the prohibition stop list that had been deleting
"no time-based tests" because "no time" is a formula.

Two candidate members were added and then removed for costing recall rather than
buying precision, which is the right direction to fail in.

## What a 1.0000 here does and does not mean

It means the extractor covers this generator's templates, in both of the
vocabularies the generator draws from, with nothing fitted to either — the
author never saw the held-out pool and their tests contain no string from the
dev dump.

It does not mean the extractor reads English. To put a number on the difference,
the reviewer wrote a **third-vocabulary probe**: 37 hand-labelled sentences in
phrasings taken from neither pool, written without reading the author's own probe
or tests, and deliberately stocked with shapes chosen to fail. Scored against the
same code that just went 346/346:

| group | |
|---|---|
| directives | 5/6 |
| reversals | 5/5 |
| guard: restatement | 1/5 |
| guard: deliberation | 1/3 |
| guard: retrospective | 0/2 |
| guard: opinion | 3/3 |
| guard: repair | 2/2 |
| one-sided prose (must stay silent) | 6/6 |
| **class generalisation** | **23/32** |
| declared ceilings | 2/5 |

Seven of the nine class misses are false positives from guards — the same
failure mode as run 1, in a vocabulary a third head produced. Two are
structural rather than lexical and worth naming:

- `_BACKREF` is anchored with `.match()`, on the reasoning that a discourse
  connective lives at the start of the sentence. "You already have this one, but
  repeating for safety: …" puts the frame after an introductory clause and
  escapes the guard entirely.
- `_OPINION` allows an adverb between the auxiliary and the verb ("don't *really*
  think") but not between the subject and the auxiliary. "I *genuinely* don't
  know which fits here" therefore reads as a prohibition, on the strength of the
  word "don't".

So the honest reading of this epoch: **the pre-registered gate passed on a blind
split, and the corpus is two samples of English rather than English.** The
secondary set named in `docs/DESIGN.md` — hand-labelled real-shaped sessions —
is still owed, and nothing in this file substitutes for it.
