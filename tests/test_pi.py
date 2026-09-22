"""The pi / oh-my-pi adapter. One test per thing the format does differently.

Fixtures are synthetic or third-party (earendil-works/pi and can1357/oh-my-pi,
both MIT). This machine's own history is never read — see DESIGN.md §3. The
third-party fixtures are cloned rather than vendored because every one of them
carries its author's home directory in a `cwd` field.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest
from conformance import check_adapter

from gitmemory.adapters import pi


def write(tmp_path, name, lines) -> str:
    p = tmp_path / name
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(p)


def header(**kw):
    return {
        "type": "session",
        "id": "s1",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "cwd": "/w",
        "provider": "anthropic",
        "modelId": "claude-opus-4-5",
        **kw,
    }


def msg(role, content, entry_id=None, **kw):
    out = {
        "type": "message",
        "timestamp": "2026-01-01T00:00:01.000Z",
        "message": {"role": role, "content": content, **kw},
    }
    if entry_id is not None:
        out["id"] = entry_id
        out["parentId"] = None
    return out


# --- What only this format does -------------------------------------------


def test_the_compaction_cut_is_resolved_through_the_entry_id(tmp_path):
    """A compaction is declared long after the cut it names, so both are kept.

    In the shipped fixtures the compaction record sits 111,630 bytes after its
    own cut point. `byte_offset` is the declaring record — one meaning per
    field — and the resolved cut is in `meta`, provable because the id was read
    out of this same file at that offset.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", [{"type": "text", "text": "keep me"}], entry_id="e1"),
            msg("assistant", [{"type": "text", "text": "and me"}], entry_id="e2"),
            {
                "type": "compaction",
                "id": "c1",
                "parentId": "e2",
                "timestamp": "2026-01-01T00:00:09.000Z",
                "summary": "# Context Checkpoint",
                "firstKeptEntryId": "e2",
                "tokensBefore": 175004,
            },
        ],
    )
    session = check_adapter(pi, path)
    (event,) = session.events
    assert event.kind == "compaction"
    # The declaring record, not the cut.
    assert event.byte_offset == session.turns[-1].byte_offset
    assert event.meta["first_kept_entry_id"] == "e2"
    assert event.meta["first_kept_byte_offset"] == session.turns[1].byte_offset
    assert event.meta["tokens_before"] == 175004


def test_the_legacy_ordinal_is_recorded_but_never_resolved(tmp_path):
    """`firstKeptEntryIndex` indexes an in-memory array, not the file's lines.

    Both shipped `before-compaction.jsonl` fixtures use this form and carry no
    entry ids at all, so there is nothing to resolve against. Turning the
    ordinal into a byte offset would be a silently wrong number; recording it
    verbatim is the honest half.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("user", [{"type": "text", "text": "hi"}]),
            {
                "type": "compaction",
                "timestamp": "2026-01-01T00:00:09.000Z",
                "summary": "# Context Checkpoint",
                "firstKeptEntryIndex": 293,
                "tokensBefore": 1,
            },
        ],
    )
    session = check_adapter(pi, path)
    (event,) = session.events
    assert event.meta["first_kept_entry_index"] == 293
    assert "first_kept_byte_offset" not in event.meta
    # The summary is the densest prose in a real transcript — it is a turn.
    assert session.turns[-1].blocks[0].text == "# Context Checkpoint"


def test_a_bash_execution_keeps_its_command_and_output_apart(tmp_path):
    """`bashExecution` has no `content` key; dropping it loses real text.

    One block would make the command unfindable separately from a long output,
    which is the search that matters.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:02.000Z",
                "message": {
                    "role": "bashExecution",
                    "command": "rg needle",
                    "output": "src/a.py:3:needle",
                    "exitCode": 0,
                },
            },
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert turn.role == "user"
    assert [(b.kind, b.text, b.tool_name) for b in turn.blocks] == [
        ("tool_use", "rg needle", "bash"),
        ("tool_result", "src/a.py:3:needle", None),
    ]


def test_a_tool_result_is_not_indexed_as_prose(tmp_path):
    """Only the message level says a list of text blocks is a tool return."""
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:02.000Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "toolu_01",
                    "toolName": "read",
                    "content": [{"type": "text", "text": "file contents"}],
                },
            },
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert turn.role == "user"
    assert turn.anchor_uuid == "toolu_01"
    assert [(b.kind, b.tool_name) for b in turn.blocks] == [("tool_result", "read")]


def test_an_unknown_role_is_not_billed_as_a_model_call(tmp_path):
    """`message.role` is the only discriminator here, so it decides spend.

    Same reasoning as claude_code's refusal to trust it: the one reading that
    costs money is the one a line must not be able to choose for itself.
    """
    path = write(tmp_path, "s.jsonl", [header(), msg("Assistant", [{"type": "text", "text": "x"}])])
    session = check_adapter(pi, path)
    assert session.turns[0].role == "system"
    assert session.turns[0].native["message"]["role"] == "Assistant"


