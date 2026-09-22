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

    Every hostile id gets a file it *would* resolve to, in both filename shapes
    this adapter globs. The version before this planted nothing, so `is None`
    held because the directory was empty — deleting `_SESSION_ID_RE` and the
    containment filter outright left all three assertions green. That is the
    same vacuity `test_claude_code.py::test_find_session_refuses_a_hostile_id`
    documents having been found and fixed once already; copying the shape of
    the test and not the reason it exists reintroduced it.
    """
    sessions = tmp_path / "sessions"
    root = sessions / "--w--"
    root.mkdir(parents=True)
    (root / "real.jsonl").write_text("")
    # A dotfile the glob matches the moment the leading-alphanumeric rule goes,
    # and an over-length name the {0,127} bound is the only thing refusing.
    # Both are inside the root, so containment passes them either way.
    for stem in (".hidden", "x" * 200):
        (root / f"{stem}.jsonl").write_text("")
        (root / f"2026-01-01T00-00-00-000Z_{stem}.jsonl").write_text("")
    # `sessions/*/../secrets.jsonl` realpaths back inside the root, so traversal
    # is not caught by containment either — only by the charset.
    (sessions / "secrets.jsonl").write_text("")
    (tmp_path / "secrets.jsonl").write_text("")

    found = pi.find_session(hostile, str(sessions))
    assert found is None, f"escaped or widened the search: {found}"


def test_find_session_ignores_a_symlink_pointing_out_of_the_root(tmp_path):
    """A resume-rewrite leaves duplicates; a symlink leaves someone else's file.

    The containment filter is the only thing between a planted link and this
    machine's own history, which is the one thing this project must never read.
    """
    root = tmp_path / "sessions" / "--w--"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "s1.jsonl").write_text("")
    (root / "s1.jsonl").symlink_to(outside / "s1.jsonl")
    assert pi.find_session("s1", str(tmp_path / "sessions")) is None


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
