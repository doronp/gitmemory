# Round 3 — Gemini: fix the trust-root findings in `store.py`, `redact.py`, `jsonl.py`

A security review and a correctness review, run independently, both landed on the same
module. Their findings are in `docs/reviews/E3-security-findings.md` and
`docs/reviews/E3-correctness-findings.md` — **read them from disk**, along with the source.
I am not pasting code into this brief.

I am working `src/gitmemory/index.py` in the same tree at the same time. Do not touch it, do
not touch `tests/test_index.py`, and do not run `tests/mutate_index.py` — it rewrites source
in place and will corrupt my edits. Run `uv run pytest tests/test_store.py tests/test_redact.py
tests/test_jsonl.py -q` for your own loop; run the full suite only at the end, and if
`test_index` fails, tell me rather than fixing it.

## The six hard constraints, again

1. **No owner data.** Never read, copy, or reference anything under `~/.claude/projects`,
   `~/memory`, or any path naming the author's account — which this line deliberately
   does not spell, for the reason it exists. Fixtures are synthetic, in `tmp_path`.
   Secrets in tests are fake (`AKIAZZZZQQQQWWWW1234`, `ghp_` + 36 `A`s).
2. **Stdlib first.** No new dependency for any of this.
3. **Fewest files.** Fix in place; no new modules.
4. **Every fix gets a test that fails without it.** Write the test first, watch it fail, then fix.
5. **A comment where the reason is not obvious**, in the voice of the surrounding code —
   why, not what.
6. **Do not widen scope.** If you find something new, write it down and tell me; do not fix it.

## Job 1 — the eight fixes

Ranked. Do them in this order; each is independent.

**S1 (BLOCKING) — `sessions()` trusts manifest segment order, `verify()` sorts.**
`store.py:508` builds the run in listed order; `store.py:605` sorts by `start` before
checking. `Stored.segments` at `store.py:122` is documented "ascending by start offset" and
is not. A manifest with two entries swapped passes `verify` clean, and then every
`Hit.byte_offset` points at the wrong bytes and the egress gate scans the wrong windows.
Sort in `sessions()`, and make the docstring's promise something the code keeps.

**S2 (BLOCKING) — a segment path that escapes the store silently drops the generation.**
`store.py:511` — `_inside` returns falsy and the generation vanishes from `sessions()`, so
`segment_groups()` omits it and the gate never scans it. The comment at `store.py:513-516`
says this exact thing must not happen ("a manifest that opts out of the scan by naming
itself oddly would be a bypass, not a skip"). Make it loud. Decide where the error surfaces
so `push` cannot proceed past it, and say in the commit why you chose that point.

**S3 (BLOCKING) — the egress gate misses a credential spanning more than one segment cut.**
`redact.py:129` carries exactly one file of context (`tail, prev = new_tail, path`), so when
segments are shorter than the secret — which is what a per-turn capture cadence produces —
the carry shrinks to one short segment and the secret is invisible in every window. Carry a
fixed `SEAM` bytes of trailing context regardless of how many segments it spans. Update the
caveat at `redact.py:29-32`; it currently understates the condition.

**S4 (MAJOR) — `verify()` builds `seg_dir` from unvalidated manifest `agent`/`session_id`.**
`store.py:640-642`. `capture()` puts both through `_safe()`; `_verify_manifest` reads them
raw, so an absolute value makes `os.path.join` discard the store root. Worse than the
disclosure: it disables the stray-file check for the *real* directory, which is the [E2]
defect the comment at `store.py:643-645` claims to have closed.

**S5 (MAJOR) — `_adopt_orphans` reads manifest segment paths with no `_inside()` guard.**
`store.py:302`. Every other reader goes through `_inside`; this one hashes out-of-store bytes
into the committed proof, and blocks forever on a FIFO while holding the session flock.

**S6 (MAJOR) — untyped manifest fields crash `capture` permanently.**
`store.py:374` and `:377-400`. `_verify_manifest` and `sessions()` type-check these fields;
`_capture` does not, and `main()` catches neither `TypeError` nor `KeyError`, so a one-byte
manifest edit stops capture for that session forever and dumps a traceback with install paths.

**S7 (MAJOR) — terminal-escape injection into `recall` / `verify` / `capture` output.**
`__main__.py:114-115`, `store.py:622/626/629`, `store.py:370-373`. Transcript text, manifest
`session_id`, and segment paths all reach stdout raw. OSC 52 writes attacker bytes to the
user's clipboard; `\x1b[2K\r` lets a hit relabel itself as a different, trustworthy session.
One escaping helper, applied at the print boundary — not scattered through the modules.

**S8 (MINOR) — raw transcripts are 0644 while the index derived from them is 0600.**
`store.py:177/180/405/410`. The lock file is deliberately 0600 (`store.py:223`); the
unredacted source is world-readable. Make the store's own files and directories owner-only.

Leave `jsonl.py`'s quadratic prefix re-encode (`jsonl.py:74`) — I want it, but it is mine and
it collides with nothing you are doing. Do not take it.

## Job 2 — review my `index.py` fixes

When I push the index commit I will tell you. Then review it line by line. I am fixing: NFC
query normalisation, a `row_number()` window replacing a `MIN()` bare-column group,
shape-hardening `_paths` plus widening `_fill`'s guard, surrogate-safe text for sqlite, a
quadratic `_PATH` regex, a negative `k`, a bare-filename `--db`, `skipped` missing from the
digest, and an unread `meta.schema`.

Three questions I want answered, not confirmed:

1. The vacuity audit found **11 of 26 tests in `tests/test_index.py` pass a mutation that
   reverts the behaviour in their name.** I am rewriting them. Which of my *replacements* is
   still vacuous? Apply the mutation yourself and show me the suite staying green.
2. `Hit.byte_offset` is documented as seekable in the source. For a **sealed** generation it
   is not — it is an offset into that generation's segment concatenation, and the live file
   has since been rewritten. Is the fix a `resolve(hit) -> (path, offset)` helper, or a
   docstring that admits the offset is generation-relative? Argue for one.
3. `turn_id` has no generation in it, so a fork whose prefix turn is byte-identical in both
   generations — the normal shape of a pruner rewrite — produces two rows and one hit. Is
   that correct (it *is* one turn) or a collapse that loses a location?

Open your reply with one line confirming you read the source and the two findings files
yourself from disk, rather than receiving their contents in this prompt.
