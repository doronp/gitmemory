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
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from gitmemory import gitrepo, index, records, store
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


def test_a_hit_in_a_multi_segment_generation_resolves_across_the_cut(home, src):
    """A hit's offset addresses the generation, not a file.

    Every other offset test in this file has one segment, where the two are the
    same number and a file seek happens to work. Two captures make them differ,
    and the wrong reading is not an error — it returns the wrong turn's bytes,
    confidently. `store.span` is the only resolver for the same reason there is
    no `(path, offset)` form of it: a turn can straddle the cut. [E3]
    """
    write(src, [user("u1", "the first aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    write(src, [user("u2", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))
    hit = index.search(db, "marmoset")[0]
    db.close()

    stored = next(s for s in store.sessions(home) if s.key == hit.session_key)
    assert len(stored.segments) == 2, "the fixture has to actually span a cut"
    assert (
        store.span(stored, hit.byte_offset, hit.byte_len) == Path(src).read_bytes().split(b"\n")[1]
    )
    with open(stored.segments[0], "rb") as fh:  # what a (path, offset) resolver would do
        fh.seek(hit.byte_offset)
        assert fh.read(hit.byte_len) != store.span(stored, hit.byte_offset, hit.byte_len)


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


def test_a_pi_tool_call_indexes_the_path_it_names(home, src):
    """The same read, one dialect over: pi keys its arguments `arguments`.

    Reading only Claude Code's `input` made this whole branch dead code on
    every pi transcript. Most of what it recovers there the path regex would
    find anyway — 890 of the 894 `path` arguments in the shipped fixtures — so
    the fixture here is one of the four it would not: a bare filename, which
    has no separator to be path-shaped and is what a reader searches by.
    """
    write(
        src,
        [
            {"type": "session", "id": "s1", "timestamp": "t", "cwd": "/w"},
            {
                "type": "message",
                "id": "e1",
                "parentId": None,
                "timestamp": "t",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "t1",
                            "name": "read",
                            "arguments": {"path": "AGENTS.md"},
                        }
                    ],
                },
            },
        ],
    )
    store.capture(src, "pi", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))
    row = db.execute("SELECT paths FROM blocks WHERE kind = 'tool_use'").fetchone()
    assert row["paths"] == "AGENTS.md"


def test_a_malformed_input_does_not_hide_the_arguments_beside_it(home, src):
    """`input or arguments` falls through on *truthiness*, and it should not.

    `input` is a tool call the model wrote, so its shape is a claim, not a
    fact: a string where an object was expected is ordinary malformed output,
    and the surrounding code already says so. But a non-empty string is truthy,
    so it won the `or`, then failed the `isinstance` below it, and the
    `arguments` sitting right there — well-formed, naming a real file — was
    never read. The fall-through has to be on shape.

    A block carrying both keys does not occur in the corpus, which is why this
    is synthetic and why nothing caught it. [review: correctness]
    """
    write(
        src,
        [
            {"type": "session", "id": "s1", "timestamp": "t", "cwd": "/w"},
            {
                "type": "message",
                "id": "e1",
                "parentId": None,
                "timestamp": "t",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "t1",
                            "name": "read",
                            "input": "AGENTS.md",
                            "arguments": {"path": "CHANGELOG.md"},
                        }
                    ],
                },
            },
        ],
    )
    store.capture(src, "pi", "sess", home=home)
    index.build(home)
    db = index.open_db(index.db_path(home))
    row = db.execute("SELECT paths FROM blocks WHERE kind = 'tool_use'").fetchone()
    assert row["paths"] == "CHANGELOG.md"


# --------------------------------------------------------------------------- #
# a query is data, not a second query language
# --------------------------------------------------------------------------- #


