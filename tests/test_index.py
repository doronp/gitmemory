"""E3: the retrieval index.

The gate for this epoch is a measured recall number, which `bench/` produces.
These tests cover the thing a benchmark cannot: that the index is *correct* —
that an offset points where it says, that a fork keeps both histories, that a
crash mid-build never replaces a working index, and that a query is data rather
than a second query language. A retriever that scores well on a benchmark and
hands back offsets nobody can seek to is worse than no retriever.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from gitmemory import index, store
from gitmemory.__main__ import main


def write(path: str, lines: list[dict], mode: str = "ab") -> int:
    """Append JSONL lines. Returns the new size."""
    with open(path, mode) as fh:
        for obj in lines:
            fh.write(json.dumps(obj).encode() + b"\n")
    return os.path.getsize(path)


def user(uid: str, text: str) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "sessionId": "s1",
        "message": {"role": "user", "content": text},
    }


def assistant(uid: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "sessionId": "s1",
        "message": {"role": "assistant", "model": "claude-sonnet-4-5", "content": blocks},
    }


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


def built(home: str, src: str, lines: list[dict], **kw):
    """Write, capture, index, and hand back an open database."""
    write(src, lines)
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home, **kw)
    return index.open_db(index.db_path(home))


# --------------------------------------------------------------------------- #
# the offset is the contract
# --------------------------------------------------------------------------- #


def test_a_word_in_a_turn_finds_that_turn(home, src):
    db = built(home, src, [user("u1", "the lock is held across the await"), user("u2", "hello")])
    hits = index.search(db, "await")
    assert [h.role for h in hits] == ["user"]
    assert "held across the await" in hits[0].text


def test_the_offset_a_hit_carries_is_where_the_turn_starts(home, src):
    """The whole system agrees on byte offsets. If seeking there lands on the
    wrong line, every other layer — bench, dashboard, recall — is lying."""
    db = built(home, src, [user("u1", "first"), user("u2", "the peculiar marmoset")])
    hit = index.search(db, "marmoset")[0]
    with open(src, "rb") as fh:
        fh.seek(hit.byte_offset)
        line = json.loads(fh.read(hit.byte_len))
    assert line["uuid"] == "u2"


def test_an_offset_stays_absolute_across_a_segment_boundary(home, src):
    """Two captures make two segments. A turn in the second is at its offset in
    the *file*, not in the segment — the concatenation is the transcript."""
    first = write(src, [user("u1", "early")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u2", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    assert len(store.sessions(home)[0].segments) == 2
    index.build(home)
    db = index.open_db(index.db_path(home))
    hit = index.search(db, "marmoset")[0]
    assert hit.byte_offset == first
    with open(src, "rb") as fh:
        fh.seek(hit.byte_offset)
        assert json.loads(fh.read(hit.byte_len))["uuid"] == "u2"


def test_a_turn_is_returned_once_however_many_of_its_blocks_match(home, src):
    db = built(
        home,
        src,
        [
            assistant(
                "a1",
                [
                    {"type": "text", "text": "marmoset"},
                    {"type": "thinking", "thinking": "marmoset again"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "marmoset"}},
                ],
            )
        ],
    )
    assert len(index.search(db, "marmoset")) == 1


# --------------------------------------------------------------------------- #
# the column split has to earn its complexity
# --------------------------------------------------------------------------- #


def test_prose_outranks_tool_output_for_the_same_word(home, src):
    db = built(
        home,
        src,
        [
            assistant("a1", [{"type": "tool_result", "tool_use_id": "t", "content": "marmoset"}]),
            assistant("a2", [{"type": "text", "text": "marmoset"}]),
        ],
    )
    assert [h.kind for h in index.search(db, "marmoset")] == ["text", "tool_result"]


def test_a_tool_argument_outranks_tool_output(home, src):
    """The fourth column, the one DESIGN did not name. A `tool_use` block is the
    command that was run; burying it with the output it produced is the exact
    mistake the prose/tool split was invented to avoid."""
    db = built(
        home,
        src,
        [
            assistant("a1", [{"type": "tool_result", "tool_use_id": "t", "content": "marmoset"}]),
            assistant(
                "a2",
                [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"cmd": "marmoset"}}],
            ),
        ],
    )
    assert [h.kind for h in index.search(db, "marmoset")] == ["tool_use", "tool_result"]


def test_weights_are_what_decides_the_order(home, src):
    """Not insertion order, not kind: the numbers. Both directions are asserted
    on purpose — with the two rows this short their unweighted scores tie, and a
    tie resolves to the byte order, which happens to look like the inverted
    answer. Checking only the inversion passes against weights wired to nothing.
    """
    lines = [
        assistant("a1", [{"type": "tool_result", "tool_use_id": "t", "content": "marmoset"}]),
        assistant("a2", [{"type": "text", "text": "marmoset"}]),
    ]
    db = built(home, src, lines)
    inverted = index.Weights(prose=1.0, tool_use=1.0, tool_result=9.0, paths=1.0)
    assert [h.kind for h in index.search(db, "marmoset")] == ["text", "tool_result"]
    assert [h.kind for h in index.search(db, "marmoset", weights=inverted)] == [
        "tool_result",
        "text",
    ]


def test_a_path_in_prose_lands_in_the_paths_column_too(home, src):
    """Not "is it findable" — the word is in the prose column either way, so
    that assertion passes with the `paths` column switched off entirely. What
    the column buys is a *second* weighted hit for the same token, which is how
    a file mention outranks an incidental one."""
    db = built(home, src, [user("u1", "look at src/gitmemory/marmoset.py for the parser")])
    assert db.execute("SELECT paths FROM blocks").fetchone()["paths"] == "src/gitmemory/marmoset.py"
    assert index.search(db, "marmoset.py")


def test_a_bare_filename_in_a_tool_argument_lands_in_the_paths_column(home, src):
    """`Read(file_path="README.md")` names a file with no separator in it, so
    the path regex cannot see it; only the argument key can. A path *with*
    separators would be found either way, which is why this fixture has none."""
    db = built(
        home,
        src,
        [
            assistant(
                "a1",
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            )
        ],
    )
    row = db.execute("SELECT paths FROM blocks WHERE kind = 'tool_use'").fetchone()
    assert row["paths"] == "README.md"


# --------------------------------------------------------------------------- #
# a query is data, not a second query language
# --------------------------------------------------------------------------- #


def test_a_wildcard_in_a_query_is_a_word_not_a_prefix_scan(home, src):
    """`hel*` unhandled is an FTS5 prefix query over the whole corpus. What
    stops it is tokenising to `\\w+` — the star is not a word character, so it
    never reaches MATCH. Quoting is a second, independent guard; see below."""
    db = built(home, src, [user("u1", "hello marmoset")])
    assert index.search(db, "marmoset")
    assert index.search(db, "hel*") == []


def test_a_hyphenated_query_matches_either_half(home, src):
    """`\\w+` splits on punctuation, so `tool-use` asks for `tool` OR `use`.
    Splitting on whitespace instead would quote `"tool-use"` as a *phrase*,
    which FTS5 only matches when both words are adjacent — so a query with a
    hyphen, a slash or a dot in it would silently stop matching."""
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    assert index.search(db, "aardvark-marmoset")
    assert index.search(db, "aardvark/marmoset")


def test_a_bareword_operator_in_a_query_is_searched_for_not_obeyed(home, src):
    """`AND`, `OR`, `NOT` and `NEAR` survive `\\w+` tokenising intact, so
    tokenising alone does not make a query safe: `find the NOT branch` becomes
    `find OR the OR NOT OR branch`, which is an FTS5 syntax error, and one the
    user cannot see or avoid. Quoting each term is what closes it."""
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    assert index.search(db, "NOT marmoset")
    assert index.search(db, "marmoset AND OR NEAR NOT")


def test_an_operator_in_a_query_cannot_reach_the_parser(home, src):
    db = built(home, src, [user("u1", "hello marmoset")])
    for hostile in ['marmoset" OR fts MATCH "', "NEAR(marmoset hello, 2)", 'unbalanced "', "^a"]:
        index.search(db, hostile)  # must not raise


def test_a_query_with_no_indexable_token_returns_nothing(home, src):
    db = built(home, src, [user("u1", "hello marmoset")])
    assert index.search(db, "!!! ??? ...") == []


def test_a_query_is_or_not_and(home, src):
    """FTS5's implicit operator is AND, and AND is not BM25: a five-word
    question that misses on one word should rank lower, not disappear."""
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    assert index.search(db, "marmoset aardvark")


def test_an_over_long_query_is_truncated_loudly(home, src):
    db = built(home, src, [user("u1", "marmoset")])
    with pytest.warns(UserWarning, match="truncated"):
        index.search(db, " ".join(["marmoset"] * (index.MAX_TERMS + 5)))


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #


def test_two_builds_of_one_store_index_the_same_content(home, src):
    write(src, [user("u1", "marmoset"), assistant("a1", [{"type": "text", "text": "yes"}])])
    store.capture(src, "claude-code", "sess", home=home)
    assert index.build(home).content_sha256 == index.build(home).content_sha256


def test_both_generations_of_a_forked_session_are_indexed(home, src):
    """A rewrite seals one byte history and starts another. Indexing only the
    live one throws away the copy that exists precisely because it was lost."""
    write(src, [user("u1", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u2", "a replacement aardvark")], mode="wb")
    cap = store.capture(src, "claude-code", "sess", home=home)
    assert cap.generation == 1
    db = built(home, src, [])
    assert index.search(db, "marmoset")[0].generation == 0
    assert index.search(db, "aardvark")[0].generation == 1


def test_an_unparseable_generation_costs_only_itself(home, src, tmp_path):
    other = tmp_path / "other.jsonl"
    write(str(other), [user("u9", "the peculiar marmoset")])
    store.capture(str(other), "claude-code", "good", home=home)
    write(src, [user("u1", "an aardvark")])
    store.capture(src, "claude-code", "bad", home=home)
    man = Path(home, "sessions", "claude-code", "bad", "g00.json")
    man.write_text(json.dumps(json.loads(man.read_text()) | {"agent": "martian"}))

    stats = index.build(home)
    assert stats.generations == 1
    assert len(stats.skipped) == 1 and "martian" in stats.skipped[0]
    db = index.open_db(index.db_path(home))
    assert index.search(db, "marmoset")


def test_a_failed_build_never_replaces_a_working_index(home, src, monkeypatch):
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    db.close()
    monkeypatch.setattr(index, "_fill", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        index.build(home)
    assert index.search(index.open_db(index.db_path(home)), "marmoset")
    # And no half-built database is left behind to be found later.
    assert not [p for p in os.listdir(os.path.dirname(index.db_path(home))) if ".building-" in p]


def test_an_empty_store_builds_an_index_that_answers_nothing(home):
    assert index.build(home).blocks == 0
    assert index.search(index.open_db(index.db_path(home)), "marmoset") == []


def test_the_index_never_outranks_the_raw_it_came_from(home, src):
    """Sanity, and a guard against the index quietly becoming authoritative:
    every row must name a generation the store still has."""
    db = built(home, src, [user("u1", "marmoset")])
    keys = {s.key for s in store.sessions(home)}
    assert {r["session_key"] for r in db.execute("SELECT DISTINCT session_key FROM blocks")} <= keys


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def test_cli_index_then_recall(home, src, capsys):
    write(src, [user("u1", "the peculiar marmoset")])
    assert main(["--home", home, "capture", src]) == 0
    capsys.readouterr()
    assert main(["--home", home, "index"]) == 0
    assert "1 turn(s)" in capsys.readouterr().out
    assert main(["--home", home, "recall", "marmoset"]) == 0
    assert "marmoset" in capsys.readouterr().out


def test_cli_recall_without_an_index_says_so(home, capsys):
    assert main(["--home", home, "recall", "marmoset"]) == 2
    assert "run `gitmemory index`" in capsys.readouterr().err


def test_cli_recall_survives_a_corrupt_database(home, src, capsys):
    """A truncated database is an ordinary state after a full disk. It must
    report, not traceback — `main` catching sqlite3.Error is what does that."""
    built(home, src, [user("u1", "marmoset")]).close()
    Path(index.db_path(home)).write_bytes(b"not a database")
    assert main(["--home", home, "recall", "marmoset"]) == 2
    assert "error:" in capsys.readouterr().err


def test_search_rejects_nothing_it_can_reach_the_database_with(home, src):
    """`sqlite3.Error` in `main`'s handler is load-bearing only if something can
    actually raise it; this is that something, pinned."""
    db = built(home, src, [user("u1", "marmoset")])
    db.execute("DROP TABLE fts")
    with pytest.raises(sqlite3.Error):
        index.search(db, "marmoset")
