# E5 — review of the labelled decision corpus

Reviewer: Claude. Reviewed: `bench/decisions.py` (732 lines),
`tests/test_decisions_bench.py` (252 lines), `docs/reviews/E5-gemini-corpus.md`,
all read in full. Against `docs/tasks/E5-gemini-corpus-brief.md` and
`docs/tasks/E5-brief.md`.

**Verdict: changes required before the corpus can grade anything.**

The attestation is clean and the generator is deterministic, seeded per session,
and genuinely synthetic — the parts that were hard to get right are right. The
problem is the part the brief said was the actual work: *"The distractors are the
actual work."* Eight families exist and are well written, but the corpus is
separable by a rule that never reads a word of the text, and that separability is
exactly what the reported `baseline_naive` precision of **0.00** is telling us. A
baseline scoring zero is not proof of a strong corpus. It is proof that the
corpus made the naive rule *anti*-correlated, which is a different and worse
property than making it *uninformative*.

## Blocking

### B1. No gold reversal ever follows a tool failure, so the corpus rewards the inverse error

`bench/decisions.py:422` plants a gold reversal; `bench/decisions.py:443` is the
`elif` that plants a flaky-retry sequence. They are mutually exclusive branches of
one chain, and the retry's turns are queued as raw `pending_turns`
(`bench/decisions.py:521`) that no plant ever touches. `Exit code 1` appears
exactly once in the whole file (`bench/decisions.py:475`) and it is inside that
distractor.

So: **every tool failure in the corpus belongs to a non-reversal, and every gold
reversal is preceded by no failure at all.** The report states it plainly —
*"True gold reversals were planted in independent turns with no preceding tool
failure"* — and reads it as a success. It is the finding.

The consequence is that an extractor can score well by learning *"a turn after a
tool failure is never a reversal"*, which is false in every real transcript and
is the inverse of the over-firing the distractor was built to punish. The corpus
teaches the mirror-image mistake and calls it a pass.

**Required:** a ninth family in which a **genuine** reversal follows a **genuine**
tool failure — the agent tries an approach, it fails for a real reason, and the
agent *abandons that approach and names a different one*. Those reversals are
gold. Keep the flaky-retry family exactly as it is; the pair is what makes the
distinction learnable rather than guessable. Enough of them that
`baseline_naive` lands in the middle — a precision somewhere around 0.2–0.5, not
0.00 and not 0.85. A distractor set that drives a baseline to zero has told you
the feature is perfectly predictive with the sign flipped.

### B2. Metadata leak: `usage` alone identifies every gold reversal

`bench/decisions.py:437` gives gold reversal turns
`{"input_tokens": rng.randint(100, 500), "output_tokens": rng.randint(50, 200)}`.
Every other assistant turn in the file takes one of five hard-coded pairs
(`:461`, `:503`, `:534`, `:552`, `:567`, `:577`, `:592`).

`usage` reaches the extractor: it is a field on the parsed record
(`src/gitmemory/records.py:111`) and is serialised
(`src/gitmemory/records.py:216`). So an extractor handed a `Session` can score
near-perfect recall on reversals with `turn.usage["output_tokens"] not in
{50, 60, 70, 80, 100, 120}` and never tokenise anything.

**Required:** draw `usage` for every assistant turn from the same distribution,
gold and distractor alike. Same for anything else that varies by branch and is
not the text — `model` is already uniform; check `timestamp` deltas too.

### B3. No committed checksum per split

The brief: *"Corpus generated on demand; commit a checksum per split, not fixture
files."* `tests/test_decisions_bench.py:68` compares two subprocesses at the same
HEAD to each other, which pins hash-seed independence but not the corpus's
identity. Edit a template and both subprocesses agree on a different corpus; every
previously published score silently becomes incomparable, with nothing failing.

**Required:** a module constant `CORPUS_SHA256 = {"dev": "...", "test": "..."}`
over the concatenated `transcript_bytes` of the full 100-session split, and a test
that recomputes and asserts it. Changing the corpus should then require changing
that constant in the same commit, which is the point — it makes the change
visible in review instead of invisible in a number.

