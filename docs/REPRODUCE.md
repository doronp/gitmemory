# Reproducing the published results

This page is for a stranger with a clone who wants to re-derive every number
in the [README's Results section](../README.md#results) and
[RESULTS.md](RESULTS.md). The gate reports in [`benchmarks/`](benchmarks/)
remain the source of truth for what each number means; this page says how to
make it appear on your own screen, and how closely it should match.

Results fall into three kinds, and they reproduce differently:

| Kind | Examples | What to expect |
|---|---|---|
| Deterministic, no network after setup | E3, E8, E9 retrieval, the decision gate, probes, the secondary set, the suite counts | The same digits, to the fourth decimal place |
| Model-judged | E9 QA accuracy | A number near the published one, never guaranteed equal |
| Machine-dependent | Hook latency | The same shape (shim versus `true`), different milliseconds |

Every command below is written from the repository root. A command marked
**(not run while writing this guide)** needs a paid API or a download that was
not made while the guide was written. Every other command was run, and its
output checked against the published figure.

## Setup

Python 3.13+, git, and [uv](https://docs.astral.sh/uv/). One environment covers
every result on this page:

```sh
git clone https://github.com/doronp/gitmemory && cd gitmemory
uv sync --locked --extra dev --extra derive --extra hybrid
```

`dev` is the test suite, `derive` is what CI installs beside it, and `hybrid`
brings the `dense`, `rerank`, `rerank12` and `hybrid` bench arms (model2vec,
numpy, flashrank). The shipped BM25 arm needs no extra: SQLite FTS5 ships with
CPython. `serve` is not needed for anything here; without it, the dashboard
tests are collected and skipped, so the test count does not change.

### Third-party data

Nothing on this page reads your own agent history, and no benchmark dataset is
vendored. Each is fetched from its publisher at a pinned revision, checked
against a pinned sha256, and refused if the digest differs.

| Data | Fetch | Lands in | Exported variable | Needed for |
|---|---|---|---|---|
| LongMemEval-S cleaned, `longmemeval_s_cleaned.json` (~277 MB) | `sh bench/fetch_longmemeval.sh` | `${TMPDIR:-/tmp}/gitmemory-longmemeval/` | `GITMEMORY_LONGMEMEVAL` | E3, E8, E9 LongMemEval rows |
| LoCoMo, `locomo10.json` | `sh bench/fetch_locomo.sh` | `${TMPDIR:-/tmp}/gitmemory-locomo/` | `GITMEMORY_LOCOMO` | E9 LoCoMo rows |
| claude-code-log, pi and oh-my-pi fixtures (git clones) | `sh tests/fetch_fixtures.sh` | `.conformance/` in the repo, gitignored | `GITMEMORY_CC_FIXTURES`, `GITMEMORY_PI_FIXTURES` | The secondary set, the 328 conformance cases |

Each script takes an optional destination directory as its first argument and
prints the `export` line to paste. A script finding its file already present
checks the digest and makes no network call. Both dataset scripts default to a
directory outside the repository; `.conformance/` is inside it and ignored by
git.

```sh
eval "$(sh bench/fetch_longmemeval.sh)"
eval "$(sh bench/fetch_locomo.sh)"
sh tests/fetch_fixtures.sh
```

The two dataset scripts were run while writing this guide against files that
were already cached, so only their digest check was exercised; the download
branch was not.

### Models the bench arms download

The first run of an optional arm downloads its model, and later runs read the
cache:

| Arm | Model | Cache |
|---|---|---|
| `dense`, `hybrid` | `minishlab/potion-base-8M` (model2vec) | the Hugging Face hub cache |
| `rerank` | `ms-marco-TinyBERT-L-2-v2` (flashrank) | `${XDG_CACHE_HOME:-~/.cache}/gitmemory-bench/` |
| `rerank12` | `ms-marco-MiniLM-L-12-v2` (flashrank) | the same |

With the models cached, `HF_HUB_OFFLINE=1` keeps a run entirely offline. The
retrieval runs checked for this guide were made that way.

## 1. Recovering evidence behind the compaction boundary (E3)

**Measures:** turn recall at k = 10 over 470 LongMemEval instances rendered as
Claude Code transcripts, under four compaction modes, behind 14 calibration
checks. Report: [E3](benchmarks/E3-longmemeval.md).

**Needs:** `GITMEMORY_LONGMEMEVAL`. The `dense` and `rerank` rows also need the
`hybrid` extra; without it the run says which arms it skipped and why, and
scores the rest.

```sh
uv run python -m bench --dataset "$GITMEMORY_LONGMEMEVAL"
```

`--k 10` and `--seed 42` are the defaults, and all four compaction modes run
unless `--compaction` narrows them. The 500 instances load as 470 scored ones.

**Expected output.** A calibration table with 14 rows, all `yes`, then
`Calibration passed.`, then the scores. If calibration fails, the scores are
withheld and the command exits 1. The published figures sit in the
`after_evidence` rows:

| Arm | Turn recall | Turn MRR |
|---|---|---|
| `candidate` (shipped BM25) | 0.7456 | 0.6209 |
| `live_context` | 0.0000 | 0.0000 |
| `dense` | 0.8215 | 0.6627 |
| `rerank` | 0.8295 | 0.7012 |

The opposite arrangement is the `before_evidence` / `live_context` row:
**0.7893**.

**Tolerance:** deterministic. Run for this guide, every one of the report's
196 table rows came out identical to four decimal places. The current harness
prints two more columns than the E3 report shows, `S-Hit@10` and `S-All@10`,
which were added for E8 over the same retrieval; the other columns are
unchanged.

**Runtime:** the report records 8m37s on one laptop core, from the end of the
dataset load to the report. The run for this guide took about 20 minutes of
wall time on a machine that was busy with other benchmarks, so treat 8m37s as
a quiet-machine figure. Nothing is cached between runs.

## 2. Against other memory systems, under their own protocols (E9)

Report: [E9](benchmarks/E9-peer-protocols.md). The gitmemory column in the
README is the `rerank12` arm; the "Shipped BM25 alone" column is `candidate`.
Dev and test halves are fixed forever by the sha256 parity of the question or
conversation id, so `--split dev` and `--split test` select the same items on
every machine.

### 2a. LongMemEval-S session R@5, R@10 and all-evidence@10 (MemPalace protocol)

**Measures:** whether at least one answer session (R@k, `recall_any`) or every
answer session (all@k, `recall_all`) appears among the top k sessions, over all
500 questions. Turns are retrieved 200 deep and collapsed to sessions in rank
order.

**Needs:** `GITMEMORY_LONGMEMEVAL`, the `hybrid` extra for every arm but
`candidate`.

```sh
uv run python -m bench.peer --protocol mempalace \
    --arms candidate,rerank,rerank12,hybrid --k 1,5,10
```

`--dataset` defaults to `$GITMEMORY_LONGMEMEVAL`. The command in the E9 report
omits it for that reason.

**Expected:**

| Arm | recall_any@5 | recall_any@10 | recall_all@10 | NDCG@10 |
|---|---|---|---|---|
| `rerank12` | 0.992 | 0.998 | 0.960 | 0.961 |
| `rerank` | 0.980 | 0.994 | 0.958 | 0.940 |
| `hybrid` | 0.974 | 0.988 | 0.946 | 0.923 |
| `candidate` | 0.9580 | 0.9720 | 0.8640 | 0.8963 |

The `candidate` row was run for this guide (`--arms candidate --k 5,10`, 37
seconds) and matches the report's 95.8 / 97.2 / 86.4 exactly. The E9 table
gives the `candidate` NDCG@10 as 0.896, which is the 0.8963 above. The
`rerank12`, `rerank` and `hybrid` rows are the report's figures, rounded as the
report rounds them; see [what was checked](#what-was-checked-while-writing-this-guide)
for how far their run got.

**Tolerance:** deterministic for `candidate`. The cross-encoder arms run ONNX
models that are fetched by name rather than by digest, so a changed upstream
model file could move their last digit; none was observed.

**Runtime:** not recorded. The cross-encoder arms score 200 turns per question
and are far slower than `candidate`.

### 2b. The official LongMemEval protocol

Printed in E9 beside Table 1, with no ranking, because nobody else publishes
under it: abstention questions skipped, and answer sessions with no user-side
answer turn skipped, which leaves n = 419.

```sh
uv run python -m bench.peer --protocol official \
    --arms candidate,rerank,rerank12,hybrid --k 5,10
```

**Expected:** `rerank12` any@5 0.9833, any@10 0.9881, all@10 0.9666, NDCG@10
0.9475; `candidate` 0.9451, 0.9618, 0.8735, 0.8531. Deterministic, as above.
**Runtime:** not recorded.

### 2c. LoCoMo session R@10 (MemPalace protocol)

**Measures:** whether the session of a question's first evidence id is among
the top 10 sessions, over all 1,986 questions, category 5 included.

**Needs:** `GITMEMORY_LOCOMO`, the `hybrid` extra for the reranking arms.

```sh
uv run python -m bench.locomo --protocol mempalace \
    --arms candidate,rerank,rerank12 --k 5,10
```

**Expected:** the `S-Recall@10` column reads 0.9131 for `rerank12`, 0.8869 for
`rerank` and 0.8648 for `candidate` (the arm prints as `gitmemory`).
`S-Recall@5` for `rerank12` is 0.8770. The held-out half is
`--split test` (n = 1,127): `rerank12` 91.84 / 88.51, `candidate` 88.36 / 82.61
at R@10 / R@5.

The `candidate` row was run for this guide (2.4 seconds) and read 0.8648 at
R@10, matching the report. **Tolerance:** deterministic. **Runtime:** not
recorded for the reranking arms.

### 2d. LongMemEval-S QA accuracy (reader + judge)

**Measures:** end-to-end answer accuracy. `rerank12` retrieves the top 10
sessions, a reader model answers from them with LongMemEval's own prompt, and a
judge model grades the answer with LongMemEval's own per-type judge prompts.

**Needs:** `GITMEMORY_LONGMEMEVAL`, the `hybrid` extra, and Vertex AI access to
`gemini-3.1-pro-preview`. `bench/qa.py` reads **no environment variable** for
credentials. It shells out to the `gcloud` CLI, which must be on `PATH`:

- `gcloud config get-value project` supplies the Google Cloud project, and
- `gcloud auth application-default print-access-token` supplies the bearer
  token, which is fetched again on a 401.

So before a run: `gcloud auth application-default login`, then
`gcloud config set project <your-project>`, on a project with the Vertex AI
API enabled and access to the model. Requests go to
`https://aiplatform.googleapis.com/v1/projects/<project>/locations/global/publishers/google/models/<model>:generateContent`
at temperature 0. `--reader` and `--judge` change the model names; both default
to `gemini-3.1-pro-preview`.

```sh
uv run python -m bench.qa --bench lme --dataset "$GITMEMORY_LONGMEMEVAL" \
    --arm rerank12 --k 10 --split all
```

**(not run while writing this guide)** beyond `--limit 0`, which loads the
data, builds no client and prints an empty table. `--arm rerank12` and
`--k 10` are also the defaults. The published 94.2 is the `rerank12` row of
Table 3; `rerank` read 93.60.

**Expected:** overall accuracy near **0.9420** over n = 500, with the per-type
figures in E9 (knowledge-update 98.6, multi-session 88.4, and so on). The dev
choices in E9 are `--split dev` (n = 247) with `--k 20` and `--cot` as the
alternatives that lost.

**Tolerance:** model-dependent; see
[what cannot be reproduced exactly](#what-cannot-be-reproduced-exactly-and-why).
A call that fails all six retries scores its question wrong and the run prints
how many did; the published run had 1 of 500.

**Cost and runtime:** not recorded. Every response is cached in a JSONL file
keyed by sha256 of (model, prompt, options), by default
`gitmemory-qa/cache.jsonl` under `$XDG_CACHE_HOME` or `~/.cache` (`--cache`
moves it), so a rerun or a resume makes no new calls.

### 2e. LoCoMo categories 1–4 QA accuracy

**Measures:** as 2d, over LoCoMo's 1,540 category 1–4 questions, with Mem0's
`ANSWER_PROMPT` as reader and `ACCURACY_PROMPT` as judge in JSON mode. This row
used the `rerank` arm, not `rerank12`.

**Needs:** `GITMEMORY_LOCOMO`, the `hybrid` extra, and the same `gcloud` set-up
as 2d.

```sh
uv run python -m bench.qa --bench locomo --dataset "$GITMEMORY_LOCOMO" \
    --arm rerank --unit session --k 10 --split all
```

**(not run while writing this guide)** beyond `--limit 0`.

**Expected:** overall near **0.8552**, n = 1,540; by category 79.1, 84.7, 61.5,
90.7. The published run had 6 of 1,540 calls fail every retry, scored wrong.
**Tolerance:** model-dependent. **Cost and runtime:** not recorded.

## 3. The E8 comparison on E3's harness

**Measures:** session-level Hit@10 and All@10 on the E3 corpus (fabricated
tool noise, a ten-*turn* budget), so the shipped arm can be set beside the
session-level numbers other systems publish. Report:
[E8](benchmarks/E8-where-we-stand.md).

**Command:** the E3 command in [section 1](#1-recovering-evidence-behind-the-compaction-boundary-e3).
There is no separate run. E8 reads the `S-Hit@10` and `S-All@10` columns of its
`after_evidence` rows.

**Expected:** `candidate` S-Hit@10 **0.9660** and S-All@10 **0.8298**; `dense`
0.9723 / 0.8702; `rerank` 0.9787 / 0.8872; `reference` 1.0000 / 0.9319;
`live_context` 0.8894 / 0.3128. All reproduced exactly while writing this guide.

agentmemory's figures in the same README sentence (0.9460 and 0.9860) are its
own self-reports, cited in E8, as are the closed systems' figures in E8's table. They are not produced by anything in this
repository and cannot be reproduced from it.

## 4. Decision extraction

Everything in this section is deterministic, offline and fast: each command
finishes in under a second. The extractor under test is
`gitmemory.derive.decisions`.

### 4a. The held-out gate (E5)

**Measures:** strict node-level precision and recall on a synthetic held-out
split, against a bar pre-registered at precision ≥ 0.85 and recall ≥ 0.60.
Report: [E5 decision gate](benchmarks/E5-decision-gate.md).

The corpus is not committed. `bench/fixture.py` regenerates a split from its
seed, and `bench/gate.py` scores a dump without importing the generator.

```sh
uv run python -m bench.fixture "${TMPDIR:-/tmp}/gm-heldout" --split test --seed 31337
uv run python -m bench.gate "${TMPDIR:-/tmp}/gm-heldout"
```

**Expected:**

```
precision 1.0000   recall 0.7428   matched 257  predicted 257  gold 346
gate (P >= 0.85, R >= 0.6): PASS
```

with directives 1.0000 / 1.0000, post-failure reversals recall 0.5243, and
other reversals recall 0.4595, as in RESULTS.md. The dev split is
`python -m bench.fixture <dir>` with no flags (seed 42), and reads
1.0000 / 0.9319, matched 342 of 367 gold. `bench.gate` also prints the path
of the `derive.py` it is scoring, which is worth reading if you run it from a
second worktree.

Both commands were run while writing this guide and printed the figures above.

The 1.0000 / 1.0000 of run 2 in the E5 report is the same split scored by the
extractor as it stood then; fix 2 moved it to 0.7428 later. Seeds 20042 and
31337 are both spent, and scoring a fresh seed is a new measurement, not a
reproduction.

### 4b. Blind probes C, D and E

**Measures:** the extractor's class score on 32 hand-labelled sentences per
probe, in a vocabulary the corpus lacks. Reports:
[C](benchmarks/E5-probe-C.md), [D](benchmarks/E5-probe-D.md),
[E](benchmarks/E5-probe-E.md).

```sh
uv run python -m bench.probes
```

This prints the current extractor's score on all five probes, and **the
current extractor does not print the published headline.** Each probe was
scored once, and the extractor was then changed with the probe's misses in
view, so a score today is a regression floor (`bench/test_probes.py` pins it)
rather than the blind number:

| Probe | Published, scored once | Current code |
|---|---|---|
| A | 26/32, then 23/32 after fix 2 | 23/32 |
| B | 27/32, then 25/32 after fix 2 | 25/32 |
| C | **14/32** | 16/32 |
| D | **14/32** | 16/32 |
| E | **19/32** | 21/32 |

The blind numbers are reproduced by running the same command against the
commit each probe was scored at. `git archive` extracts a tree without
touching your checkout, and the environment from [Setup](#setup) runs it:

```sh
repo=$PWD
for rev in d0f9596 95f4d18 d87b638; do
  dir=$(mktemp -d)
  git archive "$rev" | tar -x -C "$dir"
  (cd "$dir" && PYTHONPATH=src:. "$repo/.venv/bin/python" -m bench.probes | grep '^probe')
  rm -rf "$dir"
done
```

| Commit | What it is | Prints |
|---|---|---|
| `d0f9596` | Probe C, scored | A 26/32, B 27/32, **C 14/32** |
| `95f4d18` | Probe D, scored | as above, and **D 14/32** |
| `d87b638` | Probe E, scored | A 23/32, B 25/32, C 14/32, D 14/32, **E 19/32** |

All three were run while writing this guide and printed exactly those scores.
D's 14 counts a reversal labelled `directive` as a miss; scored kind-blind it
is 16, as the D report explains. The E report names `53c6464` as the
extractor it scored; that commit predates the probe's cases, which landed in
`d87b638` with the score, and the extractor code is unchanged between them.

The claim "no false positive on any non-decision" is the non-decision rows in
each probe's miss list: none of them appear there.

### 4c. The secondary set: real third-party sessions

**Measures:** the extractor against all 140 distinct human-typed prose blocks
in claude-code-log's `test_data/real_projects` (MIT, pin `6ad029e`), labelled
blind by three annotators; and, separately, the 61 assistant blocks it called
`reversal`, adjudicated by three more. Report:
[secondary set](benchmarks/E5-secondary-set.md).

**Needs:** the claude-code-log clone from `tests/fetch_fixtures.sh`. The text is
not vendored. [`E5-secondary-manifest.json`](benchmarks/E5-secondary-manifest.json)
holds a sha256, a filename and the labels for each item, and `bench/secondary.py`
reads the text back out of the clone, refusing to score if a digest, a filename
or the item count has moved. Without the clone it prints
`secondary set: corpus absent, nothing scored` and exits 0.

```sh
uv run python -m bench.secondary
```

**Expected (current code, after seven fixes):**

```
secondary set: 140 items  {'none': 135, 'directive': 2, 'reversal-by-user': 3}  machine-authored 55
  directive precision 0.1250  recall 0.5000
  tp 1  fp 7 (machine-authored 1)  fn 1
  ...
  assistant `reversal`: 7/7 adjudicated real, precision 1.0000  (61 adjudicated, 54 no longer emitted, 58/61 unanimous)
```

followed by the misses, each with 140 characters of the transcript. As the
report says, that 1.0000 is not a precision claim: the denominator is the
extractor's own output.

**The published headline, precision 0.0000 and recall 0.0000, is the
extractor at the commit the set was first scored at:**

```sh
repo=$PWD; dir=$(mktemp -d)
git archive 020fd0e | tar -x -C "$dir"
(cd "$dir" && GITMEMORY_CC_FIXTURES="$repo/.conformance/claude-code-log/test/test_data" \
    PYTHONPATH=src:. "$repo/.venv/bin/python" -m bench.secondary | head -5)
rm -rf "$dir"
```

It prints `directive precision 0.0000  recall 0.0000`, `tp 0  fp 30
(machine-authored 19)  fn 2`, and `assistant reversal: 9/61 adjudicated real,
precision 0.1475`. The 32 `directive` nodes in the report are those 30 false
positives and 2 more on items held aside as `reversal-by-user`. Both runs were
made while writing this guide. `GITMEMORY_CC_FIXTURES` is set because the old
tree's default clone location is not `.conformance/`.

**Tolerance:** deterministic, given the pinned corpus.

## 5. Engineering

### 5a. Hook latency

**Measures:** the wall time of `hook/gitmemory-hook.sh` handling a 58.9 KB
`PreCompact` payload, beside spawning `true` the same way as a control for
process-start cost. Record: [hook/README.md](../hook/README.md#latency).

```sh
uv run python tools/hook_latency.py 400    # three times
uptime
```

The count defaults to 1,000. The tool points `GITMEMORY_HOME` and `HOME` at a
temporary directory, so it never writes to your real home, and it refuses to
report if the warm-up run leaves no spool record.

**Expected:** a table of p50, p95, p99, max and average for the shim and for
`true`, and a line giving the shim's own share of p50. The published range is
three runs of 400 on Darwin 25.6.0, arm64 (Apple M4 Pro), `/bin/sh` being bash
3.2.57 in posix mode, at load average 10.5 – 11.8: shim p50 **7.42 – 7.53 ms**
and p99 **10.24 – 11.50 ms**, against 1.84 – 1.89 ms and 2.80 – 3.09 ms for
`true`.

**Tolerance:** machine-, shell- and load-dependent. Compare the gap between
the shim and `true` on your machine rather than the absolute milliseconds. A
50-iteration run was made while writing this guide, on a machine busy with the
benchmarks above; it worked and read far higher than the published range,
which is the load and not the shim.

### 5b. The test count and the conformance count

**Measures:** how many tests a fresh checkout collects, and how many
conformance cases the three third-party corpora add.

```sh
# the fresh-checkout count: point both corpus variables at nothing
GITMEMORY_CC_FIXTURES=$PWD/no-such-corpus GITMEMORY_PI_FIXTURES=$PWD/no-such-corpus \
    uv run pytest -q --collect-only -p no:cacheprovider | tail -1

# with the corpora cloned by tests/fetch_fixtures.sh
uv run pytest -q --collect-only -p no:cacheprovider | tail -1
uv run pytest -q --collect-only -p no:cacheprovider | grep -c '\.jsonl\]$'
```

**Expected:** `1025/1026 tests collected (1 deselected)`, then
`1350/1351 tests collected (1 deselected)`, then `328`. The deselected test is
the LongMemEval corpus test, which is opt-in:
`uv run pytest -q -m corpus` after `bench/fetch_longmemeval.sh`. The README's
paragraph on why 1,025 + 328 is 1,350 and not 1,353 is the arithmetic of these
three numbers. All three were run while writing this guide.

`tests/test_docs.py` holds the README to the first and third numbers, so a
drifted count fails the suite. To run the suite rather than count it:
`uv run pytest -q`, which CONTRIBUTING.md gives as about a minute and a half,
no network.

### 5c. Negative controls

**Measures:** for each fix, whether mutating its behaviour away makes the named
test fail. The count is the number of rows in `tests/mutate_index.py`.

```sh
uv run python -c "import sys; sys.path.insert(0, 'tests'); from mutate_index import MUTANTS; print(len(MUTANTS))"
```

**Expected:** `606`. Checked while writing this guide.

To run the controls rather than count them:

```sh
uv run python tests/mutate_index.py            # every mutant
uv run python tests/mutate_index.py "verify"   # only mutants whose name contains an argument
```

**(not run while writing this guide).** The harness edits files under `src/`
and `bench/` in place and restores each one after its row, so run it on a clean
checkout that nothing else is editing. Its docstring estimates a full pass at
one to two and a half hours, from three rows timed at 3.7 s, 17.1 s and
17.2 s. It exits 0 only if every mutant is caught by its intended test.

## What cannot be reproduced exactly, and why

- **QA accuracy (2d, 2e).** Reader and judge are a hosted model. Temperature 0
  does not make a hosted model deterministic, `gemini-3.1-pro-preview` is a
  preview name that can change or be withdrawn, and the response cache that
  made the published runs exact on rerun is local to the machine that ran them
  and is not published. A fresh run makes about two calls per question (500
  and 1,540 questions) and should land near the published figure, not on it.
  The judge is also not the leaderboards' judge (gpt-4o for LongMemEval,
  gpt-4o-mini for Mem0's LoCoMo), so the ranks in the README are approximate
  whatever you get.
- **Blind probe authorship (4b).** A probe's value is that it was written
  blind and scored once. The numbers reproduce exactly at the commits above,
  but the blindness does not: once you have read a probe, or run the current
  extractor against it, it is spent for you as well. An independent
  replication means commissioning a new probe under the same rules: written by
  someone who has read only `README.md` and `docs/DESIGN.md`, frozen before
  scoring, and scored once. The attestation in
  [E5-probe-D-items.md](benchmarks/E5-probe-D-items.md) shows the form.
- **The secondary set's labels (4c).** The scores reproduce exactly, but the
  labels are three people's blind judgements, recorded in the manifest with
  their votes. Re-labelling the 140 items is a new measurement. The set cannot
  be replaced either: it is the one corpus of real third-party sessions here,
  so from fix 1 onward it is a regression floor, not a generalisation measure.
- **The held-out gate (4a).** The split regenerates exactly from its seed, but
  it is spent, and the report's rule is that a fresh seed is a new
  measurement.
- **Hook latency (5a).** Hardware, shell and load. The published figures name
  the machine they came from.
- **Peers' numbers.** Every leader and every other system in the E8 and E9
  tables is that system's self-report, cited to its source. Nothing here
  re-runs them.
- **Cross-encoder weights.** flashrank and model2vec fetch their models by name
  into a cache, not by pinned digest, so a changed upstream file could move a
  `dense`, `rerank`, `rerank12` or `hybrid` figure. The package versions are
  pinned by `uv.lock`; the model files are not.

## Dataset licensing

- **LongMemEval-S cleaned** (`xiaowu0162/longmemeval-cleaned` on Hugging Face)
  is MIT-licensed, as recorded in `bench/fetch_longmemeval.sh` and in
  [DESIGN.md §3](DESIGN.md#3-evaluation), which also pins its revision,
  sha256 and size. It is downloaded at run time, outside the repository, and
  never committed. [THIRD_PARTY.md](../THIRD_PARTY.md) lists it with LoCoMo.
- **LoCoMo** (`snap-research/locomo`) is **CC BY-NC 4.0**, which is
  non-commercial. `bench/fetch_locomo.sh` downloads a pinned revision at run
  time into a directory outside the repository (by default under `$TMPDIR`),
  and it is never vendored. Only scores computed from it are published. Check
  that your own use fits the licence before you run it.
- **claude-code-log, pi and oh-my-pi** fixtures are MIT and are cloned at
  pinned revisions into `.conformance/`, which git ignores. They are not
  vendored, partly because every pi fixture carries its author's home directory
  in a `cwd` field. THIRD_PARTY.md credits all three. The secondary set's
  manifest carries digests and labels, not text; its write-up quotes eight
  sentences from the sessions, and `tests/test_docs.py` holds that line.
- **Models** used by the `hybrid` arms are downloaded from their publishers at
  run time and never redistributed here. Their licences are not recorded in
  this repository.

## What was checked while writing this guide

Run offline, with the datasets already cached at their pinned digests and
`HF_HUB_OFFLINE=1`:

- `python -m bench` in full: every row of E3's tables, and E8's session
  columns, identical.
- `python -m bench.peer --protocol mempalace --arms candidate --k 5,10` and
  `python -m bench.locomo --protocol mempalace --arms candidate --k 5,10`:
  identical to E9's shipped-arm rows.
- `bench.fixture` and `bench.gate` on the dev and held-out splits;
  `bench.probes` at the current tree and at the three scoring commits;
  `bench.secondary` at the current tree and at `020fd0e`: all identical.
- `bench.qa` with `--limit 0` only, which touches no model.
- The collection counts, the mutant count, `tools/hook_latency.py 50`, and
  `--help` for `bench`, `bench.peer`, `bench.locomo` and `bench.qa`.

Not run: the cross-encoder and hybrid arms of E9's retrieval tables to
completion, any model call, any dataset download, and `tests/mutate_index.py`.
