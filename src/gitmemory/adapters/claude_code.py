"""Claude Code adapter: ~/.claude/projects/**/*.jsonl -> canonical records.

Every branch here exists because recon on real transcripts found a way to get
it wrong. DESIGN.md §2.2 is the trap table; each row has a test in
tests/test_claude_code.py. Nothing in this module may touch git, the index, or
derivation.

Two rules the prior art breaks and we do not:
  - Unknown fields survive verbatim in `native`. Nothing is dropped silently.
  - Unparseable input is *counted* in `Session.skipped`, never swallowed.

Parsing runs in two passes over the decoded lines because the session id is
part of every content-derived id and Claude Code does not always put it on the
first line. Resolving it up front is the only way the ids can be correct;
back-patching `session_id` after construction leaves the ids keyed on the
empty string, which makes them collide across unrelated sessions.
"""

from __future__ import annotations

import glob as _glob
import math
import os
import re

from ..jsonl import LineTooLong, LineTruncated, iter_records
from ..records import Block, Event, Session, Turn, canonical_json, sha256_text

AGENT = "claude-code"
_DEC = ("utf-8", "surrogatepass")

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
        # Written alongside a successful Artifact publish; fully redundant with
        # the Artifact tool_result. Upstream's list exactly — an earlier version
        # of this file invented `artifact-publish` and omitted `frame-link`,
        # which is how a vendored constant quietly stops being the thing it
        # cites. Any local addition goes below this line, marked as local.
        "frame-link",
    }
)

_ROLES = {"user": "user", "assistant": "assistant"}

# A payload longer than this is machine data, not prose: inlining it makes the
# index mostly base64. Measured on the MIT corpus, image blocks alone were
# 61.7% of all indexed text before this cap existed.
_ELIDE_OVER = 1024

# json.loads happily builds structures deeper than the interpreter can walk, so
# any recursive projection needs its own bound or a crafted line raises
# RecursionError and takes the whole transcript with it.
_MAX_DEPTH = 32

# Skip reasons are keyed on a `type` value we do not control and then written
# into committed canonical JSON. Bound both the shape of each key and how many
# distinct ones a single file can mint.
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}\Z")
_MAX_REASONS = 64

# Same rule as the store's path guard: leading alphanumeric bans `..` and
# dotfiles, and the charset bans every glob metacharacter and separator. `\Z`
# for the same reason the store's do: `$` also matches before a trailing
# newline, so `sess\n` passed a guard whose whole job is to say what may become
# a path component. [E7b L2-F4]
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Every short identifier a line supplies: `sessionId`, `model`, `requestId`,
# `timestamp`, the keys of `usage`, and the uuid family — `uuid`, `parentUuid`,
# `sourceToolAssistantUUID`, `agentId`. All of them are re-hashed or rewritten
# once per turn, so an unbounded one turns a linear parse quadratic, and all of
# them reach canonical JSON and the index — that is, git. The uuid family read
# through `_str_or_none` for four epochs, which type-checks a value this
# comment already claimed was bounded; `_bounded_id` is the claim. [review]
#
# Measured over claude-code-log's 162 fixtures, 4,571 records: the longest
# `model` is 26 characters, `requestId` 28, `timestamp` 27, `usage` key 27,
# `uuid` 36. 128 is 3.5x the longest real value of any of them, and is the same
# 128 the path rule above uses, so the bounds cannot drift apart. One constant
# rather than five, for the same reason. [E7 parsing-F5 + F11]
_MAX_ID = 128

# Prefix on every bounded id, and the reason `_bounded_id` has no fixed point.
# It must be a character no real value can begin with; `…` is not in any id
# upstream emits, and a value that does start with one is re-bounded rather
# than passed through, so the marked space is unspellable from outside.
# [review: paths F4]
_BOUND_MARK = "…"


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


