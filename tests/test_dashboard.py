"""E6: the dashboard — the views it reads and the server it starts.

Two halves, and they fail for different reasons.

The **views** are the product. They are SQL in `index._VIEWS`, so a test here is
a test of arithmetic somebody will otherwise read off a web page and believe.
The one worth the most is `dash_spend`: usage in an agent transcript is repeated
on every turn of a request and is *cumulative*, so the obvious query over-counts
by a factor nobody would notice, and the view is asserted against
`adapters.claude_code.billable_usage` rather than against a number typed here.

The **server** is Datasette, which is not ours and is not tested here. What is
tested is the handful of decisions around it: loopback by default, `--immutable`
always, no silent install, and metadata that actually reaches the database it
was written for — the last of which breaks silently on a schema bump, which is
exactly why the name is computed rather than checked in.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_index import assistant, built, user, write

from gitmemory import dashboard, index, store
from gitmemory.__main__ import main
from gitmemory.adapters.claude_code import billable_usage
from gitmemory.index import parse_generation


def usage(**kw) -> dict:
    base = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    return base | kw


def billed(uid: str, request: str, text: str, **kw) -> dict:
    """An assistant turn carrying a request id and a usage block."""
    line = assistant(uid, [{"type": "text", "text": text}])
    line["requestId"] = request
    line["message"]["usage"] = usage(**kw)
    return line


def rows(db: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(r) for r in db.execute(sql)]


# The fourth copy of these four lines. Importing them from `test_index` makes
# ruff read the import as a redefinition at every use, so they are local, the
# way `test_store` and `test_derive` already keep theirs. A `conftest.py` is the
# right answer and is a separate change, since it deletes three definitions in
# files another round is currently editing.
@pytest.fixture
def home(tmp_path):
    h = tmp_path / "store"
    h.mkdir()
    return str(h)


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src" / "sess.jsonl"
    p.parent.mkdir()
    return str(p)


# --------------------------------------------------------------------------- #
# the views
# --------------------------------------------------------------------------- #


def test_every_view_the_index_defines_is_described_on_the_dashboard(home, src):
    """A new view with no description is a column of numbers with no caption,
    which is the specific failure this whole epoch is trying not to be."""
    db = built(home, src, [user("u1", "hello")])
    views = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert views, "precondition: the build creates views at all"
    assert views <= set(dashboard._TABLES), f"undescribed: {sorted(views - set(dashboard._TABLES))}"


def test_every_table_the_index_defines_is_described_too(home, src):
    """NIT-4. The check above reads `type = 'view'`, so a new *table* went
    undescribed with the suite green — even though `_TABLES` describes five of
    them. FTS5's shadow tables and `sqlite_stat1` are Datasette-hidden and are
    excluded by name rather than by hoping nobody notices them."""
    db = built(home, src, [user("u1", "hello")])
    tables = {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r["name"].startswith(("fts_", "sqlite_"))
    }
    assert tables, "precondition: the build creates tables at all"
    undescribed = sorted(tables - set(dashboard._TABLES))
    assert not undescribed, f"undescribed: {undescribed}"


def test_the_port_is_not_datasettes_default(home, src, monkeypatch):
    """NIT-3. `PORT = 8081` has a stated reason — "not 8001: Datasette's default
    collides with half the world" — and changing it to 8001 survived. The reason
    is the assertion; the number is only where it lands."""
    assert dashboard.PORT != 8001
    built(home, src, [user("u1", "hello")])
    seen = {}
    monkeypatch.setattr(dashboard.subprocess, "call", lambda argv: seen.update(argv=argv) or 0)
    dashboard.serve(home)
    assert seen["argv"][seen["argv"].index("--port") + 1] == str(dashboard.PORT)


def test_a_generation_that_would_not_parse_is_still_counted(home, src):
    """The number that makes every other number on the page readable. A store
    with an unreadable generation must not look like a whole one."""
    write(src, [user("u1", "hello")])
    store.capture(src, "claude-code", "sess", home=home)
    # Garbage bytes would not do it: the JSONL reader skips a line it cannot
    # read, on purpose, so a corrupt segment parses to an empty session rather
    # than to an error. An agent with no adapter is the case that really raises.
    man = Path(home, "sessions", "claude-code", "sess", "g00.json")
    man.write_text(json.dumps(json.loads(man.read_text()) | {"agent": "martian"}))

    stats = index.build(home)
    assert len(stats.skipped) == 1, "precondition: the build really had to skip it"
    db = index.open_db(index.db_path(home))

    gens = rows(db, "SELECT * FROM generations")
    assert len(gens) == 1
    assert gens[0]["parsed"] == 0
    assert "martian" in gens[0]["skip_reason"]
    assert rows(db, "SELECT unparseable FROM dash_corpus")[0]["unparseable"] == 1


def test_a_turn_row_exists_for_every_turn_and_carries_its_usage(home, src):
    db = built(home, src, [user("u1", "hello"), billed("a1", "req-1", "hi", output_tokens=7)])
    turns = rows(db, "SELECT role, request_id, usage FROM turns ORDER BY seq")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["usage"] == "{}"
    assert json.loads(turns[1]["usage"])["output_tokens"] == 7
    assert turns[1]["request_id"] == "req-1"


def test_a_turn_row_carries_the_fields_the_views_group_by(home, src):
    """LOW-3. Zeroing `model`, `is_sidechain` or `agent_id` in `_turn_row`
    survived — three columns nothing read back. `model` is what `dash_spend`
    groups by and what the `<synthetic>` refusal tests; the other two are how a
    subagent's work is told from its parent's on the page."""
    sub = billed("a1", "req-1", "hi", output_tokens=7)
    sub["isSidechain"] = True
    db = built(home, src, [user("u1", "hello"), sub])
    turns = rows(db, "SELECT role, model, is_sidechain, request_id FROM turns ORDER BY seq")
    assert turns[0] == {
        "role": "user",
        "model": None,
        "is_sidechain": 0,
        "request_id": None,
    }
    assert turns[1] == {
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "is_sidechain": 1,
        "request_id": "req-1",
    }


def test_a_user_turn_carries_no_usage_and_is_not_billed(home, src):
    """LOW-3. Writing `"{}"` as the usage of every non-assistant turn survived,
    so the column could have been blank for half the store with nothing to say
    so. Claude Code puts a usage block on some user-role lines — the tool-result
    turns — and it is the assistant turn's total repeated, not a second cost."""
    echo = user("u1", "tool result")
    echo["message"]["usage"] = usage(output_tokens=7)
    db = built(home, src, [echo, billed("a1", "req-1", "hi", output_tokens=7)])

    stored = rows(db, "SELECT role, usage FROM turns ORDER BY seq")
    assert json.loads(stored[0]["usage"])["output_tokens"] == 7, "the column is not blanked"
    assert rows(db, "SELECT SUM(output_tokens) AS n FROM dash_spend")[0]["n"] == 7
    assert rows(db, "SELECT reason, output_tokens FROM dash_unbilled") == [
        {"reason": "not an assistant turn", "output_tokens": 7}
    ]