### B4. The two tests that matter are deselected by default

`tests/test_decisions_bench.py:160` and `:211` carry `@pytest.mark.corpus`, and
`pyproject.toml:54` is `addopts = "-m 'not corpus'"`. So of the five new tests,
three run and the two that check corpus scale and baseline separation do not.
That mark also already means something else — `pyproject.toml:53` defines it as
*"needs the LongMemEval download"* — and neither of these downloads anything.

**Required:** unmark both. If 200 sessions is too slow for the default suite,
shrink what the default runs (20 sessions per split) and leave a separate marked
test for the full scale — but the baseline-separation assertion must run every
time, because it is the only thing standing between us and a corpus that quietly
stops being hard.

### B5. Every session has the same number of gold nodes, and none has zero

`bench/decisions.py:364` and `:421` both cap at `2 - planted`, with a forced plant
when the session is running out of turns. So every session ends at almost exactly
two directives and two reversals.

Two things go untested as a result. Precision on a **decision-free session** —
where a wrong extractor's false positives have nothing to hide behind — is never
measured, and that is the commonest real session. And because gold is near
constant per session, micro-averaging and macro-averaging give the same answer, so
the scorer's choice of one is unexamined.

**Required:** vary gold per session, **including sessions with zero** (aim for
~15–20% of each split). Keep the totals over 300 per split.

### B6. Multi-block user turns are never generated, so block index is always 0

`bench/decisions.py:344` sets `role = "user" if t_idx % 2 == 0 else "assistant"`.
Inside `if role == "user":` (`:362`), the guards `if t_idx % 2 == 1:` at `:375`
and `:389` are therefore **unreachable**. The list-shaped `content` variant they
exist to produce is never emitted, and every plant records block index `0`
(`:382`, `:443`).

The brief asked for `(line uuid, block index within that turn, kind)` precisely
because the interesting case is a directive in the *second* block of a turn whose
first block is something else. That case does not exist in the corpus, so nothing
checks that an extractor attributes to the right block rather than to the turn.

**Required:** delete the dead guards and generate multi-block user turns on their
own condition, with some gold directives at block index ≥ 1.

### B7. `resolve_gold_for_case` raises on an unresolved plant, and nothing tests that it does

`bench/decisions.py:624` does the right thing — a plant that the adapter cannot
resolve to a real `block_id` is an error, not a silently dropped label. In a repo
where every fix is pinned by a negative control, that guard needs a test that
fails when it is removed. Construct a `Case` with a plant naming a uuid that is
not in the transcript and assert it raises.

### B8. There is no gate runner

`score_predictions` exists; nothing drives it. The brief's harness is the thing
that *"hands the extractor only the `Session`"* — that boundary is what stops the
extractor from reading `planted_decisions`, and right now it lives only in the
test bodies. Add `run_gate(extractor, seed, split, n) -> dict` in `bench/`, taking
a `Callable[[Session], list[Decision]]`, and have the baseline test call it. Then
the scored run on `test` is one call with no room for the extractor to see
anything it should not.

## Non-blocking, but recorded

**N1. Disjoint templates between splits is a change to what the gate measures.**
The pre-registration said two splits from disjoint seeds, with disjoint
vocabularies. `DEV_*_TEMPLATES` vs `TEST_*_TEMPLATES` goes further: `test` now
measures generalisation to unseen *surface forms*, not just unseen fillers. It
only makes the bar harder, so it stays — but it changes how a dev/test gap reads,
and a change to the measurement has to be written down before the number is,
not after. I will add it to `docs/tasks/E5-brief.md` as an interpretation note.

**N2. The report's distractor rates are conditional, not absolute.** "~35%",
"~40%", "~15%" are the `rng.random()` thresholds inside an `elif` chain, so each
is conditional on every earlier branch missing. Family 8 at a nominal 15% fires
far less often than that. Report measured occurrence counts over a generated
split instead of the literals from the source.

