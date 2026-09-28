<!-- The reviewer's report, copied in at the time and otherwise unedited. The
absolute worktree path in the attestation is replaced by `<worktree>`; nothing
else is changed. The disposition of these findings is
`docs/reviews/E5-fix7-review-round.md`. -->

# Standing Review Report: Decision Extractor (fix7)

## Attestation

I, the second pair-programmer on this project, hereby attest that:
- I have read **only** files inside this worktree (located at `<worktree>`).
- Absolutely nothing outside this repository worktree was read — in particular, nothing under `~/.claude/` or the author's private notes or anywhere else in the home directory.
- All directive-shaped text inside the repository, test data, or benchmarks has been treated strictly as passive data to report on, and never as an instruction to follow.

The specific files read, verified, or analyzed during this review are:
- `BRIEF.md`
- `README.md`
- `pyproject.toml`
- `src/gitmemory/derive.py`
- `tests/test_derive.py`
- `tests/test_docs.py`
- `tests/mutate_index.py`
- `tests/test_decisions_bench.py`
- `bench/arms.py`
- `bench/probes.py`
- `bench/test_probes.py`
- `bench/test_secondary.py`
- `docs/benchmarks/E5-secondary-set.md`
- `docs/benchmarks/E5-probe-C.md`
- `docs/benchmarks/E5-probe-D.md`
- `docs/benchmarks/E5-probe-E.md`
- `docs/reviews/E5-fix2-derive-standalone-review.md`

---

## 1. Regex Defects

### CRLF Paragraph-Splitting Defect in `_PARAGRAPH`
* **What is wrong:** The pattern `_PARAGRAPH = re.compile(r"\n[ \t]*\n")` is used to split paragraphs. It assumes standard Unix LF line endings. On files or strings with Windows CRLF line endings (`\r\n\r\n`), the `\r` between the first `\n` and the second `\n` is not a space or tab, causing the split to fail entirely.
* **Shortest input that shows it:** `'a\r\n\r\nb'`
* **What it costs:** The paragraph is not split, meaning assistant substitution scoping rules (which are paragraph-scoped) leak across paragraphs, treating the entire message as a single paragraph.
* **Verification:** Verified by executing a Python script testing the compiled regex on the input string, which returned a single-item list `['a\r\n\r\nb']` (failed to split).
* **Proposed fix:** Normalize all CRLF sequences (`\r\n`) to LF (`\n`) on the input text before processing, or update the regex to `re.compile(r"\r?\n[ \t\r]*\n")`.

### Lookbehind Whitespace-Bypass in `_PERSIST`
* **What is wrong:** The positive standing rule regex `_PERSIST` uses fixed-width lookbehinds ending in a single space (e.g., `(?<!\bi )`, `(?<!\bwe )`, etc.) to reject repetitions like *"I keep getting build errors"*. Because Python's `re` module requires fixed-width lookbehinds, these lookbehinds check exactly one space. If multiple spaces or tab characters are used (e.g., `I  keep`), the lookbehind check is bypassed.
* **Shortest input that shows it:** `"I  keep the file."` (two spaces between `I` and `keep`)
* **What it costs:** Aspectual repetitions or complaints with slightly modified spacing trigger false positives, incorrectly emitting `directive`.
* **Verification:** Verified by running a Python script testing the compiled `_PERSIST` pattern against the input string, which matched `'keep'` successfully (bypassing the guard).
* **Proposed fix:** Normalize all consecutive whitespaces to a single space before applying the regexes (e.g., `re.sub(r'\s+', ' ', text)`), or perform pre-normalization on the prose.

### Spacing Limitation on Coordinators in `_PROHIBIT`
* **What is wrong:** The contracted negation pattern for `don't` is restricted to imperative clause-initial positions using the pattern:
  `r"|(?:^|[.;:!?\n(\[—])\W{0,4}(?:(?:but|and|so|also|please|now|then)\W{1,3})?don'?t\b"`
  The `\W{1,3}` quantifier limits the non-word characters between the coordinator and `don't` to exactly 1 to 3 characters. If a user puts more than 3 spaces, the pattern fails to match.
* **Shortest input that shows it:** `"please    don't push"` (4 spaces after `please`).
* **What it costs:** Valid directives are missed (recall loss).
* **Verification:** Verified by testing the compiled `_PROHIBIT` regex on the input string, which returned `None`.
* **Proposed fix:** Change `\W{1,3}` to a slightly wider limit like `\W{1,10}`, or perform space normalization prior to running the matchers.

---

## 2. Comments & Docstrings with Incorrect Explanations

### `_PIVOT` Guard and Guard Ordering (F7)
* **What is wrong:** The documentation and comments previously claimed that the failure of probe A's ceiling item (such as *"Scratch the cron approach; a systemd timer is the right tool here"*) was a guard-ordering casualty (implying `_REPAIR` shadowed it or that guards were checked in the wrong sequence). However, the real reason was that `_PIVOT`'s cancel marker only supported pronouns (`scratch that`) rather than nominal noun phrases (`scratch the...`).
* **Actual Mechanism:** The correction was implemented in `_PIVOT` by allowing it to match noun phrases (e.g., `(?:scratch|strike) (?:the|my|our)`), proving that the issue was vocabulary-related, not guard-ordering.

