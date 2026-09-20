# E2 review round — findings

Standalone round, as required by the project's process: reviewers are separate
from the author, findings are adversarially verified before they are acted on,
and a finding is only "confirmed" once someone has reproduced it.

Sources this round: an 8-lens agent review (run `wf_6b0a682e-f11`, 69 agents)
and a Gemini 3.1 Pro pair-review. Both are listed; neither is trusted on its
say-so.

Funnel: **73 raw → 61 deduped → 30 verified → 20 confirmed, 6 disputed, 4
refuted.** 31 low-ranked findings were dropped unverified when the verify stage
hit its cap. They are **not cleared, only unexamined**, and are the first thing
the E7 security round re-reads.

Two agents died mid-verify on an API safeguard (`verify:repro:redact.py:89`,
`verify:reachable:store.py:302`). Both findings were confirmed by other lenses,
so nothing was lost, but the dead lenses were not replaced.

## What this round says about the suite

The fixes below broke **one** of 451 existing tests. A suite that does not
notice a new session lock, a case fold, an orphan-adoption pass, a gate that
now raises, and a no-clobber publish was not testing those surfaces. That
near-zero blast radius is the test-vacuity finding, demonstrated rather than
argued.

So every fix landed with a test that fails when the fix is reverted, and that
claim was checked mechanically: 24 mutants, each reverting one fix, **24
caught, 0 survived**, and each caught by its *intended* test (18/18 attributed
— four tests initially passed the mutant and were rewritten until they did
not). Suite is now 488.

---

## CONFIRMED — reproduced, fixed, and covered by a failing-on-revert test

| # | Sev | Site | Finding | Test that now fails without the fix |
|---|---|---|---|---|
| 01 | critical | `__main__.py:47` | `.git` substring filter → gate scans zero files under the default `~/.gitmemory` | `test_cli_push_scans_files_that_are_not_segments` |
| 02 | critical | `redact.py:89` | `gate()` fails open on an empty scan | `test_the_gate_refuses_to_attest_to_nothing` |
| 03 | critical | `test_store.py:438` | No test ever made the gate say no; 5 mutants survived | `test_the_gate_says_no_to_a_credential` |
| 04 | high | `store.py:255` | `os.replace` clobbers a crashed capture's segment, destroying pre-rewrite bytes | `test_a_colliding_segment_is_never_overwritten` |
| 05 | high | `store.py:282` | Orphan segment wedges `verify` forever, with no repair path | `test_a_crashed_capture_is_adopted_on_the_next_run` |
| 06 | high | `store.py:255` | `kill -9` between segment publish and manifest write → `verify` red forever | `test_a_crashed_capture_is_adopted_on_the_next_run` |
| 07 | high | `redact.py:89` | A credential split across a segment boundary is invisible to the gate | `test_a_credential_split_across_two_segments_is_caught` |
| 08 | high | `store.py:184` | No lock: two concurrent captures leave the store un-verifiable | `test_concurrent_captures_never_leave_the_store_unverifiable` |
| 09 | high | `store.py:255` | `_fsync_dir` docstring claimed a guarantee the ordering does not provide | (docstring; see note) |
| 10 | high | `store.py:77` | Work-tree guard bypassed by a symlinked home (`abspath`, not `realpath`) | `test_a_symlinked_home_does_not_bypass_the_work_tree_guard` |
| 11 | high | `store.py:357` | One malformed manifest crashes `verify`, suppressing tamper detection everywhere else | `test_one_unparseable_manifest_does_not_hide_tampering_elsewhere` |
| 12 | high | `__main__.py:20` | Session identity is the bare basename; same-named files merge and re-copy forever | `test_cli_session_ids_do_not_collide_on_basename` |
| 13 | high | `__main__.py:47` | The gate's entire invocation was never executed by the suite | `test_cli_push_is_blocked_by_the_gate` |
| 14 | high | `__main__.py:54` | Gate printed the remote URL, password and all, on the pass path | `test_a_remote_url_password_is_never_printed` |
| 15 | high | `store.py:326` | The tiling check — invariant #1, the E2 gate — was deletable with the suite green | `test_the_tiling_check_is_not_deletable` |
| 16 | high | `store.py:340` | Three more `verify` checks deletable green: segment length, per-segment sha, generation filename | `test_the_segment_length_check_is_not_deletable`, `test_the_per_segment_hash_check_is_not_deletable`, `test_a_misfiled_manifest_is_caught` |
| 17 | high | `store.py:302` | (merged with 11) a malformed manifest aborts the sweep and hides real corruption | as 11 |
| 18 | high | `jsonl.py:64` | `iter_records` caught only `JSONDecodeError`; `RecursionError`/`ValueError` aborted the capture | `test_iter_records_survives_a_line_json_refuses` |
| 19 | high | `claude_code.py:468` | Longest-prefix pricing charged Opus 4.5 at the retired $15/$75 — ~3× over | `test_a_prefix_match_never_crosses_a_version_boundary` |
| 20 | medium | `store.py:359` | `verify`'s stray check was blind to dotfiles, including its own `.incoming` temps | `test_a_dotfile_in_a_generation_directory_is_a_stray` |

