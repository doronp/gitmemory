# E5 — standalone code review of `derive`

One reviewing agent, its own git worktree, no knowledge of the E5 corpus work.
Baseline before any edit: **800 passed, 1 deselected** (`tests` + `bench`);
`tests` alone 762 passed. Every finding marked *reproduced* was produced by
running code; every edit was reverted and the tree verified clean.

Paths here are repo-relative. The reviewer's report used absolute worktree
paths, which `tests/test_no_owner_data.py` now forbids in tracked files.

## Status

Ten findings, all reproduced. None fixed yet. This file is the record; the fixes
and their negative controls land after it.

---

## 1. Fourteen behaviours have no negative control

`src/gitmemory/derive.py`, module-wide.

Fourteen distinct behaviours can be removed and the full suite stays green,
including two of the module's three self-declared rules. Each was run as a
source mutation followed by `pytest -q -x tests bench`, then restored. All
**survived**:

| # | Mutation | Line |
|---|---|---|
| 1 | `ideas()` ignores `count` and always uses `DEFAULT_IDEAS` | 106, 150 |
| 2 | `build()` drops `count=count`, so `--ideas` reaches nothing | 253 |
| 3 | `canonical_json` replaced by `json.dumps(indent=2, sort_keys=False).encode()` | 227 |
| 4 | only the newest generation per session is derived | 251 |
| 5 | `stats.ideas` / `stats.marks` hard-zeroed | 262-263 |
| 6 | every mark's `byte_offset` written as `0` | 194 |
| 7 | every mark's `event_id` written as `""` | 197 |
| 8 | `marks` left unsorted (raw `session.events`) | 180 |
| 9 | `turns` left unsorted (raw `session.turns`) | 179 |
| 10 | `thinking` blocks admitted to `_prose` | 102 |
| 11 | `_write` writes in place, no temp, no rename | 229-233 |
| 12 | `_write` does not unlink the temp on failure | 234-237 |
| 13 | the always-emitted empty `tail` mark is omitted | 206-215 |
| 14 | `cursor += 1` after a match removed | 157 |

Mutation 3 is the worst of them: **rule 3, "rebuildable and diffable", is not
pinned at all.** Swap the serializer for pretty-printed, insertion-ordered JSON
and 800 tests still pass, so `derived/` could start emitting non-canonical bytes
and only a human reading a `git diff` would notice. Mutations 1 and 2 make the
`--ideas` flag decorative as far as the suite is concerned. Mutation 14 is the
one the module's own comment at `derive.py:146-148` exists to justify.

For 14 specifically: the committed test
`test_an_idea_is_attributed_to_the_block_it_was_read_out_of` separates its two
copies of the echo sentence by three other sentences, so the cursor is already
past the first copy by the time the second is matched — `cursor += 1` never has
to do anything. The shape it lacks is the *adjacent* duplicate, where the last
sentence of block `u1` is identical to the first sentence of block `a1`. Shipped
code attributes `['u1', 'a1']`; with `cursor += 1` deleted it attributes
`['u1', 'u1']` — a rule-2 violation, an idea naming a block it did not come
from — and the whole suite stays green.

Controls that *were* caught, confirming the harness works: `sentences_seen` /
`sentences_ranked` zeroed; `seen += len(sentences)` moved below the cap's
`continue`; `timeline.json` not written; the skip reason reduced to a bare
count; `<` changed to `<=` in the merge walk; summariser output reversed.

## 2. A generation that fails to parse keeps its previous artifacts

`derive.py:255-257` (the `except` → `continue`), `derive.py:258-263`.

`build()` skips a failing generation but never removes what a previous
successful run wrote for it, so `derived/` keeps asserting facts about a
generation `derive` now says it cannot read.

Build once against a healthy store — `derived/claude-code/sess/g00/{ideas,timeline}.json`
land with 8 ideas. Corrupt the manifest's `agent` field. Build again:
`Stats(generations=0, ideas=0, marks=0, skipped=['claude-code/sess/g00: ValueError(...)'])`,
CLI exits 0 — and both files are still on disk, byte-identical, still claiming 8
ideas and 3 turns for a generation that is now unparseable. `git diff
--exit-code` on `derived/` is clean, so the artifact tree silently stops being a
function of committed bytes. `docs/DESIGN.md:217` already says `derived/` is
"rewritten on every rebuild" — it is not; it is *overwritten where it succeeds*.

