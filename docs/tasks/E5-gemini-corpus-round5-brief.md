# E5 corpus — round 5 (last one)

Round 4 closed D1 and D2, and I verified every number in your report
independently — the checksums, both baselines on both splits, and the
post-failure shares all reproduce exactly. No identifier, tool name,
`tool_use_id` or success message is family-specific any more. That work is done;
do not revisit it.

This round is about the general leak test itself and the one relation it was
labelled by. Full reasoning is in `docs/reviews/E5-claude-review-of-gemini-corpus.md`
under "Round 4 review". After this the corpus is frozen and the extractor gets
built against it, so anything not fixed here is fixed never.

## E1. The general leak test drops every `text` field, and declares it nowhere

`walk_leaves` skips the `text` key of any dict whose `type`/`kind` is `text`. The
same walk without that carve-out reports 156 distinct gold leaves against 69
distractor, not 34 against 34.

An exclusion has to exist — the reversal sentence must differ from the retry
sentence, that is the signal being detected — but it must be as narrow as its
reason and it must be stated. As written it also drops the opening text of turn
0, which is the position C1 was about.

**Required.** Exclude exactly one thing: the decision-bearing text block of the
third turn. Everything else in all four turns is walked, including every other
text block. Say in the docstring what is skipped, why set-equality is
unachievable over it, and which test covers the skipped part instead.

## E2. The test labels the families by the signal under test

```python
if cmd == f_cmd:
    dist_leaves.extend(norm_leaves)
else:
    gold_leaves.extend(norm_leaves)
```

The label is "did the command change", which is exactly what `baseline_leak`
exploits and exactly the relation E3 tells you to stop making deterministic. A
leak check whose ground truth is the leak is blind in that dimension, and once
E3 lands this labelling is simply wrong.

**Required.** Label each window from `case.planted_decisions`, resolved to block
ids the way `resolve_gold_for_case` already does it: a window is gold when the
third turn carries a planted reversal. Ground truth, not a proxy.

## E3. The label is a deterministic function of one string comparison

Every post-failure window in the corpus, both splits, no exceptions:

```
(is_gold, command_repeated) -> {(False, True): 400, (True, False): 97}   dev
(is_gold, command_repeated) -> {(False, True): 420, (True, False): 75}   test
```

`baseline_leak` gets precision 1.0000 from five lines of code for that reason.
The gate survives it — recall 0.21 against a 0.60 bar — but a fifth of the corpus
currently measures string-diffing, not decision extraction.

Both missing cells are ordinary in real transcripts:

- a **retry that changed the command** — same intent, one flag added, a path
  corrected, a typo fixed; the text still says "let me run that again";
- a **reversal that kept the command** — the assistant announces a change of
  approach (the data structure, the library, the storage) and the command it
  runs next is the same test or build invocation.

**Required.** Generate both. Aim for roughly 20–30% of each family taking the
off-diagonal form; the exact share is yours, the requirement is that all four
cells of the 2×2 are non-empty on **both** splits and that the table is reported.
Do not make the off-diagonal cases lexically marked — they draw from the same
pools as the rest of their family.

## E4. The negative class is six strings

The retry pool is 6 fixed forms per split. The reversal pool is combinatorial and
produced 94 distinct sentences in 97 dev windows. Six memorised strings currently
classify the whole negative half of the post-failure slice.

**Required.** Give the retry pool the same template × vocabulary construction the
reversal pool has, so the two classes are comparably varied. Report the distinct
count per split for both.

## E5. One number over three populations

Dev gold is 180 directives, 90 non-post-failure reversals and 97 post-failure
reversals. A single micro-averaged P/R lets an easy slice hide a hard one.

**Required.** `run_gate` additionally returns a per-slice breakdown — directives,
post-failure reversals, other reversals — each with matched/predicted/gold,
precision and recall. The overall figure stays the gate and its shape does not
change; this is an addition to the returned dict, not a replacement.

## Acceptance

1. **E1's narrowing is negative-controlled.** The round-3 demonstration cannot be
   repeated — that corpus is gone. Instead: temporarily re-introduce a
   family-specific id (one line, e.g. suffix the third turn's `tool_use` id with
   the family), show the general test failing and naming it, revert, show it
   passing. Paste both. A leak test that has never failed is a claim.
2. E2 done: windows labelled from planted decisions. Report the window counts per
   family and confirm they equal the 2×2 totals from E3.
3. E3 done: the 2×2 reported for both splits, all four cells non-empty.
   `baseline_leak` precision reported for both splits and now strictly below
   1.0 and strictly above `baseline_naive`'s. Replace the `>= 0.90` assertion
   with that ordering — `naive < leak < 1.0` — which is the property we actually
   want and the one a future improvement should not break.
4. E4 done: distinct retry-sentence and reversal-sentence counts reported per
   split, comparable in order of magnitude.
5. E5 done: the three-slice breakdown reported for `baseline_naive` and
   `baseline_leak` on both splits.
6. Bands still hold and are reported: `baseline_naive` dev precision within
   `0.15 <= p <= 0.60`, post-failure share of gold reversals within 40–60% on
   both splits. Do not tune the corpus to land either — if a band breaks, report
   it and say why.
7. `CORPUS_SHA256` updated in the same change. `ruff check .` and
   `ruff format --check .` clean, whole suite green, README test count correct.

## Scope and standing constraints

Unchanged. Only `bench/decisions.py`, `tests/test_decisions_bench.py`,
`docs/reviews/E5-gemini-corpus.md`, and the README test count. Nothing under
`src/` and nothing under `docs/tasks/`. No real transcript is read, sampled,
anonymised or adapted. dev and test stay disjoint in seed, vocabulary and
template. Do not commit; leave the changes in the working tree. Open your report
with the attestation: the files you read, the commands you ran, and that nothing
outside this repository was read — in particular nothing under `~/.claude/` or
`~/memory/`.
