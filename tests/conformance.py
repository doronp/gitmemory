"""The adapter contract. An agent is *supported* when `check_adapter` passes.

Agent-agnostic on purpose: Hermes, Kimi and opencode adapters import this
unchanged. If a rule here only makes sense for Claude Code, it belongs in
test_claude_code.py instead.

Every rule below is written so that it *fails* for an adapter that loses data.
The first version of this file did not have that property: an adapter whose
`parse` returned an empty Session passed all 162 corpus cases, because each
rule quantified over records that no longer existed. The accounting identity in
`_nothing_is_silently_dropped` is the fix — it is the only check that compares
output against input rather than against itself.
"""

from __future__ import annotations

import json
import os

from gitmemory.records import Session, canonical_json, sha256_text

# An adapter whose spans are byte ranges of a text file can be checked against
# the file itself. One that synthesises records has no such anchor; it opts out
# by setting `SPANS_ARE_FILE_BYTES = False`.
_SPANS_DEFAULT = True


def check_adapter(module, path: str) -> Session:
    """Parse `path` with `module` and assert every contract rule. Returns the session."""
    session = module.parse(path)
    _ids_are_content_derived(session)
    _byte_order_is_the_only_order(session)
    if getattr(module, "SPANS_ARE_FILE_BYTES", _SPANS_DEFAULT):
        _spans_are_real(session, path)
        _input_is_recounted_independently(session, path)
    _nothing_is_silently_dropped(session)
    _canonical_json_is_valid_and_reparses(session)
    _reparse_is_byte_identical(module, path, session)
    return session


def _expected_turn_id(t) -> str:
    """The documented formula, recomputed independently of the dataclass.

    Asserting `len(turn_id) == 64` only proves something called sha256; it
    passes for an id keyed on `seq`, which is the churn this project exists to
    avoid. This recomputes what records.py promises so a change to the key
    breaks the contract test rather than silently rewriting every id.
    """
    import hashlib

    digest = sha256_text("".join(b.content_sha256 for b in t.blocks))
    identity = t.uuid or f"@{t.byte_offset}"
    parts = (t.session_id, identity, t.role, digest)
    return hashlib.sha256(
        b"\x1f".join(str(p).encode("utf-8", "surrogatepass") for p in parts)
    ).hexdigest()


def _expected_block_id(b) -> str:
    import hashlib

    parts = (b.turn_id, b.seq, b.kind, b.content_sha256)
    return hashlib.sha256(
        b"\x1f".join(str(p).encode("utf-8", "surrogatepass") for p in parts)
    ).hexdigest()


def _ids_are_content_derived(s: Session) -> None:
    for t in s.turns:
        assert t.turn_id == _expected_turn_id(t), (
            f"turn_id is not the documented function of its content: {t.turn_id!r}"
        )
        assert t.session_id, "a turn with no session_id bakes '' into every id it keys"
        for b in t.blocks:
            assert b.turn_id == t.turn_id, "block must be re-keyed to its final turn_id"
            assert b.content_sha256 == sha256_text(b.text)
            assert b.block_id == _expected_block_id(b), "block_id is not content-derived"
    ids = [t.turn_id for t in s.turns]
    assert len(ids) == len(set(ids)), "turn_ids collide — seq is not in the id"
    event_ids = [e.event_id for e in s.events]
    assert len(event_ids) == len(set(event_ids)), "event_ids collide"
    turn_ids = {t.turn_id for t in s.turns}
    for e in s.events:
        assert e.byte_offset >= 0, "an event must point at the byte that caused it"
        assert e.anchor in turn_ids, "an event anchors on a turn that is not in the session"


def _byte_order_is_the_only_order(s: Session) -> None:
    seqs = [t.seq for t in s.turns]
    assert seqs == sorted(seqs), "turns must be in seq order"
    assert seqs == list(range(len(seqs))), "seq must be dense — a gap means a silent drop"
    offsets = [t.byte_offset for t in s.turns]
    assert offsets == sorted(offsets), "seq must follow byte order, not timestamp order"
    assert len(offsets) == len(set(offsets)), "two turns claim the same byte offset"


def _spans_are_real(s: Session, path: str) -> None:
    from gitmemory.jsonl import read_span

    size = os.path.getsize(path)
    for t in s.turns:
        assert t.byte_len > 0
        assert 0 <= t.byte_offset < size
        assert t.byte_offset + t.byte_len <= size, "span runs past EOF"
        # The span must be exactly one JSON object — this is what makes recall
        # a seek instead of a scan, and what ties records to committed segments.
        raw = read_span(path, t.byte_offset, t.byte_len)
        try:
            json.loads(raw.decode("utf-8", "surrogateescape"))
        except ValueError as exc:
            raise AssertionError(
                f"span [{t.byte_offset},{t.byte_offset + t.byte_len}) is not one JSON "
                f"object, so recall cannot seek to it: {exc}"
            ) from exc


def _input_is_recounted_independently(s: Session, path: str) -> None:
    """`records_seen` must match a count this suite takes for itself.

    Without this the accounting identity is self-consistent and therefore
    vacuous: an adapter returning an empty Session reports zero records read,
    zero turns and zero skips, and balances perfectly. The recount is the only
    number in the contract that the adapter does not supply.
    """
    from gitmemory.jsonl import iter_records

    errors: list[int] = []
    n = sum(1 for _ in iter_records(path, on_error=lambda ln, _e: errors.append(ln)))
    assert s.records_seen == n + len(errors), (
        f"adapter says it read {s.records_seen} records; the file holds "
        f"{n + len(errors)} ({n} decoded + {len(errors)} undecodable)"
    )


def _nothing_is_silently_dropped(s: Session) -> None:
    assert isinstance(s.skipped, dict)
    for reason, count in s.skipped.items():
        assert isinstance(reason, str) and reason, "skip reasons must be named"
        assert len(reason) <= 64, f"skip reason is unbounded and reaches git: {reason[:80]!r}"
        assert count > 0
    # The accounting identity. Every value the reader pulled out of the file is
    # either a turn or a counted skip; events annotate turns and are not
    # separately accounted. An adapter that drops a line has to say so here.
    assert s.records_seen >= 0
    accounted = len(s.turns) + sum(s.skipped.values())
    assert accounted == s.records_seen, (
        f"{s.records_seen} records read but {accounted} accounted for "
        f"({len(s.turns)} turns + {sum(s.skipped.values())} skipped) — "
        f"{s.records_seen - accounted} line(s) vanished"
    )


def _canonical_json_is_valid_and_reparses(s: Session) -> None:
    blob = s.to_canonical()
    assert blob.decode("utf-8"), "canonical JSON must be valid UTF-8"
    again = json.loads(blob)
    assert canonical_json(again) == blob, "canonical JSON does not survive a round trip"
    assert len(again["turns"]) == len(s.turns), "to_canonical dropped turns"
    assert len(again["events"]) == len(s.events), "to_canonical dropped events"
    for out, t in zip(again["turns"], s.turns, strict=True):
        assert out["turn_id"] == t.turn_id
        assert len(out["blocks"]) == len(t.blocks), "to_canonical dropped blocks"


def _reparse_is_byte_identical(module, path: str, first: Session) -> None:
    assert module.parse(path).to_canonical() == first.to_canonical(), (
        "parse is not deterministic — the derived tree would churn on every rebuild"
    )
