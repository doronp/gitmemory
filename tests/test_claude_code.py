"""One test per DESIGN.md §2.2 trap. Each was found by recon on real transcripts.

Fixtures are synthetic or third-party (claude-code-log, MIT). This machine's
own history is never read — see DESIGN.md §3.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest
from conformance import check_adapter

from gitmemory.adapters import claude_code as cc
from gitmemory.adapters.claude_code import billable_usage


def write(tmp_path, name, lines) -> str:
    """Write JSONL from dicts, or raw bytes for the malformed cases."""
    p = tmp_path / name
    if isinstance(lines, bytes):
        p.write_bytes(lines)
    else:
        p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(p)


def user(uuid, text, **kw):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "s1",
        "message": {"role": "user", "content": text},
        **kw,
    }


def assistant(uuid, text, **kw):
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **kw,
    }


# --- Trap: parentUuid is null at every compact_boundary --------------------


def test_logical_parent_survives_compaction(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            user("u1", "hello"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "sessionId": "s1",
                "parentUuid": None,
                "compactMetadata": {"trigger": "manual"},
            },
            user("u2", "after", parentUuid=None, logicalParentUuid="u1", isCompactSummary=True),
        ],
    )
    s = check_adapter(cc, path)
    after = next(t for t in s.turns if t.uuid == "u2")
    assert after.parent_uuid == "u1", "fell back to null instead of logicalParentUuid"
    kinds = [e.kind for e in s.events]
    assert "compaction" in kinds and "fork" in kinds


# --- Trap: usage over-counts 2.79x; <synthetic> rows are not billable ------


def test_usage_dedups_by_request_id(tmp_path):
    # One logical response, three content blocks, cumulative usage repeated.
    cumulative = {"input_tokens": 100, "output_tokens": 50}
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            assistant("a1", "part one", requestId="req_1", message_usage=None),
            assistant("a2", "part two", requestId="req_1"),
            assistant("a3", "part three", requestId="req_1"),
            assistant("a4", "other turn", requestId="req_2"),
        ],
    )
    # inject usage into each assistant line
    lines = [json.loads(x) for x in pathlib.Path(path).read_text().splitlines()]
    for ln in lines:
        ln["message"]["usage"] = dict(cumulative)
        ln["message"]["model"] = "claude-opus-5"
    write(tmp_path, "s1.jsonl", lines)

    s = cc.parse(path)
    naive = sum(t.usage.get("input_tokens", 0) for t in s.turns)
    assert naive == 400, "fixture should over-count if summed naively"
    assert billable_usage(s)["input_tokens"] == 200, "did not dedup by request_id"


def test_synthetic_model_rows_are_not_billed(tmp_path):
    path = write(tmp_path, "s1.jsonl", [assistant("a1", "x", requestId="r1")])
    lines = [json.loads(x) for x in pathlib.Path(path).read_text().splitlines()]
    lines[0]["message"]["model"] = "<synthetic>"
    lines[0]["message"]["usage"] = {"input_tokens": 999}
    write(tmp_path, "s1.jsonl", lines)
    assert billable_usage(cc.parse(path)) == {}


# --- Trap: sessionId is not a stable conversation key ----------------------


def test_duplicate_uuid_is_deduped_and_counted(tmp_path):
    path = write(tmp_path, "s1.jsonl", [user("u1", "a"), user("u1", "a"), user("u2", "b")])
    s = check_adapter(cc, path)
    assert len(s.turns) == 2
    assert s.skipped["duplicate_uuid"] == 1, "dedup must be counted, not silent"


# --- Trap: schema drift; every field optional, never KeyError --------------


def test_snake_case_session_id_and_missing_fields(tmp_path):
    # The filename must NOT be the answer: with an `s1.jsonl` fixture the
    # basename fallback returns "s1" too, so the assertion below passed even
    # when `session_id` was ignored entirely.
    path = write(
        tmp_path,
        "not-the-session-id.jsonl",
        [
            {"type": "user", "session_id": "s1", "message": {"content": "no uuid, no ts"}},
            {"type": "assistant"},  # nothing at all
        ],
    )
    s = check_adapter(cc, path)
    assert s.session_id == "s1", "did not accept session_id as well as sessionId"
    assert len(s.turns) == 2 and s.turns[1].blocks == []


def test_unknown_line_type_is_counted_not_dropped(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            user("u1", "a"),
            {"type": "some-future-thing", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "x"},
        ],
    )
    s = check_adapter(cc, path)
    assert s.skipped["no_identity:some-future-thing"] == 1
    assert s.skipped["skip:custom-title"] == 1, "known-internal types get their own counter"


def test_future_line_type_with_a_uuid_is_kept_not_dropped(tmp_path):
    """The allowlist failure mode: a new DAG-participating type must survive."""
    exotic = {
        "type": "some-future-thing",
        "uuid": "f1",
        "sessionId": "s1",
        "parentUuid": "u1",
        "payload": {"deep": [1, 2]},
    }
    path = write(tmp_path, "s1.jsonl", [user("u1", "a"), exotic])
    s = check_adapter(cc, path)
    assert len(s.turns) == 2, "dropped a line carrying a uuid"
    kept = s.turns[1]
    assert kept.role == "system" and kept.parent_uuid == "u1"
    assert kept.native == exotic, "native payload lost"


def test_attachment_and_progress_lines_are_kept(tmp_path):
    """Both carry uuid+parentUuid in the real corpus; both are DAG nodes."""
    lines = [
        user("u1", "a"),
        {
            "type": "attachment",
            "uuid": "at1",
            "sessionId": "s1",
            "parentUuid": "u1",
            "attachment": {"kind": "file"},
        },
        {
            "type": "progress",
            "uuid": "pr1",
            "sessionId": "s1",
            "parentUuid": None,
            "toolUseID": "t9",
            "data": {"pct": 50},
        },
    ]
    s = check_adapter(cc, path := write(tmp_path, "s1.jsonl", lines))
    assert [t.uuid for t in s.turns] == ["u1", "at1", "pr1"]
    assert s.turns[2].anchor_uuid == "t9"
    assert path  # keeps ruff quiet about the walrus


def test_summary_line_keeps_its_prose(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [{"type": "summary", "summary": "we chose sqlite", "leafUuid": "u9", "sessionId": "s1"}],
    )
    s = check_adapter(cc, path)
    assert s.turns[0].blocks[0].text == "we chose sqlite"


# --- Trap: never stringify an unknown block into text ----------------------


def test_unknown_block_keeps_native_and_never_reprs(tmp_path):
    exotic = {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"q": "x"}}
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": "s1",
                "message": {"role": "assistant", "content": [exotic]},
            }
        ],
    )
    s = check_adapter(cc, path)
    block = s.turns[0].blocks[0]
    assert block.native == exotic, "native payload lost"
    # Searchable AND recoverable: valid JSON, never a Python repr. Empty text
    # would hide a secret or an error string inside an unrecognised block.
    assert json.loads(block.text) == exotic, f"not re-parseable JSON: {block.text!r}"
    assert "'" not in block.text, "a Python repr leaked into a text field"
    assert "web_search" in block.text, "unknown block is not searchable"


def test_tool_result_dict_content_is_not_reprd(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "s1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": {"foo": "bar"}}
                    ],
                },
            }
        ],
    )
    s = check_adapter(cc, path)
    text = s.turns[0].blocks[0].text
    assert text == "foo: bar", f"expected flattened text, got {text!r}"
    assert not text.startswith("{"), "Python repr leaked"


# --- Trap: six line types have no timestamp; byte order is the only order --


def test_out_of_order_timestamps_do_not_reorder(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            user("u1", "first", timestamp="2026-09-20T10:00:00Z"),
            user("u2", "second", timestamp="2026-01-01T00:00:00Z"),  # earlier clock, later byte
            user("u3", "third"),  # no timestamp at all
        ],
    )
    s = check_adapter(cc, path)
    assert [t.uuid for t in s.turns] == ["u1", "u2", "u3"], "sorted by timestamp somewhere"


# --- Trap: sidechain interleaving anchors on toolUseId, not time -----------


def test_sidechain_anchors_on_tool_use_not_time(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            assistant("a1", "spawning"),
            user(
                "u2",
                "subagent work",
                isSidechain=True,
                sourceToolAssistantUUID="a1",
                timestamp="2020-01-01T00:00:00Z",
            ),
        ],
    )
    s = check_adapter(cc, path)
    sub = s.turns[1]
    assert sub.is_sidechain and sub.anchor_uuid == "a1"


# --- Trap: worktrees move the project dir; glob, never compute the path ----


def test_find_session_globs_and_prefers_newest(tmp_path):
    root = tmp_path / "projects"
    for i, proj in enumerate(("-Users-x-work", "-Users-x-work-wt")):
        d = root / proj
        d.mkdir(parents=True)
        f = d / "abc-123.jsonl"
        f.write_text("{}\n")
        os.utime(f, (1000 + i * 1000, 1000 + i * 1000))
    found = cc.find_session("abc-123", str(root))
    assert found is not None and found.endswith("-Users-x-work-wt/abc-123.jsonl")
    assert cc.find_session("nope", str(root)) is None


# --- jsonl.py vendored behaviour: the two things a rewrite gets wrong ------


def test_concatenated_objects_on_one_line(tmp_path):
    raw = (json.dumps(user("u1", "a")) + json.dumps(user("u2", "b")) + "\n").encode()
    path = write(tmp_path, "s1.jsonl", raw)
    s = check_adapter(cc, path)
    assert [t.uuid for t in s.turns] == ["u1", "u2"], "lost an object sharing a physical line"
    assert s.turns[0].byte_offset == 0
    assert s.turns[1].byte_offset == len(json.dumps(user("u1", "a")))


def test_multibyte_before_a_second_object_on_the_same_line(tmp_path):
    """The offset of the *second* object has to count bytes, not characters.

    Every other offset test puts multi-byte text on a line of its own, where
    the line start alone carries the offset. Only a second object on the same
    line makes the within-line cursor observable — and that cursor is now
    carried rather than recomputed, so a character count here reads as a
    plausible offset that addresses the middle of a UTF-8 sequence. [E3]
    """
    raw = (
        json.dumps(user("u1", "ƒƒƒ"), ensure_ascii=False) + json.dumps(user("u2", "b")) + "\n"
    ).encode()
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", raw))  # re-reads each span from disk

    assert [t.uuid for t in s.turns] == ["u1", "u2"]
    assert s.turns[1].byte_offset == raw.index(b'{"type": "user", "uuid": "u2"')


def test_invalid_utf8_keeps_byte_true_offsets(tmp_path):
    bad = json.dumps(user("u1", "PLACEHOLDER")).replace("PLACEHOLDER", "caf\\u00e9")
    raw = bad.encode() + b"\n" + json.dumps(user("u2", "next")).encode() + b"\n"
    raw = raw.replace(b"caf\\u00e9", b"caf\xe9")  # a raw latin-1 byte: invalid UTF-8
    path = write(tmp_path, "s1.jsonl", raw)
    s = check_adapter(cc, path)  # _spans_are_real re-reads each span from disk
    assert len(s.turns) == 2
    assert s.turns[1].byte_offset == raw.index(b'{"type": "user", "uuid": "u2"')


def test_truncated_final_line_is_counted(tmp_path):
    raw = json.dumps(user("u1", "ok")).encode() + b"\n" + b'{"type":"user","uuid":"u2"'
    path = write(tmp_path, "s1.jsonl", raw)
    s = check_adapter(cc, path)
    assert len(s.turns) == 1
    assert s.skipped["json_decode_error"] == 1, "a torn tail must be reported, not swallowed"


# --- Determinism across the whole corpus ----------------------------------


def _corpus() -> list[str]:
    root = os.environ.get("GITMEMORY_CC_FIXTURES", "/tmp/gm-e0/claude-code-log/test/test_data")
    if not os.path.isdir(root):
        return []
    return sorted(str(p) for p in pathlib.Path(root).rglob("*.jsonl"))


@pytest.mark.parametrize("path", _corpus())
def test_third_party_corpus(path):
    """claude-code-log's 162 MIT fixtures. Not this machine's history."""
    check_adapter(cc, path)


def test_corpus_is_present_or_explicitly_absent():
    """A missing corpus is a green CI run that tested almost nothing.

    Locally a skip is right — not everyone has fetched the fixtures. Anywhere
    that must not silently lose 162 cases sets GITMEMORY_REQUIRE_CORPUS=1 and
    this becomes a failure.
    """
    if _corpus():
        return
    msg = "third-party corpus missing; run tests/fetch_fixtures.sh"
    if os.environ.get("GITMEMORY_REQUIRE_CORPUS") == "1":
        raise AssertionError(msg)
    pytest.skip(msg)


# --- Review round 1 (Gemini pair-review): id stability and DAG integrity ---


def test_turn_ids_survive_a_skip_rule_change(tmp_path):
    """The churn trap: ids must not be a function of a counter over kept lines.

    A line we skip today may be a line we keep tomorrow (the identity rule in
    this adapter already changed once). If `seq` were in `turn_id`, adding or
    removing one skipped line would renumber every later turn and rewrite the
    whole committed tree — the exact churn this project exists to prevent.
    """
    convo = [user("u1", "a"), assistant("a1", "b"), user("u2", "c")]
    before = cc.parse(write(tmp_path, "s1.jsonl", convo))
    # `attachment` was dropped by this adapter yesterday and is kept today.
    # That rule change must not rewrite the ids of the turns around it.
    now_kept = {
        "type": "attachment",
        "uuid": "at1",
        "sessionId": "s1",
        "parentUuid": "u1",
        "attachment": {"kind": "file"},
    }
    after = cc.parse(write(tmp_path, "s2.jsonl", [convo[0], now_kept, convo[1], convo[2]]))

    unchanged = [t for t in after.turns if t.uuid != "at1"]
    assert [t.seq for t in unchanged] == [0, 2, 3], "fixture does not renumber; it proves nothing"
    assert [t.turn_id for t in before.turns] == [t.turn_id for t in unchanged], (
        "a newly-kept line renumbered its neighbours — every later tree object would churn"
    )


def test_turn_id_changes_when_content_changes(tmp_path):
    """The other half: the same uuid with different bytes must NOT collide."""
    a = cc.parse(write(tmp_path, "a.jsonl", [user("u1", "original")]))
    b = cc.parse(write(tmp_path, "b.jsonl", [user("u1", "rewritten")]))
    assert a.turns[0].turn_id != b.turns[0].turn_id, "a rewrite would be invisible in the diff"


def test_parent_uuids_resolve_within_the_session(tmp_path):
    """A DAG with dangling edges is broken memory. Roots are legitimate; strays are not."""
    path = write(
        tmp_path,
        "s1.jsonl",
        [user("u1", "a"), assistant("a1", "b", parentUuid="u1"), user("u2", "c", parentUuid="a1")],
    )
    s = check_adapter(cc, path)
    known = {t.uuid for t in s.turns if t.uuid}
    dangling = [t.uuid for t in s.turns if t.parent_uuid and t.parent_uuid not in known]
    assert dangling == [], f"parent_uuid points outside the session: {dangling}"


def test_canonical_output_survives_lone_surrogates(tmp_path):
    """Byte fidelity means text may hold lone surrogates. Our own path must not crash."""
    raw = json.dumps(user("u1", "X")).encode().replace(b'"X"', b'"caf\xe9"') + b"\n"
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", raw))
    blob = s.to_canonical()  # must not raise UnicodeEncodeError
    assert blob.decode("utf-8", "surrogateescape")
    assert s.turns[0].blocks[0].content_sha256, "hashing a surrogate-bearing block failed"


@pytest.mark.parametrize("path", _corpus())
def test_canonical_output_has_no_floats(path):
    """Floats are the one JSON type whose text form is language-dependent.

    They never reach the manifest by construction, but if they started
    appearing in canonical output the cross-language story would need revising
    rather than silently drifting.
    """
    doc = json.loads(cc.parse(path).to_canonical().decode("utf-8", "surrogateescape"))
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, float):
            raise AssertionError(f"float reached canonical output in {path}")
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


# --- Review round 2: the 35 confirmed findings of the six-lens review ------
#
# Every test below reproduces a defect that shipped in E1 and was found by the
# standalone review round, not by the tests written alongside the code. They
# are grouped by what the defect cost: a crash, lost content, or a wrong id.


# Crashes on input a real transcript can contain.


def test_lone_high_surrogate_does_not_abort_the_parse(tmp_path):
    r"""`\ud800` is legal JSON and decodes to a lone *high* surrogate.

    JavaScript emits one whenever a tool result is sliced mid-pair, so this is
    ordinary input, not an attack. `surrogateescape` only round-trips
    U+DC80-U+DCFF, so encoding with it raised UnicodeEncodeError and took the
    whole transcript down — one bad character lost every turn in the file.
    """
    raw = (
        json.dumps(user("u1", "PLACEHOLDER")).replace("PLACEHOLDER", r"a\ud800b")
        + "\n"
        + json.dumps(user("u2", "survivor"))
        + "\n"
    ).encode()
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", raw))
    assert [t.uuid for t in s.turns] == ["u1", "u2"], "one surrogate lost the whole file"
    assert "\ud800" in s.turns[0].blocks[0].text, "the character itself must survive"


def test_canonical_json_is_ascii_and_reparses_after_surrogates(tmp_path):
    """Canonical JSON must be re-readable by our own reader.

    With ensure_ascii off, surrogateescape'd bytes passed straight through and
    the committed "canonical JSON" was not valid UTF-8 — unparseable by us and
    by `jq`, which is the whole third-party-checkability claim.
    """
    raw = json.dumps(user("u1", "X")).encode().replace(b'"X"', b'"caf\xe9\xff"') + b"\n"
    blob = check_adapter(cc, write(tmp_path, "s1.jsonl", raw)).to_canonical()
    blob.decode("ascii")  # must not raise
    assert json.loads(blob)["turns"], "our own reader cannot read what we committed"


@pytest.mark.parametrize(
    "line",
    [
        {"type": {"not": "a string"}, "uuid": "u1"},
        {"type": "user", "uuid": {"not": "a string"}},
        {"type": ["list"], "uuid": ["also a list"]},
        {"type": "user", "uuid": 17},
    ],
)
def test_non_string_type_or_uuid_is_counted_not_a_typeerror(tmp_path, line):
    """`line_type in SILENT_SKIP_TYPES` raised on an unhashable value.

    A single line with a dict `type` aborted the parse with TypeError, before
    the guard twenty lines further down that already handled the same value.
    """
    path = write(tmp_path, "s1.jsonl", [user("ok", "kept"), line])
    s = check_adapter(cc, path)
    assert any(t.uuid == "ok" for t in s.turns), "a malformed neighbour lost a good turn"


def test_deeply_nested_payload_does_not_recurse_to_death(tmp_path):
    """json.loads accepts structures deeper than a naive projection can walk."""
    deep = {"a": 1}
    for _ in range(600):
        deep = {"n": deep}
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {"role": "user", "content": [{"type": "tool_use", "name": "T", "input": deep}]},
    }
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", [line]))
    assert "nesting too deep" in s.turns[0].blocks[0].text


def test_non_finite_numbers_reach_canonical_json_as_text(tmp_path):
    """The reader accepts bare NaN; the writer rejects it. That gap was a crash.

    `json.JSONDecoder` parses `NaN`/`Infinity` by default, `canonical_json`
    sets allow_nan=False by design, so a transcript written by a Python
    harness raised ValueError at commit time — long after the parse that
    accepted it, and counted nowhere.
    """
    raw = (
        b'{"type":"assistant","uuid":"a1","sessionId":"s1","requestId":"r1",'
        b'"message":{"role":"assistant","model":"m","usage":{"input_tokens":NaN},'
        b'"content":[{"type":"text","text":"hi"}]}}\n'
    )
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", raw))
    assert s.turns[0].usage["input_tokens"] == "nan", "NaN must become text, not a crash"
    assert cc.billable_usage(s) == {}, "a non-int token count must not be summed"


# Content that was silently lost.


def test_summary_survives_a_leafuuid_that_points_at_a_kept_turn(tmp_path):
    """`leafUuid` is a *pointer*; folding it into the identity namespace lost it.

    A summary line's leafUuid names the turn it summarises. Treating that as
    the summary's own uuid made it collide with that turn and lose the dedup
    race, so the summary was dropped and counted as `duplicate_uuid`. Measured:
    3 lost summaries on the MIT corpus, which read as correct dedup.
    """
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            user("u1", "the conversation"),
            {"type": "summary", "summary": "we decided X", "leafUuid": "u1"},
        ],
    )
    s = check_adapter(cc, path)
    assert s.skipped.get("duplicate_uuid", 0) == 0, "a pointer was mistaken for an identity"
    kept = [t for t in s.turns if (t.native or {}).get("type") == "summary"]
    assert len(kept) == 1 and "we decided X" in kept[0].blocks[0].text
    assert kept[0].ref_uuid == "u1" and kept[0].uuid is None


def test_compact_boundary_is_a_turn_as_well_as_an_event(tmp_path):
    """The boundary line carries a uuid and parents the summary that follows.

    Dropping it emitted the event but deleted the node, leaving the next turn's
    parent_uuid pointing at nothing and losing `compactMetadata` entirely.
    """
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            user("u1", "before"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "cb1",
                "sessionId": "s1",
                "parentUuid": "u1",
                "compactMetadata": {"trigger": "auto", "preTokens": 150000},
            },
            user("u2", "after", parentUuid="cb1", isCompactSummary=True),
        ],
    )
    s = check_adapter(cc, path)
    known = {t.uuid for t in s.turns if t.uuid}
    assert "cb1" in known, "the boundary node was deleted from the DAG"
    assert [t.uuid for t in s.turns if t.parent_uuid and t.parent_uuid not in known] == []
    ev = next(e for e in s.events if e.kind == "compaction")
    assert ev.meta["compact_metadata"]["trigger"] == "auto", "compactMetadata was lost"


def test_system_and_attachment_prose_outside_message_is_indexed(tmp_path):
    """Text was read only from `message.content`, so most of it was invisible.

    On the MIT corpus that was 122 of 133 `system` lines and all 54
    `attachment` payloads producing zero searchable blocks.
    """
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            {
                "type": "system",
                "uuid": "s1a",
                "sessionId": "s1",
                "content": "Model changed to opus",
            },
            {
                "type": "attachment",
                "uuid": "at1",
                "sessionId": "s1",
                "attachment": {
                    "kind": "file",
                    "path": "notes.md",
                    "content": "the deploy key rotates on Fridays",
                },
            },
        ],
    )
    s = check_adapter(cc, path)
    text = " ".join(b.text for t in s.turns for b in t.blocks)
    assert "Model changed to opus" in text
    assert "rotates on Fridays" in text, "attachment payload never became searchable"


def test_queue_operation_remove_keeps_its_human_text(tmp_path):
    """`operation: remove` carries a real steering message; `dequeue` carries none."""
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            {
                "type": "queue-operation",
                "operation": "remove",
                "sessionId": "s1",
                "content": "actually - focus on the parser first",
            },
            {"type": "queue-operation", "operation": "dequeue", "sessionId": "s1"},
        ],
    )
    s = check_adapter(cc, path)
    assert len(s.turns) == 1, "kept a contentless marker, or dropped real prose"
    assert "focus on the parser first" in s.turns[0].blocks[0].text
    assert s.skipped["no_identity:queue-operation"] == 1


def test_image_payload_is_not_inlined_into_the_index(tmp_path):
    """Base64 was 61.7% of all indexed text on the corpus before this branch.

    The bytes stay in `native` and in the raw segment; what the index gets is
    that an image of a given type and size was here.
    """
    blob = "A" * 50_000
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": blob},
                }
            ],
        },
    }
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", [line]))
    b = s.turns[0].blocks[0]
    assert blob not in b.text and len(b.text) < 100
    assert "image/png" in b.text and "50000" in b.text
    assert b.native["source"]["data"] == blob, "the payload must survive in native"


def test_flatten_keeps_siblings_of_a_text_key(tmp_path):
    """The dict shortcut fired on any dict with a `text` key and dropped the rest."""
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": {
                        "text": "ok",
                        "exit_code": 137,
                        "stderr": "killed by the oom reaper",
                    },
                }
            ],
        },
    }
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", [line]))
    text = s.turns[0].blocks[0].text
    assert "oom reaper" in text and "137" in text, "siblings of `text` were discarded"


# Wrong ids.


def test_session_id_is_resolved_before_any_id_is_computed(tmp_path):
    """Ids must not be keyed on the empty string.

    `sessionId` first appears on line 2 here. The old parser built line 1 with
    session_id="" and back-patched the attribute afterwards without recomputing
    the id, so the id stayed keyed on "" — identical across unrelated sessions,
    and not the documented function of its own fields.
    """
    a = check_adapter(
        cc,
        write(
            tmp_path,
            "a.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "u1",
                    "message": {"role": "user", "content": "same words"},
                },
                user("u2", "later", sessionId="alpha"),
            ],
        ),
    )
    b = check_adapter(
        cc,
        write(
            tmp_path,
            "b.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "u1",
                    "message": {"role": "user", "content": "same words"},
                },
                user("u2", "later", sessionId="beta"),
            ],
        ),
    )
    assert a.turns[0].session_id == "alpha" and b.turns[0].session_id == "beta"
    assert a.turns[0].turn_id != b.turns[0].turn_id, "two sessions minted the same turn_id"


def test_event_ids_do_not_move_when_a_skip_rule_changes(tmp_path):
    """`seq` was removed from turn_id for churn; Event kept it and churned anyway."""
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "cb1",
        "sessionId": "s1",
        "parentUuid": None,
    }
    before = cc.parse(write(tmp_path, "a.jsonl", [user("u1", "x"), boundary]))
    with_extra = cc.parse(
        write(
            tmp_path,
            "b.jsonl",
            [
                user("u1", "x"),
                {"type": "attachment", "uuid": "at1", "sessionId": "s1", "attachment": {"k": 1}},
                boundary,
            ],
        )
    )
    assert before.events[0].event_id == with_extra.events[0].event_id, (
        "a newly-kept line renumbered an event id"
    )


def test_agent_id_and_tool_use_id_stay_in_their_own_namespaces(tmp_path):
    """`anchor_uuid` merged a turn uuid with a content-block id; joins went wrong."""
    path = write(
        tmp_path,
        "s1.jsonl",
        [
            assistant("a1", "spawn"),
            user(
                "u2",
                "sub",
                isSidechain=True,
                sourceToolAssistantUUID="a1",
                toolUseID="toolu_01",
                agentId="agent-xyz",
            ),
        ],
    )
    s = check_adapter(cc, path)
    sub = s.turns[1]
    assert sub.anchor_uuid == "a1", "a block id displaced the turn uuid"
    assert sub.agent_id == "agent-xyz", "agentId was never projected"


def test_skip_reason_keys_are_bounded(tmp_path):
    """Skip reasons key on an untrusted `type` and are written into git."""
    lines = [{"type": "x" * 5000, "sessionId": "s1"}]
    lines += [{"type": f"t{i}", "sessionId": "s1"} for i in range(200)]
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", lines))
    assert all(len(k) <= 64 for k in s.skipped), "an untrusted string reached git unbounded"
    assert len(s.skipped) <= 66, "one file minted an unbounded number of counters"


# Block kinds that had no test at all.


def test_thinking_and_tool_use_blocks_are_projected(tmp_path):
    """Neither kind was covered; dropping either passed the whole E1 suite."""
    line = {
        "type": "assistant",
        "uuid": "a1",
        "sessionId": "s1",
        "requestId": "r1",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "thinking", "thinking": "the lock is held across the await"},
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "pytest -q"},
                },
                {"type": "tool_result", "tool_use_id": "tu1", "content": "12 passed"},
            ],
        },
    }
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", [line]))
    kinds = [b.kind for b in s.turns[0].blocks]
    assert kinds == ["thinking", "tool_use", "tool_result"]
    assert "held across the await" in s.turns[0].blocks[0].text
    assert s.turns[0].blocks[1].tool_name == "Bash"
    assert "pytest -q" in s.turns[0].blocks[1].text
    assert "12 passed" in s.turns[0].blocks[2].text


# --- §2.2 traps that were listed as required and never implemented ---------


def billed(uuid, request_id, usage):
    """An assistant line whose usage is where Claude Code actually puts it."""
    line = assistant(uuid, "x", requestId=request_id)
    line["message"]["usage"] = usage
    line["message"]["model"] = "claude-sonnet-4-5"
    return json.dumps(line) + "\n"


def test_rollup_includes_subagents(tmp_path):
    """Subagents are 92% of files; one session kept 78% of cache reads in them."""
    main = tmp_path / "sess.jsonl"
    main.write_text(billed("a1", "r1", {"input_tokens": 10}))
    subs = tmp_path / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-one.jsonl").write_text(billed("b1", "r2", {"cache_read_input_tokens": 900}))
    # A subagent that spawned its own subagent, nested one level deeper.
    deeper = subs / "agent-one" / "subagents"
    deeper.mkdir(parents=True)
    (deeper / "agent-two.jsonl").write_text(billed("c1", "r3", {"input_tokens": 5}))

    assert len(cc.session_files(str(main))) == 3, "a nested subagent was missed"
    assert cc.billable_usage(cc.parse(str(main))) == {"input_tokens": 10}
    assert cc.rollup_usage(str(main)) == {"input_tokens": 15, "cache_read_input_tokens": 900}


def test_rollup_dedups_a_request_id_seen_in_two_files(tmp_path):
    main = tmp_path / "sess.jsonl"
    main.write_text(billed("a1", "r1", {"input_tokens": 100}))
    subs = tmp_path / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-one.jsonl").write_text(billed("b1", "r1", {"input_tokens": 100}))
    assert cc.rollup_usage(str(main)) == {"input_tokens": 100}, "billed one request twice"


def test_cost_is_estimated_from_a_dated_snapshot_or_not_at_all():
    """There is no costUSD field, so every cost is an estimate with a date."""
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    est = cc.estimate_cost("claude-sonnet-4-5-20260929", usage)
    assert est is not None
    assert est["priced_as"] == "claude-sonnet-4-5", "longest-prefix match failed"
    assert est["usd"] == pytest.approx(3.0 + 15.0 + 0.3)
    assert est["estimated"] is True and est["as_of"] == cc.PRICES_AS_OF
    # An unpriced model must read as unknown, never as free.
    assert cc.estimate_cost("some-model-we-have-never-priced", usage) is None
    assert cc.estimate_cost(None, usage) is None


def test_a_prefix_match_never_crosses_a_version_boundary():
    """[E2] `claude-opus-4` used to swallow `claude-opus-4-5` and bill 3x over.

    Silent 3x is worse than unknown: the result still says `estimated: True`
    with today's `as_of`, so it reads as a checked number.
    """
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    opus45 = cc.estimate_cost("claude-opus-4-5-20260101", usage)
    assert opus45 is not None
    assert opus45["priced_as"] == "claude-opus-4-5"
    assert opus45["usd"] == pytest.approx(5.0 + 25.0), "billed at the retired Opus 4 rate"

    assert cc.estimate_cost("claude-opus-4-20250514", usage)["priced_as"] == "claude-opus-4"
    # A version we have not priced reads as unknown, not as its predecessor.
    assert cc.estimate_cost("claude-opus-4-9-20270101", usage) is None
    assert cc.estimate_cost("claude-opus-4-suffixed", usage) is None


# --- Determinism, actually tested across processes -------------------------

_DIGEST_SCRIPT = """
import hashlib, sys
sys.path.insert(0, %r)
from gitmemory.adapters import claude_code as cc
h = hashlib.sha256()
for p in sys.argv[1:]:
    h.update(cc.parse(p).to_canonical())
    h.update(b"\\x00")