Three details worth keeping out of the table:

### 01 — the reproduction

A transcript holding an `AKIA`-shaped HIGH secret, two stores differing only in
the name of the root:

| home | gate blocked the push? |
|---|---|
| `/tmp/gmbypass/.gitmemory` (default shape) | **no — passed** |
| `/tmp/gmbypass/store` | yes, `high aws_access_key_id` |

Also fires on any home under a directory such as `my.github/`, and on a session
id ending `.git` (`_SAFE_RE` permits dots). Not exploitable to a real remote at
E2 only because `_push` returns 1 unconditionally; the transport lands in E4, at
which point it becomes live. Fixed by comparing path components relative to
home. Note that segments now reach the gate as ordered *groups*, which bypasses
the file walk entirely — so the regression test has to plant its secret in a
non-segment file, or the group scan masks the bug. The first version of that
test did not, and passed against the reverted fix.

### 02 / 03 — why the gate could not say no

`not any(f.tier == "high" for f in findings)` over an empty `findings` is
`True`. A gate that inspected zero bytes reported "these bytes may leave". That
is what turned 01 from a filter bug into a bypass, and it would do the same for
the next reason the file list comes back empty — a mistyped home, a permissions
error mid-walk, a race with a rebuild. `gate()` now raises unless the caller
passes `allow_empty=True`.

`test_our_own_manifests_do_not_trip_the_gate` looked like coverage and was not:
it asserted `ok is True`, and re-implemented the walk *without* the `.git`
filter — which is precisely how the bypass shipped past it.

### 09 — fsync orders, it does not make atomic

The old docstring claimed the directory fsync closed the window between
publishing a segment and publishing its manifest. It does not: it *orders* the
two publications, which is what makes "segment on disk, manifest not written"
the only reachable crash state. What closes the window is being able to recover
from that state, which is why orphan adoption (05/06) exists. Corrected in
place; the behaviour it describes is covered by
`test_a_crashed_capture_is_adopted_on_the_next_run`.

Adoption **finishes** the crashed capture rather than deleting the orphan.
Deleting is tempting and wrong: the orphan can hold bytes that are no longer in
the source — the fable-pruner case — where it is the only surviving copy, and
losing it is the exact loss generations exist to prevent
(`test_an_orphan_holding_pruned_bytes_is_kept_not_overwritten`).

### A guard that was written and then deleted

The race in 08 also suggested a "refuse to write generation *n* if *n+1*
exists" guard. It went in, then came out: `gen` is always taken from the newest
manifest, so no reachable state makes it fire, and no test can make it fire
either. Under the session lock the invariant already holds. An untestable
branch is not insurance — it is an untested branch that looks like insurance.

---

## Found while fixing, not by a reviewer

`pyproject.toml:11` declared the console script as `gitmemory.cli:main`. There
is no `cli` module. `pip install gitmemory && gitmemory` was an `ImportError`
from the first commit, and no test noticed, because nothing in a test suite
imports through an entry point. Fixed to `gitmemory.__main__:main`, with
`test_the_console_script_target_exists` resolving the declared target.