## 3. `derive` writes `derived/` without ever invoking the redaction gate

`derive.py:1-33` (imports), `derive.py:219-237` (`_write`).

`docs/DESIGN.md:210-211`, verbatim: *"Redaction applies at exactly two
boundaries: anything written to `derived/`, and anything that leaves the
machine."* `src/gitmemory/redact.py:5-7` repeats it. `derive.py` imports
`contextlib, os, re, tempfile, dataclasses, store, parse_generation, Session,
canonical_json` — no `redact`.

An assistant turn containing *"The deploy failed because the key
AKIAIOSFODNN7EXAMPLE was rejected by the endpoint"* is prose, so LexRank ranks
it; `derive.build` writes it verbatim as an idea, and `gitrepo.commit`'s `git add
--all` commits it.

Honest blast radius: the push gate at `_push` does scan `derived/`, so this does
not leak past a push — the raw copy already blocked it. The damage is that the
layer DESIGN.md designates as the sanitised, shareable one is not sanitised, and
the secret is duplicated into a second permanently-committed location. `grep -rn
redact tests/` hits only `test_store.py`, and `docs/tasks/E5-brief.md` never
mentions redaction, so this may be a deliberate deferral — but there is no
`ponytail:` comment saying so, which by the repo's own convention makes it a
finding.

## 4. A missing `derive` extra is reported as a successful no-op

`derive.py:112-120`, `src/gitmemory/__main__.py:203-210`.

The `ImportError` for the optional extra is raised as a `RuntimeError` *inside*
the per-generation try, so an environment-wide misconfiguration is laundered
into a per-generation skip and the CLI exits 0.

Install without `[derive]`: `gitmemory derive` prints one `skipped <key>:
RuntimeError("derivation needs the derive extra: ...")` per generation to
stderr, then `0 generation(s)  0 idea(s)  0 mark(s)` to stdout, and returns 0.
In CI or a watcher loop with stderr discarded this is indistinguishable from an
empty store. The condition is not per-generation data — it is identical for
every generation — so `build()`'s "one bad generation costs its own artifacts
and nothing else" rationale at `derive.py:242-247` does not cover it.

## 5. The `MAX_SENTENCES` cap does not bind on the input that needs it

`derive.py:38-43` (the `ponytail:` comment and the constant), `derive.py:123-135`.

The cap counts *sentences*, but the cost that explodes is driven by distinct
*words*, and a block with no `.`/`!`/`?` is exactly one sentence — so an
attacker-chosen input drives `sumy` superlinear while `len(owners)` stays at 1
and the cap never fires.

The comment's stated ceiling is the float64 similarity matrix, `O(n²)` in
sentences. The real hot spot is `LexRankSummarizer._compute_idf`, which is
`O(U·n·L)` because `_to_words_set` returns a list, not a set. Measured: one
block, one sentence, no terminators.

| words | block size | `derive.ideas` wall time | `sentences_seen` |
|---|---|---|---|
| 64 000 | 764 KB | 8.52 s | 1 |
| 128 000 | 1.5 MB | 33.02 s | 1 |
| 256 000 | 3.05 MB | 133.20 s | 1 |

Clean ×4 per doubling: ~35 min at 12 MB, ~9 h at 48 MB — inside what a single
agent transcript can reach. Terminators do not save you either: 400 sentences ×
80 unique words each (382 KB, `sentences_seen=400`, far under 2000) took 7.05 s.

Second-order: the `ponytail:` comment promises "the shortfall is reported rather
than hidden", and `sentences_seen` vs `sentences_ranked` is that report. On this
input both are `1`, so the artifact records no shortfall at all while the run
burns two minutes. The comment's ceiling and its upgrade path are both stated
against the wrong axis.

## 6. A negative `--ideas` returns almost every sentence

`src/gitmemory/__main__.py:274-276` (`type=int`, no lower bound), `derive.py:150`.

`count` goes straight to `summarizer(document, count)` and sumy's `ItemsCount`
does `sequence[:count]`, so a negative count means "all but the last |count|".
`gitmemory derive --ideas -1` exits 0 and writes 8 of the document's 9 sentences
instead of the 8 *best*; `--ideas -3` yields 6. The sibling code guards the
identical shape: `index.search` clamps with `max(k, 0)` and the repo pins it with
the committed mutant "a negative k means unlimited". `derive` has neither.

## 7. A write failure is not per generation and leaves a torn one

`derive.py:258-260` — both `_write` calls sit *outside* the `try`.

An I/O failure on the second write both aborts the entire build and leaves one
generation half-updated. A transcript grows 3 turns to 4; during rebuild
`os.replace` fails with `ENOSPC` for `timeline.json` only. `ideas.json` has
already been renamed into place with the new content; the exception propagates
out of `build()`, so there is **no** `stats.skipped` entry, every later
generation is never processed, and the CLI exits 2. On disk: a fresh
`ideas.json` beside a stale `timeline.json` still saying `"turns": 3`. Directly
contradicts the docstring's "One bad generation costs its own artifacts and
nothing else."

## 8. A killed `derive` leaves a temp file that git commits

`derive.py:229` (`prefix=".deriving-"`), `src/gitmemory/gitrepo.py` (`GITIGNORE`).

`_write`'s temp files are dotfiles in the target directory with no `.gitignore`
entry and no sweeper, so a SIGKILL mid-write leaves
`derived/claude-code/sess/g00/.deriving-abc123.json`, which `gitrepo.commit`
stages and commits permanently; a full rebuild never removes it. `GITIGNORE`
anchors `raw/*/*/*/.incoming.*` and `sessions/*/*/*.tmp.*` with a comment about
`git add --all` staging dotfiles for precisely this hazard; `derive`'s temp shape
is the third one and it is missing. The store also has `_sweep_temps`; `derive`
has no equivalent.

## 9. `derived/` does not inherit the store's permission stance

`derive.py:228` (`os.makedirs(parent, exist_ok=True)`).

Default umask 022 → `derived/` and its three descendants are `0755`; under
`umask 0` they are `0777`. The files are `0600` via `mkstemp`, but a
world-writable directory lets any local user unlink `ideas.json` and drop in
their own — and the artifacts are committed, so that is a path into history.
`store._mkdir` creates at `0o700` one level at a time and carries an explicit E3
comment about never accepting umask-derived modes; `derive` opts out with a
one-line `os.makedirs` and no comment saying why `derived/` is different.

## 10. Degenerate prose makes numpy divide 0/0 and the warning escapes

`derive.py:137-143`; the divide is `sumy/summarizers/lex_rank.py:165`.

A prose block with no rankable words produces a zero similarity matrix,
`power_method` normalises by a zero norm, and the `RuntimeWarning` reaches the
terminal. Scenario A: a turn whose text is `"... !!! ??? ... !!!"` —
`gitmemory derive` prints `RuntimeWarning: invalid value encountered in divide`
to stderr, exits 0, and writes an `ideas.json` full of `NaN`-ranked selections.
Scenario B: the same store under `PYTHONWARNINGS=error`, an ordinary CI setting
— the generation is reported as `skipped ... RuntimeWarning(...)`, the CLI still
exits 0, and by finding 2 the previous run's artifacts stay on disk.
`canonical_json` uses `allow_nan=False`, which suggests NaN was thought about at
the JSON boundary but not at the source; the NaN lives in the *ratings*, not the
payload, so the guard never fires.

---

## Suspicion, not a finding

**Cross-machine float determinism of the LexRank cut.** `derive.py:137-157`.
`pyproject` declares `numpy>=2.0` and `sumy>=0.13` with no upper bound, and the
selection is a sort over `power_method` output. On a document of near-identical
sentences the top-12 ratings are dense with exact ties and the gap across the
`count=8` cut measured `3.205e-08` — small enough that a different BLAS
reduction order could reorder the cut and change the committed bytes. It could
not be made to happen: varying `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and
`VECLIB_MAXIMUM_THREADS` between 1 and 8 produced byte-identical output every
time. Recorded because a tie-break that does not depend on float equality is the
generic mitigation, and there is no failing case to justify paying for it yet.

