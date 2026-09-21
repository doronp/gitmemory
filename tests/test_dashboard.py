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
    """900 of 1000 input tokens served from cache is 90%, and the denominator is
    everything that went in — not the output, which was never cacheable."""
    db = built(
        home,
        src,
        [billed("a1", "r1", "x", input_tokens=100, cache_read_input_tokens=900, output_tokens=500)],
    )
    assert rows(db, "SELECT cache_read_pct FROM dash_spend")[0]["cache_read_pct"] == 90.0


def test_a_model_with_no_usage_at_all_does_not_divide_by_zero(home, src):
    """A transcript with no usage blocks is the normal case for a non-Claude
    adapter, and `NULLIF` is the only thing standing between it and a crash on
    the page."""
    db = built(home, src, [user("u1", "hello")])
    assert rows(db, "SELECT * FROM dash_spend") == []
    assert rows(db, "SELECT * FROM dash_requests") == []


def test_the_unmeasured_panel_names_what_is_not_known(home, src):
    """It is a view rather than a paragraph in the README so that it sits in the
    same list as the numbers and cannot be scrolled past."""
    db = built(home, src, [user("u1", "hello")])
    unmeasured = rows(db, "SELECT * FROM dash_unmeasured")
    assert len(unmeasured) == 5
    statuses = {u["status"] for u in unmeasured}
    assert "UNMEASURED" in statuses and "NOT OBSERVABLE" in statuses
    questions = " ".join(u["question"] for u in unmeasured)
    assert "saved" in questions, "the tile a memory system is most tempted to fake"


def test_growth_counts_days_not_turns_without_timestamps(home, src):
    db = built(home, src, [user("u1", "hello")])
    # The fixture writes no `timestamp`, so there is nothing to group by and the
    # view must be empty rather than bucketing everything under NULL.
    assert rows(db, "SELECT * FROM dash_growth") == []


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


# --------------------------------------------------------------------------- #
# the server
# --------------------------------------------------------------------------- #


def test_the_metadata_lands_on_the_database_it_was_written_for():
    """Datasette keys descriptions by database *name*, and the name carries the
    schema version. A checked-in metadata file would stop matching on the next
    bump and the captions would vanish with no error at all."""
    name = os.path.splitext(os.path.basename(index.db_path("/nowhere")))[0]
    meta = dashboard.metadata(name)
    assert name in meta["databases"]
    assert "dash_unmeasured" in meta["databases"][name]["tables"]


def test_the_command_is_immutable_and_loopback():
    argv = dashboard.command("/tmp/x.db", metadata_path="/tmp/m.json")
    assert "--immutable" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--immutable") + 1] == "/tmp/x.db"


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