def _str_or_none(value) -> str | None:
    """Session-level metadata goes into canonical JSON; keep it a string.

    A transcript is untrusted input: `version` can be any JSON value, and a
    float NaN there would make `to_canonical()` raise at commit time, long
    after the parse that accepted it.
    """
    return value if isinstance(value, str) else None


def _safe_type(line_type) -> str:
    """A `type` value fit to key a committed skip counter."""
    return line_type if isinstance(line_type, str) and _TYPE_RE.match(line_type) else "other"


def _bounded_id(value) -> str | None:
    """A short identifier from the file, bounded so handling it stays O(1).

    `Turn.__post_init__` feeds the session id into a fresh SHA-256 for every
    turn, so a long one turns a linear parse quadratic. Measured on two
    fixtures of 20,001 turns differing in nothing else: a 16-character id
    parses in 0.13 s CPU, a 1,000,000-character id in 6.75 s — 52x the work
    for 1.6x the bytes. `to_canonical` has the same shape in space.

    `model`, `requestId`, `timestamp` and the keys of `usage` are the same
    shape of problem one layer along: each is written once per turn into
    canonical JSON and once per turn into the index, and none of them was
    bounded. Measured before this: a single line carrying 200,000-character
    values for all three, and a 100,000-character usage key, put every one of
    them into the record verbatim. [E7 parsing-F11]

    Truncated with a digest rather than rejected, so two long values stay two
    values — a `model` collapsed to a constant would merge two models' spend,
    and a rejected session id falls back to the filename stem, which two
    transcripts in different directories can share. Rejecting trades a cost bug
    for a correctness one. The verbatim value is still in every line's `native`.

    Charset is deliberately not policed here. None of these is a path component
    — `store.session_id_for` computes that separately and `_safe` guards it —
    and every render boundary already scrubs: the CLI prints through
    `safe_text` and `derive` scrubs every string it writes. [E7 parsing-F5]

    Truncate-with-digest, so two long values stay two — but only because the
    output is marked. Without `_BOUND_MARK` the bounded form was exactly
    `_MAX_ID` characters, so it passed the length test and returned *itself*:
    `f(f(x)) == f(x)`, a fixed point. That is a collision anyone can compute
    offline from public SHA-256, no search — pick a long value, publish its
    bounded form as a short one, and the two are one id. `uuid` is the dedup
    key, so the second record was dropped as `duplicate_uuid`, and `_usage_key`
    is last-write-wins, so a one-token decoy erased a real line's usage. Marking
    the output fixes both: a marked value is never passed through, so nothing a
    transcript can spell lands in the image of this function.
    [review: paths F4]
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= _MAX_ID and not value.startswith(_BOUND_MARK):
        return value
    return f"{_BOUND_MARK}{value[: _MAX_ID - 18]}-{sha256_text(value)[:16]}"


def _scrub(value, depth: int = 0):
    """`value` with anything canonical JSON cannot hold replaced by a marker.

    Two hazards, both from untrusted input: strings large enough to swamp the
    index, and non-finite floats, which `canonical_json` rejects by design. The
    verbatim value always survives in `native` — this is only the projection.
    """
    if isinstance(value, str):
        return value if len(value) <= _ELIDE_OVER else f"[{len(value)} chars elided]"
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)  # "nan" / "inf": not JSON as a literal, fine as text
    if depth >= _MAX_DEPTH:
        return "[nesting too deep]"
    if isinstance(value, dict):
        # Keys are canonical-JSON output too, and were the one string here that
        # nothing bounded — a 100,000-character `usage` key reached the record
        # verbatim. Bounded with a digest rather than elided like a value,
        # because two long keys eliding to one marker would merge two entries.
        # [E7 parsing-F11]
        return {_bounded_id(k) or k: _scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    return value


def _image_text(item: dict) -> str:
    """An image block's searchable projection: its shape, never its payload."""
    raw = item.get("source")
    src: dict = raw if isinstance(raw, dict) else {}
    data = src.get("data")
    media = src.get("media_type") or src.get("type") or "image"
    n = len(data) if isinstance(data, str) else 0
    return f"[image {media} {n} chars]"


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
        return [("text", "", None, {"gitmemory_unparsed": type(content).__name__})]

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
            name = item.get("name")
            tool = name if isinstance(name, str) else None
            out.append(("tool_use", _flatten(item.get("input")), tool, item))
        elif kind == "tool_result":
            out.append(("tool_result", _flatten(item.get("content")), None, item))
        elif kind == "image":
            # Never inline the base64. The bytes are in `native` and in the raw
            # segment; what the index needs is that an image was here.
            out.append(("text", _image_text(item), None, item))
        else:
            # Unknown block: canonical JSON, so it stays *searchable* as well
            # as recoverable. Empty text would be a retrieval hole — a secret
            # or an error string inside an unrecognised block must still be
            # findable. What we must never write is `str(dict)`: that is a
            # Python repr, not JSON, and nothing can parse it back.
            out.append(("text", canonical_json(_scrub(item)).decode(*_DEC), None, item))
    return out


