# E3 review round — findings

Four independent reviews of the E3 code, plus the pair review between Claude and Gemini.
Same rule as [E2](E2-findings.md): a finding is **CONFIRMED** only with a reproduction I ran
myself. Reviewer output is a lead, not a fact.

Reviewers: Codex (SQLite/FTS5 domain) · Gemini (pair) · three Opus agents (correctness,
security, test-vacuity). This file records the adjudication; the two pair-review documents
are [Claude on Gemini](E3-claude-review-of-gemini.md) and
[Gemini on Claude](E3-gemini-review-of-claude.md).

## CONFIRMED — Claude's half

### 1. A decomposed Unicode query is shredded before FTS5 sees it — Codex

`index.py:301`. `unicode61 remove_diacritics 2` folds a combining mark into its base letter,
so content indexes as `naive` in either normal form. Python's `\w` does not: a combining
mark is category Mn, so an NFD query term splits mid-word.

```
match_expr(NFC "naïve") = '"naïve"'          -> hit
match_expr(NFD "naïve") = '"nai" OR "ve"'    -> misses the word,
                                                spuriously hits any document with "nai"
```

macOS hands out NFD paths as a matter of course, so this is the ordinary case. Fix:
`unicodedata.normalize("NFC", query)` before tokenising. **Major** — it is a silent wrong
answer, not a crash.

### 2. The representative block for a turn is not deterministic — Codex

`index.py:346`. `MIN()` with bare columns picks *a* winning row, but SQLite does not say
which when two blocks in a turn tie on score, and they tie whenever both contain the query
term. Reproduced against the real `search()`:

```
insert order [0, 1] -> 'marmoset alpha'
insert order [1, 0] -> 'marmoset bravo'
```

Same content, same `block_seq`, different answer. The code comment two lines below claimed
exactly the opposite — that the answer cannot depend on indexing order. Fix: replace the
bare-column trick with `row_number() OVER (PARTITION BY turn_id ORDER BY score, block_seq)`,
which is a total order and does not lean on a loose convention. **Major.**

### 3. A malformed `tool_use` aborts the whole index build — Gemini C2, escalated

`index.py:285`. `_paths` assumes `native["input"]` is a mapping. It is model output.

```jsonl
{"type":"assistant", ..., "message":{"content":[
  {"type":"tool_use","id":"t1","name":"Read","input":"not-an-object"}]}}
```

```
AttributeError: 'str' object has no attribute 'get'
```

Gemini rated this MEDIUM and aimed at the wrong half of the expression — it guarded
`block.native`, which the adapter already guarantees is a dict on every path through
`_blocks`. The reachable hole is one level in, at `input`.

It is also worse than Gemini scoped it. `_fill`'s per-generation `try` covers `_parse` only;
row construction sits outside it, so a single bad block anywhere in the store takes down the
build for **every** generation — the exact failure the guard was written to prevent, with a
comment saying so. Two fixes: make `_paths` treat the shape as untrusted, and widen the
guard to cover row building, building each generation's rows before inserting any of them so
a failure is all-or-nothing. **Major.**

## REFUTED

### Gemini C1 — "`db_path` returns `raw/`, contradicting the CLI help"

`index.py:163` returns `"index"`; `__main__.py:143` says `index/`. They agree. The code
Gemini quoted does not exist in the file.

Cause worth recording: Gemini read `index.py` while a mutation-testing agent had a mutant
applied to it. **Concurrent mutation testing and concurrent review cannot share a working
tree.** The same collision clobbered two fixes I had already applied, and briefly left a
live mutant (`Weights.paths = 0.0`) in the tree. Nothing was lost, but next round the
mutation agent gets its own checkout.

## REJECTED — real observation, not worth the change

### Gemini C3 — pass an open file object to `adapter.parse` instead of a path

Motivated by "metadata/locking delays on NFS". The adapter's contract is a path, every
adapter implements it, and the store is a local directory by construction. Changing a
cross-module interface for an unmeasured latency on a filesystem we do not target is the
trade the ladder exists to refuse. No reproduction offered and none expected.

### Gemini C4 — wrap the insert loop in `with db:` or use `executemany`

The premise is wrong. Python's `sqlite3` in its default mode opens an implicit transaction
on the first DML and holds it until `commit()`, so the inserts are *already* one
transaction; there are no per-row commits to group and no extra write locks to minimise.
`executemany` may still be faster, but that is a profiling question and the rebuild is not
the bottleneck anything has measured. Revisit if it becomes one.

## Accepted risk

`index.py:201` — `os.replace(tmp, target)` with no directory fsync (Codex, unverified
durability). The index is derived and rebuilt from raw by one command; a torn rename costs a
rebuild, and `recall` already says `run gitmemory index first` when the database is absent.
`store.py` fsyncs its directories because the segments there *are* the only copy. This one
is not.

## CONFIRMED — Gemini's half

Recorded in [Claude on Gemini](E3-claude-review-of-gemini.md): G1 (owner path in a fixture),
G2 (nothing wired gitmemory into the benchmark), G3 (the bench tests did not run and the
reported count was wrong), G4 (`every_n` skipped boundaries), G5 (the leakage control was not
deranged), G6 (a missed offset did not consume its rank), G7, G8. G9 verified and closed.
G10 records that this benchmark cannot validate the four-column split and must not be used
to tune the weights.