`README.md` did not exist, and `pyproject.toml` declared it, so `uv run`
could not build the package at all — the suite only ran because a `.venv` had
already been populated. A clean checkout was unbuildable.

---

## DISPUTED — split verdicts, neither confirmed nor cleared

Carried into the E7 security round. Each is a real mechanism; what the lenses
disagreed on is whether the consequence follows.

| Site | Claim | Why it is still open |
|---|---|---|
| `store.py:178` | The lock does not roll back a partial manifest write | `_write_atomic` publishes by rename, so there is no partial manifest; the dispute is about the *segment*-then-manifest gap, which adoption now covers. Re-check that adoption is reachable from every crash point, not just the one tested. |
| `redact.py:45` | `assigned_secret` misses JSON (`"key": "…"`), underscored, and unquoted forms | True as stated; it is a SUSPECT-tier detector, so the consequence is a missed report, not a missed block. Widening it raises the false-positive rate on our own manifests. Needs a corpus to decide. |
| `redact.py:29` | HIGH tier misses base64-wrapped credentials and 8 issuer prefixes | Also true; "anchored on issuer prefixes, not entropy" is a deliberate design choice (a high-entropy heuristic fires on every sha256 we write). The gap is real, the fix is not obviously entropy. |
| `__main__.py:38` | `verify` exits 0 on an empty or mistyped home | Reproduced. Whether "nothing to verify" should be an error is a product decision, not a defect; it interacts with `gate(allow_empty=...)`. |
| `claude_code.py:177` | `ensure_ascii` regression in canonical JSON | Could not be reproduced against the current code; possibly a stale read. |
| `store.py:242` | The shrink guard misses equal-length in-place rewrites | It does, by design — that is what the prefix *hash* is for, and the generation fork it triggers is tested. The dispute is whether a rewrite that is both equal-length and re-appends to the same size can slip between the two checks. |

## REFUTED — mechanism real, consequence false

- **`claude_code.py:216` `toolUseResult`** — the field is read, not dropped.
- **`store.py:353` self-referential proof** — the manifest is not hashed into
  its own `file_sha256`; the chain hashes the *previous* manifest's bytes.
- **`claude_code.py:112` `_scrub` elision** — `_scrub` does not run on the
  capture path at all; raw is verbatim by design.
- **`store.py:221` unfsynced new directories** — `_fsync_dir` is called on the
  segment directory and the session directory after each publish.

### Gemini: "`redact.py:50` regex corrupted by a `@docs/DESIGN.md` artifact"

Not present. Line 50 reads
`rb"(?i)\b(?:postgres(?:ql)?|mysql|mongodb)://[^\s:@/]+:[^\s@/]+@"` and matches
correctly.

The artifact was in the *prompt*, not the file: the Gemini CLI expands
`@path` tokens in `-p` text before the model sees it, and the source we pasted
is full of `@`. The model reviewed a mangled copy faithfully.

**Operational lesson, worth more than the finding:** never ship source code
through the Gemini CLI's prompt *or* its stdin. At-expansion is applied to
both — a retry with the files on stdin was corrupted identically, and the CLI
resolved `@docs/reviews/E2-findings.md`, this very file, into the middle of a
regex. Source must come from Gemini's own file reads (`--approval-mode plan`
from the repo root).

Every Gemini review in this project opens with an attestation: quote a known
line back before reviewing anything. That step is what caught this — the second
run refused to review and said STOP, exactly as instructed. Cheap, and the only
thing standing between us and a confident review of bytes nobody wrote.

---

## Carried to E7 (the release-candidate security round)

1. The 31 findings dropped unverified by the cap.
2. Every DISPUTED row above.
3. Detector coverage decided against a corpus rather than by argument
   (`redact.py` HIGH and SUSPECT tables).
4. `SEAM = 512` is a fixed carry: a single secret longer than 512 bytes *and*
   cut by a segment boundary is still missed.
5. Whether `verify` should exit non-zero on an empty store.