def test_repeated_cumulative_usage_is_billed_once(home, src):
    """The 2.79x bug, in miniature. Two turns of one request, the second
    carrying the running total — a sum over turns reads 30 output tokens where
    the request cost 20."""
    db = built(
        home,
        src,
        [
            billed("a1", "req-1", "thinking", output_tokens=10),
            billed("a2", "req-1", "done", output_tokens=20),
        ],
    )
    spend = rows(db, "SELECT * FROM dash_spend")
    assert len(spend) == 1
    assert spend[0]["requests"] == 1
    assert spend[0]["output_tokens"] == 20


def test_a_replayed_generation_is_not_billed_twice(home, src):
    """A compaction forks the session and the fork replays the same requests.
    Grouping per generation would bill the conversation twice for work that
    happened once."""
    write(src, [billed("a1", "req-1", "hi", output_tokens=11), user("u9", "filler")])
    store.capture(src, "claude-code", "sess", home=home)
    # A shorter file is a divergence, which is what forks a generation — a
    # longer one is just an append. The fork replays the same request, which is
    # what a compaction does.
    write(src, [billed("a1", "req-1", "hi", output_tokens=11)], mode="wb")
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))

    assert len(rows(db, "SELECT * FROM generations")) == 2, "precondition: the store really forked"
    spend = rows(db, "SELECT * FROM dash_spend")
    assert spend[0]["requests"] == 1
    assert spend[0]["output_tokens"] == 11


