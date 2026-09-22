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
import re
from dataclasses import dataclass, field

__all__ = [
    "Block",
    "Event",
    "Session",
    "Turn",
    "canonical_json",
    "safe_text",
    "sha256_text",
]

# Transcript text can carry lone surrogates: jsonl.py decodes with
# surrogateescape so byte offsets stay true, and a JSON `\ud800` escape is
# *legal input* that decodes to a lone surrogate too — JS emits them whenever a
# tool result is sliced mid-pair. `surrogateescape` only round-trips
# U+DC80–U+DCFF, so encoding with it raises UnicodeEncodeError on the high half
# and aborts the parse of an entire transcript. `surrogatepass` is total over
# every str CPython can hold, which is the only property hashing needs.
_ENC = ("utf-8", "surrogatepass")

# Transcript text an attacker chose reaches two surfaces that *render* it: the
# terminal, via `recall`, and the committed artifacts under `derived/`, which a
# second tool reads and draws. Neither an ANSI escape nor a bidi override is
# ever meaningful in a transcript. `\x1b[2K\r` erases the line it was found on,
# which is how one recall hit hides the hit above it; U+202E reverses the
# apparent order of whatever is left; the zero-width characters hide a
# difference between two labels that look identical.
#
# The four singletons on the second line are the ones this class used to miss.
# `derive._INVISIBLE` has enumerated them since E5 — soft hyphen, the Mongolian
# vowel separator, the word joiner, the byte-order mark — for exactly the
# reason written above: invisible in the client, and a difference between two
# strings a reader cannot see. Two gates disagreeing about what "invisible"
# means is how one of them ends up wrong; they agree now. [E7b L3-F4]
UNSAFE = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f"  # C0 except tab and newline, DEL, C1
    "\u00ad\u180e\u2060\ufeff"  # soft hyphen, MVS, word joiner, BOM
    "\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069"  # zero-width, bidi marks, overrides
    "\u2028\u2029]"  # line and paragraph separators
)


def safe_text(text: str) -> str:
    r"""`text` with lone surrogates dropped and every control spelled out.

    Two transforms, because the two problems arrive together and a caller that
    remembers one forgets the other. Surrogates first: `jsonl` decodes with
    `surrogateescape` and `records` hashes with `surrogatepass`, so text that
    caught a binary `cat` in a tool result holds bytes no strict-UTF-8 consumer
    can re-encode — including `print` to a UTF-8 terminal. Then the controls,
    spelled out rather than deleted, so a reader can see what was there.

    Tab and newline survive because the CLI's own layout uses them; `\r` does
    not, because carriage return is the erase. `\x`-notation below U+0100 and
    `\u` above it — `\x202e` for U+202E reads as a space followed by `2e`.

    This is *not* a redaction gate and does not pretend to be one: `redact`
    scans for secrets, this makes what survives that scan safe to render. [E7]
    """
    return UNSAFE.sub(
        lambda m: (
            f"\\x{ord(m.group()):02x}" if ord(m.group()) < 0x100 else f"\\u{ord(m.group()):04x}"
        ),
        text.encode("utf-8", "replace").decode("utf-8"),
    )


