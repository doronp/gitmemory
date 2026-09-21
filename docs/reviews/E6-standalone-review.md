# E6 — standalone code review of the dashboard

One reviewing agent, its own git worktree, no part in writing the dashboard. It
was given `f8902f3` "E6: the dashboard" and asked for everything the commit
touched: the two new tables, the six views, `src/gitmemory/dashboard.py`, the
tests written for them, and the mutation rows claimed for them.

Baseline before any edit: **952 passed, 1 deselected**. After: **959 passed,
1 deselected**.

Every finding below was **re-derived by a route the reviewer did not use**
before it was accepted. Two of the four HIGHs are about the same SQL clause
from opposite directions, and one MEDIUM's demonstration was wrong while its
conclusion was right — recorded below as such, because a reviewer who is right
for the wrong reason is still being graded.

## Status

**23 findings: 4 HIGH, 9 MEDIUM, 5 LOW, 5 NIT.** All fixed except LOW-5, taken
in part and deferred in part, and NIT-1, which is this paragraph.

Each fix is pinned by a named test and a negative control in
`tests/mutate_index.py`. The row count went 200 → 264, and the 17 rows this
round touched were re-run: **17/17 CAUGHT by their intended test**, after four
rows and four tests were repaired for holding nothing.

---

## The clause three findings share

HIGH-1, HIGH-2, MEDIUM-1, MEDIUM-2 and MEDIUM-3 are all the same eleven lines:
the subquery that decides which turn's usage counts as *the* usage of a request.
It was

```sql
SELECT agent, session_id, COALESCE(request_id, turn_id) AS request, ...
FROM turns WHERE usage <> '{}' GROUP BY 1, 2, 3 HAVING seq = MAX(seq)
```

and every one of the five is a different thing that goes wrong with it. They
are listed separately because they were found separately and each needs its own
negative control, but there is one fix:

```sql
SELECT *, row_number() OVER (
    PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC
) AS rn
FROM turns
WHERE usage <> '{}' AND request_id IS NOT NULL
  AND role = 'assistant' AND COALESCE(model, '') <> '<synthetic>'
```

Why each piece:

- **`row_number()`, not `MAX(seq)` with bare columns.** SQLite's bare-column
  extension picks the row that produced the max — but it is explicitly
  undefined on a tie, and a fork ties constantly. One winning row also keeps
  `model`, `ts` and the four token columns coherent with each other, which
  `MAX(seq)` beside four `SUM()`s did not. `search()` already argued this.
- **`ORDER BY generation DESC` first.** `seq` is `enumerate(kept)` and restarts
  at 0 every generation, so `MAX(seq)` meant "whichever generation was longer",
  not "the last thing written". A compaction fork that drops turns therefore
  billed the *stale* copy. (HIGH-1)
- **`PARTITION BY agent, request_id`, not `session_id`.** A request id is
  unique per API call, and a subagent's turns land in a different *session*
  — `session_id_for` is basename plus a digest of the path — while sharing the
  parent's request. Partitioning by session billed that request twice. (MEDIUM-1)
- **`request_id IS NOT NULL`, no `COALESCE` to `turn_id`.** The coalesce was
  written to keep rows rather than lose them, but `turn_id` is per *turn*, so
  it reinstated exactly the per-turn over-count the view exists to remove.
  (MEDIUM-2)
- **`role = 'assistant'` and not `<synthetic>`.** The adapter already refuses to
  bill these; the view did not know that. (MEDIUM-3)

Dropping rows rather than mis-billing them creates a second problem: `dash_spend`
then silently disagrees with `billable_usage` and nothing says why. So the
excluded rows are not dropped, they are shown:

```sql
CREATE VIEW dash_unbilled (agent, reason, turns, ...) AS ...
  CASE WHEN role <> 'assistant'   THEN 'not an assistant turn'
       WHEN model = '<synthetic>' THEN 'synthetic model'
       ELSE 'no request id' END
```

`dash_spend` is now an *admitted* floor with the remainder itemised beside it,
and `test_spend_plus_unbilled_accounts_for_every_usage_block` is the check that
the two partitions cover every usage block in the store.