def test_a_wildcard_in_a_query_is_a_word_not_a_prefix_scan(home, src):
    """`hel*` unhandled is an FTS5 prefix query over the whole corpus. What
    stops it is tokenising to `\\w+` — the star is not a word character, so it
    never reaches MATCH. Quoting is a second, independent guard; see below.

    Two guards, and this test pins neither on its own: drop the tokeniser and
    quoting makes the star literal, drop the quoting and the tokeniser has
    already eaten it. A vacuity audit deleted each in turn and this test stayed
    green both times. Each is pinned by a test that needs only one of them —
    the tokeniser by `test_a_hyphenated_query_matches_either_half` and
    `test_an_identifier_is_a_phrase_and_a_hyphenation_is_an_or`, the quoting by
    `test_a_bareword_operator_in_a_query_is_searched_for_not_obeyed` and
    `test_an_operator_in_a_query_cannot_reach_the_parser`. What this one proves
    is the pair, which is the thing a user actually gets.
    [E4, review: vacuity audit]
    """
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
    hits = index.search(db, " ".join(over))
    assert hits == []
    assert hits.dropped == 1
    found = index.search(db, "marmoset")
    assert found, "the fixture is findable when nothing is dropped"
    assert found.dropped == 0


def test_every_over_long_query_says_so_not_just_the_first(home, src):
    """The channel used to be `warnings.warn`, which is once per call site per
    process. Measured: three over-long queries in one interpreter, one warning
    — and all three returned nothing, because the only term that would have
    matched was the one past the cap. Anything long-lived that searches more
    than once got a real-looking empty ranking with no explanation. [E7
    index-F10]"""
    db = built(home, src, [user("u1", "the peculiar marmoset")])
    over = " ".join(["aardvark"] * index.MAX_TERMS + ["marmoset"])
    assert [index.search(db, over).dropped for _ in range(3)] == [1, 1, 1]


def test_recall_says_a_query_was_truncated_before_it_says_no_matches(home, src, capsys):
    """"no matches" for a query whose matching word was the one past the cap is
    a wrong answer, not an empty one, so the notice goes first. [E7 index-F10]
    """
    built(home, src, [user("u1", "the peculiar marmoset")]).close()
    over = " ".join(["aardvark"] * index.MAX_TERMS + ["marmoset"])
    capsys.readouterr()
    assert main(["--home", home, "recall", over]) == 0
    err = capsys.readouterr().err
    assert "truncated to 64 terms; 1 dropped" in err, err
    assert err.index("truncated") < err.index("no matches"), err


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


# SHA-256 of the empty string: what `hashlib.sha256()` reads when nothing was
# ever fed to it, which is how the gap below was found.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_a_turn_that_produced_no_blocks_still_reaches_the_digest(tmp_path):
    """The digest was fed only block rows, so an assistant turn that emitted no
    content — a tool call, a cancelled reply — fed it *nothing at all* while
    still carrying a usage block worth any number of tokens. Two stores whose
    spend differed by an arbitrary amount hashed to `e3b0c442…`, the SHA-256 of
    the empty string, and compared equal. [E6 review]"""

    def store_with(output_tokens: int, where: str) -> str:
        home2, src2 = str(tmp_path / where), str(tmp_path / f"{where}.jsonl")
        os.mkdir(home2)
        line = assistant("a1", [])  # no blocks
        line["requestId"] = "req-1"
        line["message"]["usage"] = {"output_tokens": output_tokens}
        write(src2, [line])
        store.capture(src2, "claude-code", "sess", home=home2)
        return index.build(home2).content_sha256

    # Two digits each, deliberately. The generation row carries the segment's
    # byte count, so `10` against `9_000_000` differs in the *length* of the
    # line and the test passes with the turn row still left out of the digest —
    # which is how the first draft of this test held nothing. Equal widths take
    # the generation row out of the comparison and leave only the usage.
    cheap = store_with(10, "cheap")
    dear = store_with(99, "dear")
    assert cheap != EMPTY_SHA256, "the digest saw nothing at all"
    assert cheap != dear


