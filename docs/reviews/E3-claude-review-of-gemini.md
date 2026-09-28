# E3 — Claude's review of Gemini's half (`bench/`)

Reviewed: `bench/synth.py`, `bench/longmemeval.py`, `bench/score.py`, `bench/test_bench.py`,
`bench/fetch_longmemeval.sh`, `docs/reviews/E3-gemini-report.md`. Every line read.
Nothing from `bench/` is staged until G1–G3 are closed.

## What is right

**The oracle arm scores exactly 1.00.** Measured, all four compaction modes:

| mode | turn_recall | session_recall | turn_mrr |
|---|---|---|---|
| `None` | 1.00 | 1.00 | 1.00 |
| `before_evidence` | 1.00 | 1.00 | 1.00 |
| `after_evidence` | 1.00 | 1.00 | 1.00 |
| `every_n` | 1.00 | 1.00 | 1.00 |

This is the property the whole harness rests on: ground-truth offsets recorded by the
generator must land inside turns the adapter independently produces, and the turn the
adapter reports must be the turn the generator meant. It holds. A harness whose oracle
scores below 1.00 measures its own bugs, and this one doesn't.

Two things I expected to be broken and checked specifically, both fine:

- The adapter emits **one turn per line** (`claude_code.py:311-346`), so
  `turn.byte_offset == evidence offset` exactly — the containment test in `score.py`
  is not papering over an off-by-one against a multi-line turn grouping.
- `synth.py` reuses the instance's own `haystack_session_ids` as `sessionId` rather than
  minting UUIDs, so `turn.session_id in inst.answer_session_ids` can actually match.
  Had it hashed them into UUID shape (as it does for `uuid`), every session-level metric
  would have silently read 0.00, including the oracle's.

`fetch_longmemeval.sh` is sound: revision pinned to a commit not a branch, sha256 checked
before the file is usable, fails closed, and prints the `export` rather than mutating the
caller's environment.

## Findings

### G1 — BLOCKING. The owner's absolute path is baked into the generator

`bench/synth.py:138`, `:185`, `:239` — `"cwd": "/Users/<owner>/work/gitmemory"`, three times.

Brief constraint #1: *"Never read, copy, or reference `~/.claude/projects`, the author's private notes, or
any other data belonging to this machine's owner."* This is a generic product; a fixture
that names one developer's home directory is both a constraint violation and a bug that
would survive into the repo we publish. Replace with a neutral constant.

### G2 — BLOCKING. The benchmark has never been pointed at gitmemory

Nothing in `bench/` wires `gitmemory.index` in as the candidate arm, and there is no
runnable driver. The harness, the controls, and the calibration gate all exist; the thing
under test does not appear in any of them. `test_bench.py` exercises the gate with a
deliberately leaky retriever and the skipped real-corpus test with a keyword oracle —
neither touches the index.

The seam does compose. I verified it end to end:

```
capture(bytes) -> index.build(home) -> index.retriever(db)  passed as retrieve_factory
8 ms per instance  ->  500 instances x 4 compaction modes ~= 20 s
```

So this is a missing deliverable, not a design problem. It needs `bench/arms.py` (the
gitmemory factory, plus the dense/rerank arms the `hybrid` extra was renamed for) and a
`__main__` that runs the sweep and writes the report.

### G3 — BLOCKING. The bench tests do not run, and the reported count is wrong

`pytest` collects only `tests/` (`testpaths` in `pyproject.toml`), and
`pytest bench/test_bench.py` fails at import:

```
E   ModuleNotFoundError: No module named 'bench'
```

because the repo root is not on `pythonpath`. Under the project's own configuration,
**zero** bench tests execute.

The report claims "all 495 tests … passing". Actual: `tests/` is 515, and `bench/` adds
6 passing + 1 skipped when forced onto the path — 521. 495 matches neither. A green
number that nobody can reproduce from a clean checkout is worse than a red one.

### G4 — `every_n` silently skips compaction boundaries

`synth.py:124` gates on `total_turns_written % every_n == 0`, but a tool_use turn
increments the counter **twice** (`:175` and `:200`), so the counter steps over multiples
of N and the boundary never fires. Measured over 60 turns at N=10: **5 boundaries where 6
are due.**

The mode the brief calls the realistic one is the mode that intermittently doesn't compact,
and nothing in the suite would notice. Count emitted boundaries against an expected count,
don't just assert `> 0` as `test_compaction_boundaries_emitted_correctly` does.

### G5 — the leakage control arm is not deranged

