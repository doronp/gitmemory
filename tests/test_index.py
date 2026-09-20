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
import time
import unicodedata
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


def seek(src: str, hit) -> bytes:
    """The exact bytes a hit claims, no more and no less.

    `json.loads` here would be a hole: `jsonl` yields the span *without* its
    trailing newline, so `read(byte_len + 1)` swallows the `\\n` as trailing
    whitespace and parses clean. A test that compares parsed objects passes
    against an off-by-one in `byte_len`; comparing bytes does not. [E3]
    """
    with open(src, "rb") as fh:
        fh.seek(hit.byte_offset)
        return fh.read(hit.byte_len)


def test_the_offset_a_hit_carries_is_where_the_turn_starts(home, src):
    """The whole system agrees on byte offsets. If seeking there lands on the
    wrong line, every other layer — bench, dashboard, recall — is lying.

    Both turns are checked, and the first one is the point: its offset is 0, and
    a mutation to `turn.byte_offset or 1` is invisible against any fixture whose
    only target sits further in. Zero is the offset most likely to be special-
    cased by accident and the one no other test in this file exercises.
    """
    db = built(home, src, [user("u1", "the first aardvark"), user("u2", "the peculiar marmoset")])
    first, second = index.search(db, "aardvark")[0], index.search(db, "marmoset")[0]
    assert first.byte_offset == 0, "the opening turn starts at byte zero"
    raw = Path(src).read_bytes()
    assert seek(src, first) == raw.split(b"\n")[0]
    assert seek(src, second) == raw.split(b"\n")[1]
    assert json.loads(seek(src, second))["uuid"] == "u2"


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
    assert seek(src, hit) == Path(src).read_bytes().split(b"\n")[1]


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
    hits = index.search(db, "marmoset")
    assert len(hits) == 1
    # *Which* block speaks for the turn is the other half. Asserting only the
    # count passed with the aggregate flipped to MAX — the turn then described
    # itself by its worst-matching block, and every field on the Hit came from
    # the wrong row. Three blocks, three different scores: the one-word `text`
    # block is the tightest prose match, `thinking` is the same column diluted
    # by a second token, and `tool_use` carries a lower weight. [E3]
    assert hits[0].kind == "text"
    assert hits[0].text == "marmoset"


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
    hits = index.search(db, "marmoset")
    assert [h.kind for h in hits] == ["tool_use", "tool_result"]
    # `Hit.text` concatenates the three text columns, and `kind` alone does not
    # prove the right one carries content: with `tool_use` dropped from the
    # concat the ordering assertion above still passed and every tool-call hit
    # came back with an empty body. [E3]
    assert "marmoset" in hits[0].text


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


def test_the_paths_weight_changes_the_answer_not_just_the_column(home, src):
    """Both turns say the same words; only one says them as a path. The `paths`
    weight is the whole difference, so turning it off has to flip the order —
    with the column wired to nothing, both readings scored identically and the
    tie fell to byte order, which is the plainly-worded turn."""
    db = built(
        home,
        src,
        [
            user("u1", "check the src gitmemory marmoset py parser"),
            user("u2", "check the src/gitmemory/marmoset.py parser"),
        ],
    )
    off = index.Weights(prose=4.0, tool_use=2.0, tool_result=1.0, paths=0.0)
    assert [h.byte_offset != 0 for h in index.search(db, "marmoset")][0], "path mention first"
    assert index.search(db, "marmoset", weights=off)[0].byte_offset == 0


def test_equal_scoring_turns_come_back_in_byte_order(home, src):
    """Ranking is deterministic below the score. Six turns that score identically
    are ordered by where they are in the transcript — not by `turn_id`, which is
    a content hash and therefore arbitrary, and which is exactly the order the
    window partition leaves rows in when the final ORDER BY is dropped. [E3]"""
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    db = built(home, src, [user(f"u{i}", f"marmoset {w}") for i, w in enumerate(words)])
    hits = index.search(db, "marmoset")
    assert len(hits) == 6
    assert [h.byte_offset for h in hits] == sorted(h.byte_offset for h in hits)
    assert len({h.score for h in hits}) == 1, "the fixture must tie, or this tests nothing"


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