def _flatten(value, depth: int = 0) -> str:
    """Searchable text for a structured payload, without pretending it is prose."""
    if depth >= _MAX_DEPTH:
        return "[nesting too deep]"
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten(v, depth + 1) for v in value)
    if isinstance(value, dict):
        if value.get("type") == "image":
            return _image_text(value)
        # Only a *text block* may be reduced to its text. The old shortcut
        # fired on any dict with a `text` key and silently dropped every
        # sibling field with it.
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        return "\n".join(f"{k}: {_flatten(v, depth + 1)}" for k, v in sorted(value.items()))
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return str(value)


def _content_of(obj: dict, message: dict):
    """The prose payload of a line, wherever this line type happens to keep it.

    `message.content` covers user/assistant. Everything else keeps its text
    somewhere else, and reading only `message` loses it: on the MIT corpus that
    is 122 of 133 `system` lines, all 54 `attachment` payloads, and the
    `queue-operation`/`remove` lines, which hold real human steering text.
    """
    content = message.get("content")
    if content is not None:
        return content
    return _get(obj, "summary", "content", "attachment", "toolUseResult")


def parse(path: str) -> Session:
    """Parse one transcript file. Byte order is the only ordering authority."""
    session = Session(session_id="", agent=AGENT, source_path=os.path.abspath(path))
    skipped: dict[str, int] = {}
    seen_uuids: set[str] = set()
    seen = 0

    def bump(reason: str) -> None:
        nonlocal skipped
        if reason not in skipped and len(skipped) >= _MAX_REASONS:
            reason = "other"
        skipped[reason] = skipped.get(reason, 0) + 1

    def count_error(_lineno: int, exc: Exception) -> None:
        nonlocal seen
        seen += 1
        # A line refused for its size is not a line `json` refused, and folding
        # the two loses the only signal that content was dropped for a reason
        # the reader chose rather than one the file forced. [E7 parsing-F4]
        #
        # Nor is a line that parsed, then stopped with bytes to spare. One skip
        # is one skip whether it cost sixty bytes or six hundred, so without
        # this name the accounting balances over a transcript that lost most of
        # itself. [E7 parsing-F2]
        if isinstance(exc, LineTooLong):
            bump("line_too_long")
        elif isinstance(exc, LineTruncated):
            bump("json_decode_truncated")
        else:
            bump("json_decode_error")

    # Pass 1: decide what each line is, and find the session id. Nothing is
    # constructed yet — ids depend on the session id, which may first appear
    # on any line.
    kept: list[tuple] = []
    for rec in iter_records(path, on_error=count_error):
        seen += 1
        obj = rec.obj
        if not isinstance(obj, dict):
            bump("not_an_object")
            continue

        raw_type = obj.get("type")
        line_type = raw_type if isinstance(raw_type, str) else None
        if line_type in SILENT_SKIP_TYPES:
            bump(f"skip:{_safe_type(line_type)}")
            continue

        sid = _bounded_id(_get(obj, "sessionId", "session_id"))
        if sid and not session.session_id:
            session.session_id = sid
        session.agent_version = session.agent_version or _str_or_none(obj.get("version"))
        session.cwd = session.cwd or _str_or_none(obj.get("cwd"))
        session.git_branch = session.git_branch or _str_or_none(obj.get("gitBranch"))

        raw_message = obj.get("message")
        message: dict = raw_message if isinstance(raw_message, dict) else {}
        content = _content_of(obj, message)

        # A line belongs in the record model iff it carries an identity or
        # content, not iff its `type` is on an allowlist. Measured over
        # claude-code-log's 162-fixture corpus the identity half splits
        # cleanly: user/assistant/system/attachment/progress all carry `uuid`
        # (4,392 of 4,392); every no-DAG-role line carries none. An allowlist
        # silently drops each new type Claude Code ships — their own corpus
        # contains a `future_synthetic_record` probe for exactly that.
        # The content half is what keeps `type: summary` (leafUuid only) and
        # `queue-operation`/`remove` (prose, no id at all).
        # A non-string uuid is repaired, not counted: `skipped` means "this
        # record produced no turn", and the accounting identity in the
        # conformance suite depends on that meaning. The offending value is
        # still in `native`; the turn just falls back to byte-offset identity.
        # Bounded like every other identifier this file reads. It is the turn's
        # identity when it is present, so it is hashed and written per turn —
        # the same shape `_bounded_id` exists for, and it was the one short id
        # that skipped it. Truncate-with-digest, so two long uuids stay two.
        uuid = _bounded_id(obj.get("uuid"))
        # A *pointer*, never an identity: folding it into `uuid` makes a
        # summary collide with the turn it points at and lose the dedup race.
        raw_ref = obj.get("leafUuid")
        ref_uuid = raw_ref if isinstance(raw_ref, str) and raw_ref else None

        # A compaction boundary is structural: it is kept whether or not this
        # build of Claude Code put a uuid on it, because the event it produces
        # is how everything downstream knows the context was cut here.
        boundary = line_type == "system" and obj.get("subtype") == "compact_boundary"

        role = _ROLES.get(line_type) if line_type is not None else None
        if role is None:
            if not (uuid or ref_uuid or content or boundary):
                bump(f"no_identity:{_safe_type(line_type)}")
                continue
            role = "system"

        # sessionId is NOT a stable conversation key — a fork-from-compaction
        # reuses it. Dedup on message uuid, which is stable.
        if uuid and uuid in seen_uuids:
            bump("duplicate_uuid")
            continue
        if uuid:
            seen_uuids.add(uuid)

        kept.append((rec, obj, message, content, line_type, role, uuid, ref_uuid, sid))

    if not session.session_id:
        session.session_id = os.path.splitext(os.path.basename(path))[0]

    # Pass 2: build. Every id now sees the real session id.
    for seq, (rec, obj, message, content, line_type, role, uuid, ref_uuid, sid) in enumerate(kept):
        turn_sid = sid or session.session_id
        raw_usage = message.get("usage")
        blocks = [
            Block(turn_id="", seq=i, kind=k, text=t, tool_name=n, native=nat)
            for i, (k, t, n, nat) in enumerate(_blocks(content))
        ]
        turn = Turn(
            session_id=turn_sid,
            seq=seq,
            # The line type, never `message.role`. That override let a
            # `type: "user"` line declaring `message.role: "assistant"` be
            # billed as a model call — measured 9,000,000 tokens through
            # `dash_spend` with `dash_unbilled` empty, i.e. the panel whose job
            # is to show what spend refused showed nothing. `index.py`'s
            # `role = 'assistant'` filter exists for exactly this and was
            # reading the spoofable value, so the guard sat one layer below the
            # hole. So do `billable_usage` and `rollup_usage`.
            #
            # Deleted rather than reconciled, because it was measured to carry
            # no information: over claude-code-log's 162 fixtures `message.role`
            # is present on 4,189 of 4,571 records and agrees with the line type
            # on every one, and is absent from all 382 records whose type is not
            # user/assistant. It could only ever restate `type` or contradict it.
            #
            # `role` is now one of three literals this module writes, so the
            # unbounded length and charset it used to inherit from the file are
            # gone with it. [E7 parsing-F3]
            role=role,
            byte_offset=rec.offset,
            byte_len=rec.length,
            # Bounded, not merely typed: each is written once per turn into
            # canonical JSON and once per turn into the index. [E7 parsing-F11]
            model=_bounded_id(message.get("model")),
            ts=_bounded_id(obj.get("timestamp")),
            request_id=_bounded_id(_get(obj, "requestId", "request_id")),
            uuid=uuid,
            # null at every compaction boundary; logicalParentUuid survives it.
            parent_uuid=_bounded_id(_get(obj, "parentUuid", "logicalParentUuid")),
            usage=_scrub(raw_usage) if isinstance(raw_usage, dict) else {},
            # `is True`, not `bool()`: the string "false" is truthy, and this
            # flag decides whether a turn is a subagent's — which `rollup_usage`
            # and the sidechain views read. Over the 162-fixture corpus the
            # value is a real boolean on all 4,392 lines that carry it (3,774
            # False, 618 True), so tightening costs nothing real and closes the
            # one reading a line could choose. [E7 parsing-F14]
            is_sidechain=obj.get("isSidechain") is True,
            # Sidechains anchor on the spawning tool_use, never on time. The
            # two keys below are different namespaces and are kept apart:
            # `sourceToolAssistantUUID` is a turn uuid, `toolUseID` is a
            # content-block id, and merging them makes joins silently wrong.
            anchor_uuid=_bounded_id(obj.get("sourceToolAssistantUUID")),
            agent_id=_bounded_id(obj.get("agentId")),
            ref_uuid=ref_uuid,
            native=obj,
            blocks=blocks,
        )
        tool_use_id = _str_or_none(_get(obj, "toolUseID", "toolUseId"))
        if tool_use_id and not turn.anchor_uuid:
            turn.anchor_uuid = tool_use_id
        session.turns.append(turn)

        # A compaction writes a `system`/`compact_boundary` line with
        # parentUuid=null, followed by a user line flagged isCompactSummary.
        # The boundary line is also a real node: it carries a uuid and is the
        # parent of the summary that follows, so dropping it leaves a dangling
        # edge in the DAG.
        if line_type == "system" and obj.get("subtype") == "compact_boundary":
            meta: dict = {}
            raw_meta = obj.get("compactMetadata")
            if isinstance(raw_meta, dict):
                meta["compact_metadata"] = _scrub(raw_meta)
            session.events.append(
                Event(turn_sid, seq, "compaction", rec.offset, turn.turn_id, meta)
            )
        if obj.get("isCompactSummary"):
            session.events.append(Event(turn_sid, seq, "fork", rec.offset, turn.turn_id))

    session.skipped = skipped
    session.records_seen = seen
    if session.turns:
        # Informational only. Never used to order anything.
        session.started_at = session.turns[0].ts
        session.ended_at = session.turns[-1].ts
    return session


