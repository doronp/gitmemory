"""Claude Code adapter: ~/.claude/projects/**/*.jsonl -> canonical records.

Every branch here exists because recon on real transcripts found a way to get
it wrong. DESIGN.md §2.2 is the trap table; each row has a test in
tests/test_claude_code.py. Nothing in this module may touch git, the index, or
derivation.

Two rules the prior art breaks and we do not:
  - Unknown fields survive verbatim in `native`. Nothing is dropped silently.
  - Unparseable input is *counted* in `Session.skipped`, never swallowed.
"""

from __future__ import annotations

import os
from glob import glob

from ..jsonl import iter_records
from ..records import Block, Event, Session, Turn, canonical_json

AGENT = "claude-code"
_DEC = ("utf-8", "surrogateescape")

# Internal line types that carry no uuid/timestamp and no DAG role. Vendored
# from claude-code-log (MIT, Copyright (c) 2025 Daniel Demmel) — field
# knowledge bought with someone else's production crashes. See THIRD_PARTY.md.
SILENT_SKIP_TYPES = frozenset(
    {
        "file-history-snapshot",
        "last-prompt",
        "permission-mode",
        "mode",
        "custom-title",
        "agent-name",
        "agent-color",
        "artifact-publish",
    }
)

_ROLES = {"user": "user", "assistant": "assistant"}


def _get(obj: dict, *names: str, default=None):
    """First present, non-null key. Handles schema drift across CC versions.

    `sessionId` became `session_id` in some builds; `parentUuid` is null at
    every compaction boundary but `logicalParentUuid` survives. Never KeyError.
    """
    for n in names:
        v = obj.get(n)
        if v is not None:
            return v
    return default


def _blocks(content) -> list[tuple[str, str, str | None, dict]]:
    """(kind, text, tool_name, native) per content block.

    `message.content` is a bare string on some user lines and a list on others.
    An unrecognised block keeps its native dict and contributes empty text — we
    never write `str(dict)` into a text field, which is a Python repr and
    unrecoverable.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [("text", content, None, {})]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return [("text", "", None, {"gitmemory_unparsed": repr(type(content).__name__)})]

    out = []
    for item in content:
        if not isinstance(item, dict):
            out.append(("text", item if isinstance(item, str) else "", None, {}))
            continue
        kind = item.get("type")
        if kind == "text":
            out.append(("text", item.get("text") or "", None, item))
        elif kind == "thinking":
            out.append(("thinking", item.get("thinking") or "", None, item))
        elif kind == "tool_use":
            out.append(("tool_use", _flatten(item.get("input")), item.get("name"), item))
        elif kind == "tool_result":
            out.append(("tool_result", _flatten(item.get("content")), None, item))
        else:
            # Unknown block: canonical JSON, so it stays *searchable* as well
            # as recoverable. Empty text would be a retrieval hole — a secret
            # or an error string inside an unrecognised block must still be
            # findable. What we must never write is `str(dict)`: that is a
            # Python repr, not JSON, and nothing can parse it back.
            out.append(("text", canonical_json(item).decode(*_DEC), None, item))
    return out


def _flatten(value) -> str:
    """Searchable text for a structured payload, without pretending it is prose."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return "\n".join(f"{k}: {_flatten(v)}" for k, v in sorted(value.items()))
    return str(value)


