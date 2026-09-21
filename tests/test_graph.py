"""E5: the decision graph emitter.

`derive` decides what a decision is and is measured for it. This module only
has to hand those decisions over to graphify without losing the one property
that makes them worth drawing — that every node names bytes. So the tests here
are about the hand-off, not about extraction quality:

  - the emitted dict passes **graphify's own validator**, not our reading of it,
    so an upgrade that changes a required field breaks a test instead of
    breaking a diagram;
  - **every node id is a real `block_id`** from a real transcript, and a
    decision that names anything else is an error rather than a blank node;
  - **the same bytes are one node**, because a fork from a compaction boundary
    replays turns and drawing them twice would claim two decisions were made;
  - the emission is **order-independent**, because a store walked in a different
    order must not produce a different file.
"""

from __future__ import annotations

import pytest
from graphify.validate import validate_extraction

from gitmemory import graph
from gitmemory.derive import Decision
from gitmemory.records import Block, Session, Turn


def _session(session_id: str, texts: list[tuple[str, str]], path: str = "/t/a.jsonl"):
    """A minimal session: one turn per (role, text), one text block each."""
    turns = []
    for i, (role, text) in enumerate(texts):
        turns.append(
            Turn(
                session_id=session_id,
                seq=i,
                role=role,
                byte_offset=i * 100,
                byte_len=100,
                uuid=f"u{i}",
                blocks=[Block(turn_id="", seq=0, kind="text", text=text)],
            )
        )
    return Session(session_id=session_id, agent="claude-code", source_path=path, turns=turns)


def _ids(session):
    return [b.block_id for t in session.turns for b in t.blocks]


def _decide(*refs):
    """A stand-in extractor that returns exactly the decisions it is told to.

    The graph is tested against a fixed extractor on purpose: a test that calls
    the real one measures the extractor's guards a second time and goes red when
    somebody widens a regex, which tells you nothing about the emitter.
    """

    def extract(session):
        present = set(_ids(session))
        return [Decision("directive", r) for r in refs if r in present]

    return extract


def test_the_emitted_dict_satisfies_graphifys_own_validator():
    """Not our reading of the schema — the installed validator. The field names
    were read off `validate.py` once; this is what keeps them true."""
    s = _session("s1", [("user", "Use pathlib, not os.path."), ("assistant", "ok")])
    ids = _ids(s)
    out = graph.extraction([s], extract=_decide(ids[0]))
    assert validate_extraction(out) == []


def test_every_node_names_a_block_that_exists():
    s = _session("s1", [("user", "Store it in Parquet rather than CSV.")])
    ids = _ids(s)
    out = graph.extraction([s], extract=_decide(ids[0]))
    assert [n["id"] for n in out["nodes"]] == [ids[0]]
    assert out["nodes"][0]["source_file"] == ids[0]
    assert out["nodes"][0]["file_type"] == "rationale"


def test_a_decision_naming_a_block_from_somewhere_else_is_an_error():
    """The alternative is a node labelled with nothing, which looks like a
    rendering bug three layers downstream and is really a provenance hole."""
    s = _session("s1", [("user", "Use pathlib.")])
    with pytest.raises(ValueError, match="not in this transcript"):
        graph.extraction([s], extract=lambda _s: [Decision("directive", "deadbeef")])


def test_the_same_block_replayed_into_a_second_transcript_is_one_node():
    """A fork from a compaction boundary replays turns verbatim. `block_id` is
    content-derived, so the two transcripts meet at a shared node instead of
    claiming the decision was taken twice."""
    texts = [("user", "Timestamps go in UTC, never local.")]
    a = _session("s1", texts, path="/t/a.jsonl")
    b = _session("s1", texts, path="/t/b.jsonl")
    assert _ids(a) == _ids(b), "precondition: the replay really is the same bytes"

    out = graph.extraction([a, b], extract=_decide(_ids(a)[0]))
    assert len(out["nodes"]) == 1


def test_emission_does_not_depend_on_the_order_the_store_was_walked():
    a = _session("s1", [("user", "Use pathlib.")], path="/t/a.jsonl")
    b = _session("s2", [("user", "Use UTC.")], path="/t/b.jsonl")
    refs = _decide(*(_ids(a) + _ids(b)))
    assert graph.extraction([a, b], extract=refs) == graph.extraction([b, a], extract=refs)


def test_an_edge_joins_consecutive_decisions_and_never_crosses_a_transcript():
    a = _session("s1", [("user", "one"), ("user", "two")], path="/t/a.jsonl")
    b = _session("s2", [("user", "three")], path="/t/b.jsonl")
    ids_a, ids_b = _ids(a), _ids(b)
    out = graph.extraction([a, b], extract=_decide(*(ids_a + ids_b)))

    pairs = {(e["source"], e["target"]) for e in out["edges"]}
    assert pairs == {(ids_a[0], ids_a[1])}
    assert all(e["relation"] == "follows" for e in out["edges"])
    assert all(e["confidence"] == "EXTRACTED" for e in out["edges"])