def _usage_key(t: Turn, path: str | None = None) -> tuple:
    """Which request this line's usage belongs to, with the namespace tagged.

    `request_id or uuid or f"@{byte_offset}"` flattened three namespaces into
    one string. A line whose `uuid` spells another line's `requestId` then
    shared its slot, and since the later write wins, the earlier request's
    tokens left the bill entirely — measured 3000 input tokens billed as 2000
    on a two-request fixture. Same shape as the `turn_id` collision, same fix:
    say which namespace the value came from. [E7 parsing-F1]

    `path` scopes the offset fallback, because a byte offset only means
    anything inside one file. Request ids and uuids deliberately do not take
    it: deduping those *across* files is the whole point of `rollup_usage`.
    """
    if t.request_id:
        return ("req", t.request_id)
    if t.uuid:
        return ("uuid", t.uuid)
    return ("off", path, t.byte_offset)


def billable_usage(session: Session) -> dict[str, int]:
    """Token totals with Claude Code's 2.79x over-count removed.

    Claude Code writes one assistant line per content block and repeats the
    *cumulative* usage on each, so naive summation over-counts. Recon measured
    2.79x on a real transcript. Dedup by `request_id`; `<synthetic>` rows are
    not billed at all.

    Lives in the adapter, not in records.py: "one line per block, cumulative
    usage repeated" is a Claude Code artifact, and an agent-agnostic module
    that hardcodes it would be wrong for the next adapter rather than merely
    unused by it.
    """
    seen: dict[tuple, dict] = {}
    for t in session.turns:
        if t.role != "assistant" or t.model == "<synthetic>":
            continue
        # last write per request wins: usage is cumulative
        seen[_usage_key(t)] = t.usage
    total: dict[str, int] = {}
    for usage in seen.values():
        for k, v in usage.items():
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total


