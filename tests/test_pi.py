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
import subprocess
import sys

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


def test_a_repeated_entry_id_is_dropped_and_counted(tmp_path):
    """Two lines claiming one id: the second is not a turn, and not silent.

    A resume-rewrite can replay an entry. Keeping both would give two turns one
    identity, which the store's own uniqueness rule then has to resolve by
    luck; dropping the second without counting it would break the accounting
    rule that every line read is either a turn or a `skipped` tally.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", [{"type": "text", "text": "first"}], entry_id="e1"),
            msg("user", [{"type": "text", "text": "replay"}], entry_id="e1"),
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert turn.blocks[0].text == "first"
    assert session.skipped["duplicate_id"] == 1


def test_a_repeated_entry_id_resolves_to_the_first_offset(tmp_path):
    """The cut point is where the id was *first* seen, not last.

    The second line carrying an id is dropped as a duplicate and produces no
    turn. Letting it overwrite the offset would point a compaction cut at a
    line the reader was told does not exist — a byte offset into nothing.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", [{"type": "text", "text": "first"}], entry_id="e1"),
            msg("user", [{"type": "text", "text": "replay"}], entry_id="e1"),
            {
                "type": "compaction",
                "id": "c1",
                "parentId": "e1",
                "timestamp": "2026-01-01T00:00:09.000Z",
                "summary": "# Context Checkpoint",
                "firstKeptEntryId": "e1",
                "tokensBefore": 1,
            },
        ],
    )
    session = check_adapter(pi, path)
    (event,) = session.events
    assert event.meta["first_kept_byte_offset"] == session.turns[0].byte_offset


def test_a_context_edit_keeps_the_replacement_text(tmp_path):
    """`context_edit` wraps its prose one level down, in `replacement`.

    The replacement is what supersedes the target's contribution to model
    context. Reading only the top level left it in `native` — so the store
    answered with the superseded text and the new text was unfindable, which
    is the store returning a version of the session the model never saw.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", [{"type": "text", "text": "original"}], entry_id="e1"),
            {
                "type": "context_edit",
                "id": "e2",
                "parentId": "e1",
                "timestamp": "2026-01-01T00:00:02.000Z",
                "targetId": "e1",
                "replacement": {"content": "redacted by the user"},
            },
        ],
    )
    session = check_adapter(pi, path)
    assert session.turns[-1].blocks[0].text == "redacted by the user"


def test_a_nested_wrapper_does_not_override_the_entrys_own_text(tmp_path):
    """The nested read is a fallback, not a preference.

    `_PROSE_KEYS` and the keys inside `replacement` are the same words —
    `content` is both — so "look one level in" is only unambiguous while the
    top level has nothing to say. An entry carrying both is a tie, and the
    entry's own text wins it: a wrapper annotates a record, it does not
    replace the record's own prose.

    No upstream `ContextEditEntry` has a top-level `content`
    (`session-manager.ts:174-180` @a8ed4977), and no fixture in either fork has
    a `context_edit` at all, so this pins a tie the format permits and the
    corpus has never shown. That is the point: the tie-break was decided by
    statement order, and statement order is not a decision anyone can read.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            msg("user", [{"type": "text", "text": "original"}], entry_id="e1"),
            {
                "type": "context_edit",
                "id": "e2",
                "parentId": "e1",
                "timestamp": "2026-01-01T00:00:02.000Z",
                "targetId": "e1",
                "content": "the entry's own text",
                "replacement": {"content": "the wrapper's text"},
            },
        ],
    )
    session = check_adapter(pi, path)
    assert session.turns[-1].blocks[0].text == "the entry's own text"