**N3. `tests/test_decisions_bench.py:68` sets `PYTHONPATH=os.getcwd()`,** so the
determinism test only passes when pytest is invoked from the repo root. Derive it
from `__file__` instead.

**N4. The corpus report's closing line — "The gate is ready, fully verified, and
completely functional" — is the kind of claim this project's review record
exists to not make.** State what was run and what it showed; let the reader
conclude. B1 is the reason that matters here: the report's own evidence
(0% precision, 0 matched) was the tell, and it was written up as a success.

## What is right, and should not change

- The attestation is specific and complete, and the generator is genuinely
  synthetic — no real transcript was sampled, anonymised or adapted.
- Per-session seeding via `sha256(f"{seed}_{i}")` (`:618`) rather than a shared
  stream means adding a session does not perturb the ones before it. That is the
  right call and it is not the obvious one.
- `score_predictions` matching one-to-one through a `Counter` (`:656`) handles the
  duplicate-prediction case correctly, and the test pins it (`:141`).
- Precision defined as 0.0 on an empty prediction set, rather than 1.0 or a
  `ZeroDivisionError`. Correct, and `baseline_nothing` exists to keep it honest.
- The disjoint-vocabulary test (`:30`) checks templates *and* fillers, and it
  fails loudly rather than reporting an overlap count.

---

# Round 3 review

Reviewer: Claude. Reviewed: `bench/decisions.py` (1229 lines) and
`tests/test_decisions_bench.py` (380 lines), both read in full;
`docs/reviews/E5-gemini-corpus.md`. Against
`docs/tasks/E5-gemini-corpus-round3-brief.md`. Every number below was
recomputed here rather than read out of the report.

**Verdict: C1, C2, C3 and C4 are done. Two leaks of the same class survive,
neither of them in a position C2 checks.**

## Confirmed, independently

| claim | reported | recomputed |
|---|---|---|
| `baseline_naive` precision, dev | 0.1940 | 0.1940 |
| `baseline_naive` recall, dev | 0.2814 | 0.2814 |
| `baseline_leak` precision, dev | 1.0000 | 1.0000 |
| `baseline_leak` recall, dev | 0.2814 | 0.2814 |
| post-failure share of gold reversals, dev | 54.79% | in band |
| post-failure share of gold reversals, test | 40.23% | in band |

C1's three positions are genuinely shared now: both four-turn families draw the
opening, the first command and the tool error from one pool per split
(`bench/decisions.py:750,752,771` and `:861,863,882`). No `flaky`, no
`transient`, no `without changes` anywhere in the file. The checksum test no
longer prints the hash it wants pasted back.

## D1 (blocking). The tool_use id spells the label

`bench/decisions.py:798` builds the gold family's second tool call as
`tu_{i}_{t}_reversal`; `bench/decisions.py:897` builds the distractor's as
`tu_{i}_{t}_retry`. Both ids are echoed again by the `tool_result` that follows.

This is not hypothetical and it is not hidden behind the raw bytes. The parser
keeps the original object on `Block.native`, so the extractor — which is handed a
`Session` — reads it directly:

```
kind=tool_use     native  {"id": "tu_0_13_retry", "input": {...}, "name": "bash", ...}
kind=tool_result  native  {"content": "...", "tool_use_id": "tu_0_13_retry", ...}
```

An extractor whose entire logic is `"_reversal" in block.native["id"]` scores
1.00/1.00 on this family. That is round 1's defect again, now in an identifier.

## D2 (blocking). The success message differs by family

The fourth turn of the gold family closes with `Exit code 0\nRan 12 tests. All
passed successfully.` (dev) and the distractor's with `Exit code 0\nAll local
checks completed successfully.` (dev). One fixed literal each, one per side,
never crossing. Same on the test split.

A literal scan of the two branches finds exactly these two carriers and no
others:

```
only in the GOLD family:         "Exit code 0\nRan 12 tests. All passed successfully.\n"
                                 "Exit code 0\nDone. 8 specs passed.\n"
                                 f"tu_{session_idx}_{t_idx}_reversal"