def session_files(path: str) -> list[str]:
    """Every file belonging to one session: the transcript and its subagents.

    Claude Code parks subagent transcripts in a directory named after the
    session file's stem — `<stem>/subagents/agent-*.jsonl`, nested arbitrarily
    deep for subagents that spawn subagents. Recon found subagents are 92% of
    all files, and one session kept 78% of its cache-read tokens in there
    (DESIGN.md §2.2), so a rollup that reads only the main file is not a
    rollup. Ordered: main file first, then subagents by path.
    """
    main = os.path.abspath(path)
    stem = os.path.splitext(main)[0]
    subs = sorted(_glob.glob(os.path.join(_glob.escape(stem), "**", "*.jsonl"), recursive=True))

    # Deduped by realpath, because `**` walks into symlinked directories and a
    # directory that links back to its own ancestor turns one subagent file
    # into one path per level until the kernel's symlink limit stops it.
    # Measured: a single `agent-1.jsonl` under a self-linking `subagents/`
    # yielded 16 extra paths — and `rollup_usage` parses every path this
    # returns, so that is the same tokens billed 17 times. Bounded by the
    # kernel, not by us, which is the part that made it worth a line of code.
    # [E7 parsing-F13]
    seen = {os.path.realpath(main)}
    out = [main]
    for s in subs:
        real = os.path.realpath(s)
        if real in seen:
            continue
        seen.add(real)
        out.append(s)
    return out