def canonical_json(obj: object) -> bytes:
    """The only serializer allowed to produce bytes that reach git.

    Floats are permitted: CPython's repr is shortest-round-trip and stable
    across platforms, and the only consumer of canonical JSON is our own
    rebuild-twice check. NaN/Infinity are rejected — they are not JSON, and a
    third party verifying the tree with `jq` would choke on them.

    `ensure_ascii=True` is not cosmetic. With it off, surrogateescape'd bytes
    from an invalid-UTF-8 transcript pass straight through into the output, so
    the file we commit as "canonical JSON" is not valid UTF-8 and our own
    reader cannot parse it back. Escaping makes the output pure ASCII: valid
    UTF-8 by construction, re-parseable, and `jq`-checkable by a stranger.
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


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
    agent_id: str | None = None  # sidechain → which subagent; a different namespace
    # A *pointer* to another line's uuid (Claude Code's `leafUuid` on summaries).
    # Deliberately not folded into `uuid`: a pointer in the identity namespace
    # makes a summary collide with the turn it points at, and the dedup check
    # then drops the summary. Measured: 3 lost summaries on the MIT corpus.
    ref_uuid: str | None = None
    native: dict = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    turn_id: str = ""

    def __post_init__(self) -> None:
        # `kind` and `tool_name` are in the digest, not only the text. Hashing
        # the text alone makes a `text` block saying X and a `tool_use` block
        # whose flattened input is X the same turn — the line that *ran* a
        # command and the line that *mentioned* it, indistinguishable.
        # `tool_name` is attacker-controlled and variable length, so it is
        # hashed to fixed width rather than embedded: every field is then
        # either a closed-set token or 64 hex characters, and a `\x1e` in a
        # tool name cannot make one block hash as two. [E7 parsing-F1]
        digest = sha256_text(
            "".join(
                f"{b.kind}\x1e{sha256_text(b.tool_name or '')}\x1e{b.content_sha256}"
                for b in self.blocks
            )
        )
        # Identity, NOT position. `seq` is a counter over non-skipped lines, so
        # putting it in the id means any change to a skip rule renumbers every
        # later turn and churns the whole committed tree — the exact failure
        # this project exists to avoid. The harness already hands us a stable
        # node id; use it. Lines with no uuid fall back to byte offset, which
        # is stable under skip-rule changes and under appends.
        #
        # The namespace is tagged, because the two are one value otherwise: a
        # line whose uuid is literally the string "@101" and an uuid-less line
        # beginning at byte 101 land in the same slot, and the adapter's dedup
        # is on `uuid`, so it does not fire — one of them has none. [E7
        # parsing-F1]
        ident_kind, identity = ("uuid", self.uuid) if self.uuid else ("off", self.byte_offset)
        self.turn_id = _id(self.session_id, ident_kind, identity, self.role, digest)
        for b in self.blocks:  # blocks are built before the turn_id exists
            b.turn_id = self.turn_id
            b.block_id = _id(b.turn_id, b.seq, b.kind, b.content_sha256)


@dataclass(slots=True)
class Event:
    """A boundary in the transcript, keyed on the turn that caused it.

    Never on `seq`: that counts non-skipped lines, so any change to a skip rule
    renumbers every later event and churns the committed tree — the same defect
    that was removed from `turn_id`. `anchor` is the `turn_id` of the line the
    event was read from, which makes an event exactly as stable as that turn.
    """

    session_id: str
    seq: int
    kind: str  # session_start | session_end | compaction | fork
    byte_offset: int = -1
    anchor: str = ""  # turn_id of the line this event was read from
    meta: dict = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("session_start", "session_end", "compaction", "fork"):
            raise ValueError(f"unknown event kind: {self.kind!r}")
        if not self.anchor:
            raise ValueError("an event must anchor on the turn it was read from")
        self.event_id = _id(self.session_id, self.kind, self.anchor)


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
    # Every JSON value the reader got out of the file, including ones it could
    # not decode. The conformance suite asserts len(turns) + sum(skipped) ==
    # records_seen, which is the only check that can actually catch a dropped
    # line: without it an adapter that returns nothing passes every other rule.
    records_seen: int = 0

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
                        "session_id": t.session_id,
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
                        "agent_id": t.agent_id,
                        "ref_uuid": t.ref_uuid,
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
                    {
                        "event_id": e.event_id,
                        "seq": e.seq,
                        "kind": e.kind,
                        "byte_offset": e.byte_offset,
                        "anchor": e.anchor,
                        "meta": e.meta,
                    }
                    for e in self.events
                ],
                "skipped": self.skipped,
                "records_seen": self.records_seen,
            }
        )