## HIGH-3 — the front page claimed a tile that does not exist

The prose said the injection cost "is shown as one". It is not built. The claim
was deleted, and `dash_unmeasured` grew a sixth row — `('injection cost', 'NOT
BUILT', …)` — so the absence is a row on the page rather than a sentence that
was true in a draft. `test_the_unmeasured_panel_names_what_is_not_known` pins
that the deleted sentence stays deleted *and* that both named-absent tiles
reach the view.

## HIGH-4 — three tests passed for a reason unrelated to their name

The reviewer ran the mutation harness against the E6 rows and found three that
survived. This is the defect class this project keeps finding in itself, so the
fix was not only to repair those three: every new test written in this round was
put through the same harness, and **three more of my own new tests failed it**.

| test | why it held nothing |
|---|---|
| `test_the_sweep_does_not_eat_the_index_it_just_built` | took two wrong drafts — see below. |
| `test_a_turn_that_produced_no_blocks_still_reaches_the_digest` | the fixture used `10` against `9_000_000`, which changes the JSONL line *length*, so the generation row's byte count distinguished the two stores whether or not the turn row reached the digest. Changed to `10` against `99`. |
| `test_the_command_is_immutable_and_loopback` | `assert argv[1] == dashboard.UVX_SPEC` is true for whatever value the constant takes, including `"datasette"`. Added `assert any(c in argv[1] for c in "<=>")`. |

A test whose name promises one thing and whose body checks another is worse
than no test, because it is counted.

**The sweep row took three attempts, and the third changed what the fix means.**
Draft one used the default index path, which can never match the superseded
pattern — it is named for the current schema by construction — so the mutation
had nothing to do. Draft two used `--db gitmemory-v{SCHEMA-1}.db`, which the
pattern does match, and asserted the build's output existed afterwards. Still
green without `keep`, and the harness said so.

The reason is that the sweep runs **before** the rename, not after, which the
docstring of both drafts had backwards. On a build that succeeds, deleting the
old file a moment before `os.replace` would have overwritten it makes no
difference. `keep` protects one thing: the previous index when the build
**fails**. That is the promise temp-plus-rename exists to make, and a sweep
that eats the target first takes it away and leaves no index at all. The test
is now `test_a_failed_build_leaves_the_index_it_was_replacing` — build once,
patch `_fill` to raise, build again, assert the bytes on disk are the ones from
the first build. **CAUGHT.**

Worth stating plainly: the fix was right and the *reason given for it* was
wrong, in the commit and in two tests, until a negative control disagreed.

## MEDIUM-5 — right conclusion, wrong demonstration