def test_the_same_bytes_captured_in_two_passes_are_not_the_same_index(tmp_path):
    """Segmentation is part of what the index *is*: one capture of two turns and
    two captures of one turn each hold identical blocks and different
    `generations` rows. Only the block rows reached the digest, so the two
    compared equal — and the digest is the thing that answers "is this the same
    index". [E6 review]"""
    lines = [user("u1", "one"), user("u2", "two")]

    def store_with(splits: list[int], where: str) -> str:
        home2, src2 = str(tmp_path / where), str(tmp_path / f"{where}.jsonl")
        os.mkdir(home2)
        at = 0
        for n in splits:
            write(src2, lines[at : at + n])
            at += n
            store.capture(src2, "claude-code", "sess", home=home2)
        return index.build(home2).content_sha256

    assert store_with([2], "onepass") != store_with([1, 1], "twopass")


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
    # 1.5 s, not 10. The whole build is 0.15 s here, so 10 would have let a 60x
    # regression through green — a guard against a quadratic blow-up that only
    # fires once the blow-up is already catastrophic is not a guard. 1.5 s is
    # 10x the measured cost, which leaves room for a loaded machine and still
    # fails on anything super-linear: the unbounded regex took 1.34 s on 20 KB
    # and 5.27 s on 40 KB, so at this input it is minutes. [E7 carry-in]
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, f"the path regex went quadratic again: {elapsed:.2f}s"


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
    parent = Path(os.path.dirname(index.db_path(home)))
    junk = [parent / n for n in (".building-abc123.db", ".building-abc123.db-journal")]
    for p in junk:
        p.write_bytes(b"leftover")
    index.build(home)
    assert [p for p in junk if p.exists()] == []
    assert index.search(index.open_db(index.db_path(home)), "marmoset")


def test_a_second_build_waits_instead_of_sweeping_the_first_one_away(home, src, monkeypatch):
    """The sweep cannot tell a partial index left by a kill from one a *live*
    build is still writing — both are `.building-*.db`, and the name is all the
    evidence there is. So the second build deleted the first one's temp and
    journal mid-transaction and the first died on `sqlite3.OperationalError:
    disk I/O error`, an error that names a failing disk when the disk is fine.
    Two `gitmemory index` runs in two terminals is how you get there. [E7]"""
    write(src, [user(f"u{i}", f"marmoset {i}") for i in range(50)])
    store.capture(src, "claude-code", "sess", home=home)

    paused, go, once = threading.Event(), threading.Event(), []
    real_fill = index._fill

    def fill_then_wait(db, h):
        stats = real_fill(db, h)
        if not once:  # only the first build waits; the second is the one racing it
            once.append(1)
            paused.set()  # the temp database is on disk and the transaction is open
            go.wait(20)
        return stats

    monkeypatch.setattr(index, "_fill", fill_then_wait)
    errors: list[BaseException] = []

    def build() -> None:
        try:
            index.build(home)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, from a thread
            errors.append(exc)

    first, second = threading.Thread(target=build), threading.Thread(target=build)
    first.start()
    try:
        assert paused.wait(20), "the first build never reached the pause"
        second.start()
        second.join(0.5)
        assert second.is_alive(), "the second build did not wait for the first"
    finally:
        go.set()
        first.join(20)
        if second.ident is not None:  # it may never have been started
            second.join(20)

    assert errors == [], "a build died because the other one swept its temp away"
    assert index.search(index.open_db(index.db_path(home)), "marmoset")


