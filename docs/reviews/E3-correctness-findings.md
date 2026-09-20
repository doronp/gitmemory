# E3 — correctness review (independent agent)

Reviewer: Opus agent, own synthetic fixtures, repo untouched. Every finding came with a
reproduction the reviewer ran. Adjudication in [E3-findings.md](E3-findings.md).

Findings 1, 3 and 4 were found independently by the security reviewer as well
([E3-security-findings.md](E3-security-findings.md) 6, 2). Two reviewers who never spoke
landing on the same defect is the strongest signal in this round.

---

## 1. BLOCKING — a non-dict `input` on a `tool_use` block aborts the whole build

`index.py:285`, called from `index.py:225`. `_paths()` does
`(block.native.get("input") or {}).get(key)`; a truthy non-dict `input` raises
`AttributeError`. `_row()` is called *outside* the `try/except Exception` at 214-220 that
exists to make one bad generation cost only itself — so one malformed block in one transcript
destroys the index for the entire store, and `AttributeError` is not in `main()`'s caught
tuple (`__main__.py:155`), so a raw traceback escapes.

```
verify: []
Traceback (most recent call last):
  File ".../index.py", line 225, in _fill    row = _row(stored, turn, block)
  File ".../index.py", line 285, in _paths
AttributeError: 'str' object has no attribute 'get'
```

The store is clean per `verify`; the healthy generation is unindexable. Via CLI: `rc=1`,
`$GITMEMORY_HOME/index/` empty. Reproduced for `input` of type `str`, `list`, `int`, `bool`
(falsy values like `[]`/`0`/`null` survive via `or {}`).

## 2. BLOCKING — one lone surrogate anywhere in the store aborts the whole build

`index.py:227`. `jsonl.py` decodes with `surrogateescape` and `records.py` hashes with
`surrogatepass` precisely so invalid UTF-8 survives. The index then hands that `str` straight
to `sqlite3`, which encodes strict UTF-8.

```
verify: []
  File ".../index.py", line 227, in _fill    db.execute(
UnicodeEncodeError: 'utf-8' codec can't encode character '\udc80' in position 3: surrogates not allowed
```

Also triggers on a *legal* JSON `"\ud800"` escape — the sliced-surrogate-pair case
`records.py:28-34` is written to handle. `UnicodeEncodeError` is a `ValueError`, so the CLI
degrades to `error: ...` rc=2 with no index. Same structural problem as 1: the failure is
per-block, the blast radius is the whole store.

## 3. MAJOR — `_PATH` is quadratic; a 200 KB separator-free token hangs `index` for minutes

`index.py:76`, used at `:288`. `(?:[\w.@%+-]+[\\/])+[\w.@%+-]+` backtracks O(n²) over any long
run of `[\w.@%+-]` with no `/`. base64url, JWTs, hex digests and long IDs in a `tool_result`
are exactly that shape, and `tool_result` text is not length-capped (`_ELIDE_OVER` only applies
to unknown blocks in `_scrub`).

```
8000 0.21s
16000 0.82s
32000 3.27s      # clean 4x per doubling
```

End-to-end: a store holding one 200 KB base64url `tool_result` — `index.build()` did not finish
in 120 s (alarm-aborted, extrapolates to ~130 s). Neutering `_PATH` to a never-matching pattern
on the identical store: 0.01 s. A 2 MB blob extrapolates to ~3.6 hours.

## 4. MAJOR — `sessions()` never sorts segments, so a reordered manifest passes `verify` and makes every offset wrong

`store.py:508`/`:517-527` against `store.py:605`. `Stored.segments` is documented "ascending by
start offset" but is taken verbatim from `man["segments"]`. `verify()` sorts before checking,
so it cannot see the discrepancy. `index._parse` (`:246-252`) concatenates in the listed order.

```
verify after reversing: []                             # proof reports clean
hit offset 210 -> b'{"type":"user","uuid":"u3",...}'   # before
hit offset   0 -> b'{"type":"user","uuid":"u1",...}'   # after
```

A *hole* in the manifest is the weaker sibling: `verify` reports it, but `build()` never
consults `verify`, reports nothing in `skipped`, and emits offset 0 for a turn at 198.

