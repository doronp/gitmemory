# E5 corpus — round 3

Round 2 closed B2 through B8. Checked and confirmed: `usage` distributions
overlap, `CORPUS_SHA256` is committed with a test, neither corpus test carries a
mark, zero-gold sessions exist, a gold directive lands at block index 1
(`bench/decisions.py:550`), `resolve_gold_for_case` has a test that it raises, and
`run_gate` hands the extractor a `Session` and nothing else. Those are done; do
not revisit them.

B1 is not done. It was fixed in structure and reintroduced in vocabulary.

## C1. The two four-turn families are lexically disjoint at every position

The new genuine-failure family (`bench/decisions.py:629`) and the flaky-retry
distractor (`bench/decisions.py:728`) have the same four-turn shape, which is
right. But every string in them is a single fixed literal, and no literal is
shared:

| position | flaky (distractor) | genuine (gold) |
|---|---|---|
| opening text | `Running the test suite to verify the latest change.` | `Let's first try executing the check script directly.` |
| first command | `pytest -v tests/test_parser.py` / `npm run test` | `python check_cache.py` / `node check_lock.js` |
| tool error | `Exit code 1\nAssertionError: test failed (flaky transient error)` | `Exit code 1\nFileNotFoundError: script not found.` |
| second command | identical to the first | different from the first |

Any **one** of the first three rows separates gold from distractor with perfect
accuracy, on a substring match, without reading the reversal text. The distractor's
error message contains the literal word `flaky`. An extractor whose entire logic
is `"flaky transient" not in tool_result` scores 1.00 precision and 1.00 recall on
this family.

This is the same defect as round 1's, moved from position to vocabulary. Round 1
made *"a tool failure is never followed by a reversal"* a perfect rule; round 2
makes *"the error says AssertionError"* a perfect rule. A corpus in which some
single feature perfectly predicts the label is not measuring extraction.

**Required.** One shared pool per position, sampled with the same call in both
families:

- Opening text: one pool of at least 8 forms. Both families draw from it.
- First command: one pool. Both families draw from it.
- Tool error: one pool of at least 6 realistic failures. Both families draw from
  it. No error text may hint at which family it is in — delete `flaky`,
  `transient`, and anything else that names the answer. A `FileNotFoundError` must
  be as likely to precede a retry as a pivot, and an `AssertionError` as likely to
  precede a pivot as a retry.
- Follow-up command: **this is the only systematic difference.** The retry repeats
  the first command byte-for-byte; the reversal runs a different one. That is a
  real, learnable, structural signal and it is the one we want the extractor to
  find.
- Follow-up prose: retries draw from a retry pool, reversals from the reversal
  templates. Both pools must be free of words that only ever appear on one side.
  `without changes` currently appears only on retries; that is a giveaway of the
  same kind, just weaker. Write retry prose that could plausibly precede either.

## C2. Add the check that would have caught C1

The `usage` distribution table in your report is exactly the right instrument, and
it works — it is why B2 is closed. The lexical equivalent does not exist, which is
why C1 got through.

Add a test, in the default suite, that for each of **opening text**, **first
command**, and **tool error content**, collects the set of distinct values
observed on gold four-turn sequences and on distractor four-turn sequences, and
asserts the two sets are equal. Not overlapping — equal. If a string can occur on
one side and not the other, that test fails.

Then extend the report's leak-check section with those three value sets, the same
way you reported the `usage` distributions.

## C3. A round-1 literal survived the surface refresh

`bench/decisions.py:731` still reads
`cmd = "pytest -v tests/test_parser.py" if split == "dev" else "npm run test"`.
That is verbatim round 1. The refresh replaced the contents of the `*_VARS` and
`*_TEMPLATES` lists but not the string literals written inline inside
`generate_session`, and the inline literals are exactly where C1 lives.

Sweep every string literal inside `generate_session` — openings, commands, error
texts, retry prose, tool names, file names — and replace it as round 2's brief
required. Nothing a reviewer read in round 1 should be in the graded corpus.

## C4. Gold reversals are now 80% post-failure; aim for 40–60%

`baseline_naive` matched 150 of 371 gold, and since it only ever flags assistant
blocks, all 150 are reversals. That is 150 of 188, so roughly four in five gold
reversals immediately follow a failed tool result. Round 1 was 0%; this is the
over-correction.

It matters because the reversal-with-no-preceding-failure class is down to ~38
instances. An extractor that handles only the post-failure case gives up 20% of
reversal recall and can still clear a 0.60 recall bar. Retune so that between 40%
and 60% of gold reversals follow a failure.

## Acceptance

1. `baseline_naive` precision on dev in `0.15 <= p <= 0.60`, as before. Report it
   and do not tune the corpus to land it in the band.
2. Add a third baseline, `baseline_leak`, that predicts a reversal on every
   assistant block whose preceding `tool_result` is a failure **and** whose turn
   does not repeat the previous command. State its precision and recall. This is
   the strongest cheap rule we know of; publishing what it scores tells us how
   much headroom a real extractor has to earn. It is allowed to score well — we
   want to know, not to hide it.
3. The C2 test passes and is in the default suite.
4. No string literal inside `generate_session` is one that existed in round 1.
5. `CORPUS_SHA256` updated to the new corpus, in the same commit as the change.
6. 40% ≤ (gold reversals following a failure) / (all gold reversals) ≤ 60%,
   measured and reported.
7. `ruff check .` and `ruff format --check .` clean; whole suite green, README
   test count correct.

## One note on the checksum test

`test_corpus_sha256_checksums` prints the computed hash in its failure message.
That turns a tripwire into a copy-paste prompt: the fastest way to make it pass
is to paste the number it just printed. Drop the value from the message and say
where to recompute it instead. Not blocking, but do it while you are in there.

## Scope and standing constraints

Unchanged from round 2. Only `bench/decisions.py`,
`tests/test_decisions_bench.py`, `docs/reviews/E5-gemini-corpus.md`, and the
README test count. Nothing under `src/` or `docs/tasks/`. No real transcript is
read, sampled, anonymised or adapted. dev and test stay disjoint in seed,
vocabulary and template.
