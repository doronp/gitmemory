# E3 round 2 — Gemini

Two jobs. Do them in order. Working directory is the repo root.

Before anything else, read these, in this order:

1. `docs/reviews/E3-claude-review-of-gemini.md` — my review of your half. Findings G1–G9.
2. `docs/tasks/E3-bench-brief.md` — the original brief. Its six hard constraints still bind.
3. `docs/reviews/E2-findings.md` — how we record a review round.

The six hard constraints, restated because G1 broke the first one:

1. Never read, copy, or reference `~/.claude/projects`, `~/memory`, or any other data
   belonging to this machine's owner. This includes their absolute paths in fixtures.
2. No LLM anywhere in the scored path.
3. Deterministic: same seed, byte-identical output.
4. Stdlib first. A new dependency needs a verified licence and a pinned version.
5. Python 3.13, ruff clean at 100 columns.
6. Job 1 writes only under `bench/` and `pyproject.toml`. Job 2 writes only
   `docs/reviews/E3-gemini-review-of-claude.md`.

---

## Job 1 — fix your half

Close G1 through G8. G9 needs nothing.

**G1** is the blocking one: `bench/synth.py:138`, `:185`, `:239` hardcode
`"cwd": "/Users/<owner>/work/gitmemory"`. Use one module-level constant with a neutral
value. Nothing under `bench/` may name this machine's owner.

**G2** is the missing deliverable — the benchmark has never been pointed at gitmemory.
Add `bench/arms.py` exposing a factory per arm, matching the
`retrieve_factory(instance, transcript_bytes) -> retrieve(query, k) -> list[int]` seam you
designed:

- `gitmemory_factory` — the candidate. I verified this composes and it is fast (8 ms per
  instance, so the full 500 × 4 sweep is about 20 seconds):

  write `transcript_bytes` to a file under a fresh temp home, then
  `store.capture(src, "claude-code", session_id, home=home)`,
  `index.build(home)`, `index.open_db(index.db_path(home))`, `index.retriever(db)`.
  Clean up the temp home when the instance is done — 2000 of them will not fit on disk
  otherwise. Close the sqlite connection too.

- the dense and rerank arms, from the `hybrid` extra in `pyproject.toml`
  (`model2vec`, `numpy`, `flashrank`). These are what BM25 is being scored *against* —
  that comparison is the entire point of E3. If an arm's dependency is not installed, that
  arm must skip cleanly with a message, never crash the sweep and never silently score 0.

Then add a `bench/__main__.py` that runs the sweep and writes a report: per arm, per
compaction mode, per question type — recall@k, MRR — plus the calibration verdict. It
reads the dataset path from `GITMEMORY_LONGMEMEVAL`, and exits non-zero if calibration
fails. Deterministic output; no timestamps in the report body.

**G3** — make `pytest` from a clean checkout collect and pass the bench tests.
`testpaths` and `pythonpath` in `pyproject.toml` are the levers. Report the real total.

**G4** — `every_n` steps over multiples of N because a tool_use turn increments
`total_turns_written` twice. Fix it, and replace the `> 0` assertion in
`test_compaction_boundaries_emitted_correctly` with an exact expected count, so this
cannot regress unnoticed.

**G5** — derange the shuffle so no instance is paired with its own question. Test it.

**G6** — an offset that lands in no turn must still consume its rank. Test it: a retriever
that returns one garbage offset then the true one must score MRR 0.5, not 1.0.

**G7** — say in a comment that the `none` arm is a floor by construction, and that the
comparison against it is therefore one-sample.

**G8** — `uv run ruff check bench/` must be clean.

Every fix in G4, G5, G6 needs a test that fails before it and passes after. Do not
report Job 1 done until `uv run pytest -q` and `uv run ruff check .` are both clean from
the repo root with no extra flags, and say the real test count.

---

## Job 2 — review my half, every line

I wrote the retrieval index. Read these yourself — do not ask for them to be pasted:

- `src/gitmemory/index.py` — the whole file
- `src/gitmemory/store.py` — the `Stored`, `sessions`, `_text`, `_int`, `_seq`,
  `segment_groups` region (lines 110–150 and 485–555)
- `src/gitmemory/__main__.py` — the `index` and `recall` subcommands
- `tests/test_index.py` — the whole file
- `tests/mutate_index.py` — the mutation set
- `docs/DESIGN.md` §2.7 and §3 for what I was supposed to build

Review it the way I reviewed yours: adversarially, for real defects, with a reproduction
for anything you claim. Things worth your attention specifically —

- I built **four** FTS5 text columns where DESIGN §2.7 specifies three. I added `tool_use`
  as its own column on the argument that tool arguments are short and high-signal and get
  buried if they share a column with bulk tool output. Is that reasoning right, and is the
  weight vector (prose 4.0, tool_use 2.0, tool_result 1.0, paths 3.0) defensible or
  arbitrary?
- `store.sessions()` deliberately keeps a row whose *metadata* is garbage, and only drops
  one whose segment list is unreadable or escapes the store root. The reasoning is that
  dropping on bad metadata lets a hostile manifest opt its segments out of the egress
  gate's seam scan. Check that reasoning and check the implementation matches it.
- Query handling: `\w+` tokenisation, every term quoted, terms OR'd, capped at 64 with a
  warning. Can anything a user types reach the FTS5 operator parser? Can anything make the
  index return a hit it should not, or miss one it should return?
- The `WITH scored AS MATERIALIZED (…)` in `search()`. `MATERIALIZED` is load-bearing —
  without it SQLite flattens the CTE and `bm25()` fails at runtime. Is there a case where
  it silently returns *wrong* scores rather than failing?
- Tie-breaking is `score, session_key, byte_offset, block_seq` — never rowid. Is the
  result genuinely independent of indexing order?
- `build()` is temp-then-rename. Is the failure path actually safe, and is the temp file
  always cleaned up?
- The mutation set in `tests/mutate_index.py`: 14 mutants, all caught by their intended
  test. **What behaviour did I fail to write a mutant for?** That is the most useful thing
  you can tell me.

Write your findings to `docs/reviews/E3-gemini-review-of-claude.md`. Number them C1, C2, …
For each: file and line, what is wrong, a concrete reproduction, and severity. If you
think something is wrong but cannot reproduce it, say so and label it unverified — a
confident wrong finding costs more than a hedged right one. If a design decision is
defensible but you would have made it differently, say that too, separately from defects.

Do not change any file outside `bench/`, `pyproject.toml`, and your review document.

---

Open your reply with one line confirming you read the files yourself from disk rather than
receiving their contents in this prompt.