def test_the_spend_view_agrees_with_the_adapter(home, src):
    """Two implementations of one rule, checked against each other rather than
    against a constant typed into this file. `billable_usage` is the tested one
    and predates the view; if they ever disagree, the view is wrong."""
    lines = [
        billed("a1", "req-1", "one", input_tokens=100, output_tokens=10),
        billed("a2", "req-1", "one more", input_tokens=100, output_tokens=25),
        billed("a3", "req-2", "two", input_tokens=5, cache_read_input_tokens=900),
    ]
    db = built(home, src, lines)
    expected = billable_usage(parse_generation(next(iter(store.sessions(home)))))
    spend = rows(db, "SELECT * FROM dash_spend")[0]

    assert spend["input_tokens"] == expected["input_tokens"]
    assert spend["output_tokens"] == expected["output_tokens"]
    assert spend["cache_read_tokens"] == expected["cache_read_input_tokens"]


def test_cache_share_is_a_share_of_what_went_in(home, src):
    """800 of 1000 input tokens served from cache is 80%, and the denominator is
    everything that went in — not the output, which was never cacheable.

    The cache *write* is in the fixture because it is in the denominator: the
    numbers here were 100/900/500 with no write at all, so removing the
    `cache_write_tokens` term changed nothing and half of "everything that went
    in" was unasserted. A cache-write-heavy store — the first turn of every
    session — would have over-reported its share with the suite green. [E6]"""
    db = built(
        home,
        src,
        [
            billed(
                "a1",
                "r1",
                "x",
                input_tokens=100,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=800,
                output_tokens=500,
            )
        ],
    )
    assert rows(db, "SELECT cache_read_pct FROM dash_spend")[0]["cache_read_pct"] == 80.0


def test_spend_keeps_one_row_per_model(home, src):
    """LOW-3. The panel is called "Tokens by model" and merging every model into
    one row changed no test — which is the whole reason the column split exists.
    Two models, two rows, and the request each one was billed for."""
    opus = billed("a1", "r1", "x", output_tokens=10)
    haiku = billed("a2", "r2", "y", output_tokens=3)
    haiku["message"]["model"] = "claude-haiku-4-5"
    db = built(home, src, [opus, haiku])
    assert rows(db, "SELECT model, requests, output_tokens FROM dash_spend ORDER BY model") == [
        {"model": "claude-haiku-4-5", "requests": 1, "output_tokens": 3},
        {"model": "claude-sonnet-4-5", "requests": 1, "output_tokens": 10},
    ]


def test_corpus_counts_conversations_once_and_generations_every_time(home, src):
    """LOW-3. `COUNT(DISTINCT session_id)` → `COUNT(session_id)` survived, so the
    two numbers the top panel leads with — how many conversations, how many
    times the store forked them — were interchangeable as far as the suite
    knew."""
    write(src, [user("u1", "one"), user("u2", "two")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u1", "one")], mode="wb")  # shorter: a divergence, so a fork
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))

    corpus = rows(db, "SELECT sessions, generations, bytes FROM dash_corpus")[0]
    assert corpus["sessions"] == 1, "one conversation, forked"
    assert corpus["generations"] == 2
    # `"bytes": stored.size` -> `0` survived too, and "how much store there is"
    # is the first number on the page. Against the store, not a constant.
    assert corpus["bytes"] == sum(s.size for s in store.sessions(home)) > 0


def test_contiguity_names_why_a_generation_could_not_be_read(home, src):
    """LOW-3. Dropping `group_concat(skip_reason, …)` survived. `unparseable: 1`
    with no reason next to it is a number you cannot act on, and this column is
    the only place the reason reaches a page."""
    write(src, [user("u1", "hello")])
    store.capture(src, "claude-code", "sess", home=home)
    man = Path(home, "sessions", "claude-code", "sess", "g00.json")
    man.write_text(json.dumps(json.loads(man.read_text()) | {"agent": "martian"}))
    index.build(home)
    db = index.open_db(index.db_path(home))

    row = rows(db, "SELECT unparseable, skipped FROM dash_contiguity")[0]
    assert row["unparseable"] == 1
    assert "martian" in (row["skipped"] or "")


def test_a_transcript_with_no_usage_blocks_produces_no_spend_rows(home, src):
    """A transcript with no usage blocks is the normal case for a non-Claude
    adapter.

    Renamed. It was `..._does_not_divide_by_zero` and its docstring said `NULLIF`
    was the only thing standing between the page and a crash, which was wrong
    twice: this fixture has no usage rows at all, so the expression was never
    evaluated, and SQLite returns NULL for `x/0` rather than raising, so there
    was no crash to prevent. What it does pin is `WHERE usage <> '{}'`. The
    division is covered by the test below. [E6 review]"""
    db = built(home, src, [user("u1", "hello")])
    assert rows(db, "SELECT * FROM dash_spend") == []
    assert rows(db, "SELECT * FROM dash_requests") == []


