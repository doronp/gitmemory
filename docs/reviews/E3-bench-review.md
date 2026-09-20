# E3 — review of `bench/`, and what was done about it

Standalone review round on the benchmark harness (`bench/score.py`, `synth.py`,
`arms.py`, `longmemeval.py`, `__main__.py`, `test_bench.py`,
`fetch_longmemeval.sh`). The reviewer ran read-only probes plus a mutation
sandbox on a copy of `bench/` under `/tmp`; it edited nothing in the tree.

Its two headlines were both about the same thing — the harness could not tell a
working retriever from a broken one:

> **The gate does not gate.** A retriever that returns `[]` for every query
> scores `candidate=0.000` and `calibration_passed=True`, and `python -m bench`
> exits 0.
>
> **The tests do not test.** 7 of 7 semantic mutations I applied to `bench/`
> leave all 8 tests green — including deleting the derangement, making
> `shuffled` query its own question, and making the oracle arm return nothing.

**Constraint check: clean.** Nothing in `bench/` reads `~/.claude`, `~/memory`,
`$HOME`, or any local corpus. `arms.gitmemory_factory` passes an explicit
`home=tempfile.mkdtemp()`; `fetch_longmemeval.sh` writes to `$TMPDIR` and pins a
HuggingFace revision plus a sha256. This is the hard constraint on the whole
project and it held.

## How a finding was adjudicated

Per the standing rule in `E3-findings.md`: **a finding is CONFIRMED only with a
reproduction I ran myself.** Four of the reviewer's claims I reproduced and
found *worse* than reported:

| Probe | Result |
|---|---|
| dead retriever (`lambda q, k: []`) | `calibration_passed = True` — `{candidate 0.0, none 0.0, shuffled 0.0, reference 1.0}` |
| the **real** `gitmemory_factory` | `calibration_passed = False` — `{candidate 0.5, none 0.0, shuffled 0.5, reference 1.0}` |
| `paired_z_test([0.02, 0.02], [0.0, 0.0])` | `(0.02, 0.0)` — a fabricated p-value, and n=2 is enough to trip the gate |
| `paired_z_test([1.0], [0.0])` | `(1.0, 1.0)` — n=1 could never trip it |
| rank 1 on every candidate query | `off=1277 'What is the secret for q_0?'` — the planted question turn |
| `grep -n "boundaries\|\.events" src/gitmemory/index.py` | no matches — the four-mode compaction axis could not reach retrieval at all |

So the gate passed a dead retriever **and failed the real one**. Both directions
were wrong, which is the only outcome that makes "the gate does not gate" an
understatement.

## Disposition

`FIXED` = changed and pinned by a test that fails against the mutation.
`DEVIATED` = the defect is real, the recommended fix is not what landed; reason
below.

| # | Finding | Disposition |
|---|---|---|
| 1 | Calibration gate passes on a null retriever | FIXED |
| 2 | p-value fabricated when variance is zero | FIXED |
| 3 | Vacuity: 7/7 mutations survive the suite | FIXED |
| 4 | Question planted in the haystack, capping candidate MRR at 0.5 | FIXED |
| 5 | Compaction axis measures nothing (inert boundary; multi-evidence scores around the wall) | FIXED (a), FIXED (b) |
| 6 | `shuffled` shuffles queries, not the corpus | DEVIATED |
| 7 | Unbounded rejection loop hangs on duplicate `question_id` | FIXED |
| 8 | With n=1 the control arm *is* the candidate arm | FIXED |
| 9 | Bonferroni family mis-specified; test two-tailed | FIXED |
| 10 | Prints candidate scores, then fails | FIXED |
| 11 | `hybrid` extra puts a HuggingFace download in the default suite | FIXED |
| 12 | Corpus test runs in the default suite once you follow the fetch script | FIXED |
| 13 | Interrupted download poisons the cache permanently | FIXED |
| 14 | `turn_recall` is a hit-rate, not recall | FIXED |
| 15 | `reference` never gated; silent 0 on evidence-free instances | FIXED |
| 16 | Turn windows do not tile; unmatched offsets uncounted | FIXED |
| 17 | `__del__` for temp-store cleanup | FIXED |
| 18 | `np.argsort` unstable | FIXED |
| 19 | First-stage depth hardcoded to 50 | FIXED |
| 20 | `rerank_factory` re-captures and re-indexes | FIXED |
| 21 | `ScoreBreakdown` dead code | FIXED (deleted) |
| 22 | Dead `try/except statistics.StatisticsError` | FIXED (deleted) |
| 23 | Magic `mean_diff > 0.01` | FIXED |
| 24 | `k`, seed, limit, compaction, weights all hardcoded | FIXED |
| 25 | `every_n` counts across session boundaries; seed-picked assertion | FIXED |
| 26 | User content emitted as a bare string, not the block list | FIXED |

## The blocking seven, in detail

**1 — the gate.** `score.py` is now built around `_gate`, which records five
kinds of check per compaction mode and fails the run if any of them fails:

| Check | Asserts |
|---|---|
| `sample_size` | `n >= MIN_INSTANCES` (20) — below this no permutation test can reach α, so the harness refuses a verdict rather than returning a weak one |
| `apparatus` | `reference turn_recall == 1.0` **exactly** — the oracle arm reads the ground-truth offsets, so anything else is a broken harness, not a weak retriever |
| `signal` | `candidate − none` significant at the corrected α **and** larger than `MIN_EFFECT` (0.05) |
| `no_leakage` | `shuffled − none <= LEAK_MARGIN` (0.05) |
| `claim` | `candidate − live_context` significant and above `MIN_EFFECT`, under `CLAIM_MODE = "after_evidence"` |

