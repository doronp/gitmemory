# Results — the full record

This page holds every result the project has published, including the ones it
loses. The [README](../README.md#results) carries the summary table and the
figures that tests pin: the test count, the negative controls, the hook latency.
Each section below links to the gate report it summarises. The gate reports
are the source of truth, and this page is a reader's path through them.

## The number this is built on

An agent's transcript is an append-only byte stream that nothing durably
keeps. Compaction discards it. So the case that matters is the one where the
evidence is *behind* the compaction boundary — where the live context window
has genuinely lost it.

On 470 LongMemEval instances, k = 10, seed 42, under that arrangement:

| Arm | Turn recall | Turn MRR |
|---|---|---|
| **gitmemory's index** | **0.7456** | **0.6209** |
| The live context window | 0.0000 | 0.0000 |

The zero is not a weak baseline. The bytes are gone; the window cannot reach
them at any k.

**Now the caveat, because it is the same size as the result.** Under the
*opposite* arrangement — evidence ahead of the boundary — the live window
**beats** the index, 0.7893 to 0.7456, because walling off the past also walls
off the distractors. The reported mode was pre-registered before anything was
measured, for exactly this reason. A benchmark that picked its own mode would
have published the 0.7893 as a gitmemory number.

**And the second caveat: the index above is BM25, and BM25 is the worst of the
three retrievers.** A dense arm scores 0.8215 and a reranking arm 0.8295 on the
same instances under the same mode. That does not touch the claim — it is about
the boundary, and every arm is behind it — but it does mean the default install
ships the arm that came last.
[Full gate report](benchmarks/E3-longmemeval.md) — four compaction modes,
fourteen calibration gates, every arm including the ones that lost.

## Where this sits against other memory systems

**Under the field's own protocols, a bench arm is first on one benchmark and
second on two; the shipped arm is first on none.** Adding a local cross-encoder
to the shipped BM25 index (`rerank12`, no LLM) reads LongMemEval-S session
R@5 **99.2** and R@10 **99.8** — first among no-LLM retrievers — and is second on
all-evidence@10 (96.0 to Total Recall's 97.73) and on LoCoMo session R@10 (91.3
to MemPalace's 92.4). End-to-end QA with a Gemini reader and judge is 94.2 on
LongMemEval-S (5th of 10) and 85.5 on LoCoMo (last of 8); neither judge matches the
leaderboard's. [E9 rankings](benchmarks/E9-peer-protocols.md) — every
table sourced, the shipped arm in each, dev/test splits for every choice.

What follows is the E8 comparison, on E3's harness (noisy corpus, ten-turn
budget), kept because its caveats still hold for the shipped arm.

**We do not beat the leaders on the numbers as each side publishes them.** On
session-level retrieval on LongMemEval-S — the one axis where a shape-matched
comparison exists — the shipped arm reads **S-Hit@10 0.9660** and **S-All@10
0.8298**, against Total Recall's self-reported 0.9940 and 0.9773 and
Recallium's 0.9840 and 0.9360. Those are the session-level columns; the
turn-level `Hit@10` and `All@10` in the table above are different quantities
and are lower.

Against agentmemory — the one baseline running the *identical* dataset file
with an LLM-free retrieval harness — we split: ahead of its BM25 configuration
**0.9660 to 0.9460**, behind its hybrid configuration at **0.9860**, which
beats all three of our arms.

Our own ground-truth oracle scores 0.9319 on S-All@10, and our shipped arm sits
10.2 points under it. That gap is retrieval quality, not apparatus. Their
`k = 10` selects ten sessions where ours selects ten turns, which is a real
difference we have **not** measured the size of and should not lean on.

[Full comparison](benchmarks/E8-where-we-stand.md) — three tables split by
measurement axis, what every cell is sourced to, and the ordered work that would
make the comparison real.

## The measurement board, extraction rows

| What | How it was measured | Result |
|---|---|---|
| Retrieval across a compaction boundary | 470 LongMemEval instances, 4 compaction modes, 14 calibration gates | **0.7456** turn recall, against **0.0000** for the live window |
| Decision extraction | held-out split, gate pre-registered at precision ≥ 0.85 / recall ≥ 0.60 | **1.0000 / 0.7428** — it read 1.0000 / 1.0000 for five rounds; fix 2 spent a quarter of the recall and see below |
| The same extractor, in a vocabulary its corpus does not contain | two adversarial probes hand-written by a reviewer, both since spent | 26/32 and 27/32 — **23/32 and 25/32** once the precision fix below traded five of them away |
| The same extractor again, on two probes written blind and each scored once | 32 items apiece, in two unrelated domains chosen to share no vocabulary with the corpus or with each other | **14/32** and **14/32** — 16 and 16 after the fix their shared miss list bought, which is a floor and not a second measurement |
| The same extractor on text nobody wrote for a benchmark | a census of every distinct human turn in a third-party MIT corpus of real sessions — 140 items, labelled blind by three annotators at 139/140 agreement | **precision 0.0000, recall 0.0000**. Seven fixes later, 0.1250 / 0.5000 — one true positive, and the set is a regression floor from the first fix onward |
| …and the assistant side of the same sessions | the 61 blocks it called `reversal`, adjudicated by three more | 9 of 61 — precision 0.15. After the two fixes: **54 withdrawn, all 7 left are reversals** — and read the caveat below before quoting that |

**Read the extraction numbers carefully — the gap between them is the finding.**
The gate is a score on a held-out split the extractor's author could not see,
against a bar pre-registered before anything was measured. It is not evidence
the extractor reads English. It read 1.0000 / 1.0000 through five rounds of real
defects, every one of which it scored identically with and without — and then
fix 2, tightening the extractor against the real sessions two rows below, took
held-out recall to **0.7428** (257 of 346; post-failure reversals 0.5243, other
reversals 0.4595). That is a priced trade, not a gate failure: 0.7428 still
clears the 0.60 floor, and precision stayed at 1.0000 on both splits.

What is worth saying plainly is that **nobody noticed for a commit**. No test
scored `derive.decisions` against the synthetic corpus, so a quarter of the
recall went missing behind a green suite of 1168. `tests/test_decisions_bench.py`
now pins the dev split's `(matched, predicted, gold)` — the counts, not the
ratio, because `matched / predicted` reads 1.0000 while both shrink together.
The test split stays out of the suite; it is scored by hand, once per round.

Probes C and D are what that gate cannot see. Each was written by an agent that
read only this README and the design document — never the extractor, never the
other probes — in a domain chosen so that lexical overlap with the corpus is
impossible. Each was frozen unscored and scored once. **14 of 32**, against 26
and 27 for the two earlier probes, which are spent because each set the brief
for a revision and then graded it.

The 18 misses are two facts, and they point in opposite directions:

- **10 of 10 on the items that are not decisions** — no false positive anywhere
  in a vocabulary the guards have never seen, including three imperative work
  requests and two sentences carrying *must* about somebody else's rule. The
  conservatism is real and it generalises.
- **4 of 22 on the items that are.** All eleven reversals were missed, and all
  eleven were the *user* reversing their own earlier instruction — a ceiling the
  extractor declares in a source comment and nowhere a reader of these documents
  could find it. Seven of eleven directives were missed, all plain positive
  standing rules ("Patch file stays YAML.") with no prohibition to match on.

D was commissioned to ask whether that was C's domain rather than the
extractor, and the answer is no. Aviation line maintenance, an author with no
knowledge of C's theatrical show control, **and the same 14** — 10/10, 4/11,
0/11, the identical split, with the four directives that land being the same two
constructions (a prohibition, or "use X, not Y"). Two of D's reversals came back
labelled `directive`: the rule extracted is the right one and only its kind is
wrong, which is a different defect from not seeing it, and is why the shortfall
is reported as 14 rather than 16.

The extractor was **not** changed in response to either, because editing it
against a probe's own miss list turns the unspent measurement into more training
data. Full records: [C](benchmarks/E5-probe-C.md),
[D](benchmarks/E5-probe-D.md).

**Then it was run on text nobody wrote for a benchmark, and scored zero.**
Probes are fiction; a person asking for work at 1am does not write like a probe
author. The secondary set is every distinct human turn in four real projects
from the same third-party MIT corpus this repository already clones — a census,
not a sample, 140 items, each labelled by three annotators who worked
independently and never saw the extractor. It produced 32 `directive` nodes,
**none of them a directive**, and missed both of the two that were there.

Two things only real text could say. **A standing rule is rare** — 2 turns in
140, against a third or more in every probe, because an author asked to write
decisions writes decisions. And **two turns in five in the user role were not
typed by a user**: `<ide_opened_file>`, `<local-command-stdout>`, slash-command
wrappers. Nineteen of the thirty false positives are those, and the mechanism is
one injected sentence — *"This may or may not be related to the current task"* —
in which `_PROHIBIT` matches **`may not`**. No probe can contain this, because
every probe item is a sentence somebody wrote on purpose.

The assistant side was larger and worse: 61 distinct blocks called `reversal`, of
which three adjudicators kept **9**. The substitution frame did most of the
damage — *"pass a Path instead of a string"* is two-sided and is not a change of
course.

It was recorded first with nothing changed, against the code exactly as it stood
when the labels were written, because a number produced after the fix is a
number about the fix. **Then the first fix landed**: a block in a human's turn
that a program put there is not prose. False positives 30 → 12, machine-authored
ones 19 → 1. Precision did not move — it was 0/30 and is now 0/12, with still no
true positive — and saying it improved would be the dishonest version.

**The second fix** is the assistant side, and it rests on a definition: a
reversal is the assistant putting down *its own prior position*, not any
substitution in the artifact. Writing code is substitution all day long, so the
frame now has to be joined by evidence that something was withdrawn — a
concession, a comparative ("a different approach"), or a verdict that the thing
does not work. 52 of the 61 nodes went; 7 of the 9 left are reversals, precision
0.15 → **0.78**. It cost five items on the two spent probes, three of them the
trade itself and two collateral, and the floors were re-pinned rather than
argued with.

A review of that fix found its conjunction scoped to the whole block, which is
not a scope: both survivors were a concession opening the message and, four
hundred characters of narration later, an `instead` belonging to a description
of the bug. Scoped to the **paragraph** it withdraws those two and keeps all
seven true ones, at no cost to either gate split or to any of the four probes —
the only free correction in the round. **54 of 61 withdrawn, 7 left, none of
them known to be wrong.** Do not read that as precision 1.0000: the denominator
is the extractor's own output, so it shrinks every time the extractor gets
shyer, and an extractor that emitted nothing would score the same. The claim
that survives is the narrow one — of what it still writes, nothing is wrong —
and it rests on seven items. The set is a floor, not a generalisation measure, from fix 1
onward. What settles fix 2 is a blind probe, and that probe has now run:

**Probe E, written blind and scored once: 19 of 32.** It uses COBOL batch
maintenance, a domain chosen to share no vocabulary with anything above. Fix 2's
claim holds cleanly: **12 of 12** items that are not decisions were left alone,
and ten of them carry the substitution frame that used to fire. The cost is the
other half. Only **2 of 12** genuine assistant reversals were caught, against 7
of 7 on the secondary set, because nine of the twelve carry no self-correction
marker at all. Tracing the misses found two defects; one is fixed, and the
headline stays 19 because a rescore after seeing the misses is not a second
measurement. Full record: [probe E](benchmarks/E5-probe-E.md).

Unlike a probe this set cannot be replaced by writing another one: there is one
corpus of real third-party sessions here, so from that fix onward its score is a
regression floor and nothing more. Full record:
[the secondary set](benchmarks/E5-secondary-set.md).

## What is *not* measured, said on the dashboard itself

A memory system that reports only its wins is a marketing surface. These ship
as rows on the page, not as omissions:

- **Net saving versus no memory — UNMEASURED.** No "tokens saved" tile exists,
  and none will before the A/B harness runs.
- **Injection cost — NOT BUILT.** It is a *cost*, it would be shown first, and
  it does not exist yet.
- **Whether an injected memory influenced an answer — not observable.**
  Attention leaves no trace. The panel can say REFERENCED; it cannot say USED.
- **Both tails are invisible.** A prevented dead-end is an unbounded unmeasured
  saving; a stale memory that misled is an unbounded unmeasured cost. Any single
  frugality number assumes both are zero.
- **BM25/FTS5 is the weakest retriever measured, not the best.** All three
  retriever arms have now run, and the shipped default loses: 0.7456 turn
  recall against 0.8215 for `dense` (model2vec) and 0.8295 for `rerank`
  (flashrank over BM25's top 50). Both are behind the `hybrid` extra, so a
  default install gets the arm that came last.
- **The decision graph is close to useless on real sessions**, and that is
  measured rather than suspected. Precision 0.0000 on the user side — 14 nodes,
  none of them a directive — and 0.15 on the assistant side, since lifted by
  emitting 54 fewer nodes, which leaves seven and is not a precision claim. Four projects and one developer is not a
  population, and recall rests on two gold items; the false-positive count does
  not. Seven rounds of fixes later the user side emits 8 nodes and **1** of them
  is a directive — the first true positive the set has produced — which is 0.1250
  and is still close to useless.
