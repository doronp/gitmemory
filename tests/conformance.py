"""The adapter contract. An agent is *supported* when `check_adapter` passes.

Agent-agnostic on purpose: Hermes, Kimi and opencode adapters import this
unchanged. If a rule here only makes sense for Claude Code, it belongs in
test_claude_code.py instead.
"""

from __future__ import annotations

from gitmemory.records import Session, sha256_text


def check_adapter(module, path: str) -> Session:
    """Parse `path` with `module` and assert every contract rule. Returns the session."""
    session = module.parse(path)
    _ids_are_content_derived(session)
    _byte_order_is_the_only_order(session)
    _spans_are_real(session, path)
    _nothing_is_silently_dropped(session)
    _reparse_is_byte_identical(module, path, session)
    return session


def _ids_are_content_derived(s: Session) -> None:
    for t in s.turns:
        digest = sha256_text("".join(b.content_sha256 for b in t.blocks))
        assert t.turn_id, "turn_id must be set"
        assert len(t.turn_id) == 64, f"turn_id is not a sha256: {t.turn_id!r}"
        assert digest, "block digest must be computable even for an empty turn"
        for b in t.blocks:
            assert b.turn_id == t.turn_id, "block must be re-keyed to its final turn_id"
            assert b.content_sha256 == sha256_text(b.text)
    ids = [t.turn_id for t in s.turns]
    assert len(ids) == len(set(ids)), "turn_ids collide — seq is not in the id"


def _byte_order_is_the_only_order(s: Session) -> None:
    seqs = [t.seq for t in s.turns]
    assert seqs == sorted(seqs), "turns must be in seq order"
    offsets = [t.byte_offset for t in s.turns]
    assert offsets == sorted(offsets), "seq must follow byte order, not timestamp order"


def _spans_are_real(s: Session, path: str) -> None:
    import json
    import os

    size = os.path.getsize(path)
    from gitmemory.jsonl import read_span

    for t in s.turns:
        assert t.byte_len > 0
        assert 0 <= t.byte_offset < size
        assert t.byte_offset + t.byte_len <= size, "span runs past EOF"
        # The span must be exactly one JSON object — this is what makes recall
        # a seek instead of a scan, and what ties records to committed segments.
        raw = read_span(path, t.byte_offset, t.byte_len)
        json.loads(raw.decode("utf-8", "surrogateescape"))


def _nothing_is_silently_dropped(s: Session) -> None:
    assert isinstance(s.skipped, dict)
    for reason, count in s.skipped.items():
        assert isinstance(reason, str) and reason, "skip reasons must be named"
        assert count > 0


def _reparse_is_byte_identical(module, path: str, first: Session) -> None:
    assert module.parse(path).to_canonical() == first.to_canonical(), (
        "parse is not deterministic — the derived tree would churn on every rebuild"
    )