def test_a_share_of_nothing_is_null_not_zero(home, src):
    """A request that sent no input at all — the denominator is zero and the
    cache share is unanswerable, which is NULL and is not 0%."""
    db = built(home, src, [billed("a1", "r1", "x", output_tokens=500)])
    spend = rows(db, "SELECT requests, cache_read_pct FROM dash_spend")
    assert spend[0]["requests"] == 1, "precondition: the row exists to divide"
    assert spend[0]["cache_read_pct"] is None


def test_the_unmeasured_panel_names_what_is_not_known(home, src):
    """It is a view rather than a paragraph in the README so that it sits in the
    same list as the numbers and cannot be scrolled past."""
    db = built(home, src, [user("u1", "hello")])
    unmeasured = rows(db, "SELECT * FROM dash_unmeasured")
    assert len(unmeasured) == 6
    statuses = {u["status"] for u in unmeasured}
    assert "UNMEASURED" in statuses and "NOT OBSERVABLE" in statuses
    questions = " ".join(u["question"] for u in unmeasured)
    assert "saved" in questions, "the tile a memory system is most tempted to fake"
    # The front page said "Injection cost is a cost and is shown as one" and no
    # page showed it — no hook records an injected token and no column holds one
    # — while this view, the one place a reader checks for what is *not* known,
    # did not mention it either. So the claim is gone, and the front page's
    # replacement promise ("Both are named in dash_unmeasured") is checked
    # against the view rather than trusted. [E6 review]
    assert "is shown as one" not in dashboard._DESCRIPTION
    for absent in ("saved", "injection cost"):
        assert absent in dashboard._DESCRIPTION, "the description still names it as missing"
        assert absent in questions, "named as missing on the front page, absent from the list"
    assert not any("injection" in t for t in dashboard._TABLES), "still no panel to describe"


def test_growth_is_absent_for_turns_with_no_readable_timestamp(home, src):
    """Absent, not zero, and not a NULL bucket: an agent that does not stamp its
    turns is invisible here rather than small."""
    db = built(home, src, [user("u1", "hello")])  # the fixture writes no `timestamp`
    assert rows(db, "SELECT * FROM dash_growth") == []


def test_growth_buckets_by_utc_day_and_drops_unreadable_stamps(home, src):
    """The non-vacuous half of the test above, which passed on an empty view.

    `strftime` returns NULL for anything SQLite cannot parse, so `WHERE day IS
    NOT NULL` filtered junk by accident — and the same test passed after the
    expression was changed to `substr(ts, 1, 10)`, which buckets `not-a-date`
    under `not-a-dat`. `datetime(ts)` is the explicit filter, and it normalises
    an offset to UTC on the way through. [E6 review]"""
    stamped = [
        user(uid, f"turn {uid}") | {"timestamp": ts}
        for uid, ts in [
            ("t1", "2026-09-20T23:00:00.000Z"),
            ("t2", "2026-09-20T23:59:59.000Z"),
            ("t3", "2026-09-21T00:30:00.000Z"),
            ("t4", "2026-09-21T02:00:00+05:00"),  # 21:00 UTC on the *20th*
            ("t5", "not-a-date"),
            ("t6", ""),
        ]
    ]
    db = built(home, src, stamped)
    assert rows(db, "SELECT COUNT(*) AS n FROM turns")[0]["n"] == 6, "precondition: all six stored"
    assert rows(db, "SELECT day, stored_turns FROM dash_growth") == [
        {"day": "2026-09-20", "stored_turns": 3},
        {"day": "2026-09-21", "stored_turns": 1},
    ], "UTC days, the offset converted rather than sliced, both junk stamps in neither"