only in the DISTRACTOR family:   "Exit code 0\nAll local checks completed successfully."
                                 "Exit code 0\nVerification passed. 0 errors."
                                 f"tu_{session_idx}_{t_idx}_retry"
```

## Why C2 did not catch either

C2 does what it was asked for and that is the problem with it. It checks three
named positions — opening, first command, tool error — so a leak that moves to a
fourth position is invisible to it, which is what happened. The instrument has to
be general: compare the two families over *every* string the transcript carries,
not over a list of places we already thought of. That is D3 in the round-4 brief.

## Non-blocking

- `test_baseline_leak_on_corpus` asserts `precision == 1.0`. The brief asked for
  the number to be *reported*, not enforced; as written, a future change that
  makes the strongest cheap rule imperfect — an improvement — fails the suite.
- `baseline_naive` precision on the **test** split is 0.1423, below the 0.15 the
  brief set for dev. The brief scoped the band to dev, so this is not a breach,
  but it should be stated in the report rather than left to be discovered.
- `npm run test:unit` and friends contain round 1's `npm run test` as a prefix.
  Different literals, so C3 is met; noted only so nobody re-derives the question.
- C3's "no literal from round 1" cannot be fully verified from here: round 1's
  `bench/decisions.py` was never committed, so there is nothing to diff against.
  Checked against the literals the round-3 brief itself quoted; all are gone.

---

# Round 4 review

D1 and D2 are closed. D3 and D4 are delivered, and reviewing D3 as code turned
up the thing this round is really about: the general leak test carries an
exclusion nobody declared, and it labels the two families by the very relation
the corpus is supposed to make non-trivial.

## Confirmed, independently

Recomputed from a script written against the brief rather than against
`tests/test_decisions_bench.py` — generate, resolve gold, parse, score with my
own `Counter` arithmetic. Every reported figure reproduces exactly:

| | dev | test |
|---|---|---|
| corpus sha256 | `8743844f…680e` ✓ | `c8b9a6a9…8bf0` ✓ |
| `baseline_naive` | P 0.1952 R 0.2643 (97/497, gold 367) ✓ | P 0.1515 R 0.2131 (75/495, gold 352) ✓ |
| `baseline_leak` | P 1.0000 R 0.2643 (97/97) ✓ | P 1.0000 R 0.2131 (75/75) ✓ |
| gold reversals / post-failure | 187 / 97 = 51.87% ✓ | 173 / 75 = 43.35% ✓ |

**D1 closed.** No identifier is family-specific. My walk collects every string
leaf of every turn `native` *and* every block `native` in the four-turn window,
normalises digits and uuids, and diffs the two families: not one `tu_…` id, tool
name or `tool_use_id` appears on one side only. The round-3 separator is gone.

**D2 closed.** Same walk: no `Exit code 0` message appears on one side only. The
success pool is shared.

**Acceptance 1 met.** The new test was shown failing against the round-3 corpus,
and the pasted failure names exactly the two round-3 carriers.

## E1 (blocking). The general leak test drops every `text` field, and says so nowhere

`walk_leaves` has a carve-out:

```python
if obj.get("type") == "text" or obj.get("kind") == "text":
    for _k, v in obj.items():
        if _k != "text":
            walk_leaves(v, leaves)
```

Every text block in all four turns loses its content before the comparison. That
is why the test reports 34 distinct leaves per family where the same walk without
the carve-out reports 156 gold against 69 distractor.

Something like it has to exist — the reversal sentence *must* differ from the
retry sentence, that is the signal being detected, and set-equality over the
decision text is unachievable by construction. The objection is not that content
is excluded but that **this exclusion is undeclared and far wider than the
reason for it.** It also drops the opening text of turn 0, which is the exact
position C1 was about; the three-position test still covers that one, so nothing
is currently unguarded, but a check called *general* now has a hole in it that
its name denies.

Narrow it to the one block the reason applies to — the decision-bearing text of
the third turn — and say in the docstring what is skipped, why, and what covers
the skipped part instead.

## E2 (blocking). The test labels the families by the signal under test

```python
if cmd == f_cmd:
    dist_leaves.extend(norm_leaves)