def test_the_header_and_the_title_slot_are_named_skips(tmp_path):
    """Neither carries content or entry identity, and the accounting says so.

    The title slot is oh-my-pi's rewritable first line — the record that makes
    this format not strictly append-only.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            {
                "type": "title",
                "v": 1,
                "title": "",
                "updatedAt": "2026-01-01T00:00:00.000Z",
                "pad": " " * 200,
            },
            header(version=3),
            msg("user", "hello", entry_id="e1"),
        ],
    )
    session = check_adapter(pi, path)
    assert session.skipped == {"skip:title": 1, "skip:session_header": 1}
    assert session.records_seen == 3
    assert session.cwd == "/w"


def test_the_header_id_is_not_an_entry_id(tmp_path):
    """Two namespaces in one field name. Folding them makes the header collide."""
    path = write(tmp_path, "s.jsonl", [header(id="e1"), msg("user", "hi", entry_id="e1")])
    session = check_adapter(pi, path)
    assert session.session_id == "e1"
    (turn,) = session.turns
    assert turn.uuid == "e1"


def test_the_model_carries_forward_in_byte_order(tmp_path):
    """The header splits provider/modelId; `model_change` writes one string.

    Without the carry-forward every turn has `model=None` and nothing can tell
    which model produced which answer.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("assistant", "a"),
            {
                "type": "model_change",
                "id": "m1",
                "parentId": None,
                "timestamp": "t",
                "model": "openai/gpt-5",
            },
            msg("assistant", "b"),
        ],
    )
    session = check_adapter(pi, path)
    assert [t.model for t in session.turns] == [
        "anthropic/claude-opus-4-5",
        "openai/gpt-5",
        "openai/gpt-5",
    ]


def test_a_branch_summary_is_a_fork(tmp_path):
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", "hi", entry_id="e1"),
            {
                "type": "branch_summary",
                "id": "b1",
                "parentId": "e1",
                "timestamp": "t",
                "fromId": "e1",
                "summary": "picked up from here",
            },
        ],
    )
    session = check_adapter(pi, path)
    (event,) = session.events
    assert event.kind == "fork"
    assert event.meta["from_entry_id"] == "e1"
    assert event.meta["from_byte_offset"] == session.turns[0].byte_offset


def test_a_v3_entry_with_no_content_is_still_a_node(tmp_path):
    """v3 gave the metadata entries DAG identity, so they are nodes.

    The legacy dialect gives them none, and they fall out by name rather than
    through an allowlist that would drop every entry type the next release adds.
    """
    v3 = write(
        tmp_path,
        "v3.jsonl",
        [
            header(version=3),
            {
                "type": "thinking_level_change",
                "id": "t1",
                "parentId": None,
                "timestamp": "t",
                "thinkingLevel": "off",
            },
        ],
    )
    legacy = write(
        tmp_path,
        "legacy.jsonl",
        [header(), {"type": "thinking_level_change", "timestamp": "t", "thinkingLevel": "off"}],
    )
    assert len(check_adapter(pi, v3).turns) == 1
    legacy_session = check_adapter(pi, legacy)
    assert legacy_session.turns == []
    assert legacy_session.skipped["no_identity:thinking_level_change"] == 1


def test_model_usage_keeps_its_own_model_and_usage(tmp_path):
    """A call made outside the conversation is not billed to the session model."""
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            {
                "type": "model_usage",
                "id": "u1",
                "parentId": None,
                "timestamp": "t",
                "purpose": "title",
                "api": "anthropic-messages",
                "provider": "anthropic",
                "model": "anthropic/claude-haiku-4-5",
                "usage": {"input": 12, "output": 3},
            },
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert turn.model == "anthropic/claude-haiku-4-5"
    assert turn.usage == {"input": 12, "output": 3}


# --- find_session ----------------------------------------------------------


def test_find_session_matches_the_timestamp_prefixed_name(tmp_path):
    root = tmp_path / "sessions"
    (root / "--w--").mkdir(parents=True)
    hit = root / "--w--" / "2026-01-01T00-00-00-000Z_s1.jsonl"
    hit.write_text("")
    assert pi.find_session("s1", str(root)) == str(hit)


def test_find_session_refuses_a_traversal_id(tmp_path):
    assert pi.find_session("../../etc/passwd", str(tmp_path)) is None
    assert pi.find_session("*", str(tmp_path)) is None
    assert pi.find_session("", str(tmp_path)) is None


# --- The third-party corpus ------------------------------------------------

# Cloned by tests/fetch_fixtures.sh, gitignored, and never vendored: every
# fixture carries its author's home directory in a `cwd` field.
PI_FIXTURES = [
    pathlib.Path(__file__).resolve().parent.parent
    / ".conformance"
    / name
    / "packages"
    / "coding-agent"
    / "test"
    / "fixtures"
    for name in ("pi", "oh-my-pi")
]


def _corpus() -> list[str]:
    # `or`, not a `get` default: an env var set to the empty string is not an
    # unset one, and `export GITMEMORY_PI_FIXTURES=` is what a shell script
    # whose variable did not expand looks like.
    env = os.environ.get("GITMEMORY_PI_FIXTURES") or ""
    roots = [pathlib.Path(p) for p in env.split(os.pathsep) if p] or PI_FIXTURES
    out: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Resolved and bounded, because a corpus is somebody else's directory
        # and a `*.jsonl` symlink planted in it is read by whatever it points
        # at. The one thing this suite must never read is this machine's own
        # history.
        base = root.resolve()
        out += [str(p) for p in base.rglob("*.jsonl") if p.resolve().is_relative_to(base)]
    return sorted(out)


@pytest.mark.parametrize("path", _corpus())
def test_third_party_corpus(path):
    """pi's and oh-my-pi's MIT fixtures. Not this machine's history."""
    check_adapter(pi, path)


def test_corpus_is_present_or_explicitly_absent():
    """A missing corpus is a green run that tested almost nothing."""
    if _corpus():
        return
    msg_ = "pi corpus missing; run tests/fetch_fixtures.sh"
    if os.environ.get("GITMEMORY_REQUIRE_CORPUS") == "1":
        raise AssertionError(msg_)
    pytest.skip(msg_)