def test_a_decomposed_query_finds_the_word_it_spells(home, src):
    """macOS hands out NFD filenames as a matter of course, so a decomposed query
    is the ordinary case. `unicode61 remove_diacritics 2` folds the mark either
    way, but a combining mark is category Mn and `\\w` does not match it, so
    without NFC the term shattered into `"nai" OR "ve"` — missing the word and
    hitting anything containing "nai" instead. Both halves are asserted. [E3]"""
    db = built(home, src, [user("u1", "a naive marmoset"), user("u2", "nai gong")])
    nfd = unicodedata.normalize("NFD", "naïve")
    assert len(nfd) == 6, "the fixture must actually be decomposed"
    assert [h.byte_offset for h in index.search(db, nfd)] == [0]


def test_an_identifier_is_a_phrase_and_a_hyphenation_is_an_or(home, src):
    """`_` is a `\\w` character and `-` is not, so `parse_manifest` survives as one
    term and quotes to an FTS5 *phrase* (the two tokens, adjacent), while
    `parse-manifest` splits into an OR. Identifiers want precision and
    hyphenated words want recall; this is the right way round for a transcript
    full of code. Both directions, or the split proves nothing."""
    db = built(
        home,
        src,
        [user("u1", "call parse_manifest here"), user("u2", "we parse the manifest later")],
    )
    assert [h.byte_offset for h in index.search(db, "parse_manifest")] == [0]
    assert len(index.search(db, "parse-manifest")) == 2


def test_a_negative_k_returns_nothing_rather_than_the_corpus(home, src):
    """SQLite reads `LIMIT -1` as unbounded, so a caller's off-by-one asking for
    `k-1` results handed back every turn in the store, each with its full text.
    `max(k, 0)` is the clamp; zero results is the honest answer to "give me
    fewer than none". [E3]"""
    db = built(home, src, [user(f"u{i}", "marmoset") for i in range(5)])
    assert len(index.search(db, "marmoset", k=2)) == 2
    assert index.search(db, "marmoset", k=0) == []
    assert index.search(db, "marmoset", k=-1) == []


def test_an_over_long_query_is_truncated_loudly(home, src):
    """Loud *and* actually truncated.

    Warning on its own is half the contract: with the slice dropped the query
    still warned and still searched all 69 terms, so the cap that exists to stop
    a thousand-term OR from scanning the table was doing nothing. The matching
    word sits past the cap, so a hit here means nothing was dropped. [E3]
    """
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    over = ["aardvark"] * index.MAX_TERMS + ["marmoset"]
    with pytest.warns(UserWarning, match="truncated"):
        assert index.search(db, " ".join(over)) == []
    assert index.search(db, "marmoset"), "the fixture is findable when nothing is dropped"


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #


def test_two_builds_of_one_store_index_the_same_content(home, src, tmp_path):
    """Stable across rebuilds *and* sensitive to what changed.

    Equality alone is `X == X`: it holds with the digest fed an empty string or
    a constant, which is what the audit found. The second half is what makes the
    first half mean anything — one different word, one different hash. [E3]
    """
    write(src, [user("u1", "marmoset"), assistant("a1", [{"type": "text", "text": "yes"}])])
    store.capture(src, "claude-code", "sess", home=home)
    stable = index.build(home).content_sha256
    assert stable == index.build(home).content_sha256

    other_home, other_src = str(tmp_path / "h2"), str(tmp_path / "s2.jsonl")
    os.mkdir(other_home)
    write(other_src, [user("u1", "aardvark"), assistant("a1", [{"type": "text", "text": "yes"}])])
    store.capture(other_src, "claude-code", "sess", home=other_home)
    assert index.build(other_home).content_sha256 != stable