def test_a_fork_bills_the_newest_generation_not_the_longest(home, src):
    """HIGH-1. `seq` restarts at 0 in every generation, so `MAX(seq)` picked
    whichever generation was *longer* — a compaction that drops turns leaves the
    superseded copy winning, and the page bills the corrected number away."""
    write(src, [billed("a1", "req-1", "hi", output_tokens=11), user("u9", "filler")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [billed("a1", "req-1", "hi", output_tokens=99)], mode="wb")  # shorter: a fork
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))

    gens = rows(db, "SELECT generation, seq FROM turns ORDER BY generation, seq")
    assert [g["generation"] for g in gens] == [0, 0, 1], "precondition: the store forked"
    assert max(t["seq"] for t in gens if t["generation"] == 0) > 0, (
        "precondition: the superseded generation is the one with the larger seq"
    )
    assert rows(db, "SELECT output_tokens FROM dash_requests") == [{"output_tokens": 99}]


def test_two_turns_of_one_request_bill_the_last_one_not_the_first(home, src):
    """HIGH-2. Within one generation the tie-break is `seq`, and usage is
    cumulative, so the last turn of a request is the running total. Grouping
    without an explicit order left it to insert order — true today by accident."""
    db = built(
        home,
        src,
        [
            billed("a1", "req-1", "thinking", input_tokens=5, output_tokens=10),
            billed("a2", "req-1", "done", input_tokens=5, output_tokens=40),
        ],
    )
    got = rows(db, "SELECT input_tokens, output_tokens FROM dash_requests")
    assert got == [{"input_tokens": 5, "output_tokens": 40}]


def test_a_subagent_file_does_not_double_bill_its_parents_request(home, src):
    """MEDIUM-1. A subagent transcript is a separate *session* — `session_id_for`
    is basename plus a digest of the path — but not a separate API call. Grouping
    by session billed one request once per file it appears in."""
    line = billed("a1", "req-1", "hi", input_tokens=100, output_tokens=20)
    write(src, [line])
    store.capture(src, "claude-code", "sess", home=home)
    store.capture(src, "claude-code", "sess-agent-7", home=home)  # the same call, two files
    index.build(home)
    db = index.open_db(index.db_path(home))

    sessions = rows(db, "SELECT DISTINCT session_id FROM turns ORDER BY 1")
    assert len(sessions) == 2, "precondition: two sessions really do hold this request"
    assert rows(db, "SELECT request, output_tokens FROM dash_requests") == [
        {"request": "req-1", "output_tokens": 20}
    ]
    assert rows(db, "SELECT SUM(output_tokens) AS n FROM dash_spend")[0]["n"] == 20


def test_usage_with_no_request_id_is_shown_as_unbilled_not_billed_or_dropped(home, src):
    """MEDIUM-2/3. Without a request id there is nothing to deduplicate on, and
    the usage is cumulative, so counting it is the 2.79x error and dropping it
    silently makes `dash_spend` disagree with the store by an invisible amount."""
    orphan = assistant("a1", [{"type": "text", "text": "no id"}])
    orphan["message"]["usage"] = usage(output_tokens=33)
    db = built(home, src, [orphan, billed("a2", "req-1", "billed", output_tokens=7)])

    assert rows(db, "SELECT SUM(output_tokens) AS n FROM dash_spend")[0]["n"] == 7
    assert rows(db, "SELECT reason, turns, output_tokens FROM dash_unbilled") == [
        {"reason": "no request id", "turns": 1, "output_tokens": 33}
    ]


def test_a_synthetic_turn_is_not_billed(home, src):
    """MEDIUM-3. `<synthetic>` is Claude Code's marker for a turn it produced
    without an API call — a cancelled request, an injected error. It carries a
    usage block and it cost nothing. `billable_usage` already excludes it."""
    fake = billed("a1", "req-1", "cancelled", output_tokens=5000)
    fake["message"]["model"] = "<synthetic>"
    db = built(home, src, [fake, billed("a2", "req-2", "real", output_tokens=8)])

    assert rows(db, "SELECT SUM(output_tokens) AS n FROM dash_spend")[0]["n"] == 8
    unbilled = rows(db, "SELECT reason, output_tokens FROM dash_unbilled")
    assert unbilled == [{"reason": "synthetic model", "output_tokens": 5000}]


