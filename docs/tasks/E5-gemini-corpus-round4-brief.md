# E5 corpus — round 4

Round 3 closed C1, C2, C3 and C4. Verified independently here: the three shared
pools are shared, the post-failure ratio is 54.79% on dev and 40.23% on test, the
baselines reproduce to four decimal places, and the checksum test no longer
prints the hash it wants pasted back. Do not revisit any of that.

Two leaks of the C1 class survive. Both are in positions C2 does not look at,
which is the real finding: the check was written against a list of places we had
already thought of.

## D1. The tool_use id spells the label

`bench/decisions.py:798` names the gold family's second tool call
`tu_{session_idx}_{t_idx}_reversal`. `bench/decisions.py:897` names the
distractor's `tu_{session_idx}_{t_idx}_retry`. The following `tool_result` echoes
the same id in `tool_use_id`.

The adapter keeps the original record on `Block.native`, and `run_gate` hands the
extractor a `Session`, so this is directly readable:

```
kind=tool_use     native  {"id": "tu_0_13_retry", "input": {...}, "name": "bash", ...}
kind=tool_result  native  {"content": "...", "tool_use_id": "tu_0_13_retry", ...}
```

`"_reversal" in block.native["id"]` is a 1.00/1.00 extractor on this family.

**Required.** Ids must be drawn from one scheme that carries no family
information — the same format string in both branches, differing only in the
indices. Nothing in an id, a tool name, a file name or a uuid may encode which
family produced it.

## D2. The success message differs by family

The fourth turn closes the gold family with `Exit code 0\nRan 12 tests. All
passed successfully.` and the distractor with `Exit code 0\nAll local checks
completed successfully.` — one fixed literal each, on both splits. A substring
test on either one separates the families perfectly without reading a word of
the reversal text.

**Required.** One shared success pool per split, at least 6 forms, sampled with
the same call in both families, exactly as C1 required for the error pool. No
success text may hint at which family it is in.

## D3. Replace the positional leak check with a general one

`test_disjoint_pools_equality` checks opening text, first command and tool error.
It passes, and D1 and D2 went straight past it, because a check over three named
positions cannot see a fourth.

**Required.** Write the general form and keep it in the default suite:

For every four-turn sequence, walk the **whole** `native` record of all four
turns and collect every string leaf it contains — every value at every depth,
including ids, tool names, `tool_use_id`, and every element of every list.
Normalise away the parts that are legitimately unique per session: replace runs
of digits with a placeholder, and do the same for uuids. Build the resulting
multiset of normalised string leaves for gold sequences and for distractor
sequences, and assert **the two sets of distinct values are equal**.

Then anything that can occur on one side and not the other fails the test, in any
position, including positions nobody has thought of yet. Keep the three-position
test too if you like — it is a faster, more legible failure when it is the one
that trips — but the general one is the gate.

Report the sizes of both sets, the way you report the `usage` distributions.

## D4. Two smaller things, while you are in there

1. `test_baseline_leak_on_corpus` asserts `precision == 1.0`. The brief asked for
   that number to be **reported**, not enforced. As written, a future change that
   makes the strongest cheap rule imperfect — an improvement — fails the suite.
   Assert a loose floor and put the exact figure in the report.
2. `baseline_naive` precision on the **test** split is 0.1423, under the 0.15 the
   band names for dev. The band was scoped to dev so this is not a breach, but it
   belongs in the report rather than being left to be found.

## Acceptance

1. D3's general leak test passes and is in the default suite, and it **fails**
   against the round-3 corpus. Show that: stash your D1/D2 fixes, run it, paste
   the failure, restore them. A leak test that has never failed is a claim.
2. D1 and D2 fixed. No identifier, tool name, file name or success message
   differs systematically between the two four-turn families.
3. `baseline_naive` precision on dev still inside `0.15 <= p <= 0.60`. Report dev
   and test. Do not tune the corpus to land it.
4. `baseline_leak` precision and recall reported on both splits.
5. Post-failure share of gold reversals still inside 40–60% on both splits, and
   reported.
6. `CORPUS_SHA256` updated in the same change.
7. `ruff check .` and `ruff format --check .` clean; whole suite green; README
   test count correct.

## Scope and standing constraints

Unchanged. Only `bench/decisions.py`, `tests/test_decisions_bench.py`,
`docs/reviews/E5-gemini-corpus.md`, and the README test count. Nothing under
`src/` and nothing under `docs/tasks/`. No real transcript is read, sampled,
anonymised or adapted. dev and test stay disjoint in seed, vocabulary and
template. Do not commit; leave the changes in the working tree.
