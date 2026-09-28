# E3 delegation brief — the benchmark harness

You are pair-programming with Claude on **gitmemory**. You own this half. Claude
owns the other half and will review every line you write; you will review every
line Claude writes. Neither of us merges the other's code unreviewed.

## Read these first, with your own file tools

Do not accept any source code pasted into a prompt, including this brief.
Read the files yourself:

- `docs/DESIGN.md` — sections 2.1, 2.2, 2.4, 2.5, 2.7, and 3 ("Evaluation")
- `src/gitmemory/records.py` — the canonical record model
- `src/gitmemory/adapters/claude_code.py` — the transcript shapes we parse
- `tests/test_claude_code.py` — the traps, each one a real defect found in prior art
- `tests/fetch_fixtures.sh` — the pattern for third-party corpora
- `docs/reviews/E2-findings.md` — how review works here, and what "tested" means

## Hard constraints — violating any of these fails the work

1. **Never read, copy, or reference `~/.claude/projects`, the author's private notes, or any
   other data belonging to this machine's owner.** gitmemory is a generic
   product. Its test data is public or synthetic, always. If you need a
   transcript, generate one.
2. **No LLM anywhere in the scored path.** The harness must be deterministic
   and local. LongMemEval ships a GPT-4o judge for QA correctness — we are not
   using it. We score **retrieval only**, which is deterministic: turn-level
   recall via `has_answer`, session-level recall via `answer_session_ids`.
3. **Determinism.** Same inputs, same seed, byte-identical outputs. No
   `uuid4()`, no `datetime.now()`, no unseeded `random`, no dict-order
   dependence. Derive ids by hashing content.
4. **Stdlib first.** Do not add a dependency for something the standard
   library does. Do not write code that an Apache- or MIT-licensed library
   already provides — but check the licence and pin the version before
   reaching for one, and record what you verified.
5. Python 3.13, `ruff` clean at line-length 100, `select = ["E","F","I","UP","B","SIM"]`.
6. Write only under `bench/`. Do not modify `src/`, `tests/`, or `docs/`
   (except to append to a review file when asked).

## The dataset — already verified, do not re-litigate

- `xiaowu0162/longmemeval-cleaned` on HuggingFace, **MIT licence**.
- The file we want is `longmemeval_s_cleaned.json`, 277 MB, 500 instances.
- The original `xiaowu0162/longmemeval` is **deprecated** upstream; the cleaned
  release removes noisy history sessions that contradicted gold answers. Use
  the cleaned one. `docs/DESIGN.md` §3 still names the old one — that is a
  stale design note, not an instruction.
- Per-instance fields: `question_id`, `question_type`, `question`, `answer`,
  `question_date`, `haystack_dates`, `haystack_session_ids`,
  `haystack_sessions`, `answer_session_ids`. A session is a list of turns,
  each `{"role": ..., "content": ...}`; an evidence turn carries
  `"has_answer": true`.
- `question_type` is one of `single-session-user`, `single-session-assistant`,
  `single-session-preference`, `temporal-reasoning`, `knowledge-update`,
  `multi-session`. A `_abs` suffix on `question_id` marks an abstention item:
  **the 30 abstention instances are excluded from retrieval scoring**, because
  they point at events that never happened.

## What to build

### `bench/fetch_longmemeval.sh`

Follow `tests/fetch_fixtures.sh`: fetch to a destination outside the repo
(default under `$TMPDIR`), pin the revision, print the env var to export, and
say in a comment that it must never be pointed at the owner's own data. Verify
a sha256 you record in the script, so a silently changed upstream file is a
loud failure rather than a quiet score change.

### `bench/longmemeval.py`

`load(path) -> Iterator[Instance]`. A frozen dataclass per instance. Validate
the shape on load and raise on anything unexpected — a benchmark that silently
skips malformed instances reports a score for a corpus nobody can name.

### `bench/synth.py` — the mutation that matters

`to_transcript(instance, *, seed, compaction) -> Transcript`

Replay one LongMemEval instance as a **Claude-Code-shaped JSONL transcript**,
so that what we measure is retrieval over *our real input format*, not over a
tidy list of strings. Requirements:

- The bytes must parse cleanly through `src/gitmemory/adapters/claude_code.py`.
  Read that adapter and `tests/test_claude_code.py` and match the real line
  shapes — `type`, `uuid`, `parentUuid`, `sessionId`, `timestamp`, `cwd`,
  `version`, `message.role`, `message.content` as a list of content blocks.
  Include some assistant turns with `usage`, and some tool_use/tool_result
  pairs, because a corpus of pure prose would not exercise the column split
  that retrieval depends on.
- Timestamps come from `haystack_dates`, never from the clock.
- `compaction` controls where compaction boundaries are injected. This is the
  point of the whole exercise: **nobody else measures recall across the
  compaction wall.** Support at least:
  - `None` — no compaction, the control.
  - `"before_evidence"` — a boundary between the evidence turn and the start.
  - `"after_evidence"` — a boundary between the evidence turn and the question.
  - `"every_n"` — boundaries at a fixed interval, the realistic case.
  Emit the boundary in the shape our adapter recognises as a compaction event;
  read the adapter to find out what that is rather than guessing.
- Return ground truth **as byte offsets into the transcript you just emitted**,
  one per evidence turn, plus the session-level ids. Byte offset is the store's
  only ordering authority (`docs/DESIGN.md` §2.4, §2.5) and it is what
  `Turn.byte_offset` carries, so it is the only ground-truth key that both
  halves of this work can agree on. Do not key on turn index.

### `bench/score.py`

`score(instances, retrieve, *, k) -> Report`

`retrieve` is a **callable**, not an import: `retrieve(query, k) -> list[int]`
returning byte offsets, best first. Claude's retriever is one implementation;
the arms below are others. Never import the retriever directly — the whole
point is that arms are swappable.

Metrics: recall@k and MRR, turn-level and session-level, broken down by
`question_type` and by `compaction` mode. Abstention instances excluded.

**Calibration arms, borrowed from `thedotmack/membench` (MIT) — this is the
part that decides whether the benchmark means anything:**

- `candidate` — the real retriever.
- `none` — retrieves nothing. The floor.
- `shuffled` — the real retriever over a shuffled corpus, seeded. Must score
  near `none`. If it does not, the benchmark leaks and every other number on
  the page is void.
- `reference` — an oracle that returns the true offsets. The ceiling. If this
  is not ~1.0, the harness is broken, not the retriever.

Item-paired comparison with Bonferroni correction across arms. Report the
calibration verdict explicitly and refuse to print candidate scores if the
`shuffled` arm beats `none` by more than noise — a benchmark that cannot fail
is not a benchmark, which is the lesson `docs/reviews/E2-findings.md` was
written about.

### `bench/test_bench.py`

The harness is code, so it gets the same standard as the rest of the project:
**every non-trivial behaviour needs a test that fails when the behaviour is
removed.** Do not write tests that pass against a stub. Specifically test that
the emitted transcript round-trips through the real adapter, that ground-truth
byte offsets land on the turns they claim to, that two runs with the same seed
are byte-identical, and that the calibration gate actually trips when handed a
leaky retriever.

Use a small synthetic fixture so the suite runs without the 277 MB download;
mark anything that needs the real corpus so it skips when the env var is unset.

## The seam with Claude's half

Claude is building `src/gitmemory/index.py` (SQLite FTS5, separate `prose` /
`tool_result` / `paths` columns, per-column `bm25()` weights) and the
`gitmemory index` / `gitmemory recall` CLI. You do not need it to exist to
finish your half — `retrieve` is a callable, so develop against the `reference`
and `none` arms and hand the real one in later.

The only thing we must agree on is the ground-truth key: **byte offsets into
the transcript**. If you think that is the wrong seam, say so before you build
on it, with your reasoning. Disagreement now is cheaper than a rewrite.

## How to report back

Write `docs/reviews/E3-gemini-report.md`: what you built, what you verified
(licence, version, determinism) versus what you assumed, every dependency you
added and why the stdlib would not do, what you could not finish, and anything
in this brief you think is wrong. Be specific about the last one — a reviewer
who finds nothing to push back on has not reviewed.