def test_a_generation_that_could_not_be_parsed_changes_the_digest(home, src, tmp_path):
    """A skip is part of what the index *is*. Left out of the digest, a whole
    store and a store missing a generation compared equal — which is the one
    question this hash exists to answer. [E3]"""
    write(src, [user("u1", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    whole = index.build(home).content_sha256

    bad = tmp_path / "bad.jsonl"
    write(str(bad), [user("u9", "an aardvark")])
    store.capture(str(bad), "claude-code", "bad", home=home)
    man = Path(home, "sessions", "claude-code", "bad", "g00.json")
    man.write_text(json.dumps(json.loads(man.read_text()) | {"agent": "martian"}))

    stats = index.build(home)
    assert len(stats.skipped) == 1
    assert stats.content_sha256 != whole, "a skipped generation left no trace in the digest"


def test_every_block_of_every_turn_is_counted(home, src):
    """`Stats.blocks` was asserted only as `== 0` on an empty store, so
    `blocks += 0` was invisible. The count is what the CLI prints. [E3]"""
    write(src, [user("u1", "one"), assistant("a1", [{"type": "text", "text": "two"}])])
    store.capture(src, "claude-code", "sess", home=home)
    stats = index.build(home)
    assert (stats.generations, stats.turns, stats.blocks) == (1, 2, 2)


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


def test_a_malformed_tool_call_costs_nothing(home, src, tmp_path):
    """`input` is a value the model wrote, so a string where an object belongs is
    ordinary malformed output, not corruption. It used to raise `AttributeError`
    from inside the row loop — outside the per-generation guard — and take the
    whole store's index down with it. Both generations must survive, and the
    malformed one is still searchable; only its `paths` column is empty. [E3]"""
    good = tmp_path / "good.jsonl"
    write(str(good), [user("u9", "the peculiar marmoset")])
    store.capture(str(good), "claude-code", "good", home=home)
    write(
        src,
        [
            assistant(
                "a1",
                [{"type": "tool_use", "id": "t1", "name": "Read", "input": "an aardvark"}],
            )
        ],
    )
    store.capture(src, "claude-code", "bad", home=home)

    stats = index.build(home)
    assert (stats.generations, stats.skipped) == (2, ())
    db = index.open_db(index.db_path(home))
    assert index.search(db, "marmoset") and index.search(db, "aardvark")
    assert db.execute("SELECT paths FROM blocks WHERE kind='tool_use'").fetchone()["paths"] == ""


def test_a_row_that_cannot_be_built_costs_only_its_generation(home, src, tmp_path, monkeypatch):
    """The guard wraps the parse *and* the rows built from it, and the two known
    ways a row used to blow up are both fixed at source now — so this forces the
    failure rather than waiting for the next one. `_row` reads block text, tool
    names and timestamps, all of them transcript content; a per-block failure
    must not cost the store every other generation. [E3]"""
    good = tmp_path / "good.jsonl"
    write(str(good), [user("u9", "the peculiar marmoset")])
    store.capture(str(good), "claude-code", "good", home=home)
    write(src, [user("u1", "an aardvark")])
    store.capture(src, "claude-code", "bad", home=home)

    real = index._row

    def explode(stored, turn, block):
        if stored.session_id == "bad":
            raise RuntimeError("no row for you")
        return real(stored, turn, block)

    monkeypatch.setattr(index, "_row", explode)
    stats = index.build(home)
    assert stats.generations == 1
    assert len(stats.skipped) == 1 and "no row for you" in stats.skipped[0]
    assert index.search(index.open_db(index.db_path(home)), "marmoset")


def test_a_lone_surrogate_costs_nothing(home, src, tmp_path):
    """`jsonl` decodes with `surrogateescape` and `records` hashes with
    `surrogatepass` precisely so a transcript that caught a binary `cat` keeps
    those bytes. sqlite3 encodes strict UTF-8 and raised on the first one — from
    the row loop again, so one bad byte in one turn cost the entire store its
    index. The derived copy loses a character; raw still has it. [E3]"""
    good = tmp_path / "good.jsonl"
    write(str(good), [user("u9", "the peculiar marmoset")])
    store.capture(str(good), "claude-code", "good", home=home)
    write(src, [user("u1", "an aardvark \ud800 here")])
    store.capture(src, "claude-code", "bad", home=home)

    stats = index.build(home)
    assert (stats.generations, stats.skipped) == (2, ())
    db = index.open_db(index.db_path(home))
    assert index.search(db, "marmoset") and index.search(db, "aardvark")


def test_a_separator_free_megabyte_does_not_hang_the_build(home, src):
    """A JWT, a hex digest, a base64url blob — a long run of word characters with
    no separator is what tool output is full of, and the unbounded path regex
    rescanned it from every start position. 32 KB took 3.4 s, 200 KB hung for
    minutes. The bound makes it linear; this fails long before it finishes if
    the bound is removed. [E3]"""
    blob = "A1_z9" * 40_000  # 200 KB, no separator anywhere
    write(src, [assistant("a1", [{"type": "tool_result", "tool_use_id": "t", "content": blob}])])
    store.capture(src, "claude-code", "sess", home=home)
    started = time.monotonic()
    assert index.build(home).blocks == 1
    assert time.monotonic() - started < 10, "the path regex went quadratic again"


def test_two_sessions_holding_the_same_turn_both_come_back(home, src, tmp_path):
    """`turn_id` is content-derived over the record's `sessionId`, which the
    adapter itself warns is reused across a fork — so two *store* sessions can
    hold turns with the same id. Grouping on `turn_id` alone returned one of
    them and silently under-delivered `k`. Two sessions are two places to go
    look. [E3]"""
    other = tmp_path / "other.jsonl"
    write(src, [user("u1", "the peculiar marmoset")])
    write(str(other), [user("u1", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sessa", home=home)
    store.capture(str(other), "claude-code", "sessb", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))
    hits = index.search(db, "marmoset")
    assert len({h.turn_id for h in hits}) == 1, "the fixture must collide, or this tests nothing"
    assert [h.session_id for h in hits] == ["sessa", "sessb"]


def test_a_turn_carried_across_a_fork_comes_back_once_from_the_newest(home, src):
    """A rewrite copies most turns forward unchanged. Returning one per
    generation spends the budget re-reading the same text, so the partition
    collapses them — and the winner is the newest, because its offsets are the
    ones that still address the live file. [E3]"""
    write(src, [user("u1", "the peculiar marmoset"), user("u2", "first tail")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u1", "the peculiar marmoset"), user("u3", "second tail")], mode="wb")
    assert store.capture(src, "claude-code", "sess", home=home).generation == 1
    index.build(home)
    db = index.open_db(index.db_path(home))
    assert (
        db.execute("SELECT COUNT(*) c FROM blocks WHERE prose LIKE '%marmoset%'").fetchone()["c"]
        == 2
    ), "both generations must hold the turn, or this tests nothing"
    hits = index.search(db, "marmoset")
    assert [(h.generation, h.byte_offset) for h in hits] == [(1, 0)]


def test_a_partial_index_left_by_a_kill_is_swept(home, src):
    """`except BaseException` cannot cover SIGKILL, and three killed builds left
    three partial databases plus journals next to the real one forever. The
    store reaps orphans in `raw/`; `index/` had no equivalent. [E3]"""
    built(home, src, [user("u1", "marmoset")]).close()
    junk = Path(os.path.dirname(index.db_path(home)), ".building-abc123.db")
    junk.write_bytes(b"leftover")
    index.build(home)
    assert not junk.exists()
    assert index.search(index.open_db(index.db_path(home)), "marmoset")


def test_an_index_from_another_schema_is_refused_not_answered(home, src, tmp_path):
    """`db_path` versions the filename, so the default path cannot collide —
    `--db` bypasses it, and a stale database answered from the old schema in
    silence. A wrong answer from a retrieval index looks exactly like a right
    one. [E3]"""
    other = str(tmp_path / "out.db")
    index.build(home, path=other)
    db = index.open_db(other)
    assert index.search(db, "marmoset") == []
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    with pytest.raises(ValueError, match="schema 99"):
        index.search(db, "marmoset")
    with pytest.raises(ValueError, match="not a gitmemory index"):
        index.search(index.open_db(str(tmp_path / "empty.db")), "marmoset")


def test_a_bare_filename_is_a_usable_db_path(home, src, tmp_path, monkeypatch, capsys):
    """`--db out.db` has no dirname, and `makedirs("")` raises ENOENT — on the
    most obvious value anyone would pass to a flag documented as "database
    path". [E3]"""
    write(src, [user("u1", "the peculiar marmoset")])
    assert main(["--home", home, "capture", src]) == 0
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert main(["--home", home, "index", "--db", "out.db"]) == 0
    assert index.search(index.open_db(str(tmp_path / "out.db")), "marmoset")


def test_a_failed_build_never_replaces_a_working_index(home, src, monkeypatch):
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    db.close()
    monkeypatch.setattr(index, "_fill", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        index.build(home)
    assert index.search(index.open_db(index.db_path(home)), "marmoset")
    # And no half-built database is left behind to be found later.
    assert not [p for p in os.listdir(os.path.dirname(index.db_path(home))) if ".building-" in p]


def test_an_interrupted_build_also_leaves_nothing_behind(home, src, monkeypatch):
    """`except Exception` would not have caught this. Ctrl-C during a rebuild is
    the single most likely way to interrupt one, and it is a `BaseException`."""
    built(home, src, [user("u1", "the peculiar marmoset")]).close()

    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(index, "_fill", interrupt)
    with pytest.raises(KeyboardInterrupt):
        index.build(home)
    assert not [p for p in os.listdir(os.path.dirname(index.db_path(home))) if ".building-" in p]
    assert index.search(index.open_db(index.db_path(home)), "marmoset")


def test_an_empty_store_builds_an_index_that_answers_nothing(home):
    assert index.build(home).blocks == 0
    assert index.search(index.open_db(index.db_path(home)), "marmoset") == []


def test_the_index_never_outranks_the_raw_it_came_from(home, src):
    """Sanity, and a guard against the index quietly becoming authoritative:
    every row must name a generation the store still has."""
    db = built(home, src, [user("u1", "marmoset")])
    keys = {s.key for s in store.sessions(home)}
    rows = {r["session_key"] for r in db.execute("SELECT DISTINCT session_key FROM blocks")}
    # `rows and` is the load-bearing half: a subset assertion is vacuously true
    # for an index containing nothing, so deleting the INSERT passed this. [E3]
    assert rows and rows <= keys


# --------------------------------------------------------------------------- #
# the seam bench/ scores through
# --------------------------------------------------------------------------- #


def test_the_retriever_hands_back_offsets_in_rank_order(home, src):
    """`bench/` scores this function and nothing else, and it had no test: an
    audit found it referenced by zero of them. `byte_offset` and `byte_len` are
    both ints on the same object, so returning the wrong one produces a number
    that looks like an answer and scores as a miss on every arm."""
    db = built(
        home,
        src,
        [
            user("u1", "an early aardvark"),
            user("u2", "the peculiar marmoset"),
        ],
    )
    hits = index.search(db, "marmoset aardvark")
    assert index.retriever(db)("marmoset aardvark", 10) == [h.byte_offset for h in hits]
    assert [h.byte_offset for h in hits] != [h.byte_len for h in hits], (
        "the fixture must tell the two ints apart, or this asserts nothing"
    )


def test_the_retriever_honours_k_and_weights(home, src):
    """Both are keyword arguments that a caller can forget to thread through, and
    `bench/` varies exactly these two to compare arms. Dropped, every arm scores
    identically and the benchmark reports a tie it did not measure."""
    db = built(
        home,
        src,
        [
            assistant("a1", [{"type": "tool_result", "tool_use_id": "t", "content": "marmoset"}]),
            assistant("a2", [{"type": "text", "text": "marmoset"}]),
        ],
    )
    assert len(index.retriever(db)("marmoset", 1)) == 1
    inverted = index.Weights(prose=1.0, tool_use=1.0, tool_result=9.0, paths=1.0)
    assert index.retriever(db)("marmoset", 2) != index.retriever(db, weights=inverted)(
        "marmoset", 2
    )


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