def test_spend_plus_unbilled_accounts_for_every_usage_block(home, src):
    """The invariant that makes `dash_unbilled` worth having: nothing with a
    usage block is invisible. It is either billed or named as not billed."""
    fake = billed("a2", "req-2", "cancelled", output_tokens=5000)
    fake["message"]["model"] = "<synthetic>"
    orphan = assistant("a3", [{"type": "text", "text": "no id"}])
    orphan["message"]["usage"] = usage(output_tokens=33)
    db = built(home, src, [billed("a1", "req-1", "real", output_tokens=8), fake, orphan])

    with_usage = rows(db, "SELECT COUNT(*) AS n FROM turns WHERE usage <> '{}'")[0]["n"]
    requests = rows(db, "SELECT COUNT(*) AS n FROM dash_requests")[0]["n"]
    unbilled = rows(db, "SELECT COALESCE(SUM(turns), 0) AS n FROM dash_unbilled")[0]["n"]
    assert with_usage == 3
    # One turn per request in this fixture, which is what makes the two
    # countable against each other at all — `dash_requests` is per request.
    assert requests == 1
    assert requests + unbilled == with_usage


def test_contiguity_is_per_conversation_not_per_generation(home, src):
    write(src, [user("u1", "one"), user("u2", "two")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u1", "one")], mode="wb")  # shorter: a divergence, so a fork
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))

    contiguity = rows(db, "SELECT * FROM dash_contiguity")
    assert len(contiguity) == 1
    assert contiguity[0]["generations"] == 2


def test_a_fork_makes_the_stored_count_exceed_the_conversation(home, src):
    """MEDIUM-4. The column was called `turns`, and a fork replays turns, so the
    page reported a two-turn conversation as three — the store's size read as a
    conversation's length. `COUNT(DISTINCT turn_id)` would trade that for an
    under-count, because `turn_id` is content-derived and two byte-identical
    turns are one id. The count is right; the name was not."""
    write(src, [user("u1", "one"), user("u2", "two")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u1", "one")], mode="wb")  # shorter: a divergence, so a fork
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))

    assert rows(db, "SELECT stored_turns FROM dash_contiguity") == [{"stored_turns": 3}]
    assert rows(db, "SELECT stored_turns, stored_blocks FROM dash_corpus") == [
        {"stored_turns": 3, "stored_blocks": 3}
    ], "the conversation is two turns long; three is what the store holds"
    for view in ("dash_corpus", "dash_contiguity"):
        assert "stored_turns" in dashboard._TABLES[view], f"{view}: the caveat is in the caption"


# --------------------------------------------------------------------------- #
# the server
# --------------------------------------------------------------------------- #


def test_the_metadata_lands_on_the_database_it_was_written_for(home, src, monkeypatch):
    """Datasette keys descriptions by database *name*, and the name carries the
    schema version. A checked-in metadata file would stop matching on the next
    bump and the captions would vanish with no error at all.

    This used to compute the name itself and pass it to `metadata()`, which keys
    the dict by whatever it is handed — so it asserted `name in {name: ...}` and
    could not fail. The name that has to be right is the one `serve` derives from
    the path it gives Datasette, so the test reads both out of one call. Datasette
    names a database by its file stem. [E6 review]"""
    built(home, src, [user("u1", "hello")])
    seen = {}

    def fake_call(argv):
        with open(argv[argv.index("--metadata") + 1], encoding="utf-8") as fh:
            seen["meta"] = json.load(fh)
        seen["db"] = argv[argv.index("--immutable") + 1]
        return 0

    monkeypatch.setattr(dashboard.subprocess, "call", fake_call)
    dashboard.serve(home)

    stem = os.path.splitext(os.path.basename(seen["db"]))[0]
    assert stem == f"gitmemory-v{index.SCHEMA}", "precondition: the name carries the version"
    assert list(seen["meta"]["databases"]) == [stem]
    assert "dash_unmeasured" in seen["meta"]["databases"][stem]["tables"]
    # NIT-3: deleting `description_html` survived. It is the front page, and it
    # is where HIGH-3's claim lived, so it reaches Datasette or this fails.
    assert "saved" in seen["meta"]["description_html"]


def test_the_command_is_immutable_and_loopback(monkeypatch):
    # Pinned, not inherited: this asserted argv positions that shift when the
    # `uvx` prefix is present, so it was reading whichever branch the machine
    # happened to take. Both branches, explicitly. [E6 review]
    for present, head in [("datasette", "datasette"), ("uvx", "uvx")]:
        which = lambda n, p=present: f"/bin/{n}" if n == p else None  # noqa: E731
        monkeypatch.setattr(dashboard.shutil, "which", which)
        argv = dashboard.command("/tmp/x.db", metadata_path="/tmp/m.json")
        assert argv[0] == head, "precondition: this is the branch under test"
        assert argv[argv.index("--immutable") + 1] == "/tmp/x.db"
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("allow_download") + 1] == "off"
        assert argv[argv.index("--setting") + 1] == "allow_download"
        if head == "uvx":
            # Against a version constraint, not against `UVX_SPEC` itself: the
            # first draft asserted `argv[1] == dashboard.UVX_SPEC`, which is true
            # for any value the constant takes, including an unpinned one.
            assert argv[1] == dashboard.UVX_SPEC
            assert any(c in argv[1] for c in "<=>"), (
                f"{argv[1]!r} resolves against PyPI at run time and installs whatever is current"
            )
            assert "datasette" not in argv[2:], "the executable name is not passed twice"


