# E5 — standalone code review of the gate

One reviewing agent, its own git worktree, no part in writing the scorer. It was
asked for the scoring path only: `bench/gate.py`, `bench/fixture.py`, and the
half of `bench/decisions.py` that produces labels — the code that decides
whether E5 passed, which until now nobody had reviewed.

Baseline before any edit: **844 passed, 1 deselected** (`tests` + `bench`).

Every finding below was **re-verified by a route the reviewer did not use**
before it was accepted. That is the whole discipline of this file: a reviewer
reporting that a scorer is wrong is a claim about the scorer, and the way to
test it is not to re-read the reviewer's test.

## Status

Six findings, five minors. **All fixed**, each pinned by a named test and a
negative control in `tests/mutate_index.py`: **9/9 CAUGHT by their intended
test**. One item recorded and not fixed, at the bottom.

The fixes moved nothing. After them, the test split re-dumped at the same seed
is **byte-identical** to the dump made before them — same `gold.json`, same
slices `{directives: 179, post_failure_reversals: 81, other_reversals: 92}`,
`unslotted {predicted: 0, gold: 0}`. The extractor's target did not shift while
its author was working.

---

## F1 — a prediction could satisfy gold planted in a different transcript

`bench/gate.py`, `score_fixture`.

`block_id` is content-derived: `sha256(turn_id, seq, kind, content_sha256)`, and
`turn_id` is itself derived from the session id and the turn's content. Two
transcripts that share a session id and replay a turn verbatim therefore share
block ids — and that is not hypothetical, it is exactly what a
fork-from-compaction is. The adapter says so itself.

Predictions and gold were pooled flat across the whole fixture, so a prediction
read out of file B satisfied gold planted in file A.

**Verified independently.** Not with the reviewer's hand-written fixture: I
dumped a real split, took the first stem with non-empty gold, wrote its bytes to
two files (`000.jsonl`, `001.jsonl`), labelled only `000`, and ran a
call-counting extractor that returns nothing for the first file and `000`'s gold
for the second. Old scorer: **precision 1.0, recall 1.0** on a run that found
nothing in the only labelled file.

**Fix.** `scoped()` namespaces every key to the file it was read out of. Test:
`test_a_prediction_cannot_satisfy_gold_planted_in_another_transcript`.

## F2 — the failure predicate called every green test run a failure

`bench/gate.py`, `post_failure_blocks`.

The two reversal slices are split on "did a tool result before this fail?", and
the split was a substring test for `"failed"` or `"Exit code 1"`. pytest, jest
and npm all print **`0 failed`** when they pass; pytest's real failure marker is
uppercase **`FAILED`** with **exit code 2**. So on real runner output the
predicate was not merely noisy — it was inverted on the two most common cases,
and the two slices swapped.

**Verified independently.** Three synthetic tool results (`Exit code 0 / 15
passed, 0 failed`, `Exit code 2 / FAILED …`, `Tests: 3 passed, 0 failed`) written
as transcripts and parsed through the real adapter, not through the reviewer's
stub.

**Fix.** `looks_failed()`: the adapter's structural `is_error` flag first, then
the exit code, then word tests that survive a green run — `_COUNTED` refuses a
leading zero, `_FAILED_MARKER` is deliberately **case-sensitive** and kept out of
the `IGNORECASE` group, because matching pytest's marker case-insensitively is
precisely the bug being removed. Both baselines in `bench/decisions.py` now call
it, so there is one predicate rather than two that drift.
Test: `test_a_green_test_run_is_not_a_failure_and_a_red_one_is`.

## F3 — gold could name any block of the right turn

`bench/decisions.py`, `resolve_gold_for_case`.

The fixture is two halves that have to agree: transcript bytes, and labels
pointing into them. The resolver looked up the planted turn and took a block
from it. An off-by-one in the block index still produced a `block_id` that
existed, so the label was plausible, resolvable, and wrong — and nothing
downstream could tell, because everything downstream only asks whether the ref
resolves.

**Verified independently.** The `turn.blocks[0]` mutant applied in the main tree,
not the reviewer's: **844 green**. Nothing in the suite held the label to the
text it was supposed to name.

