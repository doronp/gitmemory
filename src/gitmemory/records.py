"""Canonical records — the agent-agnostic projection every adapter must produce.

Two rules make the committed tree diffable, and both are load-bearing:

1. **Canonical JSON.** Sorted keys, no whitespace, no NaN/Infinity, UTF-8, LF.
   Serialize with `canonical_json` or the determinism gate (build twice,
   `git diff --exit-code`) will fail.
2. **Content-derived IDs.** An id is a function of content, never of insertion
   order, wall-clock, or a counter. Unstable ids churn every tree object on
   rebuild, which destroys the diffability that is the point of the project.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

__all__ = [
    "Block",
    "Event",
    "Session",
    "Turn",
    "canonical_json",
    "sha256_text",
]

# Transcript text can carry lone surrogates (jsonl.py decodes with
# surrogateescape so byte offsets stay true). Every encode on this path must
# therefore use surrogateescape too, or hashing a real transcript raises.
_ENC = ("utf-8", "surrogateescape")


def canonical_json(obj: object) -> bytes:
    """The only serializer allowed to produce bytes that reach git.

    Floats are permitted: CPython's repr is shortest-round-trip and stable
    across platforms, and the only consumer of canonical JSON is our own
    rebuild-twice check. NaN/Infinity are rejected — they are not JSON, and a
    third party verifying the tree with `jq` would choke on them.
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode(*_ENC)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode(*_ENC)).hexdigest()


def _id(*parts: object) -> str:
    return hashlib.sha256(b"\x1f".join(str(p).encode(*_ENC) for p in parts)).hexdigest()


@dataclass(slots=True)
class Block:
    """One content block. `kind` is the queryable projection; `native` is truth.

    An adapter that meets a block shape it does not understand emits
    `kind="text"`, `text=""` and a verbatim `native`. It must never stringify
    the dict into `text` — `str(some_dict)` is a Python repr, not JSON, and is
    unrecoverable. (This is the single most common defect in the prior art.)
    """

    turn_id: str
    seq: int
    kind: str  # text | thinking | tool_use | tool_result
    text: str
    tool_name: str | None = None
    native: dict = field(default_factory=dict)
    content_sha256: str = ""
    block_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("text", "thinking", "tool_use", "tool_result"):
            raise ValueError(f"unknown block kind: {self.kind!r}")
        self.content_sha256 = sha256_text(self.text)
        self.block_id = _id(self.turn_id, self.seq, self.kind, self.content_sha256)


@dataclass(slots=True)
class Turn:
    """One message. `seq` is byte order, never timestamp order.

    `byte_offset`/`byte_len` are an addition to the design's record list: they
    are what lets recall seek instead of scan, and what ties a canonical record
    back to a committed segment. Without them the contiguity proof and the
    record model are two unrelated things.
    """

    session_id: str
    seq: int
    role: str
    byte_offset: int
    byte_len: int
    model: str | None = None
    ts: str | None = None
    request_id: str | None = None
    uuid: str | None = None
    parent_uuid: str | None = None
    usage: dict = field(default_factory=dict)
    is_sidechain: bool = False
    anchor_uuid: str | None = None  # sidechain → the tool_use that spawned it
    native: dict = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    turn_id: str = ""

    def __post_init__(self) -> None:
        digest = sha256_text("".join(b.content_sha256 for b in self.blocks))
        self.turn_id = _id(self.session_id, self.seq, self.role, digest)
        for b in self.blocks:  # blocks are built before the turn_id exists
            b.turn_id = self.turn_id
            b.block_id = _id(b.turn_id, b.seq, b.kind, b.content_sha256)


@dataclass(slots=True)
class Event:
    session_id: str
    seq: int
    kind: str  # session_start | session_end | compaction | fork
    meta: dict = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("session_start", "session_end", "compaction", "fork"):
            raise ValueError(f"unknown event kind: {self.kind!r}")
        self.event_id = _id(self.session_id, self.seq, self.kind)


@dataclass(slots=True)
class Session:
    session_id: str
    agent: str
    source_path: str
    agent_version: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    started_at: str | None = None  # informational only — never a sort key
    ended_at: str | None = None
    parent_session_id: str | None = None
    native: dict = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count, never silent

    def to_canonical(self) -> bytes:
        """Canonical JSON for the whole session. Stable across rebuilds."""
        return canonical_json(
            {
                "session": {
                    k: getattr(self, k)
                    for k in (
                        "session_id",
                        "agent",
                        "agent_version",
                        "cwd",
                        "git_branch",
                        "started_at",
                        "ended_at",
                        "parent_session_id",
                    )
                },
                "turns": [
                    {
                        "turn_id": t.turn_id,
                        "seq": t.seq,
                        "role": t.role,
                        "model": t.model,
                        "ts": t.ts,
                        "request_id": t.request_id,
                        "uuid": t.uuid,
                        "parent_uuid": t.parent_uuid,
                        "byte_offset": t.byte_offset,
                        "byte_len": t.byte_len,
                        "usage": t.usage,
                        "is_sidechain": t.is_sidechain,
                        "anchor_uuid": t.anchor_uuid,
                        "blocks": [
                            {
                                "block_id": b.block_id,
                                "seq": b.seq,
                                "kind": b.kind,
                                "tool_name": b.tool_name,
                                "content_sha256": b.content_sha256,
                            }
                            for b in t.blocks
                        ],
                    }
                    for t in self.turns
                ],
                "events": [
                    {"event_id": e.event_id, "seq": e.seq, "kind": e.kind, "meta": e.meta}
                    for e in self.events
                ],
                "skipped": self.skipped,
            }
        )


def billable_usage(session: Session) -> dict[str, int]:
    """Token totals with Claude Code's 2.79x over-count removed.

    Claude Code writes one assistant line per content block and repeats the
    *cumulative* usage on each, so naive summation over-counts. Recon measured
    2.79x on a real transcript. Dedup by `request_id`; `<synthetic>` rows are
    not billed at all.
    """
    seen: dict[str, dict] = {}
    for t in session.turns:
        if t.role != "assistant" or t.model == "<synthetic>":
            continue
        key = t.request_id or t.uuid or f"seq:{t.seq}"
        seen[key] = t.usage  # last write per request wins: usage is cumulative
    total: dict[str, int] = {}
    for usage in seen.values():
        for k, v in usage.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    return total