def test_an_aborted_assistant_turn_keeps_its_error(tmp_path):
    """`errorMessage` sits next to `content`, and `content` is then empty.

    In the shipped fixtures that is 19 and 22 assistant messages per fork, 13
    and 14 of them with nothing in `content` at all: turns with zero blocks
    whose only prose is the reason the transcript goes quiet. Appended rather
    than substituted — a message can carry both.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("assistant", [], errorMessage="Request was aborted."),
            msg(
                "assistant",
                [{"type": "text", "text": "partial"}],
                errorMessage="Request was aborted.",
            ),
        ],
    )
    aborted, partial = check_adapter(pi, path).turns
    assert [b.text for b in aborted.blocks] == ["Request was aborted."]
    assert [b.text for b in partial.blocks] == ["partial", "Request was aborted."]
    # `native` is the file's own object everywhere else, so the one block
    # gitmemory composed says so rather than carrying a bare `{}` that reads
    # as "this entry had no fields". [review: correctness]
    assert aborted.blocks[0].native == {"gitmemory_synthesized": "errorMessage"}


def test_an_error_message_that_is_not_prose_is_not_read_as_prose(tmp_path):
    """`errorMessage?: string` is upstream's type, not the file's guarantee.

    A transcript is untrusted input, and a `Block.text` is a string by the
    record model — an object or a number there is a `to_canonical()` failure at
    commit time, long after the parse that accepted it, which is the same shape
    as the `_str_or_none` rule one file over. `is not None` would let all three
    through; the guard is `isinstance`.

    The empty string is checked in the same breath, because a zero-length block
    is a row in the index that says nothing and matches nothing.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("assistant", [{"type": "text", "text": "a"}], errorMessage={"code": 500}),
            msg("assistant", [{"type": "text", "text": "b"}], errorMessage=500),
            msg("assistant", [{"type": "text", "text": "c"}], errorMessage=""),
        ],
    )
    turns = check_adapter(pi, path).turns
    assert [[b.text for b in t.blocks] for t in turns] == [["a"], ["b"], ["c"]]
    # Still in `native`, as every rejected value is.
    assert turns[0].native["message"]["errorMessage"] == {"code": 500}


def test_a_failed_background_call_keeps_the_reason_it_failed(tmp_path):
    """The same field, on the entry type that has no `message` at all.

    `ModelUsageEntry` declares `errorMessage?: string`
    (`session-entries.ts:89` @b52e1f5) and is how a tiny/smol call — a title,
    a classification — records itself. When one fails, the whole explanation
    lived only in `native`: the entry has no `content` to fall back on, so the
    turn had zero blocks and a search for why the background work stopped
    returned nothing, which is the same defect as the assistant case above
    reached through a different entry type.

    Synthetic rather than corpus-pinned on purpose: the four shipped fixtures
    contain no failed `model_usage` entry, so nothing here would have caught
    it. [review: correctness]
    """
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
                "role": "tiny",
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "usage": {"input": 4, "output": 0},
                "errorMessage": "Rate limited after 3 retries.",
            },
        ],
    )
    (turn,) = check_adapter(pi, path).turns
    assert [b.text for b in turn.blocks] == ["Rate limited after 3 retries."]
    assert turn.model == "anthropic/claude-haiku-4-5"


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
    """Two namespaces in one field name. Folding them makes the header collide.

    The collision is only observable through a compaction that cuts at the
    shared id: fold the two and `first_kept_byte_offset` resolves to byte 0,
    the header, instead of to the message the cut actually names. Without that
    compaction the test passed whether or not the header entered `offset_of`,
    which is the whole defect it is named after. [pair review, Gemini]
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(id="e1"),
            msg("user", "hi", entry_id="e1"),
            {
                "type": "compaction",
                "id": "c1",
                "parentId": "e1",
                "timestamp": "t",
                "summary": "s",
                "firstKeptEntryId": "e1",
            },
        ],
    )
    session = check_adapter(pi, path)
    assert session.session_id == "e1"
    assert session.turns[0].uuid == "e1"
    (event,) = session.events
    assert event.meta["first_kept_byte_offset"] == session.turns[0].byte_offset
    assert event.meta["first_kept_byte_offset"] != 0


def test_the_pre_rename_lineage_field_is_still_read(tmp_path):
    """`SessionHeader.branchedFrom` became `parentSession` (pi CHANGELOG).

    Both shipped `before-compaction.jsonl` headers carry the old name, so
    reading only the new one loses the lineage on every file written before the
    rename — which is every file anybody already has.
    """
    old = write(tmp_path, "old.jsonl", [header(branchedFrom="/w/prev.jsonl"), msg("user", "hi")])
    new = write(tmp_path, "new.jsonl", [header(parentSession="/w/prev.jsonl"), msg("user", "hi")])
    assert check_adapter(pi, old).parent_session_id == "/w/prev.jsonl"
    assert check_adapter(pi, new).parent_session_id == "/w/prev.jsonl"


def test_the_model_carries_forward_in_byte_order(tmp_path):
    """Four kinds of line name a model and they disagree on how to spell it.

    The header splits it into `provider` + `modelId`; a `model_change` writes
    one joined string on a current build and the split pair on every one of the
    twelve in the shipped fixtures; an assistant message splits it as `provider`
    + `model`. Reading only the joined spelling made `model_change` a no-op on
    all four corpus files — with nothing red, because the carry-forward from the
    header still produced a plausible answer.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("assistant", "a"),
            {  # the spelling every fixture uses
                "type": "model_change",
                "id": "m1",
                "parentId": None,
                "timestamp": "t",
                "provider": "anthropic",
                "modelId": "claude-sonnet-4-5",
            },
            msg("assistant", "b"),
            {  # the spelling oh-my-pi's own tests write
                "type": "model_change",
                "id": "m2",
                "parentId": "m1",
                "timestamp": "t",
                "model": "openai/gpt-5",
            },
            msg("user", "c"),
        ],
    )
    session = check_adapter(pi, path)
    # Five turns, not three: a v3 `model_change` carries an id, so it is a node
    # of the DAG and gets the model it just switched to.
    assert [t.model for t in session.turns] == [
        "anthropic/claude-opus-4-5",
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-5",
        "openai/gpt-5",
    ]