The dead retriever now fails `apparatus`-adjacent checks immediately: `signal`
is `0.0 - 0.0`, which is not `> MIN_EFFECT`.

**2 — the statistics.** `paired_z_test` is gone. `sign_flip_test` is a one-sided
paired randomisation test: under the null each paired difference is equally
likely to have come out negative, so the p-value is the fraction of the `2**n`
sign assignments whose sum reaches the observed sum. Exact by enumeration for
`n <= EXACT_MAX_N` (14), normal form `z = Σd / sqrt(Σd²)` above it. `n = 1` gives
`p = 0.5`; all-zero differences give `p = 1.0`. There is no branch that can
return a p-value of zero.

**3 — the vacuity.** `bench/test_bench.py` was rewritten to 34 tests, and 19
`bench/` mutants were appended to `tests/mutate_index.py` (49 → 68). Each mutant
names the test that must catch it, so a test that passes against its own
mutation is reported as `MISSED`, not as a pass. The seven mutations the
reviewer applied are all in that list.

The sweep found one of the new tests decorative on its first run, which is the
point of running it: the mutant that makes the `claim` gate check
unconditionally true survived, because
`test_the_live_context_arm_loses_what_fell_before_the_boundary` asserts the two
arms differ on the real index and says nothing about what the gate does when
they do not. `test_the_gate_fails_when_the_candidate_finds_only_what_the_live_window_had`
closes it. Final: **68/68 caught by their intended test.**

**4 — the planted question.** `synth.py` no longer writes the question into the
transcript. It was arm-asymmetric: `reference` bypasses retrieval and never paid
it, and `shuffled` issues a foreign query and did not self-match, so it
handicapped exactly the arm under evaluation. A comment at the deletion site
explains it so it does not come back.

**5 — the compaction axis.** (a) `_measure` now takes the compaction boundaries,
and the new `live_context` arm restricts the same retriever to offsets at or
after the last boundary. That is the first thing in the harness that the
compaction mode can actually change. (b) `synth.py` tracks `first_evidence` and
`last_evidence` rather than a single `evidence_coord`, so `before_evidence` walls
off the first and `after_evidence` walls off the last, and `turn_recall` counts
every evidence turn instead of breaking on the first.

**7 — the hang.** The rejection loop is gone. `_deranged` shuffles each
`question_type` group and rotates it by one; a rotation of length ≥ 2 has no
fixed point, so it terminates by construction. Duplicate `question_id` is now
refused by `longmemeval.load` with the offending index in the message — that is
the layer that knows which record it was.

**8 — n=1.** Subsumed by `sample_size`: the harness will not return a verdict
below 20 instances at all.

**9 — Bonferroni and the tail.** `alpha = ALPHA / max(1, len(modes))`, carried on
every recorded check so the report shows the α each verdict used.
`sign_flip_test` is one-sided by construction.

**10 — the report order.** `__main__` prints calibration first and, on failure,
prints `**Calibration failed. Scores withheld: they would not mean anything.**`
and returns 1 without printing a single candidate number. A verdict buried 200
lines below the numbers does not survive a copy-paste; withholding the numbers
does.

## The two deviations

**#6 — kept query-shuffling; did not shuffle the corpus.** The brief did say
"the real retriever over a shuffled corpus," and the reviewer is right that
query-shuffling couples the control to question templates. But corpus-shuffling
makes the control degenerate to the `none` floor: the evidence offsets of
instance *i* do not exist in instance *j*'s haystack, so the control scores zero
by construction and measures nothing. A control that cannot score is not a
control.

The real defect behind the reviewer's demonstration was the fixture's shared
vocabulary, and that is what was fixed. `_deranged` pairs *within*
`question_type`, which is the harder case, and a new test
(`test_the_fixture_question_shares_only_its_token_with_the_transcript`) asserts
that an instance's question shares exactly one token — its nonsense key — with
its own transcript. Two earlier versions of that fixture failed the property
quietly: first on `ledger` and `filed`, which the evidence turn also used, then
on `is` and `the`, which appear in the compaction-summary boilerplate and are
short enough that BM25 ranked the summary above the evidence. Neither broke a
test; each moved a number. So the property is asserted now rather than assumed.

**#15, second half — `no_leakage` is an equivalence bound, not a significance
test.** The natural reading of "assert `shuffled ≈ none`" is a test that fails to
reject equality. Failing to reject a null is not evidence for it — a small,
noisy sample would "prove" no leakage precisely by being uninformative. So the
check is `shuffled − none <= LEAK_MARGIN`: a bound the data has to clear, which
a weak sample cannot satisfy by being weak.

## Added beyond the review: the `live_context` arm

The product claim is that gitmemory keeps what the agent's context window lost.
Nothing in the harness measured that. `live_context` runs the same retriever over
the same index with the window restricted to offsets at or after the last
compaction boundary — i.e. what the agent can still see unaided. `candidate −
live_context` under `CLAIM_MODE = "after_evidence"` is that claim as a number,
and it is a gate condition, so the claim cannot be made in a report that did not
measure it.

## Follow-ups not closed here

- The sweep on the real 470-instance LongMemEval corpus with every arm scored is
  the E3 ship gate and has not been run. Until it has, no number in this
  directory describes retrieval quality on real data.
- `index.py`'s comment that `index/` "is gitignored" is not true today. The
  store's own `.gitignore` for `index/` and `spool/` lands in E4.