def test_the_sweep_deletes_what_the_build_writes_and_not_what_it_finds(home, src, tmp_path):
    """`--db` points the sweep at a directory the *user* chose, and the prefix
    on its own is not the name of anything gitmemory wrote: `.building-` plus
    anything at all was deleted on sight, so a half-written
    `.building-manifest.yaml` in the working directory was collateral. Every
    temp this module makes ends `.db`, because `mkstemp` is called with
    `suffix=".db"`. [E7]"""
    write(src, [user("u1", "marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    theirs = [tmp_path / n for n in (".building-manifest.yaml", ".building-notes")]
    for p in theirs:
        p.write_bytes(b"not ours")

    index.build(home, path=str(tmp_path / "out.db"))
    assert [p.name for p in theirs if not p.exists()] == []


def test_an_index_from_a_newer_schema_is_not_mistaken_for_a_leftover(home, src):
    """Forward is not the same direction as stale. The test was `!=`, so an
    index written by a *newer* gitmemory looked exactly like one written by an
    older one: run both against the same home and each deletes the other's
    index on every build, each rebuilds from raw, and neither says a word. [E7]"""
    write(src, [user("u1", "marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    parent = Path(os.path.dirname(index.db_path(home)))
    parent.mkdir(parents=True, exist_ok=True)
    future = parent / f"gitmemory-v{index.SCHEMA + 1}.db"
    past = parent / f"gitmemory-v{index.SCHEMA - 1}.db"
    for p in (future, past):
        p.write_bytes(b"an index")

    index.build(home)
    assert future.exists(), "a newer schema's index was swept as a leftover"
    assert not past.exists(), "precondition: the older one is still swept"


def test_a_failed_build_leaves_the_index_it_was_replacing(home, src, tmp_path, monkeypatch):
    """What `keep` is actually for — and it took two wrong tests to find out.

    The sweep runs *before* the rename, not after, so on a build that succeeds
    `keep` changes nothing: without it the old file is deleted a moment before
    `os.replace` would have overwritten it anyway. Two drafts of this test
    asserted the successful path and were green with `keep` deleted.

    The difference only shows when the build fails. Temp-plus-rename exists so
    that a crash leaves the previous index intact; a sweep that eats the target
    first takes that promise away and leaves no index at all. Reachable only via
    `--db`, since the default name is the current schema's by construction and
    the pattern never matches it. [E6 review]"""
    write(src, [user("u1", "marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    target = str(tmp_path / f"gitmemory-v{index.SCHEMA - 1}.db")  # a name the sweep matches
    index.build(home, path=target)
    before = Path(target).read_bytes()

    def explode(*_args, **_kwargs):
        raise RuntimeError("the build died here")

    monkeypatch.setattr(index, "_fill", explode)
    with pytest.raises(RuntimeError):
        index.build(home, path=target)
    assert Path(target).exists(), "the failed build swept away the index it was replacing"
    assert Path(target).read_bytes() == before


def test_an_index_from_an_older_schema_is_swept(home, src):
    """A schema bump renames the file, so the previous index stays on disk with
    every transcript in it — indefinitely, invisibly, and unreachable by any
    command that would rebuild or redact it. [E6 review]"""
    built(home, src, [user("u1", "marmoset")]).close()
    parent = os.path.dirname(index.db_path(home))
    old = Path(parent, f"gitmemory-v{index.SCHEMA - 1}.db")
    old.write_bytes(b"an index from two schemas ago")
    unrelated = Path(parent, "notes.db")
    unrelated.write_bytes(b"not ours")

    index.build(home)
    assert not old.exists()
    assert unrelated.exists(), "the sweep matches the versioned name, not every .db"
    assert Path(index.db_path(home)).exists()


def test_an_index_from_another_schema_is_refused_not_answered(home, src, tmp_path):
    """`db_path` versions the filename, so the default path cannot collide —
    `--db` bypasses it, and a stale database answered from the old schema in
    silence. A wrong answer from a retrieval index looks exactly like a right
    one. [E3]"""
    other = str(tmp_path / "out.db")
    index.build(home, path=other)
    db = index.open_db(other, write=True)  # the test is corrupting it; see [E7]
    assert index.search(db, "marmoset") == []
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    with pytest.raises(ValueError, match="schema 99"):
        index.search(db, "marmoset")
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="not a gitmemory index"):
        index.search(index.open_db(str(empty)), "marmoset")


def test_a_reader_cannot_create_or_scribble_on_an_index(home, src, tmp_path):
    """`sqlite3.connect(path)` opens for writing and creates the file if it is
    absent, so `recall --db typo.db` left a 0-byte database at the typo — a file
    that answers nothing, forever, with no sign of where it came from. Every
    caller but `build` is a reader. [E7]"""
    absent = tmp_path / "not-here.db"
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        index.open_db(str(absent))
    assert not absent.exists(), "opening an index for reading created one"

    built(home, src, [user("u1", "marmoset")]).close()
    db = index.open_db(index.db_path(home))
    assert index.search(db, "marmoset"), "the read path still reads"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM blocks")


def test_a_meta_that_is_not_a_table_cannot_run_forever(tmp_path):
    """SQLite files are code-adjacent: in a file gitmemory did not write, `meta`
    can be a **view**, and a view is arbitrary SQL that runs inside the
    innocuous `SELECT` in `_check_schema`. A view over a recursive CTE counting
    to 4x10^8 held it for 29.7 s and could not be interrupted — SQLite is
    executing in C, so `signal.alarm` does not fire and neither does Ctrl-C.
    Scale the constant in the file and it is unbounded. [E7]"""
    hostile = str(tmp_path / "hostile.db")
    db = sqlite3.connect(hostile)
    db.executescript(
        "CREATE VIEW meta AS "
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 400000000) "
        "SELECT 'schema' AS key, CAST(count(*) AS TEXT) AS value FROM c;"
    )
    db.commit()
    db.close()

    started = time.monotonic()
    with pytest.raises(ValueError, match="not a gitmemory index"):
        index.search(index.open_db(hostile), "marmoset")
    assert time.monotonic() - started < 5, "the budget did not stop the hostile view"


def test_a_bare_filename_is_a_usable_db_path(home, src, tmp_path, monkeypatch, capsys):
    """`--db out.db` has no dirname — the most obvious value anyone would pass
    to a flag documented as "database path", and the one that goes through the
    empty string. [E3]

    This test has no row in `tests/mutate_index.py`, deliberately. It had two,
    and both scored MISSED: nothing here is load-bearing on its own. `_mkdir`'s
    walk is `while path:`, so `""` is zero levels to create rather than the
    `makedirs("")` ENOENT the original comment blamed; `os.path.join("", name)`
    is `name`; a relative path resolves against the working directory. Three
    tolerances, no single line to revert. A row that can only ever score MISSED
    costs a reader more than the missing row does. [E7 pair review]
    """
    write(src, [user("u1", "the peculiar marmoset")])
    assert main(["--home", home, "capture", src]) == 0
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert main(["--home", home, "index", "--db", "out.db"]) == 0
    assert index.search(index.open_db(str(tmp_path / "out.db")), "marmoset")


def test_the_index_directory_is_owner_only(home, src):
    """`index/` was 0755 where `raw/` and `sessions/` beside it are 0700.

    Nothing *in* it was exposed — the database and its journal are 0600 — so
    what group and world could read was the listing: that a store is here, how
    big its index is, and when it last built. [E7 index-F9]
    """
    built(home, src, [user("u1", "the peculiar marmoset")]).close()
    dirs = ("index", "raw", "sessions")
    made = {d: os.lstat(os.path.join(home, d)).st_mode & 0o777 for d in dirs}
    assert made == dict.fromkeys(made, 0o700), made


def test_a_db_path_creates_its_parents_owner_only(home, src, tmp_path):
    """Every directory on the way, which is the half `os.makedirs(mode=...)`
    does not do: it applies the mode to the leaf and leaves the rest at
    0777 & ~umask. [E7 index-F9]"""
    built(home, src, [user("u1", "the peculiar marmoset")]).close()
    assert main(["--home", home, "index", "--db", str(tmp_path / "a" / "b" / "out.db")]) == 0
    for d in (tmp_path / "a", tmp_path / "a" / "b"):
        assert os.lstat(d).st_mode & 0o777 == 0o700, d


def test_a_db_path_through_a_symlinked_directory_is_still_allowed(home, src, tmp_path):
    """`--db` is the one write path whose directory the *caller* chose, and
    `/tmp` is a symlink on macOS — so the refusal that index-F1 put in `_mkdir`
    was refusing `gitmemory index --db /tmp/x.db`, from `_lockfile`, one line
    past the `makedirs` that had just succeeded. Refusing a symlink gitmemory
    invented the name of is the point; refusing one the caller typed is not.
    [E7 index-F9]"""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    built(home, src, [user("u1", "the peculiar marmoset")]).close()
    assert main(["--home", home, "index", "--db", str(tmp_path / "link" / "out.db")]) == 0
    assert index.search(index.open_db(str(real / "out.db")), "marmoset")


def test_a_symlink_planted_at_the_index_directory_is_refused(home, src, tmp_path):
    """The other half of the same branch. `<home>/index` is a name gitmemory
    invents and nothing else creates, so it is there to be planted, and the
    index is a second full copy of the transcript text. [E7 index-F9]"""
    away = tmp_path / "away"
    away.mkdir()
    os.symlink(away, os.path.join(home, "index"))
    write(src, [user("u1", "the peculiar marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    with pytest.raises(NotADirectoryError, match="symbolic link"):
        index.build(home)
    assert not sorted(p.name for p in away.iterdir())


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


def test_a_block_sqlite_refuses_costs_its_own_generation_and_no_other(home, tmp_path, monkeypatch):
    """The per-generation guard did not cover the writes, which is where the
    error is.

    `prose`, `tool_result` and `paths` are unbounded, and SQLite refuses a value
    over `SQLITE_LIMIT_LENGTH` — 10⁹ bytes by default — at bind time, outside
    the `try`. One transcript block over that took the whole build down, for
    every session in the store, on every run until someone found and deleted
    the generation by hand. The limit is lowered here so the test costs 200 KB
    instead of a gigabyte; what is being tested is the guard, not the number.
    [E7]
    """
    for n, body in enumerate(["the peculiar marmoset", "X" * 200_000, "the second marmoset"]):
        src = tmp_path / f"s{n}.jsonl"
        write(str(src), [user(f"u{n}", body)])
        store.capture(str(src), "claude-code", f"sess{n}", home=home)

    real = index.open_db
    monkeypatch.setattr(
        index,
        "open_db",
        lambda path, *, write=False: _capped(real(path, write=write), 100_000),
    )
    stats = index.build(home)

    assert stats.generations == 2, stats.skipped
    assert any("too big" in s for s in stats.skipped), stats.skipped
    db = index.open_db(index.db_path(home))
    assert index.search(db, "peculiar"), "the generation before the oversized one"
    assert index.search(db, "second"), "the generation after it"
    # Rolled back, not half-written: the oversized generation left no turn row
    # behind either, and it is in the index as a skip with a reason.
    rows = db.execute("SELECT turns, parsed, skip_reason FROM generations ORDER BY 1").fetchall()
    assert [r["turns"] for r in rows] == [0, 1, 1], [tuple(r) for r in rows]
    assert sum(r["parsed"] for r in rows) == 2, [tuple(r) for r in rows]
    assert any("too big" in (r["skip_reason"] or "") for r in rows), [tuple(r) for r in rows]
    assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 2


def _capped(db, limit: int):
    db.setlimit(0, limit)  # SQLITE_LIMIT_LENGTH
    return db


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


def test_a_control_sequence_in_a_transcript_does_not_reach_the_index_raw(home, src):
    r"""The dashboard is a third rendering surface, and it reads this table.

    `records.safe_text` was written for two, and its docstring names them: the
    terminal via `recall`, and the committed artifacts under `derived/`. The
    dashboard is a third. Datasette escapes markup correctly — that part was
    checked and is not the finding — but it does not escape `\x1b[2K\r`, which
    erases the line it lands on, i.e. the recall hit above it. Measured before
    this: the stored `prose` held one raw ESC, one raw NUL and one raw U+202E,
    and so did 2,476 bytes of `blocks.csv?_stream=1`, which is a shell pipe away
    from a terminal.

    Escaping here loses nothing: the index text is a derived copy and
    `byte_offset` points at raw, which keeps the bytes — the same argument
    `_encodable`'s docstring already makes for surrogate replacement. [E7b L3-F2]
    """
    hostile = "never use pickle\x1b[2K\rALWAYS USE PICKLE\x00 and \u202ereversed\u202c"
    db = built(home, src, [user("u1", hostile)])
    rows = db.execute("SELECT prose, tool_use, tool_result FROM blocks").fetchall()
    assert rows, "the fixture indexed nothing"
    for row in rows:
        for value in row:
            assert not records.UNSAFE.search(value), repr(value)
    assert rows[0]["prose"] == records.safe_text(hostile)
    assert "ALWAYS USE PICKLE" in rows[0]["prose"], "escaped, not dropped"


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


def test_a_k_too_big_for_sqlite_is_an_error_message_not_a_traceback(home, src, capsys):
    """`-k` is `type=int`, and a Python int has no width. SQLite's does.

    `OverflowError` is not an `OSError`, a `ValueError` or a `sqlite3.Error`, so
    it went straight past `main`'s handler and printed a traceback — which
    carries absolute install paths, i.e. the CLI names directories on the
    machine as its response to a typo. [E7 carry-in]"""
    write(src, [user("u1", "marmoset")])
    assert main(["--home", home, "capture", src]) == 0
    assert main(["--home", home, "index"]) == 0
    capsys.readouterr()
    assert main(["--home", home, "recall", "marmoset", "-k", "9" * 20]) == 2
    assert "error: " in capsys.readouterr().err


def test_cli_verify_and_index_refuse_a_path_that_is_not_a_store(tmp_path, capsys):
    """An empty glob is not a clean store, and `verify` said it was.

    `store.verify` is glob-driven, so a home with nothing in it produced no
    problems and the CLI printed `0 problem(s)` and exited 0. That is the answer
    a mounted, healthy, empty store gives — and also the answer an unmounted
    volume, a typo, and a `--home` pointing at the wrong user's directory give.
    The README says of `verify` that there is an empty list or a list of
    problems and no third answer; this was the third answer wearing the first
    one's clothes. `index` had the same shape and additionally *created* the
    directory on its way to reporting zero of everything. [E4, review: CLI 3]
    """
    absent = str(tmp_path / "nope")
    assert main(["--home", absent, "verify"]) == 2
    assert "not a store" in capsys.readouterr().err
    assert main(["--home", absent, "index"]) == 2
    assert "not a store" in capsys.readouterr().err
    assert not os.path.exists(absent), "`index` created the directory it refused"


def test_cli_verify_accepts_a_store_that_is_merely_empty(tmp_path, capsys):
    """The other side of the floor, because a floor that refuses too much is worse.

    `watch` inits the repository on its first pass and may capture nothing for
    an hour. That store has `.git` and nothing else, it is a real store, and
    `verify` on it must give the ordinary clean answer rather than the refusal
    above. [E4, review: CLI 3]
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    assert main(["--home", home, "verify"]) == 0
    assert "0 problem(s)" in capsys.readouterr().out


def test_cli_recall_survives_a_corrupt_database(home, src, capsys):
    """A truncated database is an ordinary state after a full disk. It must
    report, not traceback.

    The second half of that sentence used to read "`main` catching sqlite3.Error
    is what does that", and it is not: `_check_schema` asks `meta` first, a file
    of arbitrary bytes has no `meta`, and the `sqlite3.DatabaseError` is
    converted there into a `ValueError` that `main` has caught since E3. A
    vacuity audit dropped `sqlite3.Error` from the handler and the whole suite
    stayed green — this test cannot see that clause, and the one below only
    proved something *could* raise it, never that anything catches it.

    Left here as the ValueError path, which is a real path and worth a test. The
    clause itself is pinned below. [E4, review: vacuity audit]
    """
    built(home, src, [user("u1", "marmoset")]).close()
    Path(index.db_path(home)).write_bytes(b"not a database")
    assert main(["--home", home, "recall", "marmoset"]) == 2
    assert "error:" in capsys.readouterr().err


def test_search_rejects_nothing_it_can_reach_the_database_with(home, src, capsys):
    """`sqlite3.Error` in `main`'s handler is load-bearing only if something can
    actually raise it; this is that something, pinned — and then driven through
    `main`, because "something can raise it" and "the handler catches it" are
    two claims and this test only made the first.

    A dropped `fts` leaves `meta` intact, so `_check_schema` passes and the
    failure happens where nothing converts it. That makes `sqlite3.Error` the
    only clause in `main` that can return 2 here, which is the whole point of
    the clause. [E4, review: vacuity audit]
    """
    built(home, src, [user("u1", "marmoset")]).close()
    # `write=True` on purpose: the test is corrupting an index, which is a write,
    # and `open_db`'s default has been read-only since [E7].
    db = index.open_db(index.db_path(home), write=True)
    db.execute("DROP TABLE fts")
    with pytest.raises(sqlite3.Error):
        index.search(db, "marmoset")
    db.commit()
    db.close()

    assert main(["--home", home, "recall", "marmoset"]) == 2
    assert "error:" in capsys.readouterr().err