**Fix.** `Case.planted_decisions` carries the exact planted text as a fourth
field, and the resolver refuses a block whose text is not that text. This is the
structurally important fix of the six: it makes a disagreement between the two
halves loud inside the generator.
Test: `test_gold_must_name_the_block_that_holds_the_planted_text`.

## F4 — predictions outside the slice vocabulary vanished

`bench/gate.py`, `score_with_slices`.

A `kind` the breakdown does not know lands in no bucket and was silently
dropped. Fed two correct predictions and eight junk ones, the report showed
three perfect slices under an overall gate of 0.20, with nothing saying that
eight of ten predictions were not shown.

**Verified independently** by feeding eight `kind="decision"` predictions
straight to `score_with_slices` and reading the printed report.

**Fix.** An `unslotted` count, printed when non-zero.
Test: `test_predictions_the_slices_cannot_hold_are_reported`.

## F5 — an unlabelled transcript was scored past in silence

`bench/gate.py`, `score_fixture`.

Iteration was over `gold.json`, so a `.jsonl` with no entry was never parsed and
never scored. A dump half-written by an interrupted run would have scored
against the half that landed and reported a clean number for it.

**Fix.** `sessions/` and `gold.json` must name the same set, and the error says
which side is short.
Test: `test_a_transcript_with_no_label_is_refused_rather_than_skipped`.

## F6 — a decision that is not a pair came back as nonsense

`bench/gate.py`, everywhere a decision was unpacked.

A `str` is a sequence of two characters when it has two characters, and a dict
is subscriptable. An extractor returning either got either nonsense pairs or a
`KeyError` raised three frames from the code that caused it.

**Fix.** `_pair()` accepts an object with `.kind`/`.source_ref` — the extractor
under test defines its own type and is not obliged to import ours — or a real
pair, and otherwise names what it was given.
Test: `test_a_decision_that_is_neither_a_pair_nor_an_object_is_named`.

## Minors, all fixed

- **An empty slice printed as a slice that scored zero.** `P 0.0000 R 0.0000`
  read identically for "nothing to find here" and "found nothing"; the slices
  are very different sizes. Now `no gold in this slice`.
  Test: `test_a_slice_with_no_gold_does_not_read_as_a_failed_one`.
- **`except ImportError` around `from gitmemory.derive import decisions`** caught
  any import failure *inside* derive — a missing numpy, a typo in a dependency —
  and reported it as "no extractor written yet", swallowing the cause. Now the
  module is imported and asked for the attribute.
  Test: `test_a_derive_that_fails_to_import_is_not_reported_as_unwritten`.
- **Two `NamedTemporaryFile` leaks** in `bench/decisions.py` — the file was
  created outside the `try`, so a failure between creation and cleanup left it
  behind. Fixed in both places.
- **`test_disjoint_pools_equality` was deleted, not repaired.** It partitioned
  the template pools on "did the command change?" rather than on "is this gold?",
  so gold values landed on both sides and set-equality could never see a leak.
  `test_general_leak_check` is a strict superset. A test that passes for a reason
  other than the one it is named for is worse than no test — the comment left in
  its place says so.
- **A stray file in the dump.** Not a defect found in the code, but the only
  catcher for `test_the_gold_labels_carry_no_hint_of_how_the_corpus_was_made`'s
  directory assertion was the third-field check, which three other tests also
  catch. A `meta.json` mutant now pins the line that matters.

## Negative controls

`.venv/bin/python tests/mutate_index.py "credited to gold" "green test run"
"outside every slice" "gold may name" "unlabelled transcript" "two-character
pair" "empty slice" "inside derive" "stray file"` → **9/9 caught by their
intended test**. A fix nothing distinguishes is a fix nothing is holding in
place.

## Recorded, not fixed

- `tests/test_decisions_bench.py` builds a subprocess `PYTHONPATH` that omits
  `src`, and the subprocess works anyway because the editable install is a plain
  `.pth` on the default path. It is a latent dependency on how the package
  happens to be installed, not a live defect; noted here so the next person who
  sees it does not have to re-derive why it passes.