def test_neither_datasette_nor_uvx_is_an_error_not_an_install(monkeypatch):
    """A read-only command that quietly downloads a web framework is a surprise
    nobody asked for. It says what to run instead."""
    monkeypatch.setattr(dashboard.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="datasette"):
        dashboard.command("/tmp/x.db", metadata_path="/tmp/m.json")


def test_uvx_is_the_fallback_not_the_default(monkeypatch):
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: f"/bin/{name}")
    assert dashboard.command("/tmp/x.db", metadata_path="/tmp/m.json")[0] == "datasette"
    monkeypatch.setattr(
        dashboard.shutil, "which", lambda name: None if name == "datasette" else "/bin/uvx"
    )
    assert dashboard.command("/tmp/x.db", metadata_path="/tmp/m.json")[0] == "uvx"


def test_serving_a_store_with_no_index_says_so(home):
    with pytest.raises(FileNotFoundError, match="gitmemory index"):
        dashboard.serve(home)


def test_a_bind_outside_loopback_warns_and_a_loopback_alias_does_not(capsys, home, src):
    """MEDIUM-8. The warning tested the default value, not the set, so `--host
    localhost` printed "serving the store on localhost, not loopback" — which is
    false, and a false warning is how a true one stops being read."""
    built(home, src, [user("u1", "hello")])
    for host, expect in [("localhost", False), ("::1", False), ("0.0.0.0", True)]:  # noqa: S104
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(dashboard.subprocess, "call", lambda _argv: 0)
            assert main(["--home", home, "dashboard", "--host", host]) == 0
        warned = "not loopback" in capsys.readouterr().err
        assert warned is expect, f"--host {host}"


def test_the_start_up_line_names_the_build_being_served(capsys, home, src, monkeypatch):
    """LOW-1. Datasette holds the file open and `--immutable` lets SQLite cache
    it, while `index.build` renames a fresh file over the path — so a dashboard
    left running across a rebuild serves the old inode with no sign of it. The
    digest does not fix that; it makes it checkable against `gitmemory index`."""
    built(home, src, [user("u1", "hello")])
    monkeypatch.setattr(dashboard.subprocess, "call", lambda _argv: 0)
    dashboard.serve(home)

    db = index.open_db(index.db_path(home))
    digest = rows(db, "SELECT value FROM meta WHERE key = 'content_sha256'")[0]["value"]
    assert digest and digest != "unknown", "precondition: the index really has a digest"
    assert digest[:12] in capsys.readouterr().out


def test_the_start_up_line_survives_an_index_it_cannot_read(tmp_path, capsys, monkeypatch):
    """A caption must not be the thing that stops the server starting."""
    db = tmp_path / f"gitmemory-v{index.SCHEMA}.db"
    db.write_bytes(b"not a database")
    monkeypatch.setattr(dashboard.subprocess, "call", lambda _argv: 0)
    assert dashboard.serve(db=str(db)) == 0
    assert "unknown" in capsys.readouterr().out


def test_serve_hands_datasette_a_metadata_file_that_exists(home, src, monkeypatch):
    """The metadata is written to a temp directory that goes away with the
    process. If the file is gone by the time Datasette reads it, the captions
    are silently absent — so the test opens it from inside the call."""
    built(home, src, [user("u1", "hello")])
    seen = {}

    def fake_call(argv):
        path = argv[argv.index("--metadata") + 1]
        with open(path, encoding="utf-8") as fh:
            seen["meta"] = json.load(fh)
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(dashboard.subprocess, "call", fake_call)
    assert dashboard.serve(home, port=9999) == 0
    assert seen["meta"]["title"] == "gitmemory"
    assert seen["argv"][seen["argv"].index("--port") + 1] == "9999"
