"""Mutation tests for the conformance suite itself.

The E1 conformance suite was vacuous. A six-lens review proved it by mutating
the adapter and running the full 277-test suite green: `to_canonical` returning
`b"{}"`, dropping the `blocks` list, renaming `events`, truncating at 20 turns,
and skipping every `thinking` block all passed. Each rule quantified over
records that the mutation had already removed, so removing them satisfied it.

A test suite that cannot fail is a claim, not a check. Every mutation below is
one the old suite missed; each must now be caught. This file is the evidence
for the E1 ship gate, and it is why that gate can be signed at all.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from conformance import check_adapter

from gitmemory.adapters import claude_code as cc
from gitmemory.records import Session, canonical_json


def _fixture(tmp_path) -> str:
    """A transcript with everything a mutation could hide behind.

    Thirty turns so a truncation is visible, thinking and tool blocks so a
    dropped kind is visible, a skipped line so the skip counter matters, and a
    compaction so there is an event to lose.
    """
    lines: list[dict] = []
    for i in range(30):
        lines.append(
            {
                "type": "user",
                "uuid": f"u{i}",
                "sessionId": "s1",
                "message": {"role": "user", "content": f"question {i}"},
            }
        )
        lines.append(
            {
                "type": "assistant",
                "uuid": f"a{i}",
                "sessionId": "s1",
                "requestId": f"r{i}",
                "parentUuid": f"u{i}",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {"type": "thinking", "thinking": f"reasoning {i}"},
                        {"type": "text", "text": f"answer {i}"},
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
            }
        )
    lines.append(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "cb1",
            "sessionId": "s1",
            "parentUuid": "a29",
            "compactMetadata": {"trigger": "auto"},
        }
    )
    lines.append({"type": "custom-title", "customTitle": "x"})  # a counted skip
    path = tmp_path / "s1.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(path)


def _recast(cls, s: Session) -> Session:
    return cls(**{f.name: getattr(s, f.name) for f in dataclasses.fields(s)})


class _EmptyCanonical(Session):
    __slots__ = ()

    def to_canonical(self) -> bytes:
        return b"{}"


class _CanonicalWithoutBlocks(Session):
    __slots__ = ()

    def to_canonical(self) -> bytes:
        doc = json.loads(Session.to_canonical(self))
        for t in doc["turns"]:
            t["blocks"] = []
        return canonical_json(doc)


class _CanonicalRenamesEvents(Session):
    __slots__ = ()

    def to_canonical(self) -> bytes:
        doc = json.loads(Session.to_canonical(self))
        doc["boundaries"] = doc.pop("events")
        return canonical_json(doc)


def _return_nothing(s: Session) -> Session:
    return Session(session_id=s.session_id, agent=s.agent, source_path=s.source_path)


def _truncate(s: Session) -> Session:
    del s.turns[20:]
    return s


def _drop_all_blocks(s: Session) -> Session:
    for t in s.turns:
        t.blocks = []
    return s


def _drop_thinking(s: Session) -> Session:
    for t in s.turns:
        t.blocks = [b for b in t.blocks if b.kind != "thinking"]
    return s


def _drop_a_skip_counter(s: Session) -> Session:
    s.skipped.clear()
    return s


def _renumber_from_one(s: Session) -> Session:
    for t in s.turns:
        t.seq += 1
    return s


def _forge_a_turn_id(s: Session) -> Session:
    s.turns[5].turn_id = "0" * 64  # right shape, wrong function
    return s


def _reorder_by_timestamp(s: Session) -> Session:
    s.turns.reverse()
    return s


def _cover_the_tracks(s: Session) -> Session:
    """Drop a turn *and* adjust the input count so the books still balance.

    The accounting identity alone cannot see this — it is self-consistent.

    An earlier version of this docstring said "only the independent recount in
    the conformance suite can", and that is not what the suite does. Vacuity
    pass 2 removed the recount alone: still caught. The byte-order rule alone:
    still caught. The seq-density check holds it too. Only removing the recount
    *and* the byte-order rule together lets this mutant through. Three
    overlapping checks is the right answer for a mutant this quiet — the
    docstring was just claiming credit for one of them. [E4, vacuity pass 2: D2]
    """
    del s.turns[7]
    s.records_seen -= 1
    return s


def _widen_a_span(s: Session) -> Session:
    s.turns[0].byte_len += 5
    return s


def _unset_a_session_id(s: Session) -> Session:
    s.turns[3].session_id = ""
    return s


MUTATIONS = [
    ("parse returns nothing at all", _return_nothing),
    ("truncate at 20 turns", _truncate),
    ("drop every content block", _drop_all_blocks),
    ("drop thinking blocks", _drop_thinking),
    ("forget a skip counter", _drop_a_skip_counter),
    ("renumber seq from 1", _renumber_from_one),
    ("forge a sha256-shaped turn_id", _forge_a_turn_id),
    ("order by something other than bytes", _reorder_by_timestamp),
    ("cover the tracks of a dropped turn", _cover_the_tracks),
    ("widen a byte span past its object", _widen_a_span),
    ("leave a turn with no session_id", _unset_a_session_id),
    ("to_canonical returns an empty object", lambda s: _recast(_EmptyCanonical, s)),
    ("to_canonical drops the blocks", lambda s: _recast(_CanonicalWithoutBlocks, s)),
    ("to_canonical renames events", lambda s: _recast(_CanonicalRenamesEvents, s)),
]


class _Mutant:
    """The real adapter, wrong in exactly one way."""

    def __init__(self, mutate):
        self._mutate = mutate

    def parse(self, path: str) -> Session:
        return self._mutate(cc.parse(path))


def test_the_fixture_passes_unmutated(tmp_path):
    """Otherwise every mutation below 'fails' for the wrong reason."""
    s = check_adapter(cc, _fixture(tmp_path))
    assert len(s.turns) == 61 and len(s.events) == 1
    assert sum(s.skipped.values()) == 1


@pytest.mark.parametrize("name,mutate", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_conformance_catches(tmp_path, name, mutate):
    path = _fixture(tmp_path)
    with pytest.raises((AssertionError, KeyError, IndexError)):
        check_adapter(_Mutant(mutate), path)
    assert name  # the id is the point of the test; keep it referenced


def test_dropping_an_event_is_deliberately_not_a_contract_violation(tmp_path):
    """One mutation the generic suite must NOT catch, recorded so it stays honest.

    An adapter with no events is legitimate — most agents have no compaction —
    so "the compaction event went missing" cannot be an agent-agnostic rule. It
    is a Claude Code invariant and is tested as one, in test_claude_code.py.
    Listing it here as a caught mutation would overstate what this file proves.
    """

    def drop_events(s: Session) -> Session:
        s.events.clear()
        return s

    check_adapter(_Mutant(drop_events), _fixture(tmp_path))  # passes, by design


def test_the_line_floor_is_counted_without_the_reader(tmp_path, monkeypatch):
    """"Independent" was a word in a docstring; this is the check for it.

    `_input_is_recounted_independently` calls `iter_records`, so it agrees
    with the reader by construction and can only catch drops the *adapter*
    makes. A reader that loses input balances against itself, and the whole
    accounting contract goes quiet. `_every_line_produced_a_record` is the
    half that shares nothing: a floor of one record per non-blank physical
    line, counted with no JSON parsing at all.

    Proved two ways, because the shared-reader bug is exactly the kind a
    reading cannot rule out: the floor fires on a session that under-reports,
    and it still works with `iter_records` replaced by something that raises.
    [E7 parsing-F2]
    """
    import conformance

    from gitmemory import jsonl

    path = _fixture(tmp_path)
    honest = cc.parse(path)

    def explode(*a, **kw):
        raise AssertionError("the floor called the code it is checking")

    monkeypatch.setattr(jsonl, "iter_records", explode)
    conformance._every_line_produced_a_record(honest, path)

    short = cc.parse(path)
    short.records_seen -= 1
    with pytest.raises(AssertionError, match="produced"):
        conformance._every_line_produced_a_record(short, path)