def rollup_usage(path: str) -> dict[str, int]:
    """`billable_usage` over the whole session, subagents included.

    Dedup spans files: a `requestId` seen in both the main transcript and a
    subagent must be billed once.
    """
    seen: dict[tuple, dict] = {}
    for f in session_files(path):
        for t in parse(f).turns:
            if t.role != "assistant" or t.model == "<synthetic>":
                continue
            seen[_usage_key(t, f)] = t.usage
    total: dict[str, int] = {}
    for usage in seen.values():
        for k, v in usage.items():
            if isinstance(v, int) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total


# Claude Code writes no `costUSD`, so cost is always *estimated* from tokens.
# A vendored, dated snapshot — never a live lookup, because a number that moves
# under a committed transcript is not a record of anything. USD per million
# tokens, list price, keyed on the longest matching model-id prefix.
# Source: https://www.anthropic.com/pricing (API tab).
PRICES_AS_OF = "2026-09-20"
PRICES_SOURCE = "https://www.anthropic.com/pricing"
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-haiku": (0.25, 1.25),
}
# Anthropic's published multipliers on the input rate.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1

# What may follow a matched key: nothing, or a release date. NOT another version
# segment — bare `startswith` let `claude-opus-4` swallow `claude-opus-4-5-…`
# and bill it at the retired $15/$75, ~3x over, while reporting `estimated:
# True` with a same-day `as_of` that reads as freshly checked. A version we have
# not priced has to come back unknown, which is what the docstring promises. [E2]
_MODEL_TAIL = re.compile(r"^(?:-\d{8})?\Z")