print(h.hexdigest())
"""


def _corpus_digest(env_extra: dict) -> str:
    src = str(pathlib.Path(__file__).resolve().parent.parent / "src")
    env = dict(os.environ, **env_extra)
    out = subprocess.run(
        [sys.executable, "-c", _DIGEST_SCRIPT % src, *_corpus()],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return out.stdout.strip()


@pytest.mark.skipif(not _corpus(), reason="third-party corpus not fetched")
def test_canonical_output_is_identical_in_a_fresh_interpreter():
    """The determinism gate, run the only way it means anything.

    `parse(p).to_canonical() == parse(p).to_canonical()` inside one process
    proves almost nothing: it shares the same interned strings, the same dict
    ordering and the same PYTHONHASHSEED. The committed tree is rebuilt by a
    *new* process, so that is where the comparison has to happen — and under
    two different hash seeds, because set iteration order is the classic way a
    build stops being reproducible.
    """
    a = _corpus_digest({"PYTHONHASHSEED": "0"})
    b = _corpus_digest({"PYTHONHASHSEED": "12345"})
    assert a == b, "canonical output depends on PYTHONHASHSEED — the tree would churn"

    h = hashlib.sha256()
    for p in _corpus():
        h.update(cc.parse(p).to_canonical())
        h.update(b"\x00")
    assert h.hexdigest() == a, "a fresh interpreter produced a different tree"


# --- find_session takes an untrusted id from a hook payload ----------------


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
    """The id arrives in a hook payload and was interpolated straight into a glob.

    `..` walked out of the projects root and glob metacharacters widened the
    search to whatever matched — both reachable by anything that can write a
    hook payload.
    """
    root = tmp_path / "projects" / "-Users-x-work"
    root.mkdir(parents=True)
    (root / "real.jsonl").write_text("{}\n")
    outside = tmp_path / "secrets.jsonl"
    outside.write_text("{}\n")
    found = cc.find_session(hostile, str(tmp_path / "projects"))
    assert found is None, f"escaped or widened the search: {found}"


def test_find_session_breaks_mtime_ties_deterministically(tmp_path):
    """`max(set(...))` over equal mtimes returned whichever the set yielded first."""
    root = tmp_path / "projects"
    for proj in ("-a", "-b", "-c"):
        d = root / proj
        d.mkdir(parents=True)
        f = d / "abc-123.jsonl"
        f.write_text("{}\n")
        os.utime(f, (1000, 1000))  # identical mtimes: the tie-break is all there is
    answers = {cc.find_session("abc-123", str(root)) for _ in range(20)}
    assert len(answers) == 1, f"the same input gave different answers: {answers}"


def test_find_session_ignores_a_symlink_pointing_out_of_the_root(tmp_path):
    root = tmp_path / "projects" / "-Users-x-work"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "abc-123.jsonl").write_text("{}\n")
    (root / "abc-123.jsonl").symlink_to(outside / "abc-123.jsonl")
    assert cc.find_session("abc-123", str(tmp_path / "projects")) is None
