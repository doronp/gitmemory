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

from ..jsonl import iter_records
from ..records import Block, Event, Session, Turn, canonical_json

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
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")
_MAX_REASONS = 64

# Same rule as the store's path guard: leading alphanumeric bans `..` and
# dotfiles, and the charset bans every glob metacharacter and separator.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
        return {k: _scrub(v, depth + 1) for k, v in value.items()}
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

    def count_error(_lineno: int, _exc: Exception) -> None:
        nonlocal seen
        seen += 1
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

        sid = _get(obj, "sessionId", "session_id")
        if isinstance(sid, str) and sid and not session.session_id:
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
        raw_uuid = obj.get("uuid")
        uuid = raw_uuid if isinstance(raw_uuid, str) and raw_uuid else None
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
        turn_sid = sid if isinstance(sid, str) and sid else session.session_id
        raw_usage = message.get("usage")
        role_field = message.get("role")
        blocks = [
            Block(turn_id="", seq=i, kind=k, text=t, tool_name=n, native=nat)
            for i, (k, t, n, nat) in enumerate(_blocks(content))
        ]
        turn = Turn(
            session_id=turn_sid,
            seq=seq,
            role=role_field if isinstance(role_field, str) else role,
            byte_offset=rec.offset,
            byte_len=rec.length,
            model=_str_or_none(message.get("model")),
            ts=_str_or_none(obj.get("timestamp")),
            request_id=_str_or_none(_get(obj, "requestId", "request_id")),
            uuid=uuid,
            # null at every compaction boundary; logicalParentUuid survives it.
            parent_uuid=_str_or_none(_get(obj, "parentUuid", "logicalParentUuid")),
            usage=_scrub(raw_usage) if isinstance(raw_usage, dict) else {},
            is_sidechain=bool(obj.get("isSidechain")),
            # Sidechains anchor on the spawning tool_use, never on time. The
            # two keys below are different namespaces and are kept apart:
            # `sourceToolAssistantUUID` is a turn uuid, `toolUseID` is a
            # content-block id, and merging them makes joins silently wrong.
            anchor_uuid=_str_or_none(obj.get("sourceToolAssistantUUID")),
            agent_id=_str_or_none(obj.get("agentId")),
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
    return [main] + [s for s in subs if os.path.abspath(s) != main]


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
_MODEL_TAIL = re.compile(r"^(?:-\d{8})?$")


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
        v = usage.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    usd = (
        tok("input_tokens") * rate_in
        + tok("output_tokens") * rate_out
        + tok("cache_creation_input_tokens") * rate_in * _CACHE_WRITE_MULT
        + tok("cache_read_input_tokens") * rate_in * _CACHE_READ_MULT
    ) / 1_000_000
    return {"usd": usd, "estimated": True, "as_of": PRICES_AS_OF, "priced_as": match}


def find_session(session_id: str, projects_root: str | None = None) -> str | None:
    """Locate a transcript by id without computing the project directory.

    Worktrees move the project dir and Claude's encoding of it is undocumented
    (non-alphanumerics to `-`, plus a base-36 JS string hash past 200 chars).
    Globbing is shorter than reimplementing that and cannot drift with it.

    `session_id` reaches this function from a hook payload, so it is untrusted:
    it is charset-checked before use, escaped so glob metacharacters cannot
    widen the search, and every hit is confirmed to resolve inside the root.
    Newest mtime wins when a worktree left duplicates, with the path breaking
    ties so the answer does not depend on set iteration order.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    root = os.path.realpath(projects_root or os.path.expanduser("~/.claude/projects"))
    name = _glob.escape(session_id) + ".jsonl"
    base = _glob.escape(root)
    hits = set(_glob.glob(os.path.join(base, "*", name)))
    hits |= set(_glob.glob(os.path.join(base, "*", "**", name), recursive=True))
    inside = [h for h in hits if os.path.realpath(h).startswith(root + os.sep)]
    if not inside:
        return None

    def newest(p: str) -> tuple[float, str]:
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (-1.0, p)

    return max(inside, key=newest)