## Checked and found correct

- **The cursor walk is sound for every ordering sumy can return.**
  `_get_best_sentences` stable-sorts descending by rating, applies `ItemsCount`,
  then re-sorts by document order, so its output is always a document-order
  subsequence of `texts` and the `while` scan never goes backwards. Two blocks
  with the identical sentence: `ratings = dict(zip(document.sentences, scores))`
  collapses text-equal sentences to one rating, so a split tie provably takes the
  earlier copy and the cursor lands on the right owner. One block with the same
  sentence twice, and the adjacent duplicate across a block boundary, both
  attribute correctly. Sumy returning fewer sentences than requested is a no-op
  for the walk. The `# pragma: no cover` `break` at `derive.py:154` is genuinely
  unreachable for `LexRankSummarizer`. The "upstream stops returning document
  order" mutant was **caught** — that dependency is pinned.
- **Determinism, everything except the float suspicion.** No dict or set
  iteration reaches output; `PYTHONHASHSEED=0/1/random` gives identical bytes;
  `store.sessions()` is `sorted(glob(...))` so readdir order cannot leak in;
  `LC_ALL` / `LANG` variation changes nothing (`_SENT_RE`/`_WORD_RE` are ASCII
  classes, `canonical_json` is `ensure_ascii=True`); per-generation output is
  independent of processing order. Even the degenerate NaN case of finding 10 is
  repeatable byte-for-byte.