def parse(path: str) -> Session:
    """Parse one transcript file. Byte order is the only ordering authority."""
    session = Session(session_id="", agent=AGENT, source_path=os.path.abspath(path))
    skipped: dict[str, int] = {}
    seen_uuids: set[str] = set()
    seq = 0

    def bump(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for rec in iter_records(path, on_error=lambda _ln, _e: bump("json_decode_error")):
        obj = rec.obj
        if not isinstance(obj, dict):
            bump("not_an_object")
            continue

        line_type = obj.get("type")
        if line_type in SILENT_SKIP_TYPES:
            bump(f"skip:{line_type}")
            continue

        sid = _get(obj, "sessionId", "session_id")
        if sid and not session.session_id:
            session.session_id = sid
        session.agent_version = session.agent_version or obj.get("version")
        session.cwd = session.cwd or obj.get("cwd")
        session.git_branch = session.git_branch or obj.get("gitBranch")

        # A compaction writes a `system`/`compact_boundary` line with
        # parentUuid=null, followed by a user line flagged isCompactSummary.
        if line_type == "system" and obj.get("subtype") == "compact_boundary":
            session.events.append(
                Event(sid or session.session_id, seq, "compaction", {"byte_offset": rec.offset})
            )
            seq += 1
            continue

        # A line belongs in the record model iff it carries an identity, not
        # iff its `type` is on an allowlist. Measured over claude-code-log's
        # 162-fixture corpus this splits perfectly: user/assistant/system/
        # attachment/progress all carry `uuid` (4,392 of 4,392); every
        # no-DAG-role line (queue-operation, permission-mode, last-prompt,
        # file-history-snapshot, and Codex's response_item/event_msg) carries
        # none. An allowlist silently drops each new type Claude Code ships —
        # their own corpus contains a `future_synthetic_record` probe for
        # exactly that. This rule absorbs it instead.
        # `user`/`assistant` are turns unconditionally — they carry the
        # conversation, and dropping one over a missing id loses content.
        # Everything else is a turn iff it has an identity.
        uuid = _get(obj, "uuid", "leafUuid")
        role = _ROLES.get(line_type) if isinstance(line_type, str) else None
        if role is None:
            if not uuid:
                bump(f"no_identity:{line_type}")
                continue
            role = "system"

        # sessionId is NOT a stable conversation key — a fork-from-compaction
        # reuses it. Dedup on message uuid, which is stable.
        if uuid and uuid in seen_uuids:
            bump("duplicate_uuid")
            continue
        if uuid:
            seen_uuids.add(uuid)

        raw_message = obj.get("message")
        message: dict = raw_message if isinstance(raw_message, dict) else {}
        raw_usage = message.get("usage")
        content = message.get("content")
        if content is None and isinstance(obj.get("summary"), str):
            content = obj["summary"]  # `type: summary` carries prose outside `message`
        blocks = [
            Block(turn_id="", seq=i, kind=k, text=t, tool_name=n, native=nat)
            for i, (k, t, n, nat) in enumerate(_blocks(content))
        ]
        turn = Turn(
            session_id=sid or session.session_id,
            seq=seq,
            role=message.get("role") or role,
            byte_offset=rec.offset,
            byte_len=rec.length,
            model=message.get("model"),
            ts=obj.get("timestamp"),
            request_id=_get(obj, "requestId", "request_id"),
            uuid=uuid,
            # null at every compaction boundary; logicalParentUuid survives it.
            parent_uuid=_get(obj, "parentUuid", "logicalParentUuid"),
            usage=raw_usage if isinstance(raw_usage, dict) else {},
            is_sidechain=bool(obj.get("isSidechain")),
            # Sidechains anchor on the spawning tool_use, never on time.
            anchor_uuid=_get(obj, "sourceToolAssistantUUID", "toolUseID", "toolUseId"),
            native=obj,
            blocks=blocks,
        )
        session.turns.append(turn)
        if obj.get("isCompactSummary"):
            session.events.append(Event(turn.session_id, seq, "fork", {"turn_id": turn.turn_id}))
        seq += 1

    session.skipped = skipped
    if session.turns:
        # Informational only. Never used to order anything.
        session.started_at = session.turns[0].ts
        session.ended_at = session.turns[-1].ts
    if not session.session_id:
        session.session_id = os.path.splitext(os.path.basename(path))[0]
    for t in session.turns:
        t.session_id = t.session_id or session.session_id
    return session


def find_session(session_id: str, projects_root: str | None = None) -> str | None:
    """Locate a transcript by id without computing the project directory.

    Worktrees move the project dir and Claude's encoding of it is undocumented
    (non-alphanumerics to `-`, plus a base-36 JS string hash past 200 chars).
    Globbing is shorter than reimplementing that and cannot drift with it.
    Newest mtime wins when a worktree left duplicates.
    """
    root = projects_root or os.path.expanduser("~/.claude/projects")
    hits = glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    hits += glob(os.path.join(root, "*", "**", f"{session_id}.jsonl"), recursive=True)
    return max(set(hits), key=os.path.getmtime) if hits else None