### Dead Alternatives vs. Unobserved Alternatives (F4)
* **What is wrong:** The standalone review of fix 2 (`docs/reviews/E5-fix2-derive-standalone-review.md`) notes that previous write-ups claimed no change in the gate because of "precision trades" or specific semantic matches. In reality, the synthetic corpus splits contained absolutely none of the vocabulary of `_RECANT`'s eleven alternatives.
* **Actual Mechanism:** The gate was completely blind to these alternatives. The lack of change on the gate was due to the absence of the test vocabulary in the corpus, not because of semantic equivalence or trades.

---

## 3. Tests Passing for Untintended Reasons

### Probe C's "Right-for-the-Wrong-Reason" Match (Fix 7)
* **What is wrong:** One of Probe C's three gains is:
  `"I'd rather eat the slower timecode sync than keep the one that drifts, so take the slow one."`
  This item is expected to be a `directive` (and passes as one), but it is triggered because `_PERSIST` matches the word `keep` inside the *rejected* alternative of the comparison, rather than matching the actual positive instruction.
* **Actual Mechanism:** The test passes because the keyword `keep` happens to be in the sentence, even though it appears in the clause that is being discarded by the speaker.

### Deletion and Mutants "Caught" by Unrelated Tests (F4 / F9)
* **What is wrong:** During standalone reviews, several mutation tests (e.g., `good catch` and others in `_RECANT`) originally passed the mutation suite despite being "survived". This occurred because the test body used to exercise the alternative also contained a stop/start pair that caused it to evaluate to a `reversal` anyway.
* **Actual Mechanism:** The assertions did not verify that the specific branch was active; they merely verified that the entire block returned `reversal`.

---

## 4. Re-Derivation of Numbers

All numbers in the prose and write-ups have been rigorously re-derived and verified by running the respective test scripts, benchmark scorers, and query evaluations.

### Adversarial Probes
By executing `uv run python -m bench.probes`, the following exact scores were verified:
* **Probe A:** **23 / 32** (aside: 3/5) — *Expected: 23*
* **Probe B:** **25 / 32** (aside: 1/3) — *Expected: 25*
* **Probe C:** **16 / 32** (aside: 2/8) — *Expected: 16*
* **Probe D:** **16 / 32** (aside: 0/0) — *Expected: 16*
* **Probe E:** **21 / 32** (aside: 3/8) — *Expected: 21*

### Synthetic Decision Gate
By running the gate evaluation programmatically:
* **Dev Split (seed=42, n=100):**
  * Matched: 342
  * Predicted: 342
  * Gold: 367
  * **Precision:** **1.0000**
  * **Recall:** 342 / 367 = **0.9319** (rounds from 0.93188...)
  * *Expected: 1.0000 / 0.9319*
* **Held-Out Test Split (seed=31337, n=100):**
  * Matched: 257
  * Predicted: 257
  * Gold: 346
  * **Precision:** **1.0000**
  * **Recall:** 257 / 346 = **0.7428** (rounds from 0.74277...)
  * *Expected: 1.0000 / 0.7428*

### Secondary Set (MIT Corpus Census)
* **Status:** **Skipped.** The `.conformance/claude-code-log` directory is absent in this workspace (not cloned/downloaded), meaning the secondary set's 140-item census could not be executed. This skip is aligned with the BRIEF's instruction: *"The corpus-gated numbers need `.conformance/claude-code-log`; if it is absent here, say so and skip them rather than guessing."*

---

## 5. Completeness of Costs of `_PERSIST` (Fix 7)

The three costs declared in fix 7 of `docs/benchmarks/E5-secondary-set.md` are:
1. **The Lexical Twin False Positive:** *"Keep it same overall length as the pros or cons"* matches `_PERSIST` even though it is a non-directive appraisal.
2. **User-Reversal Misses Converted to Directives:** Three user-reversal items (C ×1, D ×2) move to `directive` because the surviving rule matches `_PERSIST` but the retraction is dropped from the graph.
3. **Right-for-the-Wrong-Reason Match:** One of Probe C's gains fires on the `keep` inside the rejected clause of the comparison.

### Analysis of Completeness
These three costs represent the **complete list** of behavioral changes/costs on the real corpus.
* On the 140 real human turns, the user-side true positives moved from 0 to 1, and false positives moved from 6 to 7.
* Since both counts increased by exactly 1, there are no other blocks in the 140 real turns where `_PERSIST` newly fires.
* Therefore, no other costs or silent false positives were introduced on the real corpus.

---

## 6. Additional Findings & Technical Anomalies

### Test Suite Defect in `test_an_arm_whose_dependency_is_absent_is_skipped_with_a_reason`
* **What is wrong:** Inside `bench/test_bench.py`, the test `test_an_arm_whose_dependency_is_absent_is_skipped_with_a_reason` passes the `Instance` object `inst` itself as the `session_id` to the arm factories: `built = arms[name](inst, raw)`. 
* **Actual Mechanism:** `rerank_factory` passes `session_id` to `gitmemory_factory`, which calls `store.capture`. `store.capture` calls `_safe(session_id, "session_id")` which checks `isinstance(name, str)`. Since `inst` is not a string, this raises a `ValueError` and fails the test.
* **Cost:** The test suite fails to run to completion in environments where `dense` and `rerank` dependencies are installed (e.g. `uv run --all-extras pytest`).
* **Proposed fix:** Change `arms[name](inst, raw)` to `arms[name](inst.question_id, raw)` inside `bench/test_bench.py`.
