"""pi / oh-my-pi adapter: ~/.pi/agent/sessions/**/*.jsonl -> canonical records.

pi (earendil-works/pi) and oh-my-pi (can1357/oh-my-pi, a fork of it) write the
same JSONL dialect, so one adapter reads both and `AGENT` names the *format*
rather than the binary. The registry accepts "omp" as an alias for that reason.

Three things this format does that Claude Code's does not, all load-bearing:

  - **The file is not strictly append-only.** oh-my-pi keeps a fixed-width
    256-byte `title` slot on line 1 that is rewritten in place
    (`session-entries.ts:16 SESSION_TITLE_SLOT_BYTES` @b52e1f5), and the
    session manager rewrites the *whole* file synchronously on several paths —
    including every resume where records were migrated or malformed
    (`session-manager.ts:1878`: `#rewriteRequired = migrated ||
    loaded.malformedRecords > 0`). Capture is content-addressed per generation,
    so a rewrite surfaces as a new generation rather than as corruption, but
    nothing in this module may assume earlier bytes are immutable.

  - **`message.role` is the only role discriminator.** Every conversational
    entry has `type: "message"`; the role lives one level down and includes
    `toolResult` and `bashExecution`, which have no Claude Code counterpart.
    `claude_code` deliberately ignores `message.role` because its line `type`
    already carries the role and the nested value is spoofable — here there is
    nothing else to read, so the native value is kept verbatim in `native` and
    only ever *mapped* into one of the three literals the record model allows.

  - **A compaction is declared long after the cut it describes.** The entry
    names `firstKeptEntryId` (or, on legacy files, `firstKeptEntryIndex`) and is
    itself written at the end of the pass. In oh-my-pi's
    `before-compaction.jsonl` the two compaction records sit 111,630 and
    102,084 bytes *after* their own cut points; in pi's, 111,830 and 102,180 —
    the same session, differing only in the length of a path. Both numbers come
    from indexing `firstKeptEntryIndex` into the file's physical line list,
    which is exactly the mapping `parse` refuses to make (see `first_kept_
    entry_index` below): fine for sizing the gap, not fine for computing an
    offset to record. `Event.byte_offset` therefore stays the offset of the
    declaring record, as everywhere else, and the resolved cut goes in `meta`
    only when an *id* was there to resolve.

Same two rules as every adapter: unknown fields survive verbatim in `native`,
and unparseable input is *counted* in `Session.skipped`, never swallowed.
"""

from __future__ import annotations

import glob as _glob
import os

from ..jsonl import LineTooLong, LineTruncated, iter_records
from ..records import Block, Event, Session, Turn, canonical_json

# ponytail: these are format-agnostic (bounds, scrubbing, first-present-key) and
# only live next door because claude_code was written first. Extract them to
# adapters/_common.py when a third adapter needs them. Copying them here would
# let the two `_MAX_ID`s drift, which is the one thing their own comments warn
# about — "one constant rather than five, for the same reason".
from .claude_code import (
    _DEC,
    _MAX_REASONS,
    _SESSION_ID_RE,
    _bounded_id,
    _flatten,
    _get,
    _safe_type,
    _scrub,
    _str_or_none,
)

AGENT = "pi"

# `message.role` -> one of the three literals `Turn.role` allows. `toolResult`
# and `bashExecution` are user-side: a tool return occupies the user position in
# the next request, which is also where Claude Code puts the same content.
#
# Never casefold the key before the lookup. The roles are camelCase, so a
# `.lower()` anywhere on this path turns every one of them into an unknown and
# silently demotes half the transcript to `system`.
_ROLES = {
    "user": "user",
    "assistant": "assistant",
    "toolResult": "user",
    "bashExecution": "user",
}

# The header is not an entry. Its `id` is the *session* id — a different
# namespace from the `id` on every other line — so folding the two together
# makes the header collide with whichever entry the DAG hangs off, exactly the
# way `leafUuid` collides with a turn uuid in claude_code.
_HEADER = "session"

# oh-my-pi's rewritable first line: a title plus 256 bytes of padding, no
# conversation content and no entry identity. It is the one record whose bytes
# change without the file growing, and it is the reason this format needs the
# append-only caveat in the module docstring.
_TITLE_SLOT = "title"