def test_an_assistant_message_states_the_model_that_answered_it(tmp_path):
    """And it beats the carry-forward, for that turn and not for the next one.

    `large-session.jsonl` has one aborted `openai/gpt-5.1-codex` call among 452
    anthropic ones. Reading the header alone labels it anthropic; letting it
    clobber the carry-forward labels the other 451 after it openai. Both are
    wrong in the column `index.py` writes as `turns.model`.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(),
            msg("assistant", "a", provider="openai", model="gpt-5.1-codex"),
            msg("assistant", "b"),
        ],
    )
    session = check_adapter(pi, path)
    assert [t.model for t in session.turns] == [
        "openai/gpt-5.1-codex",
        "anthropic/claude-opus-4-5",
    ]


def test_a_custom_message_keeps_its_prose(tmp_path):
    """`custom_message` puts `content` at the top level and has no `message`.

    Zero blocks is the one loss the accounting rule cannot see: the text is in
    `native`, so `skipped` stays empty and the turn count is right, while the
    prose is absent from canonical JSON and from the index.
    """
    path = write(
        tmp_path,
        "s.jsonl",
        [
            header(version=3),
            {
                "type": "custom_message",
                "id": "cm1",
                "parentId": None,
                "timestamp": "t",
                "customType": "skill-prompt",
                "content": "read the deploy runbook",
                "display": True,
            },
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert [(b.kind, b.text) for b in turn.blocks] == [("text", "read the deploy runbook")]


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
    """A call made outside the conversation is not billed to the session model.

    The `model` here is the *bare* id next to a `provider`, which is the shape
    every `model_usage` line in both forks' fixtures actually has. An earlier
    version of this test wrote the joined string in both fields at once — a
    shape nothing upstream emits — and it was the only test holding up a
    `startswith` guard in `_compose_model` that fired zero times in 1,890
    compositions over the corpus. A fabricated input is worse than no test:
    it makes dead code look pinned.
    """
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
                "model": "claude-haiku-4-5",
                "usage": {"input": 12, "output": 3},
            },
        ],
    )
    session = check_adapter(pi, path)
    (turn,) = session.turns
    assert turn.model == "anthropic/claude-haiku-4-5"
    assert turn.usage == {"input": 12, "output": 3}


def test_a_provider_qualified_model_id_is_not_collapsed(tmp_path):
    """`provider` and a slash-bearing `model` name two different things.

    A proxy serving `anthropic/claude-opus-5` under its own provider is the
    case upstream's model-selection issue exists to keep distinct: the id is
    the model's literal name, the provider is who served it. Joining is what
    keeps two copies of one model apart; collapsing on a prefix match throws
    away the only field that tells them apart.
    """
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
                "provider": "openrouter",
                "model": "example/model",
                "usage": {"input": 1, "output": 1},
            },
            {
                "type": "model_usage",
                "id": "u2",
                "parentId": None,
                "timestamp": "t",
                "provider": "anthropic",
                "model": "anthropic/claude-opus-5",
                "usage": {"input": 1, "output": 1},
            },
        ],
    )
    first, second = check_adapter(pi, path).turns
    assert first.model == "openrouter/example/model"
    assert second.model == "anthropic/anthropic/claude-opus-5"


def test_model_usage_without_a_model_does_not_borrow_the_sessions(tmp_path):
    """Null, not the carry-forward. A `model_usage` entry is by construction a
    call some *other* model made — `role: "tiny"`/`"smol"`
    (`session-entries.ts:82-83` @b52e1f5) — so the conversation model is not a
    weak guess for it, it is the one answer known to be wrong.

    An earlier revision fell back here too, reasoning that a call that happened
    should not read as "no model". What that produces is the turn below
    reporting that `anthropic/claude-opus-4-5` burned 12/3 tokens generating an
    auto-title, in a field `dash_spend` groups by. The honest answer is that
    the file does not say. [review: correctness]
    """
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
                "role": "tiny",
                "purpose": "title",
                "usage": {"input": 12, "output": 3},
            },
        ],
    )
    (turn,) = check_adapter(pi, path).turns
    assert turn.model is None
    # The usage is still read — it is the entry's own, and that is the half of
    # the record the file does state.
    assert turn.usage == {"input": 12, "output": 3}


# --- find_session ----------------------------------------------------------


def test_find_session_matches_the_timestamp_prefixed_name(tmp_path):
    root = tmp_path / "sessions"
    (root / "--w--").mkdir(parents=True)
    hit = root / "--w--" / "2026-01-01T00-00-00-000Z_s1.jsonl"
    hit.write_text("")
    assert pi.find_session("s1", str(root)) == str(hit)


def _would_glob(sessions: pathlib.Path, session_id: str) -> set[str]:
    """What `find_session`'s globs reach, with its guards removed — the control.

    A control that called the function under test would measure the guards,
    which are the thing on trial; this measures the *fixture*, which is the
    thing that was wrong. So it needs the glob without the validation, and
    `pi._glob_hits` is exactly that half, called rather than restated.

    It used to be a copy of two implementation lines, which is a control that
    can drift — and drift *green*: change a pattern in the adapter and the
    fixture stops planting anything reachable, every hostile id returns `None`
    because there is nothing to find, and this test goes on passing while
    proving nothing about `_SESSION_ID_RE`. That is the exact failure the
    reachability assertion below was added to catch, so it must not be
    reintroduced by the assertion's own helper. [review: tests]
    """
    return pi._glob_hits(session_id, os.path.realpath(sessions))


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..",
        "../secrets",
        "*",
        "?",
        "[a-z]*",
        "**/id_rsa",
        "a/../../b",
        "",
        ".hidden",
        "x" * 200,
    ],
)
def test_find_session_refuses_a_hostile_id(tmp_path, hostile):
    """The id arrives in a hook payload and is interpolated into a glob.

    Every hostile id gets a file at the exact path each of the two globbed
    filename shapes would resolve to, `..` components and all — created through
    the unnormalised path so the literal intermediate directories the glob has
    to walk exist too. The sessions root is nested four deep so even
    `../../../../` lands inside `tmp_path`.

    Planting is not the assertion, though. Two earlier versions of this test
    passed for the wrong reason: the first planted nothing at all, and the
    second planted for five of the eleven ids, so deleting `_SESSION_ID_RE`
    turned only three of eleven cases red. Both looked like coverage. So
    reachability is measured rather than assumed: the unguarded globs are run
    against the fixture, and if they find nothing the test fails as a bad
    fixture instead of passing as a good guard.

    Seven of the eleven reach their plant, and the vacuity floor asserts the
    other four do *not* rather than skipping them. When `_glob_hits` stopped
    building patterns and started comparing against real directory entries,
    every id holding a separator became unspellable: no filename contains `/`,
    so there is no tree in which `../secrets.jsonl` is an entry. Of the seven
    that are reachable, all land inside the root, so the charset rule is the
    only thing refusing them; for the four that are not, it is now the second
    lock rather than the only one. [review: paths F2]
    """
    sessions = tmp_path / "a" / "b" / "c" / "sessions"
    root = sessions / "--w--"
    root.mkdir(parents=True)
    # A decoy with an ordinary name: what a widened `*` would sweep up.
    (root / "real.jsonl").write_text("")
    for shape in (f"{hostile}.jsonl", f"2026-01-01T00-00-00-000Z_{hostile}.jsonl"):
        target = root / shape
        # `pathlib`'s `/` *resets* to the right operand when it is absolute, so
        # an id of `/etc/passwd` would plant outside `tmp_path` entirely and
        # this loop would do it silently. None of the eleven is absolute today;
        # the twelfth someone adds might be. Fail loudly instead. [review: tests]
        assert str(target.resolve()).startswith(str(tmp_path.resolve()) + os.sep), (
            f"plant escaped tmp_path: {target}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")

    reachable = _would_glob(sessions, hostile)
    if "/" in hostile:
        assert not reachable, f"a filename cannot hold a separator, yet: {reachable}"
    else:
        assert reachable, f"vacuous fixture: nothing to find for {hostile!r}"

    found = pi.find_session(hostile, str(sessions))
    assert found is None, f"escaped or widened the search: {found}"


def test_find_session_does_not_return_a_session_whose_id_ends_in_the_one_asked_for(tmp_path):
    """`*_<id>.jsonl` is a suffix match, and an id may contain `_`.

    `assertValidSessionId` (`session-manager.ts:268` @a8ed4977) permits
    underscores explicitly, so `alpha_beta` is a legal session id and
    `…Z_alpha_beta.jsonl` is its legal filename. Asking for `beta` matched it —
    `*` swallowed `…Z_alpha` — and the function returned *a different
    session's transcript*, which is worse than returning nothing: the caller
    gets a real, parseable file for the wrong conversation.

    The separator is recoverable because the prefix is `fileTimestamp`,
    `timestamp.replace(/[:.]/g, "-")` (`session-manager.ts:1079`), which cannot
    contain `_`. So the first underscore is the boundary.

    Both halves are asserted. Rejecting the near-miss is worthless if it also
    broke the id it is named after, and an id containing `_` is precisely the
    case the fix reasons about. [review: paths]
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    hit = root / "2026-01-01T09-00-00-000Z_alpha_beta.jsonl"
    hit.write_text("")
    sessions = str(tmp_path / "sessions")

    assert _would_glob(tmp_path / "sessions", "beta"), "vacuous fixture: the glob reaches nothing"
    assert pi.find_session("beta", sessions) is None
    assert pi.find_session("alpha_beta", sessions) == str(hit)

    # The other direction: an id whose *tail* is the one asked for is the case
    # above; this is one whose head is. `…Z_beta_alpha.jsonl` is session
    # `beta_alpha`, and `*_alpha.jsonl` matches it, so unlike the `alpha` probe
    # that used to sit here — which asserted `None` against a glob that reached
    # nothing, and held no matter what `_names_session` did — this one is
    # reachable and the guard is what rejects it. [review: tests T-4]
    other = root / "2026-01-01T09-00-00-000Z_beta_alpha.jsonl"
    other.write_text("")
    assert _would_glob(tmp_path / "sessions", "alpha"), "vacuous: nothing to reject for `alpha`"
    assert pi.find_session("alpha", sessions) is None
    assert pi.find_session("beta_alpha", sessions) == str(other)


