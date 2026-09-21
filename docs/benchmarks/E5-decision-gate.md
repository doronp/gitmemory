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