else:
    gold_leaves.extend(norm_leaves)
```

The family label is "did the command change" — which is precisely what
`baseline_leak` exploits, and precisely the relation E3 below asks you to stop
making deterministic. A leak check whose ground truth is the leak cannot see a
leak in that dimension, and the moment E3 lands this labelling is simply wrong:
windows will be mislabelled and the comparison will silently become meaningless.

Label from `case.planted_decisions` — the gold that `resolve_gold_for_case`
already resolves to block ids. Ground truth, not a proxy for it.

## E3 (blocking). The label is a deterministic function of one string comparison

Across both splits, every post-failure window without exception:

```
(is_gold, command_repeated) -> {(False, True): 400, (True, False): 97}   dev
(is_gold, command_repeated) -> {(False, True): 420, (True, False): 75}   test
```

Zero off-diagonal. `baseline_leak` scores precision **1.0000** on both splits for
that reason, and it is five lines long. The gate survives it — recall is 0.21, so
the 0.60 bar still requires the other 277 test decisions — but 21% of the corpus
currently measures string-diffing rather than decision extraction.

Both off-diagonal cells are ordinary in a real transcript: an agent re-runs after
a transient failure with one flag added (a retry that changed the command), and
an agent announces a change of approach while running the same command again (a
reversal that did not). Generate both, report the 2×2, and require each cell to
be non-empty on both splits.

## E4 (blocking). The negative class is six strings

The distractor retry pool is 6 fixed forms per split; the gold reversal pool is
combinatorial and produced 94 distinct sentences in 97 dev windows. Memorising
six strings therefore classifies the entire negative half of the post-failure
slice. Give the retry pool the same template × vocabulary treatment the reversal
pool gets, so the two classes are comparably varied.

## E5 (non-blocking). `run_gate` returns one number over three populations

Dev gold is 180 directives, 90 non-post-failure reversals, 97 post-failure
reversals. One micro-averaged P/R lets an easy slice hide a hard one. Have
`run_gate` also return the per-slice breakdown; the overall figure stays the gate.

## Checked and clean

- The **directive** half is structurally sound. Describing every text block by
  position and neighbours only — role, block count, block kinds, previous and
  next role, index in turn, length bucket — and asking which descriptions no
  non-gold block ever occupies: 5 of 180 on dev, 11 of 179 on test, and all of
  them are "first turn of the session", which is a legitimate place for a user
  to state a directive. No structural separator.
- Window **shape** is identical between the two reversal families: one distinct
  shape each, and it is the same shape. No block-count or block-kind separator.
- D4 both items done: the precision assertion is a floor, and both figures are
  in the report. Note that the floor is `>= 0.90`, which E3 will breach on
  purpose — round 5 has to move it.
- `ruff check`, `ruff format --check`, and the full suite: clean, 810 tests in
  that worktree (main is at 829; the count is reconciled at merge).

# Round 5 review

**Accepted. The corpus is frozen at these checksums:**

| Split | Seed | SHA256 over the concatenated transcripts |
|---|---|---|
| dev | 42 | `0c82d7567678913da5375b47107db0419c1d4f8ffd7cc322d01ce68538906a7b` |
| test | 20042 | `1f424e92a6c612beeace0da6dac60f64ab2c5278af7ab1d9932c75e8cb2b154a` |

## Confirmed, independently

Recomputed by a separate route, not by reading the test file: the leak walk
filters the decision sentence **by value** instead of blanking a fixed JSON
path, the window filter is "the previous turn failed" instead of "the next
result says Exit code 0", and the slices are rebuilt from
`resolve_gold_for_case` instead of read out of `run_gate`. Every figure in the
round-5 report reproduced exactly.

| | dev | test |
|---|---|---|
| gold: directives / post-failure reversals / other reversals | 180 / 101 / 86 | 179 / 81 / 92 |
| `baseline_naive` P / R | 0.1917 / 0.2752 | 0.1582 / 0.2301 |
| `baseline_leak` P / R | 0.4149 / 0.2125 | 0.3316 / 0.1818 |
| post-failure share of gold reversals | 54.01% | 46.82% |
| distinct retry / reversal sentences | 269 / 97 | 268 / 79 |
| `(is_gold, command_repeated)` | 110 / 316 / 78 / 23 | 129 / 302 / 64 / 17 |

## The four blocking findings

**E1 closed.** The carve-out is now one field of one block — `t2.blocks[0]`'s
text, the sentence that gold and distractor *must* differ in or there would be
nothing to detect — and the docstring says so and says what covers the gap.
Everything else is walked, including the three other turns' text, which is
where round 4's version was blind. Verified from outside: with that one string
filtered out by value, the normalised leaf sets are **equal, 63 each, zero
difference, on both splits**. The remaining difference in the corpus is the
decision sentence itself, which is the task.

The negative control was run and is in the report: re-introducing a
family-specific suffix on the third turn's `tool_use` id makes the test fail
and *name* the leaking identifiers (`tu_<NUM>_<NUM>_retry_gold` against
`…_retry_distractor`), and reverting makes it pass. The test can fail, so its
passing means something.

**E2 closed.** `is_gold` now comes from `resolve_gold_for_case(case)`, not from
`cmd == f_cmd`. The circularity is gone.

**E3 closed.** All four cells of the 2×2 are populated: on dev, 23 of 101 gold
reversals repeat the command and 110 of 426 retries change it. Repetition is
now evidence, not the answer. `baseline_leak` precision fell from a
five-line-derived **1.0000** to **0.4149** / **0.3316**, and the ordering
`naive < leak < 1.0` holds strictly on both splits.

**E4 closed.** The retry pool is combinatorial like the reversal pool: 269
distinct retry sentences against 97 reversal sentences on dev, 268 against 79
on test. The negative class is no longer six strings anyone could memorise.

**E5 closed.** `run_gate` reports matched/predicted/gold, precision and recall
per slice as well as overall. The breakdown immediately earns its place: both
baselines score **0.00 on directives and 0.00 on other reversals**, so the
single micro-average was averaging two untouched populations into one number.

## Checked and clean

- **No lexical cheat channel in the directive half.** No token appears in every
  gold directive and no other text block, on either split. The most
  discriminative gold-exclusive token covers 2.78% of dev gold and **nothing at
  all on test**, and the set of gold-exclusive tokens shared by both splits is
  empty. A word-list cheat cannot reach the recall floor.
- **Structure, re-measured on the regenerated corpus.** 5 of 180 dev and 11 of
  179 test gold directives sit in a context no non-gold block occupies, all of
  them "first turn of the session" — unchanged from round 4, and bounded at 6%
  of one slice, which is far below the 0.60 recall floor.
- Window shape is still one distinct shape per family, and the same one.

## Recorded, not fixed

- **The vestigial pool test still labels by `cmd == f_cmd`.** The three
  opening/command/error pool assertions above the general check use exactly the
  proxy E2 was about. They are not wrong so much as superseded: the general
  check walks those same strings under ground-truth labels and subsumes them.
  Left in place because the corpus is frozen and deleting a test at freeze time
  buys nothing; noted so nobody reads it as independent evidence.
- **Half the gate has no baseline under it.** Directives are 49% of gold and
  "other reversals" another 24%, and no baseline predicts either, so the
  extractor's floor on 73% of the corpus is zero by construction. That is a
  statement about what baselines exist, not a defect in the corpus — but it
  means the headline number will be carried by the slice with the least
  adversarial pressure, and the per-slice breakdown is the thing to read.
