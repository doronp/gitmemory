# E5 corpus — round 2

Round 1 is reviewed. Read `docs/reviews/E5-claude-review-of-gemini-corpus.md`
in full before changing anything; it cites file and line for every point.

Fix **B1 through B8**. B1 is the one that decides whether the gate means
anything — do it first and let the rest follow from the corpus it produces.
N1 is mine to write up, not yours. Fix N2 and N3.

## Also: every template and filler is replaced

Not a defect in your work — a consequence of reviewing it. The reviewer read all
732 lines, including every template and every filler word, and the reviewer is
also the author of the extractor this corpus is built to grade. That breaks the
blind the gate depends on, and it is recorded in `docs/tasks/E5-brief.md`.

So: **regenerate `DEV_VARS`, `TEST_VARS`, and all four `*_TEMPLATES` sets with
fresh surface forms.** Same shapes, same families, same disjointness rules
between splits — different words and different sentences. None of the strings
currently in the file survives into the graded corpus. Keep the structure; throw
away the prose.

## Scope

Change only these files:

- `bench/decisions.py`
- `tests/test_decisions_bench.py`
- `docs/reviews/E5-gemini-corpus.md`
- `README.md` — the test count on the Status line, and nothing else.

Do not touch `docs/tasks/`, `uv.lock`, or anything under `src/`. The suite's one
failing test right now is the README count, which is yours to correct at the end.
Everything else is green; if something under `src/` or `tests/` other than your
own file fails, that is a signal you changed something you should not have.

## Acceptance

1. `baseline_naive` precision on **dev** lands in `0.15 <= p <= 0.60`. Not 0.00.
   If it lands outside that band, do not tune it by hand until it fits and do not
   move the band — report the number and say what shape of the corpus produced
   it. A band met by fiddling is not evidence of anything.
2. `baseline_nothing` stays at 0.00 / 0.00 and fails the gate.
3. No non-text field distinguishes gold from distractor. Before you claim this,
   check it: for each of `usage.input_tokens`, `usage.output_tokens`, `model`,
   block count per turn, and inter-turn timestamp delta, compute the
   distribution over gold turns and over non-gold turns and show they overlap.
   Put the numbers in the report.
4. `CORPUS_SHA256` committed for both splits, with a test that recomputes it.
5. Neither new corpus test carries `@pytest.mark.corpus`. `pytest -q` from the
   repo root runs all of them.
6. ~15–20% of sessions in each split have **zero** gold nodes; total gold per
   split stays ≥ 300.
7. Some gold directives sit at block index ≥ 1 in a multi-block user turn.
8. A test that `resolve_gold_for_case` raises on an unresolvable plant.
9. `run_gate(extractor, seed, split, n)` in `bench/`, taking
   `Callable[[Session], list[Decision]]`. The extractor sees the `Session` and
   nothing else — not the `Case`, not `planted_decisions`.
10. `ruff check .` and `ruff format --check .` clean. Whole suite green.

## Report

Update `docs/reviews/E5-gemini-corpus.md` in place. Keep the attestation
format — the files you read and the commands you ran, and the statement that you
read nothing outside this repository, in particular nothing under `~/.claude/`
or `~/memory/`.

Report **measured** occurrence counts per distractor family over a generated
split, not the `rng.random()` thresholds from the source. Report the leak-check
distributions from acceptance item 3.

Drop the closing verdict sentence. State what you ran and what it showed.

## Standing constraints

Everything in the round-1 brief still holds. In particular: no real transcript
is read, sampled, anonymised or adapted; every session comes from templates and
a seeded RNG; dev and test stay disjoint in seed, vocabulary and template.