def test_a_bare_filename_does_not_answer_to_the_suffix_after_its_underscore(tmp_path):
    """The prefix has to be checked, not assumed, because the bare shape exists too.

    `_names_session` splits on the first underscore and compares the tail,
    which is right for `<fileTimestamp>_<id>.jsonl` and wrong for the other
    shape the glob asks for. `alpha_beta.jsonl` is a legal bare filename for
    session `alpha_beta` — `assertValidSessionId` (`session-manager.ts:268`
    @a8ed4977) permits interior underscores — and its tail is `beta`, so asking
    for `beta` got back a different session's transcript. The fix for the
    suffix collision had reintroduced the suffix collision one shape over.

    The prefix is checkable because upstream builds it as
    `new Date().toISOString()` (`:1062`) with `[:.]` replaced by `-` (`:1079`),
    so a real separator is preceded by `YYYY-MM-DDTHH-MM-SS-sssZ` and nothing
    else. Both shapes are asserted, since rejecting the near-miss is worthless
    if it also broke either filename pi actually writes. [review: paths C-1]
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    bare = root / "alpha_beta.jsonl"
    bare.write_text("")
    stamped = root / "2026-01-01T09-00-00-000Z_gamma_delta.jsonl"
    stamped.write_text("")
    sessions = str(tmp_path / "sessions")

    assert _would_glob(tmp_path / "sessions", "beta"), "vacuous fixture: the glob reaches nothing"
    assert pi.find_session("beta", sessions) is None
    assert pi.find_session("delta", sessions) is None
    assert pi.find_session("alpha_beta", sessions) == str(bare)
    assert pi.find_session("gamma_delta", sessions) == str(stamped)


def test_a_folding_variant_of_an_id_does_not_open_another_session(tmp_path):
    """pi's bare-stem branch folds the same way claude_code's does.

    The twin of `test_claude_code.py`'s case of the same name; see it for the
    mechanism. pi is only affected on the bare shape: `*_<id>.jsonl` carries a
    metacharacter, so glob routes it through `fnmatch`, which is
    case-sensitive. That is worth pinning in both directions — the folding hole
    must close, and the suffix shape must not start folding. [review: paths F1]
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    (root / "ABCDEF01-2222.jsonl").write_text("")
    sessions = str(tmp_path / "sessions")

    if not os.path.exists(root / "abcdef01-2222.jsonl"):
        pytest.skip("filesystem does not fold case, so there is nothing to defend against")

    assert pi.find_session("abcdef01-2222", sessions) is None
    assert pi.find_session("ABCDEF01-2222", sessions) == str(root / "ABCDEF01-2222.jsonl")