def _priced_as(model: str) -> str | None:
    """Longest key that matches at a model boundary, or None."""
    for key in sorted(PRICES_USD_PER_MTOK, key=len, reverse=True):
        if model.startswith(key) and _MODEL_TAIL.match(model[len(key) :]):
            return key
    return None


def estimate_cost(model: str | None, usage: dict) -> dict | None:
    """Estimated USD for one model's usage, or None when the price is unknown.

    Returns None rather than a guess for a model absent from the snapshot: an
    unpriced model must read as "we don't know", not as $0.00 quietly summed
    into a total. The result carries `as_of` so every display can say which
    day's prices it used, which is the only honest way to show an estimate.
    """
    if not model:
        return None
    match = _priced_as(model)
    if match is None:
        return None
    rate_in, rate_out = PRICES_USD_PER_MTOK[match]

    def tok(key: str) -> int:
        """One token count, clamped into the range arithmetic survives.

        JSON integers are arbitrary precision and Python's are too, so a
        transcript saying `"input_tokens": 1e400` as an integer literal used to
        raise `OverflowError: int too large to convert to float` here — the
        dashboard's cost column crashing on a number a line chose. Negatives
        are the same input the other way round: unclamped, a line could bill
        itself a refund.

        `2**53` is where float stops counting integers exactly, so past it the
        multiplication was already fiction; clamping there keeps the estimate
        monotonic in the input instead of resetting an absurd count to zero,
        which would read as "free". [E7 parsing-F7]
        """
        v = usage.get(key)
        if not isinstance(v, int) or isinstance(v, bool):
            return 0
        return min(max(v, 0), 2**53)

    usd = (
        tok("input_tokens") * rate_in
        + tok("output_tokens") * rate_out
        + tok("cache_creation_input_tokens") * rate_in * _CACHE_WRITE_MULT
        + tok("cache_read_input_tokens") * rate_in * _CACHE_READ_MULT
    ) / 1_000_000
    return {"usd": usd, "estimated": True, "as_of": PRICES_AS_OF, "priced_as": match}


def _glob_hits(session_id: str, root: str) -> set[str]:
    """Every path the filename shape reaches under `root`. No validation.

    Split out so the hostile-id test's negative control can ask the
    implementation what it would find instead of restating the pattern. A
    restated control drifts *green* when the pattern changes here: the fixture
    stops planting anything reachable, every hostile id returns `None` for the
    wrong reason, and the test that exists to prove the charset guard works
    proves nothing instead. Mirrors `pi._glob_hits`. [review: tests]

    One pattern, not two: `**` matches *zero* or more directories, so
    `base/*/**/name` already returns everything `base/*/name` does — symlinked
    project directories included, since `*` and `**` treat those the same way.
    The second glob was a second full traversal for a strictly smaller set.
    [pair review]

    Unlike pi's, the shape is the bare stem only. There is no `*_<id>.jsonl`
    here, so the suffix-collision `pi._names_session` exists for cannot arise:
    a hit's stem *is* the id.
    """
    name = _glob.escape(session_id) + ".jsonl"
    base = _glob.escape(root)
    return set(_glob.glob(os.path.join(base, "*", "**", name), recursive=True))