Same root cause, second instance: `sessions()` takes `agent`/`session_id` from manifest
*content*, not from the path, and nothing cross-checks. Two distinct generations both indexed
as `session_key='claude-code/alpha/g00'` at `byte_offset=0` — `Stored.key` ("stable identity
for a generation") is not unique, and `(session_key, byte_offset)` stops identifying a location.

## 5. MINOR — a killed `index` leaves a full plaintext transcript copy in the system temp dir

`index.py:246-255`. `_parse` writes the whole concatenated generation to `tempfile.mkstemp()`
and relies on `finally` to unlink it. SIGKILL leaves it. Reproduced: mode 0600, containing the
transcript verbatim including a planted `AWS_SECRET_ACCESS_KEY=...`. Nothing reaps it, it is
outside the store, and it bypasses the redaction gate entirely. Note `build()` correctly uses
`dir=os.path.dirname(target)`; `_parse` uses the default tmpdir, which may also be a different
filesystem.

## 6. MINOR — `.building-*.db` partial indexes accumulate forever

`index.py:186-205`. `except BaseException` cleans up on exception, but not on SIGKILL. After 3
killed builds: three `.building-*.db` plus `-journal` files, 173 KB leaked, still present after
a successful rebuild. The store has `_adopt_orphans` for exactly this in `raw/`; `index/` has no
equivalent. Atomicity itself is correct: a simulated `OSError(28)` mid-`_fill` left the previous
DB intact and queryable.

## 7. MINOR — `meta.schema` is written and never read

`index.py:166-169`. `db_path()` versions the filename so the default path is safe, but `--db`
bypasses it. With `SCHEMA` bumped and `COLUMNS` unchanged, `search()` silently answers from a
schema-1 database. With `COLUMNS` changed it fails as `ProgrammingError: Incorrect number of
bindings supplied` — a correct refusal with an unhelpful message. Old versioned DBs are also
never deleted.

## 8. MINOR — `--db` rejects a bare filename

`index.py:181`. `gitmemory index --db out.db` → `error: [Errno 2] No such file or directory: ''`
from `os.makedirs(os.path.dirname("out.db"))`. The most obvious value for a flag documented as
"database path".

## 9. MINOR — a negative `k` means unlimited

`index.py:352`. `LIMIT -1` is unbounded in SQLite. `search(db, "quaggle", k=-1)` returned all 30
turns against 5 for `k=5`. Since `Hit.text` carries the full block text with no cap, a negative
`k` on a large store materialises the corpus.

## 10. MINOR — `content_sha256` is blind to zero-block turns and to unparseable generations

`index.py:222-233`. Two stores, one with an extra `{"type":"summary","leafUuid":...}` line, hash
identically (`d28d227d2b1e`) while `Stats.turns` differs (1 vs 2). `skipped` is likewise not in
the digest. Accurate to the field's docstring ("over the indexed rows") but not to "two builds
can be compared".

## 11. MINOR — `GROUP BY b.turn_id` merges genuinely distinct turns

`index.py:347-351`. `turn_id` is content-derived over the record's `sessionId`, which the
adapter itself warns is reused across a fork-from-compaction (`claude_code.py:298`). Two store
sessions sharing a `sessionId` and one verbatim turn at different offsets produce a single hit:
rows `(2,'claude-code/sessa/g00',121)` and `(8,'claude-code/sessb/g00',605)` both had
`turn_id 938e6648`; search returned only `sessa@121`. The other location is silently dropped and
`k` under-delivers. The comment at `:349-350` describes intra-turn grouping; this is
cross-session collapse.

---

## Clean

- **Offsets in normal operation.** Multi-segment offsets stay absolute (hit at 210 = true source
  offset), including when a segment boundary cuts a record mid-line (segment 1 ends 40 bytes
  into a record; hit offset 105 = exact record start). The concat path is correct.
- **Order determinism w.r.t. indexing order.** Insertion order is pinned by `sorted(glob(...))`
  in `sessions()`, not by capture order — capturing `(a,b)` vs `(b,a)` gave byte-identical
  results and `content_sha256`. The `ORDER BY score, session_key, byte_offset, block_seq`
  tiebreak is total. `b.block_seq` in the ORDER BY is unreachable dead weight (all blocks of a
  turn share session_key and byte_offset) but harmless.
- **Build atomicity under exception.** Correct.
- **`content_sha256` stability.** Identical across rebuilds, and identical for the same bytes
  captured as 1 segment vs 3 (`b81ea89e4622f27a` both ways) — the segment seam does not perturb
  IDs.
- **Query sanitisation.** `___`, `_`, `hel*`, a 100 000-char token, 5 000 digits, `café` vs
  `cafe`, `foo_bar` — all handled, no crash, no operator injection found.
- **sqlite connection leaks.** None; `build()` and `_recall()` both close in `finally`.
- **Owner paths in shipped code.** None. `rg <owner> src/ bench/ tests/` → no hits.

## Unverified suspicions

- **`index/` "is gitignored" (`index.py:24-25`) is not true today.** Nothing writes a
  `.gitignore` into `$GITMEMORY_HOME`, and `__main__._push`'s skip set is `{".git", ".locks"}` —
  `index/` is walked, so the gate scans SQLite files containing the full transcript text (and
  the leaked `.building-*.db` partials). Harm not demonstrable because push has no transport yet.
- **Memory amplification.** A 9.2 MB transcript took peak RSS from 27 MB to 160 MB — ~17× the
  transcript, because `adapter.parse()` materialises every line twice (the `kept` list plus
  `Turn.native`). Per-generation, not per-store, so not unbounded for the store; a single ~1 GB
  session extrapolates to ~17 GB. Not run to OOM.

## Note on the test suite

One run showed `1 failed, 522 passed`
(`test_a_bare_filename_in_a_tool_argument_lands_in_the_paths_column`, `paths == ''`). Not a
defect: `tests/mutate_index.py` rewrites `src/gitmemory/index.py` in place, and its "paths from
tool arguments dropped" mutant produces exactly that failure. A concurrent mutation run collided
with the reviewer's pytest invocation. Ten subsequent runs: `523 passed, 1 skipped`.
`ruff check .` clean. **The mutation harness makes any concurrent test run untrustworthy.**
