# E5 — the decision extractor

Write `decisions(session)` in `src/gitmemory/derive.py`: the function that reads
a parsed transcript and returns the decisions in it.

You are being given this task **because you have not read the corpus
generator**, and the harness you get is built to keep it that way. Do not go
looking for it. Details under "Why your worktree is missing things".

## The contract

```python
@dataclass(frozen=True, slots=True)
class Decision:
    kind: str  # "directive" | "reversal"
    source_ref: str  # a Block.block_id belonging to this session


def decisions(session: Session) -> list[Decision]: ...
```

- **directive** — the user states a constraint or an instruction that governs
  later work. "Use uv, not pip." "Never touch the vendored files." It is a
  standing rule, not a request to do one thing now.
- **reversal** — the assistant abandons an approach in favour of another.
  "I'll drop the regex and parse it properly." The distinguishing feature is
  that something previously in play is being given up.

**No node without a `source_ref`.** The id must be the `block_id` of a block
that is actually in the session handed to you. A decision graph whose nodes
cannot be traced to committed bytes is fiction, and that rule is the only thing
standing between this feature and fiction. `Block.block_id` is content-derived;
read it off the block, never reconstruct it.

Return them in the order they occur. One block yields at most one decision.

## What "right" means here

Two things, and the second is the one that usually gets lost:

1. **It scores.** The pre-registered bar is precision ≥ 0.85 and recall ≥ 0.60,
   declared before anything was measured and not negotiable afterwards. You will
   develop against a dev fixture. The bar is judged on a held-out split you will
   never see.
2. **It generalises.** The dev fixture is synthetic. Rules that key on its exact
   phrasings will score well there and be worthless on a real transcript. Key on
   linguistic shape — the "X instead of Y" frame, the stop/start pair, the
   modal + negation of a standing constraint — not on the specific verbs the
   fixture happens to use. If you find yourself adding a literal because it
   appears in the fixture, that is the moment to generalise it instead.

Judge yourself on both. A submission that hits 0.90 on dev by memorising it is a
failure I will find, because the held-out split has a disjoint vocabulary.

## Constraints

- **Deterministic and local. No LLM, no network, no model download.** This
  project's design says, in as many words, that a 4B-class model hallucinating
  rationale that was never in the transcript is the worst possible failure mode
  for a decision record. Regex, string work, and the structure of the record.
- **Standard library only.** `derive.py` already imports `sumy` for the ideas
  path; do not add a dependency for this one.
- Follow the file you are editing. Read `src/gitmemory/derive.py` and
  `src/gitmemory/records.py` first and match them: the naming, the comment
  density, the habit of writing down what a shortcut's ceiling is. A deliberate
  simplification gets a `ponytail:` comment naming the ceiling and the upgrade
  path.
- Do not touch anything under `raw/`, the store, the index, or the daemon. This
  is one function and its tests.

## Tests

In `tests/test_derive.py`, in the style already there — a docstring per test
saying what would break if the behaviour went away, not a restatement of the
assertion.

Write your session fixtures **by hand**. Do not copy from the dev fixture: a
test whose input came from the thing you are tuning against tests the tuning.
Cover at minimum: a directive, a reversal, a block that is neither, a retry of a
failed command (which is *not* a reversal — repeating something is not
abandoning it), an empty session, and a session where the same sentence appears
twice and must yield two distinct `source_ref`s.

Every test must fail if you break the behaviour it names. Check that by breaking
it on purpose before you move on.

## The harness you have

```
.venv/bin/python -m bench.gate bench/fixture-dev
```

It prints precision, recall, matched, predicted, gold, and the same figures per
slice — directives, post-failure reversals, other reversals. Run it as often as
you like. It imports `gitmemory.derive.decisions` directly, so there is nothing
to wire up.

Read the per-slice lines, not just the headline. The three populations are very
different sizes and very different difficulties, and a micro-average lets a
good slice carry a bad one.

## Why your worktree is missing things

`bench/decisions.py`, `bench/fixture.py` and `tests/test_decisions_bench.py`
are not here. They are the corpus generator: they spell out every template and
every vocabulary the fixture is built from, and an extractor written with them
open is fitted to a generator rather than to the task. `bench/fixture-dev/` is
a frozen dump instead — 100 sessions as `.jsonl`, plus `gold.json`, which is
`{stem: [[kind, source_ref], ...]}` and nothing more. Everything you need to
iterate; nothing you need to cheat with.

The held-out split is not in your worktree either, and `bench/gate.py` cannot
reach it — it reads a dump and has no idea a generator exists. Do not
reconstruct either one, do not look for them in git history, and do not read
anything outside this worktree. If you think you need something that is
missing, say so in your report and stop; do not work around it.

## Report

Open with the attestation: the files you read, the commands you ran, and the
statement that you read nothing outside this worktree — in particular nothing
under `~/.claude/` or `~/memory/`.

Then:

1. The dev score, and the per-slice breakdown.
2. Every rule you implemented, one line each, and the linguistic shape it keys
   on rather than the strings it matches.
3. What you expect to be your weakest point on a vocabulary you have not seen,
   and why.
4. Anything you deliberately did not do, and the ceiling you left behind.

`ruff check .` and `ruff format --check .` clean, whole suite green, README test
count correct. Do not commit; leave the changes in the working tree.