def test_a_decision_is_never_joined_to_itself():
    """`decisions` cannot emit the same block twice, but `extract` is injectable
    and a self-loop is meaningless on a decision diagram — graphify would draw
    it rather than complain."""
    a = _session("s1", [("user", "one")])
    ref = _ids(a)[0]
    out = graph.extraction([a], extract=lambda _s: [Decision("directive", ref)] * 2)
    assert out["edges"] == []


def test_a_label_is_one_line_and_ends_on_a_word():
    """Cut *on a word boundary*, not at the character limit.

    The first version of this test asserted only that the label was short
    enough, ended in an ellipsis, and had no trailing space — all three of
    which a hard cut at `LABEL_CHARS` also satisfies whenever the limit happens
    to land mid-word, which is most of the time. A mutation row that replaced
    the word search with `cut = -1` survived it. So the assertion has to name
    the boundary: the source character immediately after the body is the space
    the label stopped at.
    """
    assert "\n" not in graph._label("first line\nsecond line")
    assert graph._label("first line\nsecond line") == "first line second line"

    long = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 4).strip()
    label = graph._label(long)
    assert label.endswith("…")
    assert len(label) <= graph.LABEL_CHARS + 1  # the ellipsis
    body = label[:-1]
    assert not body.endswith(" ")
    assert long[len(body)] == " ", f"cut mid-word: ...{long[len(body) - 6 : len(body) + 6]!r}"


def test_a_label_with_no_space_in_range_is_still_cut():
    """A URL or a long identifier has no word boundary to cut on, and a label
    that silently came back full-length would blow out the diagram."""
    label = graph._label("x" * 500)
    assert len(label) == graph.LABEL_CHARS + 1


def test_a_block_with_no_visible_text_gets_a_caption_not_a_blank_node():
    """A whitespace-only block is a real block naming real bytes, so it is not
    an error — but an empty label draws as a blank node, which is the failure
    the missing-ref check exists to prevent. [review: Gemini 2]"""
    s = _session("s1", [("user", "\n \t ")])
    out = graph.extraction([s], extract=_decide(_ids(s)[0]))
    assert out["nodes"][0]["label"] == graph.NO_TEXT
    assert validate_extraction(out) == []


def test_a_key_past_the_cut_still_redacts_the_whole_label():
    """Scan the block, not the label.

    `derive._write` refuses any artifact carrying a secret, so a key in a
    decision block would otherwise cost that generation every derived file it
    has. Redacting the label keeps the node — it still names the bytes — and
    keeps the other two artifacts.

    The key here sits past `LABEL_CHARS` on purpose. Scanning the returned label
    instead would see a clean 120-character prefix and publish it, and if the
    limit landed mid-key it would publish a fragment of the key as well.
    """
    key = "AKIAZZZZQQQQWWWW1234"  # synthetic, right shape
    text = "word " * 40 + key
    assert len(text) > graph.LABEL_CHARS + len(key), "the key has to be past the cut"
    s = _session("s1", [("user", text)])
    out = graph.extraction([s], extract=_decide(_ids(s)[0]))
    assert out["nodes"][0]["label"] == graph.REDACTED
    assert validate_extraction(out) == []
    assert "AKIA" not in str(out), "a fragment of the key is still the key's shape"


def test_the_extraction_says_how_many_labels_it_redacted():
    """[E7] `ideas()` reports `sentences_redacted`; this surface reported
    nothing. One false positive is a block that merely *names* a PEM header, so
    a store where the gate misfires on every block looked exactly like a store
    full of secrets — and both looked like a store with none."""
    key = "AKIAZZZZQQQQWWWW1234"  # synthetic, right shape
    s = _session("s1", [("user", f"chose {key}"), ("user", "chose the other one")])
    out = graph.extraction([s], extract=_decide(*_ids(s)))
    assert out["labels_redacted"] == 1
    assert [n["label"] for n in out["nodes"]].count(graph.REDACTED) == 1
    assert validate_extraction(out) == [], "graphify tolerates the extra key"

    clean = _session("s2", [("user", "chose the other one")])
    assert graph.extraction([clean], extract=_decide(_ids(clean)[0]))["labels_redacted"] == 0


def test_a_graphify_option_reaches_graphify():
    """`build` takes `extract` for us and passes the rest on. The first version
    funnelled everything into `extraction`, so `directed=True` died on a
    `TypeError` naming a function the caller never called. [review: Gemini 1]"""
    a = _session("s1", [("user", "one"), ("user", "two")])
    g = graph.build([a], extract=_decide(*_ids(a)), directed=True)
    assert g.is_directed()
    assert not graph.build([a], extract=_decide(*_ids(a))).is_directed()


def test_graphify_assembles_the_emitted_dict_into_a_graph():
    """The seam, end to end. If `build_from_json` ever stops accepting what we
    emit, this is the test that says so rather than a broken dashboard."""
    a = _session("s1", [("user", "one"), ("user", "two")])
    ids = _ids(a)
    g = graph.build([a], extract=_decide(*ids))
    assert set(g.nodes) == set(ids)
    assert g.number_of_edges() == 1