def test_find_session_ignores_a_symlink_pointing_out_of_the_root(tmp_path):
    """A resume-rewrite leaves duplicates; a symlink leaves someone else's file.

    The containment filter is the only thing between a planted link and this
    machine's own history, which is the one thing this project must never read.

    Ends with a positive control on the same path: replacing the link with a
    real file finds it. Without that, `is None` is equally consistent with the
    lookup being broken for every bare-named file — which it briefly was, since
    nothing else in the suite noticed when the bare glob shape was dropped.
    """
    sessions = tmp_path / "sessions"
    root = sessions / "--w--"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "s1.jsonl").write_text("")
    link = root / "s1.jsonl"
    link.symlink_to(outside / "s1.jsonl")
    assert pi.find_session("s1", str(sessions)) is None

    link.unlink()
    link.write_text("")
    assert pi.find_session("s1", str(sessions)) == str(link)


def test_both_filename_shapes_cost_one_traversal(scandir_budget, tmp_path):
    """pi paid for its second shape in whole tree walks, and no longer does.

    Two glob patterns meant two full recursive walks of the same directory for
    a strictly smaller second result set — the bare shape is rarer than the
    prefixed one and the corpus has none of it. Now the shapes are two string
    comparisons per file inside one walk, so the second one is free.

    Measured as directory reads rather than seconds, for the reason the
    `scandir_budget` fixture gives. The budget is deliberately tight enough that
    walking this four-directory tree twice fails it: the point is the factor,
    not the absolute number. The self-links make it the pi twin of
    `test_a_self_linking_directory_does_not_multiply_the_search` at the same
    time — `**` would still be running. [review: paths F2/F3]
    """
    sessions = tmp_path / "sessions"
    root = sessions / "--w--"
    (root / "nested").mkdir(parents=True)
    hit = root / "2026-01-01T09-00-00-000Z_s1.jsonl"
    hit.write_text("")
    os.symlink(".", root / "a")
    os.symlink(".", root / "b")

    calls = scandir_budget(6)
    assert pi.find_session("s1", str(sessions)) == str(hit)
    assert calls[0] <= 6, calls[0]