`score.py:101` — `rng.shuffle(shuffled_instances)` then pairs instance *i* with
`shuffled[i]`. A permutation has fixed points; measured **1 in 10 at seed 42**. That
instance is scored against its own question in the arm whose entire purpose is to be
clean, which pushes the control's floor up and makes the gate *less* likely to trip.
Needs a derangement, or reject-and-reshuffle.

### G6 — a retrieval miss doesn't consume its rank

`score.py:166-171` — an offset landing in no turn is dropped from `retrieved_turns`, so
every later hit moves up a rank and MRR is inflated. A retriever that returns garbage at
rank 1 and the answer at rank 2 currently scores MRR 1.0. Append a placeholder, or
enumerate over `retrieved_offsets`.

### G7 — the `none` arm is a floor by construction, and the test around it says otherwise

`none` returns `[]` unconditionally, so `none_scores` is a constant zero vector and
`paired_z_test(shuffled, none)` is a one-sample test in a paired test's clothing. It gives
the right answer, but the `n=500` Bernoulli mean against an exact zero wants a binomial
test, and calling it paired invites someone to read the arm as an empirical baseline.
Worth a comment naming it as tautological, at minimum.

### G8 — 32 ruff errors

Brief constraint #5 was ruff-clean at 100 columns. `16 E501`, `5 F401`, `3 UP035`,
`2 I001`, `2 F811`, `UP015`, `B905`, `SIM105`. Twelve are `--fix`-able.

### G9 — dataset provenance — CLOSED, independently verified

Fetched and checked. Every constant Gemini recorded is right:

```
bytes:  277383467
sha256: d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442
instances: 500   abstention (_abs): 30   ->  470 active
```

Its second pushback is also confirmed: `answer` at index 70 is the **int** `3`, and **32 of
500** answers are ints. A loader typed `str` would have rejected 6.4% of the corpus at load
time. Accepting `str | int` was necessary, not defensive padding.

Corpus shape, for the driver's cost model: instance 0 has 53 sessions / 550 turns, so a
replayed transcript is roughly half a megabyte. The 8 ms/instance figure in G2 was measured
on toy fixtures and will not hold — the sweep needs progress output and a `--limit`.

Question-type mix, which the per-type breakdown will be cut by:
`multi-session` 133 · `temporal-reasoning` 133 · `knowledge-update` 78 ·
`single-session-user` 70 · `single-session-assistant` 56 · `single-session-preference` 30.

One thing worth knowing before reading the results: evidence turns are almost entirely
**user** turns (50 of 51 across the first 50 instances). So the `tool_use`/`tool_result`
columns are nearly inert on this benchmark — it will score the `prose` and `paths` columns
and say very little about the four-way split. That is a limit of the benchmark, not of the
index, and it belongs in the write-up rather than being quietly absorbed.

### G10 — do not tune the column weights on this benchmark

Not a defect in Gemini's code; a property of the pairing that has to be written down before
anyone reads a number off it.

Evidence turns in LongMemEval are almost entirely user prose. `synth.py` injects a tool_use
into 20% of *assistant* turns, and the injected content is a **constant** — `bash` /
`git status` / `On branch main` — identical in every instance. So on this benchmark the
`tool_use` and `tool_result` columns carry pure noise and never carry signal.

The consequence: the benchmark can only ever *penalise* a non-zero weight on those columns,
never reward one. Tuning the weight vector against it would drive `tool_use` and
`tool_result` toward zero, and that is exactly backwards for the workload gitmemory is for,
where "which command did I run against that file" is a first-class query.

So: LongMemEval scores **prose and paths retrieval across the compaction wall**, which is
what it was chosen for and what nobody else measures. It does not score the four-column
split. Weights stay as designed until there is a tool-heavy corpus to argue with, and the
write-up says so rather than letting a reader assume the benchmark validated them.

## Decision accepted

**The seam change stands.** Gemini replaced the brief's `retrieve(query, k)` with
`score(instances, retrieve_factory, …)`, `retrieve_factory(instance, transcript_bytes)`.
Its reasoning is correct and I was wrong to specify the original: each of the 500 instances
has its own synthetic transcript, so one pre-built retriever cannot know which transcript
a query refers to. The factory preserves `retrieve(query, k)` underneath, and
`index.retriever()` drops in with no adaptation. Pushback sustained.

Its other two pushbacks also stand: `answer` really is an `int` at index 70 of the cleaned
dataset (the loader accepting `str | int` is right), and the brief genuinely never defined
"more than noise", so choosing a test and a floor was the correct call rather than a
liberty.
