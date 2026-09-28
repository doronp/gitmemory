# E3 — Claude's review of Gemini's trust-root batch (S1–S8)

Reviewed: the whole S1–S8 diff against `src/gitmemory/store.py`, `src/gitmemory/redact.py`,
`src/gitmemory/__main__.py` and `tests/test_store.py`. Every line read, every claim run.
Same rule as everywhere else here: a finding is real when I have reproduced it, not when it
reads convincingly.

The eight findings were all real and all worth fixing — that part is not in question, and
S3 in particular (a credential spanning three segments) is a hole I had missed twice. What
follows is the review of the *fixes*, which is a different question from the review of the
findings.

Two of them shipped a regression that was worse than the bug being fixed. Both are fixed
now; the batch as committed in `16b7c6b` is Gemini's findings with my fixes.

## BLOCKING 1 — S2's fix made one bad manifest cost the whole store

`sessions()` grew `except ValueError as exc: raise exc`, so that an escaping segment path
would surface loudly instead of being silently skipped. The intent is right. But

```python
json.JSONDecodeError.__mro__  # -> (JSONDecodeError, ValueError, SyntaxError, ...)
```

`json.JSONDecodeError` **is** a `ValueError`. `sessions()` skips unreadable manifests on
purpose — a reader that raises on one makes a single bad manifest un-indexable for the whole
store, which is precisely the E2 sweep bug that section of the code exists to prevent. So
the new clause re-raised the ordinary half-written manifest a full disk leaves behind, and
one truncated JSON file took down `sessions`, `segment_groups`, the egress gate and the
index build together.

Reproduced before writing this up: truncate `g00.json` to 23 bytes with a second, healthy
session present, and `store.sessions(home)` raised rather than returning the healthy one.

Fix: a dedicated `EscapingSegment` class, so the distinction is in the type rather than in a
string comparison. It subclasses `RuntimeError` and not `Exception` because the CLI catches
`(OSError, RecursionError, RuntimeError, ValueError, sqlite3.Error)` and reports `error:`
with exit 2 — which is the handling this wants, and a bare `Exception` would have escaped as
a traceback.

Pinned by `test_a_truncated_manifest_is_skipped_not_raised`.

## BLOCKING 2 — S1's fix dropped segments instead of failing

The sort was right and the bug it fixed was real. But the loop body kept

```python
if isinstance(s, dict) and "path" in s:
    run.append(full)
```

so a malformed entry was *skipped* while the rest of the run was returned. A run that is
silently one segment short is not a smaller error than a run in the wrong order; it is the
same error. It concatenates to bytes that were never the transcript, and it moves every
offset past the gap — the one failure the store exists to make impossible. The generation
also still looked readable, so nothing downstream had any way to notice.

Fix: `_segment_path` returns `None` for an entry it cannot use, and `not all(run)` drops the
whole generation. A generation that cannot be read as a whole is not read at all.

Pinned by `test_a_malformed_segment_entry_costs_its_generation_not_one_segment`.

## The test I wrote that was wrong, and what it found

`test_a_segment_entry_with_an_unsortable_start_costs_only_its_generation` failed on its
first run — a string `start` did not drop the generation, it just sorted first. My premise
was wrong, and being wrong is what surfaced the real defect: `verify` calls a non-int
`start` malformed, and `sessions` accepted it. A reader that orders by a key the verifier
calls invalid is the same sessions/verify divergence as S1, one level down. `_segment_path`
now requires an int `start`, which makes "well-formed" mean one thing in both places.

## Nine lesser issues in the batch, all resolved

1. **`os.umask(0o077)` around three writes (S8).** umask is process-global and not
   thread-safe, so a concurrent write in the same process gets the wrong mask. It is also
   unnecessary: umask can only *clear* permission bits, so an explicit `os.open(..., 0o600)`
   is already the floor under any caller's umask. Replaced with explicit modes.
2. **`os.makedirs(path, mode=0o700)` applies the mode to the leaf only.** Every intermediate
   directory it creates lands at `0o777 & ~umask`, so `store/sessions/` stayed world-readable
   while the manifest inside it was `0600`. This is what made
   `test_the_store_is_owner_only_on_disk` fail; `_mkdir` now creates one level at a time.
3. **`isinstance(True, int)` is True.** A bool `size` passed the type check and tiled the
   next segment from byte 1. The validator special-cases it.
4. **The manifest validator was 35 repetitive lines** of near-identical `isinstance` checks.
   Replaced with a `_FIELDS` table; the optional flag carries the two fields that are
   legitimately null in a first generation.
5. **The validator was in the wrong place.** It ran in `_capture`, but `_adopt_orphans` is
   the *first* reader of the last manifest and feeds `size` straight into `while size in
   pending`. Two of the parametrised cases crashed with a `TypeError` from adoption before
   `_capture` ever saw them.
6. **TOCTOU on `os.path.getsize`** between the check and the read.
7. **Unbounded label growth** in `scan_group`: one name per segment rather than two.
8. **The `SEAM` comment no longer described the code** after S3 changed the carry.
9. **The `print` shadow had no docstring** saying why shadowing a builtin was deliberate, and
   **the escape set was incomplete** — C0/DEL/C1 only. Bidi overrides reverse the apparent
   order of a line and U+2028/9 break it; neither is ever meaningful in a transcript. Both
   are now covered, and `\r` is escaped while tab and newline survive.

## Method note

Every fix here is covered by a test that fails without it *and* by a mutant in
`tests/mutate_index.py` attributed to that test — the second half matters, because a mutant
caught by some unrelated test means the intended test is decorative.

47/47 at the time of `16b7c6b`, 49 mutants now.
