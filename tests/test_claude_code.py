"""One test per DESIGN.md §2.2 trap. Each was found by recon on real transcripts.

Fixtures are synthetic or third-party (claude-code-log, MIT). This machine's
own history is never read — see DESIGN.md §3.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest
from conformance import check_adapter

from gitmemory.adapters import claude_code as cc
from gitmemory.records import billable_usage


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
            {"type": "system", "subtype": "compact_boundary", "sessionId": "s1",
             "parentUuid": None, "compactMetadata": {"trigger": "manual"}},
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
    path = write(
        tmp_path,
        "s1.jsonl",
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
        [user("u1", "a"), {"type": "some-future-thing", "sessionId": "s1"},
         {"type": "custom-title", "customTitle": "x"}],
    )
    s = check_adapter(cc, path)
    assert s.skipped["no_identity:some-future-thing"] == 1
    assert s.skipped["skip:custom-title"] == 1, "known-internal types get their own counter"


def test_future_line_type_with_a_uuid_is_kept_not_dropped(tmp_path):
    """The allowlist failure mode: a new DAG-participating type must survive."""
    exotic = {"type": "some-future-thing", "uuid": "f1", "sessionId": "s1",
              "parentUuid": "u1", "payload": {"deep": [1, 2]}}
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
        {"type": "attachment", "uuid": "at1", "sessionId": "s1", "parentUuid": "u1",
         "attachment": {"kind": "file"}},
        {"type": "progress", "uuid": "pr1", "sessionId": "s1", "parentUuid": None,
         "toolUseID": "t9", "data": {"pct": 50}},
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
        [{"type": "assistant", "uuid": "a1", "sessionId": "s1",
          "message": {"role": "assistant", "content": [exotic]}}],
    )
    s = check_adapter(cc, path)
    block = s.turns[0].blocks[0]
    assert block.native == exotic, "native payload lost"
    assert block.text == "", "unknown block must not be stringified"
    assert "'" not in block.text, "a Python repr leaked into a text field"


def test_tool_result_dict_content_is_not_reprd(tmp_path):
    path = write(
        tmp_path,
        "s1.jsonl",
        [{"type": "user", "uuid": "u1", "sessionId": "s1", "message": {"role": "user",
          "content": [{"type": "tool_result", "tool_use_id": "t1", "content": {"foo": "bar"}}]}}],
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
            user("u2", "subagent work", isSidechain=True, sourceToolAssistantUUID="a1",
                 timestamp="2020-01-01T00:00:00Z"),
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
    if not _corpus():
        pytest.skip("run tests/fetch_fixtures.sh to enable the third-party corpus")