def test_a_file_in_the_sessions_root_is_not_a_transcript(tmp_path):
    """The twin of the claude-code case: one directory level is required.

    pi writes `<root>/<encoded-cwd>/<stamp>_<id>.jsonl`, and the pattern this
    replaced spent its `*` on that middle component. Both filename shapes are
    checked here so the guard cannot be half-deleted. [review: paths F2]
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for name in ("s1.jsonl", "2026-01-01T09-00-00-000Z_s1.jsonl"):
        loose = sessions / name
        loose.write_text("")
        assert pi.find_session("s1", str(sessions)) is None, name
        loose.unlink()

    root = sessions / "--w--"
    root.mkdir()
    (root / "s1.jsonl").write_text("")
    assert pi.find_session("s1", str(sessions)) == str(root / "s1.jsonl")


def test_find_session_matches_the_bare_name(tmp_path):
    """The un-prefixed shape is globbed too, and only this says so.

    pi's own writer emits `<ISO-timestamp>_<id>.jsonl`, so the bare pattern
    covers a renamed file or a build that drops the prefix — a second pattern
    justified by nothing in the corpus, which is exactly the kind of code that
    gets deleted as redundant. Dropping it used to leave the whole suite green.
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    hit = root / "s1.jsonl"
    hit.write_text("")
    assert pi.find_session("s1", str(tmp_path / "sessions")) == str(hit)