def _contained(hit: str, root: str) -> bool:
    """Whether `hit` is a real directory entry that resolves inside `root`.

    Two holes, and the first one is the reason this is not a one-liner.

    macOS and Windows match filenames case-insensitively, and APFS folds more
    than ASCII case: `U+017F` folds to `s`, `U+212A` to `k`. `glob` sees a final
    component with no metacharacter in it, tests it with `os.path.lexists`, and
    on a hit **returns the spelling it was asked for** rather than the one on
    disk. `os.path.realpath` does not canonicalize case either, so containment
    passed on the fabricated name. Asking for `secret` returned the bytes of
    `ſecret.jsonl`, and asking for a lowercased uuid returned the transcript
    named in uppercase — a different session, handed back as if it were the one
    requested, from an id that satisfies `_SESSION_ID_RE`. Measured: 256 case
    variants of one id produced 64 distinct path strings behind one inode.
    Listing the parent is what separates "the filesystem would open this" from
    "this is the name of a file that exists". [review: paths F1]

    Second, `startswith(root + os.sep)` is wrong when `root` is `/`: realpath
    gives `/`, the concatenation is `//`, and no real path starts with that, so
    a root of `/` rejected everything — after globbing the whole filesystem.
    Stripping the separator before appending it makes the comparison mean what
    it reads as. [review: paths F8]
    """
    parent, name = os.path.split(hit)
    try:
        if name not in os.listdir(parent):
            return False
    except OSError:
        return False
    return os.path.realpath(hit).startswith(root.rstrip(os.sep) + os.sep)


def _require_root(projects_root: str) -> str:
    """The resolved root, or `ValueError` if the caller did not name one.

    `os.path.realpath("")` is the current directory and `realpath(".")` is too,
    so an empty or relative root silently searched wherever the process
    happened to be standing. That is the default this function had removed at
    [E7 S11] — "roots have no default" — coming back through the argument that
    replaced it. A missing root is a caller bug, not a cache miss, so it raises
    rather than returning `None`. [review: paths F7]
    """
    if not projects_root or not os.path.isabs(projects_root):
        raise ValueError(f"projects_root must be an absolute path, got {projects_root!r}")
    return os.path.realpath(projects_root)


def find_session(session_id: str, projects_root: str) -> str | None:
    """Locate a transcript by id without computing the project directory.

    Worktrees move the project dir and Claude's encoding of it is undocumented
    (non-alphanumerics to `-`, plus a base-36 JS string hash past 200 chars).
    Globbing is shorter than reimplementing that and cannot drift with it.

    `session_id` is untrusted — nothing in-tree calls this yet outside the
    tests, and the caller it is written for is a hook payload. So it is
    charset-checked before use, escaped so glob metacharacters cannot widen
    the search, and every hit is confirmed to resolve inside the root. Newest
    mtime wins when a worktree left duplicates, with the path breaking ties so
    the answer does not depend on set iteration order.

    `projects_root` is required, and used to default to the real
    `~/.claude/projects`. Nothing in the product passed it that way — every
    caller in the tree names a root — so the default's only reachable effect
    was that a future caller, or a test, would silently read the developer's
    own machine and the watcher's whole "roots have no default" rule would
    have one exception nobody chose. A parameter whose default is "somebody's
    real transcripts" has to be spelled out at the call site. [E7 S11]

    The returned path is the globbed one, not the resolved one, so a symlink
    inside the root is followed by the caller as the user intended — the
    containment proof is on the target, the answer is the name. That leaves
    the usual TOCTOU window, and `isfile` does not close any half of it: it is
    one more check before the same unsynchronised open, and the name can be
    replaced between the two. What it buys is that the *ordinary* wrong shapes
    — a directory or a FIFO named `<id>.jsonl`, both of which satisfy the glob
    and the containment proof — are not returned as transcripts, so the reader
    blocks or raises only if someone is racing it.
    """
    root = _require_root(projects_root)
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    inside = [h for h in _glob_hits(session_id, root) if _contained(h, root) and os.path.isfile(h)]
    if not inside:
        return None

    def newest(p: str) -> tuple[float, str]:
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (-1.0, p)

    return max(inside, key=newest)