The finding was that the content digest no longer covers what the index holds.
The reviewer's demonstration does not reproduce: the specific two stores it
names hash differently, for a reason the report did not account for. The
conclusion survives by two other routes — an assistant turn that emits no
blocks feeds the digest nothing at all (that is HIGH-4's second row), and
generation rows were outside it entirely. `_fill` now folds both in:

```python
digest.update(_insert(db, "turns", row))
digest.update(_generation_row(db, stored, turns=..., blocks=..., reason=None))
```

Recorded this way deliberately. The fix is in because the claim is true; the
report is marked because the evidence offered for it was not the evidence that
holds.

## MEDIUM-4 — replayed turns, counted twice

`dash_corpus` reported `turns` and `blocks` that a fork inflates, because a
replayed turn is stored again. The arithmetic was not changed. The columns were
renamed to `stored_turns` and `stored_blocks`, and `dash_corpus` counts
conversations with `COUNT(DISTINCT session_id)` beside them.

`COUNT(DISTINCT turn_id)` was considered and rejected: `turn_id` is
content-derived, so it would trade a fork over-count for an under-count on two
byte-identical turns — a worse error, because it is invisible. The honest
column name says what the number is.

## MEDIUM-6, MEDIUM-7, MEDIUM-8, LOW-5 — what `serve()` actually does

- The `uvx` fallback downloads a package. The docstring claimed it avoided
  that. It now prints a warning naming what it is about to fetch, and fetches
  `UVX_SPEC = "datasette<2"` rather than whatever is newest. (MEDIUM-6)
- `test_the_command_is_immutable_and_loopback` only exercised the branch for
  whichever binary the machine happened to have. It now loops both. (MEDIUM-7)
- The `--host` warning — the security decision the commit advertised — had no
  test and no mutation row. `LOOPBACK = frozenset({"127.0.0.1", "localhost",
  "::1"})`, and the test checks all three aliases stay quiet while `0.0.0.0`
  warns. (MEDIUM-8)
- LOW-5 is taken in part: `--setting allow_download off`, and the front page now
  says loopback is not authentication. The rest — Datasette's arbitrary-SQL
  console, DNS rebinding, no `Host` validation — is **deferred to the E7
  security round**, where the threat model belongs, and is listed there rather
  than closed here.

## MEDIUM-9, LOW-1 — the index is a file that moves

- Schema 2 orphaned the v1 database with no cleanup. `_sweep_partials(parent,
  keep=target)` removes `gitmemory-v{N}.db` files from superseded schemas and
  leaves everything else — including the target it was told to keep, and
  including `notes.db`, which is not ours. (MEDIUM-9)
- `--immutable` plus `os.replace` means a rebuild leaves the running dashboard
  serving a file that no longer exists at that path. The start-up line now names
  the content digest being served and says the page will not notice a rebuild.
  `_digest` returns `"unknown"` rather than raising if it cannot read the index
  — a dashboard that refuses to start because it could not print a banner is a
  worse failure than a banner that says it does not know. (LOW-1)

## LOW-2 — `ts IS NOT NULL` is not "has a usable timestamp"

`datetime()` returns NULL for `''` and for junk, and `substr(ts, 1, 10)` slices
whatever the local clock wrote — bucketing `not-a-date` under `not-a-dat`.
`dash_growth` now filters on `datetime(ts) IS NOT NULL` and buckets
`substr(datetime(ts), 1, 10)`, which is UTC. The test fixture includes
`2026-09-21T02:00:00+05:00`, which is the 20th in UTC, so the two spellings of
the bucket give different answers and the row that mutates it is caught.

## LOW-3, LOW-4, NIT-3, NIT-4, NIT-5 — six unpinned behaviours and a column

Six mutations survived that nobody had written a test for: per-model grouping,
conversation counting, the stored byte total, `group_concat(skip_reason)`, and
the `model` / `is_sidechain` / usage fields of a turn row. Each now has a test
and a row. `PORT = 8081` and `description_html` are pinned too (NIT-3); the
table-coverage check read `type = 'view'` and so let a new *table* go
undescribed (NIT-4); `dash_requests` gained `ts` and dropped `seq`, which is an
implementation artifact and not something to put in front of a reader (NIT-5).

LOW-4's cache-share fixture summed to a number that was right whether or not
cache writes were in the denominator. Changed to `input 100 / write 100 / read
800 / output 500` → **80.0**, which only the correct denominator produces.

## NIT-1 — the commit message overcounted its own rows

Recorded, not fixed; the commit is pushed.

`f8902f3` says "Ten new rows: the two Gemini fixes from 0bcabea, and eight for
E6", then lists seven. An AST diff of `MUTANTS` across the commit gives **nine**
new rows, 191 → 200. The same paragraph calls it "the 186-row index"; it was
191 before the commit and 200 after.

Three numbers, none of them the number. The bookkeeping is the claim that the
negative controls exist, so it is worth getting right — the count in this file
(200 → 264) was produced by `ast.literal_eval` over the list, not by counting.

## What this round did not settle

- The E5 gate still reads **1.0000 / 1.0000** on 367 gold instances, through
  five rounds of real defects — including this one. The gate cannot see any of
  what is in this file. That is a fact about the gate, and it is in the README.
- The full 264-row mutation index has not been re-run end to end. 17 rows were
  run for this round. RC1.
- LOW-5's remainder — Datasette's arbitrary-SQL console, DNS rebinding, no
  `Host` validation — is open and carried into the E7 security round.
- `docs/DESIGN.md` §2.9 now carries an **As built** block naming the three
  panels E6 did not build and why, the deliberate refusal of a dollar figure,
  and `dash_unbilled`, which the design never mentioned.