def test_find_session_ignores_a_directory_with_a_transcripts_name(tmp_path):
    """A directory or FIFO named `<id>.jsonl` is not a transcript.

    Both satisfy the glob and the containment proof; handing one back gives the
    reader something that raises on open, or worse, blocks forever on it.
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    (root / "s1.jsonl").mkdir()
    assert pi.find_session("s1", str(tmp_path / "sessions")) is None

    os.mkfifo(root / "2026-01-01T00-00-00-000Z_s2.jsonl")
    assert pi.find_session("s2", str(tmp_path / "sessions")) is None


_TIE_SCRIPT = """
import sys
sys.path.insert(0, %r)
from gitmemory.adapters import pi
print(pi.find_session("s1", sys.argv[1]) or "")
"""


def test_find_session_breaks_mtime_ties_deterministically(tmp_path):
    """`max` over a set of equal mtimes returns whichever the set yielded first.

    Run in fresh interpreters: within one process set iteration order is a
    constant, so looping proves nothing. With equal mtimes `max` falls through
    to the path, so the lexicographically largest wins — spelled out rather
    than only checked for agreement, because six runs that agree on the wrong
    file are still six runs that agree.
    """
    root = tmp_path / "sessions"
    for proj in ("--a--", "--b--", "--c--"):
        d = root / proj
        d.mkdir(parents=True)
        f = d / "2026-01-01T00-00-00-000Z_s1.jsonl"
        f.write_text("")
        os.utime(f, (1000, 1000))  # identical mtimes: the tie-break is all there is

    src = str(pathlib.Path(__file__).resolve().parent.parent / "src")
    answers = {
        subprocess.run(
            [sys.executable, "-c", _TIE_SCRIPT % src, str(root)],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONHASHSEED=seed),
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "2", "7", "12345", "random")
    }
    expected = str(root / "--c--" / "2026-01-01T00-00-00-000Z_s1.jsonl")
    assert answers == {expected}, (
        f"the same input gave different answers across interpreters: {answers}"
    )


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
