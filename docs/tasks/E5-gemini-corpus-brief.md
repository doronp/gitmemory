# E5 job A — Gemini builds the labelled corpus and the gate harness

You are the second pair-programmer on `gitmemory`. This epoch has a
pre-registered gate, and you own the half that decides whether it is passed:
the corpus and the scorer. Claude owns the extractor and will not see your
source until the first scored run, exactly as you will not see the extractor's.
That separation is the only reason the number at the end will mean anything.

Read `docs/tasks/E5-brief.md` first. It is the pre-registration, it is already
committed, and nothing in it moves because of what the numbers turn out to be.

**Attestation.** Open your deliverable with a paragraph naming exactly which
files you read and which commands you ran, and stating that you did not read,
list, or reference anything outside this repository — in particular nothing
under `~/.claude/` or `~/memory/`. That constraint is absolute. This product
must never touch its author's own machine history; `tests/test_no_owner_data.py`
enforces it over tracked *and* untracked files and is not to be weakened,
skipped, or worked around. Every session in the corpus is **written by you**.
None is harvested, sampled, anonymised, or adapted from a real transcript.

## What you are building

Two files, and no more than two:

```
bench/decisions.py            generator + gold resolution + scorer
tests/test_decisions_bench.py its tests
```

`bench/` already holds `synth.py`, `score.py`, `arms.py`, `longmemeval.py` —
read them for the house style before writing a line. Do not modify any tracked
file outside those two; in particular do not touch `src/gitmemory/`.

### 1. The generator

`generate(seed: int, n: int) -> list[Case]`, where a `Case` carries a synthetic
Claude Code transcript (the JSONL shape the real adapter parses — see
`src/gitmemory/adapters/claude_code.py` and `docs/DESIGN.md` §2.2) together with
the decisions you planted in it.

Plant against the two labels in `E5-brief.md` — `directive` on a user block,
`reversal` on an assistant block — and record each plant as
**`(line uuid, block index within that turn, kind)`**. You control both of those
when you write the line, and neither depends on anything inside the store.

**The distractors are the actual work.** A corpus of clean, well-signposted
decisions proves nothing; the extractor would clear the bar on a regex and the
gate would be theatre. Every session must be thick with near-misses that a
plausible heuristic flags and the gold does not contain:

- a tool call that fails and is retried *unchanged* — the linter rerun, the
  flaky test, the transient network error;
- a user comparing two options in a question, choosing neither;
- an assistant enumerating alternatives inside a plan it has not committed to;
- a `reversal`-shaped sentence about something outside the work ("we moved off
  Postgres last year"), which is narration, not a decision made here;
- a user directive that is *restated* later — one gold node, not two;
- whatever else you can think of. This list is a floor.

Write down the distractor families you planted and roughly how many of each, in
a short `## Distractors` section of your report. Claude needs to know what the
corpus contains in aggregate; it must not see which specific block is which.

**Two splits, disjoint seeds.** `dev` and `test`, with no session, no sentence
template, and no planted phrasing shared between them. Claude develops against
`dev`. `test` is scored once. If those two are the same corpus with different
seeds, the split buys nothing — vary the surface forms, not just the RNG.

**Size:** each split at least 100 sessions of 20–80 turns, carrying at least 300
gold nodes. A precision figure decided by five items is not a measurement.

**Determinism.** Same seed in, byte-identical corpus out, across processes and
under two different `PYTHONHASHSEED` values. Test it that way — this project has
already shipped one determinism test that compared an object with itself. The
corpus is generated on demand rather than committed; commit a checksum of each
split instead, so drift is loud and the repository does not grow a hundred
fixture files.

### 2. Gold resolution

The gold anchor is a `Block.block_id`, which is content-derived and which you
therefore cannot know while writing the line. Resolve it afterwards, through the
ordinary adapter, with no private path into the store:

```python
session = adapters.get("claude-code").parse(transcript_path)
# turn.uuid -> turn.blocks[i].block_id
```

If a planted `(uuid, block index)` fails to resolve, **raise**. A corpus whose
gold cannot be located is a broken corpus, and silently scoring it as a miss
would charge the extractor for your bug.

### 3. The scorer

`score(predicted, gold) -> dict` implementing exactly the rule in
`E5-brief.md`: strict one-to-one match on `(kind, source_ref)`, micro-averaged
precision and recall over the whole split, no partial credit, no second
prediction consuming an already-matched gold node. An empty prediction set
scores precision 0, not 1. Return the two numbers and the pass/fail against
`P ≥ 0.85, R ≥ 0.60` — and report the verdict whichever way it falls.

### 4. Prove the corpus can fail something

A gate that nothing can fail is not a gate. Ship two reference baselines in the
same module and assert their scores in the tests:

- **`baseline_nothing`** — predicts no decisions at all. Must score P 0, R 0 and
  must fail the gate. This is the metric's own floor.
- **`baseline_naive`** — the heuristic Gemini objected to at E2: flag the first
  assistant block after any failed tool result. Must score **below the bar**,
  and the test should assert that with a margin rather than exactly, so a small
  corpus change does not turn it red.

If `baseline_naive` clears 0.85 precision on your corpus, the corpus is too easy
and the distractors are not doing their job. Say so and fix it rather than
shipping it; that result is a finding about your own work and it is worth more
than a green run.

## What you must not do

- Do not read, infer, or ask about the extractor. If `src/gitmemory/derive.py`
  exists by the time you run, do not open it.
- Do not tune the corpus so that any particular extractor scores well. Tune it
  so that a *wrong* extractor scores badly.
- Do not weaken, skip, or special-case `tests/test_no_owner_data.py`.
- Do not use an LLM at generation time. The corpus is templates and a seeded
  RNG, like `bench/synth.py`; a generated corpus we cannot regenerate byte-exact
  is not evidence of anything.

## Then: review

When both halves land, you review Claude's extractor line by line and Claude
reviews this corpus line by line, on the E4 standard:

- **Verify before reporting.** Reproduce each finding with the smallest script
  you can write; say what you ran and what it printed. **CONFIRMED** only if you
  reproduced it, **PLAUSIBLE** if you reasoned it out, never blurred.
- **Negative control.** Claim code is load-bearing → delete it and show the
  suite notices. Claim a test holds a property → break the property and show
  that test fails.
- Disagreement is the point. The interesting review finds a distractor family
  the corpus is missing, or a way the extractor is reading the gold's shape
  rather than the transcript's.

Job B, after this lands: the 30 hand-authored real-shaped sessions §2.6 asks
for, as a confirmation set that the synthetic corpus is not too easy. Authored
by you, short is fine, never harvested. Do not start it until job A is reviewed.

## Running things

- Python is `.venv/bin/python`, from the repo root — an absolute path here would
  name the author's home directory, which is the one thing this repo may not
  record. There is no bare `python` on this machine and no `timeout` command.
- `.venv/bin/python -m pytest -q` from the repo root: 784 tests, all green.
  `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .`
  must stay clean.
- `bench/` is on the default `testpaths`, so your tests run with everyone's.
  Keep them fast; the `corpus` marker exists for anything that is not.
- No `sudo`, no `crontab`, no network installs.