- **`_write`'s missing fsync is a durability question, not a correctness one.**
  `os.replace` is atomic within the directory, so a reader never sees a partial
  file; a crash loses a rebuild, and the rebuild is the command that just ran.
  The real crash exposure in `_write` is the committed litter of finding 8.
- **`derived_dir` cannot escape `derived/`.** `derive.py:76-86` derives agent and
  session from the manifest's own path rather than from untrusted JSON, and
  `store.sessions()`'s glob `sessions/*/*/g*.json` matches neither a path
  separator nor a leading dot, so a separator, a `..`, or a null byte in a
  transcript's `agent`/`sessionId` cannot reach the output path. The generation
  component is `os.path.splitext` of a name that already matched `g*.json`.
- **The `<` in the merge walk is right and it is pinned.** `derive.py:188`: a turn
  sitting at exactly the event's byte offset belongs *after* the event.
  `sum(turns_since) == turns` held for every input tried, including events with
  `byte_offset == -1` (they sort first and consume nothing) and events past the
  last turn. The always-emitted empty `tail` is what makes that invariant total.
- **Attribution is honest in the shipped code.** No idea named a block it did not
  come from in any constructed input, and every mark's `source_ref`/`event_id`
  came from the same parsed generation — `timeline()` only reads
  `session.events` and `session.turns`, so cross-session contamination is
  structurally impossible.
- **Untrusted input does not crash the regexes.** Lone surrogates survive
  end-to-end (`surrogatepass` hashing, `ensure_ascii=True` output) with no
  `UnicodeEncodeError`. `_SENT_RE` and `_WORD_RE` have no nested quantifier and
  no catastrophic backtracking — a 3 MB block of pure punctuation splits in
  milliseconds. The superlinearity in finding 5 is all inside sumy.
  `power_method` cannot loop forever: self-similarity keeps the diagonal
  positive.
- **`_prose`'s exclusions are justified** and the `thinking` exclusion carries a
  proper `ponytail:` comment with a ceiling and an upgrade path
  (`derive.py:95-99`) — though the behaviour itself is unpinned (mutation 10).
- **`Event.meta` being dropped** (`derive.py:174-176`) is reasoned, not
  accidental, and keeps adapter-shaped untrusted values out of canonical JSON.
