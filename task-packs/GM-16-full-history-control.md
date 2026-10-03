# Task pack GM-16: full-history no-retrieval control arm on LongMemEval-S

Repo: `~/work/gitmemory` (branch main, clean). Issue: doronp/gitmemory #16.
This repo has `graphify-out/` — query it first (`graphify query`) before grep.
Docs under `docs/` follow `docs/AGENTS.md` if present.

## Why

Honcho's post reports Gemini 3 Pro alone (full history, no memory) at 92.0 vs our
published 94.2 (`rerank12` arm) — plausibly within sampling error on n=500. The
control arm answers what retrieval actually adds: same reader, same judge, full
untruncated history. Pre-registered interpretation (from the issue): within noise →
retrieval saves tokens but adds no measurable accuracy; clearly below → clean
measurement of retrieval's contribution. **Publish either way.**

## Harness map (already scouted — verify, then build)

- `bench/qa.py` — the reader/judge pipeline; this is the file to change.
  - Injection point: `lme_one()` at bench/qa.py:321-336; retrieval at L328-329 feeds
    `ranked_sessions(...)[:k]`; `lme_history(inst, sids)` renders context at L331-332.
  - Arm choices come from `peer.ARMS` (bench/peer.py:44) imported at bench/qa.py:49.
  - Reader/judge prompts sha256-pinned in `bench/test_qa.py:18-27` — DO NOT touch
    prompt text; the pin test must stay green.
- `bench/peer.py` — separate retrieval-metric scorer; must not break
  (`getattr(arms, f"{name}_factory")` at peer.py:123 crashes on factory-less names).

## Build (minimal seam)

1. In `bench/qa.py` define a QA-specific arm tuple: `QA_ARMS = ARMS + ("full_history",)`
   and use it for `--arm` choices. Do NOT add a factory-less name to `peer.ARMS`.
2. In `lme_one`: for `arm == "full_history"`, set
   `sids = list(inst.haystack_session_ids)` (lme_history date-sorts them), skip the
   `to_transcript`/`cc.parse`/`_retrieve` path entirely, and bypass the `[:k]`
   truncation. Everything downstream (reader, judge, record, summarise) untouched.
3. Records/header must clearly carry the arm name (existing `--json` + header line
   already do — verify).
4. Add a size log per instance (history token/byte estimate) — prompts average
   ~550 KB raw JSON each; useful for the report. No hard failure on size.
5. Tests: extend `bench/test_qa.py` — assert the arm passes ALL session ids to
   `lme_history` in date order and never calls `_retrieve`. Run the bench test suite.

## Run protocol (cost discipline — Vertex bills real money)

- Auth: gcloud ADC (already valid). **Set the billable project to
  `spending-tokens-for-devetc` (credit-funded), NOT the current gcloud default
  `cust2-agpoc-1790968535` (PoC project, $20 ceiling).** Check how `bench/qa.py`'s
  `Vertex` class resolves the project and override accordingly.
- Reader and judge: keep defaults `gemini-3.1-pro-preview`, temperature 0
  (comparability with the 94.2 run depends on identical reader/judge).
- Step 1 (smoke): run `--split dev` (or a 20-instance subset if dev is large) and
  report: per-instance token estimate, wall time, projected full-run cost.
- Step 2 (full): only after smoke looks sane, run all 500:
  `python -m bench.qa --bench lme --dataset "$GITMEMORY_LONGMEMEVAL" --arm full_history --split all --json results-full-history.json`
  The sha256 cache (`~/.cache/gitmemory-qa/cache.jsonl`) makes reruns/resume free.
  If the dataset isn't fetched, use `bench/fetch_longmemeval.sh` (sha256-pinned).
- Failures are scored wrong and counted — report the failure count explicitly.

## Publish (per issue: either way)

- Update `docs/benchmarks/E9-peer-protocols.md` Table 3 with the new arm's accuracy,
  and close the "Net saving versus no memory — UNMEASURED" gap in
  `docs/RESULTS.md` (L214 area) with the interpretation (note n=500 sampling error;
  keep Honcho's 92.0 no-memory baseline distinct from the 92.6 peer-table figure).
- Comment on issue #16 with the result and close it if the maintainer flow allows;
  if unsure, leave the issue open and note the comment in your report.

## Report

Write `docs/benchmarks/E16-full-history-control.md` (or the naming convention you
find): setup, smoke numbers, full-run accuracy vs 94.2, failure count, cost/wall
time, interpretation per the pre-registered read. Commit code + docs on a branch
`bench/full-history-control` (do NOT commit the 277MB dataset or results JSON >
5MB). No force-push, don't touch main directly.
