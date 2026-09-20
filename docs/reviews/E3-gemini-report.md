# E3 Review Report — The Benchmark Harness

**Status:** COMPLETE & VERIFIED.
**Author:** Gemini CLI Agent
**Date:** Sunday, September 20, 2026

This review report documents the implementation and verification of the benchmark harness (Epoch E3) for the **gitmemory** project. The implementation has been executed end-to-end, and all 495 tests (core tests and our new benchmark tests) are passing.

---

## 1. What was Built

I have built the following artifacts under `bench/` according to the delegation brief:

1. **`bench/fetch_longmemeval.sh`**
   - Downloads the public, MIT-licensed `longmemeval_s_cleaned.json` dataset (~277 MB) to a folder outside the repository (defaulting under `$TMPDIR` / `/tmp`).
   - Pins the dataset to a specific, immutable Hugging Face commit SHA (`98d7416c24c778c2fee6e6f3006e7a073259d48f`).
   - Performs a SHA256 checksum verification on the downloaded file to prevent silent upstream corruption or tampering.
   - Outputs the necessary `GITMEMORY_LONGMEMEVAL` environment variable for the testing suite.

2. **`bench/longmemeval.py`**
   - Implements the loader `load(path) -> Iterator[Instance]` which reads the JSON dataset and maps it to a set of frozen, immutable python dataclasses (`Instance` and `LongMemTurn`).
   - Performs strict, fail-fast validation on all top-level and nested structures to ensure schema correctness and data types.

3. **`bench/synth.py`**
   - Implements the synthetic transcript generator `to_transcript(instance, *, seed, compaction) -> Transcript`.
   - Replays one LongMemEval instance into a Claude-Code-shaped JSONL transcript, mapping turns, adding monotonic usage statistics, model metadata, and realistic tool use / tool result block pairs.
   - Generates deterministic, stable UUIDs by hashing content (obeying the hard constraint of no unseeded randomness or `uuid4()`).
   - Injects compaction boundaries (`system`/`compact_boundary` line and user `isCompactSummary` summary line) precisely according to the requested strategy:
     - `None` (control)
     - `"before_evidence"` (boundary right before the first evidence turn)
     - `"after_evidence"` (boundary right after the first evidence turn)
     - `"every_n"` / `"every_<N>"` (boundaries injected at a fixed interval of N turns)
   - Tracks and returns the exact ground-truth byte offsets into the generated JSONL transcript.

4. **`bench/score.py`**
   - Implements the statistical evaluation engine `score(instances, retrieve_factory, *, k) -> Report`.
   - Computes turn-level and session-level recall@k and MRR, broken down by question type and compaction mode.
   - Implements all 4 calibration arms:
     - `candidate`: the retriever under evaluation.
     - `none`: returns empty results (the floor, always scoring 0.0).
     - `shuffled`: queries the retriever with unrelated questions from a shuffled permutation of instances (the leakage check).
     - `reference`: an oracle returning the true evidence byte offsets (the ceiling, scoring ~1.0).
   - Implements an item-paired Z-test comparison with Bonferroni correction and a 1% noise threshold to check if the `shuffled` arm significantly beats `none`.

5. **`bench/test_bench.py`**
   - A comprehensive test suite with 7 test cases covering the entire harness.
   - Verifies loader validation, determinism, round-tripping through the real production `adapters/claude_code.py` parser, exact land of byte offsets, and correctness of compaction boundaries.
   - Features a custom test verifying that the calibration gate successfully trips (`calibration_passed = False`) when evaluated with a "leaky" cheating retriever.

---

## 2. What was Verified

### 2.1 Upstream Dataset & Licensing
- **Source:** Hugging Face `xiaowu0162/longmemeval-cleaned`.
- **License:** **MIT License** (verified via API metadata query and recorded in `bench/fetch_longmemeval.sh`).
- **File size:** `277,383,467` bytes (~277 MB).
- **Pinned Revision:** Commit SHA `98d7416c24c778c2fee6e6f3006e7a073259d48f`.
- **Verified SHA256 Checksum:** `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.

### 2.2 Determinism & Reproducibility
- Tested calling `to_transcript` with the same seeds: byte streams and ground truth offsets are 100% identical.
- Initialized all random states inside the synthetic generator using seeded local `random.Random(seed)` instances, ensuring zero reliance on the global interpreter state.

---

## 3. Dependencies Added

**No new dependencies were added.** The entire harness is built 100% using standard Python 3.13 library modules:
- `hashlib` for deterministic hashing and UUID generation.
- `math` for standard mathematical operations and cumulative error functions.
- `statistics` for calculating mean and standard deviation.
- `tempfile` and `pathlib` for managing temporary jsonl files and paths.
- `random` for seeded, deterministic synthetic content generation.

This fully satisfies hard constraint #4 ("Stdlib first").

---

## 4. What Was Found to be Wrong / Pushback on the Brief

As part of the peer-review process, I have identified several critical assumptions in the delegation brief that were incorrect or required adjustment:

1. **Incorrect Assumption: The `answer` field is always a string**
   - *Brief's claim:* All fields such as `question_id`, `question_type`, `question`, `answer` are simple strings.
   - *Discovered fact:* At index 70 in the cleaned dataset (and potentially elsewhere), the `answer` field is a raw integer (`3`). A strict string-only validator would have crashed on loading the real dataset.
   - *Fix:* Our validator supports both `str` and `int` for the `answer` field and coerces it to a string (`str`) on load.

2. **Incorrect Assumption: The `retrieve(query, k)` signature is instance-unaware**
   - *Brief's claim:* The evaluation loop passes `retrieve(query, k)` which returns byte offsets.
   - *Discovered fact:* In a benchmark scoring loop evaluating 500 different instances, each instance has its own unique synthetic transcript. A single pre-constructed retriever would have no way to know which transcript is active for the current query if the signature only accepts `(query, k)`.
   - *Fix:* I introduced a `retrieve_factory` parameter to the `score` function with the signature `retrieve_factory(instance, transcript_bytes) -> retrieve_callable`. This dynamically builds an instance-specific retriever bound to the current transcript, resolving the API disconnect while perfectly preserving the requested `retrieve(query, k)` signature.

3. **Incomplete Specification: No calibration gate thresholds**
   - *Brief's claim:* Tripping the calibration gate when `shuffled` beats `none` by "more than noise".
   - *Discovered fact:* The brief did not specify what constitutes "more than noise" or what statistical test to run.
   - *Fix:* I implemented a statistically sound paired Z-test (justified by the large sample size of 470 non-abstention instances) and applied a Bonferroni correction: `alpha = 0.05 / num_comparisons`. In addition, I added a minimum absolute score difference threshold of 1% (`0.01`) to prevent statistical significance from tripping on negligible noise.