# Entries whose prose lives somewhere other than `message.content`. Read in
# order, first present wins — the same shape as claude_code's `_content_of`,
# which exists because reading only `message` lost 122 of 133 `system` lines on
# the MIT corpus. Here it keeps `compaction` and `branch_summary` summaries,
# which are the densest text in the file.
#
# `content` is here for `custom_message`, which puts its prose at the *top*
# level next to `customType` and has no `message` key at all
# (`compaction.test.ts:138-148`, `session-manager-internal-details.test.ts:24`
# @b52e1f5). Without it that entry is a turn with zero blocks: the text is in
# `native`, so nothing is lost, but it is not in canonical JSON, not in the
# index, and not counted in `skipped` either — invisible in the one direction
# the accounting rule exists to prevent.
_PROSE_KEYS = ("summary", "shortSummary", "content", "data", "text")


def _blocks(content) -> list[tuple[str, str, str | None, dict]]:
    """(kind, text, tool_name, native) per content block.

    An unrecognised block keeps its native dict and is re-serialized as
    canonical JSON so it stays searchable — never `str(dict)`, which is a Python
    repr and unrecoverable. `_scrub` elides any string over 1 KiB on that path,
    which is also what keeps an inline image payload out of the index without a
    special case for a block shape we have never seen.
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
        elif kind == "toolCall":
            name = item.get("name")
            tool = name if isinstance(name, str) else None
            out.append(("tool_use", _flatten(item.get("arguments")), tool, item))
        else:
            out.append(("text", canonical_json(_scrub(item)).decode(*_DEC), None, item))
    return out


def _message_blocks(message: dict, native_role) -> list[tuple[str, str, str | None, dict]]:
    """Blocks for one `type: "message"` entry, whatever its role keeps them in."""
    if native_role == "bashExecution":
        # No `content` key at all: the command and its output are message
        # fields. Skipping the entry would lose real transcript text, and
        # concatenating the two would make the command unfindable separately
        # from a 10,000-line output, which is the search that matters.
        return [
            ("tool_use", _flatten(message.get("command")), "bash", {}),
            ("tool_result", _flatten(message.get("output")), None, {}),
        ]
    blocks = _blocks(message.get("content"))
    if native_role == "toolResult":
        # The content is a list of plain `text` blocks; only the message level
        # says it is a tool return. Without this the index sees a tool's stdout
        # as prose. The tool name is carried on the block — unlike Claude Code,
        # where a tool_result genuinely does not know it and the name has to be
        # joined from the matching tool_use.
        name = message.get("toolName")
        tool = name if isinstance(name, str) else None
        return [("tool_result", t, tool, nat) for _k, t, _n, nat in blocks]
    return blocks


def _compose_model(obj: dict) -> str | None:
    """`provider` + `modelId`/`model` read as the one `"provider/model"` string.

    Four kinds of line name a model and they do not agree on how. The session
    header splits it into `provider` and `modelId`; an assistant message splits
    it into `provider` and `model`; a `model_usage` entry writes the joined
    string *and* a redundant `provider`; and a `model_change` does whichever the
    build was current for — every one of the twelve in the shipped fixtures
    splits it, while oh-my-pi's own tests write it joined
    (`sdk-model-selection.test.ts:1218,1331`). Reading only the joined spelling
    is how `model_change` silently did nothing on all four corpus files.

    Joining here rather than at each site keeps `Turn.model` one namespace. The
    `startswith` is what stops `model_usage` becoming `anthropic/anthropic/…`.
    """
    name = _get(obj, "modelId", "model")
    if not isinstance(name, str) or not name:
        return None
    provider = obj.get("provider")
    if not isinstance(provider, str) or not provider or name.startswith(provider + "/"):
        return _bounded_id(name)
    return _bounded_id(f"{provider}/{name}")


def parse(path: str) -> Session:
    """Parse one transcript file. Byte order is the only ordering authority."""
    session = Session(session_id="", agent=AGENT, source_path=os.path.abspath(path))
    skipped: dict[str, int] = {}
    seen_ids: set[str] = set()
    # `firstKeptEntryId` -> the byte offset of the entry it names. Built over
    # every line that carries an entry id, kept or skipped, because a compaction
    # may cut at a line this adapter did not turn into a turn.
    offset_of: dict[str, int] = {}
    seen = 0

    def bump(reason: str) -> None:
        if reason not in skipped and len(skipped) >= _MAX_REASONS:
            reason = "other"
        skipped[reason] = skipped.get(reason, 0) + 1

    def count_error(_lineno: int, exc: Exception) -> None:
        nonlocal seen
        seen += 1
        if isinstance(exc, LineTooLong):
            bump("line_too_long")
        elif isinstance(exc, LineTruncated):
            bump("json_decode_truncated")
        else:
            bump("json_decode_error")

    # Pass 1: decide what each line is, and find the session id. Nothing is
    # constructed yet — every content-derived id is keyed on the session id, and
    # back-patching it afterwards leaves the ids hashed on the empty string.
    kept: list[tuple] = []
    model: str | None = None
    for rec in iter_records(path, on_error=count_error):
        seen += 1
        obj = rec.obj
        if not isinstance(obj, dict):
            bump("not_an_object")
            continue

        raw_type = obj.get("type")
        line_type = raw_type if isinstance(raw_type, str) else None

        if line_type == _HEADER:
            session.session_id = session.session_id or (_bounded_id(obj.get("id")) or "")
            session.cwd = session.cwd or _str_or_none(obj.get("cwd"))
            # Both spellings: `branchedFrom` was renamed to `parentSession`
            # (pi CHANGELOG, "`SessionHeader.branchedFrom` →
            # `SessionHeader.parentSession`"), and both shipped
            # `before-compaction.jsonl` headers still carry the old one. Reading
            # only the new name loses the lineage on every pre-rename file,
            # which is every file anybody already has.
            session.parent_session_id = session.parent_session_id or _bounded_id(
                _get(obj, "parentSession", "branchedFrom")
            )
            model = _compose_model(obj) or model
            # `version` here is the *file format* version (3 at the time of
            # writing), not the agent's — a different namespace from the
            # `version` claude_code reads, so it is not mapped to
            # `agent_version`. The header produces no turn, so it is not in
            # `native` either: what is kept of it is the four fields above.
            bump("skip:session_header")
            continue
        if line_type == _TITLE_SLOT:
            bump(f"skip:{_TITLE_SLOT}")
            continue

        entry_id = _bounded_id(obj.get("id"))
        if entry_id is not None:
            offset_of.setdefault(entry_id, rec.offset)

        if line_type == "model_change":
            model = _compose_model(obj) or model

        raw_message = obj.get("message")
        message: dict = raw_message if isinstance(raw_message, dict) else {}
        native_role = message.get("role")
        content = message.get("content") if raw_message is not None else _get(obj, *_PROSE_KEYS)

        # A line belongs in the record model iff it carries an identity or
        # content, not iff its `type` is on an allowlist — an allowlist silently
        # drops every entry type the next release adds, and this union already
        # has eleven members. On the legacy dialect nothing carries an `id`, so
        # `model_change` and `thinking_level_change` fall out by name; on v3
        # they carry one and become no-block system turns, which is faithful:
        # v3 gave them DAG identity, so they are nodes.
        role = _ROLES.get(native_role) if isinstance(native_role, str) else None
        if role is None:
            if not (entry_id or content):
                bump(f"no_identity:{_safe_type(line_type)}")
                continue
            # A `message` whose role this build does not know is *not* billed as
            # a model call. Same reasoning as claude_code's refusal to trust
            # `message.role`: the one reading that costs money is the one a line
            # must not be able to choose for itself.
            role = "system"

        if entry_id and entry_id in seen_ids:
            bump("duplicate_id")
            continue
        if entry_id:
            seen_ids.add(entry_id)

        kept.append((rec, obj, message, content, line_type, native_role, role, entry_id, model))

    if not session.session_id:
        session.session_id = os.path.splitext(os.path.basename(path))[0]

    # Pass 2: build. Every id now sees the real session id.
    for seq, item in enumerate(kept):
        rec, obj, message, content, line_type, native_role, role, entry_id, turn_model = item
        # A `model_usage` entry records a call made outside the conversation, so
        # its own `model`/`usage` win over the carried-forward session model.
        raw_usage = obj.get("usage") if line_type == "model_usage" else message.get("usage")
        blocks = [
            Block(turn_id="", seq=i, kind=k, text=t, tool_name=n, native=nat)
            for i, (k, t, n, nat) in enumerate(
                _message_blocks(message, native_role)
                if obj.get("message") is not None
                else _blocks(content)
            )
        ]
        turn = Turn(
            session_id=session.session_id,
            seq=seq,
            role=role,
            byte_offset=rec.offset,
            byte_len=rec.length,
            # An assistant message states the model that answered it, and that
            # beats whatever the carry-forward believes: `large-session.jsonl`
            # has one aborted `openai/gpt-5.1-codex` call among 452 anthropic
            # ones, and the header alone labels it anthropic. It is read per
            # turn and does *not* clobber `model` for the lines after it —
            # upstream's precedence rule is about which model is selected next,
            # a different question from which model wrote this turn.
            model=(
                _compose_model(obj)
                if line_type == "model_usage"
                else _compose_model(message) or turn_model
            ),
            ts=_bounded_id(obj.get("timestamp")),
            uuid=entry_id,
            parent_uuid=_bounded_id(obj.get("parentId")),
            usage=_scrub(raw_usage) if isinstance(raw_usage, dict) else {},
            # The tool call this result answers — a content-block id, the same
            # namespace claude_code's `toolUseID` lives in, and deliberately not
            # merged with the entry-id namespace above.
            anchor_uuid=_bounded_id(message.get("toolCallId")),
            native=obj,
            blocks=blocks,
        )
        session.turns.append(turn)

        if line_type == "compaction":
            meta: dict = {}
            for src, dst in (
                ("firstKeptEntryId", "first_kept_entry_id"),
                # NOT resolved to an offset. The ordinal indexes an in-memory
                # `compactionEntries` array (`compaction.ts`:
                # `compactionEntries[cutPoint.firstKeptEntryIndex]`), not the
                # file's line list, and the two differ by every skipped and
                # every non-entry line. Recording it verbatim is honest;
                # inventing the mapping would be a silently wrong byte offset.
                ("firstKeptEntryIndex", "first_kept_entry_index"),
                ("tokensBefore", "tokens_before"),
                ("tokensAfter", "tokens_after"),
                ("method", "method"),
            ):
                if obj.get(src) is not None:
                    meta[dst] = _scrub(obj[src])
            first_kept = _bounded_id(obj.get("firstKeptEntryId"))
            if first_kept in offset_of:
                # Provable: the id was read out of this file at this offset.
                # `Event.byte_offset` stays the declaring record's, so the two
                # meanings never share a field.
                meta["first_kept_byte_offset"] = offset_of[first_kept]
            session.events.append(
                Event(session.session_id, seq, "compaction", rec.offset, turn.turn_id, meta)
            )
        elif line_type == "branch_summary":
            # The session continues from some earlier entry with the rest
            # summarized — the same shape as Claude Code's compact-summary fork.
            meta = {}
            from_id = _bounded_id(obj.get("fromId"))
            if from_id:
                meta["from_entry_id"] = from_id
                if from_id in offset_of:
                    meta["from_byte_offset"] = offset_of[from_id]
            session.events.append(
                Event(session.session_id, seq, "fork", rec.offset, turn.turn_id, meta)
            )

    session.skipped = skipped
    session.records_seen = seen
    if session.turns:
        # Informational only. Never used to order anything.
        session.started_at = session.turns[0].ts
        session.ended_at = session.turns[-1].ts
    return session


def find_session(session_id: str, projects_root: str) -> str | None:
    """Locate a transcript by session id under a sessions root.

    pi names a file `<ISO-timestamp>_<session-id>.jsonl` inside a directory
    named after an encoded cwd, so the id is a *suffix* of the stem rather than
    the stem itself. Both shapes are globbed; the bare one costs a pattern and
    covers a renamed file or a build that drops the prefix.

    `session_id` reaches this function from a hook payload, so it is untrusted:
    charset-checked before use, glob-escaped so metacharacters cannot widen the
    search, and every hit confirmed to resolve inside the root. Newest mtime
    wins when a resume-rewrite left duplicates, with the path breaking ties so
    the answer does not depend on set iteration order.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    root = os.path.realpath(projects_root)
    base = _glob.escape(root)
    esc = _glob.escape(session_id)
    hits: set[str] = set()
    for name in (f"{esc}.jsonl", f"*_{esc}.jsonl"):
        # Two *filename* shapes, one depth pattern. `**` matches zero or more
        # directories, so this already covers `base/*/name`. [pair review]
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
