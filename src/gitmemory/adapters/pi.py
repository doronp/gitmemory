"""pi / oh-my-pi adapter: ~/.pi/agent/sessions/**/*.jsonl -> canonical records.

pi (earendil-works/pi) and oh-my-pi (can1357/oh-my-pi, a fork of it) write the
same JSONL dialect, so one adapter reads both and `AGENT` names the *format*
rather than the binary. The registry accepts "omp" as an alias for that reason.

Three things this format does that Claude Code's does not, all load-bearing:

  - **The file is not strictly append-only.** oh-my-pi keeps a fixed-width
    256-byte `title` slot on line 1 that is rewritten in place
    (`session-entries.ts:15 SESSION_TITLE_SLOT_BYTES` @b52e1f5), and the
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
    102,084 bytes *after* their own cut points; in pi's, 111,830 and 102,180.
    It is the same session in both forks — the fixtures are the same 1,003
    lines, carrying the author's home directory 780 times over 113 distinct
    paths — and the gap between the two numbers is an npm scope:
    `@mariozechner/pi` became `@oh-my-pi/pi` inside the transcript text, 649
    times, 4 bytes shorter each time. 50 of those occurrences fall *inside the
    first gap* — between the cut point and the record that declares it — which
    is 200 bytes, and 24 inside the second, which is 96: the whole of the
    difference in each case. (Counted in the gap, not before the cut; before
    the cuts there are 342 and 482, which are not the numbers that explain
    anything.) Both gap sizes come
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
# (pi's `compaction.test.ts:138-148` @a8ed4977 `createCustomMessageEntry`, and
# oh-my-pi's `session-manager-internal-details.test.ts:24` @b52e1f5 — two repos
# at two revs, which the single trailing `@b52e1f5` this comment used to carry
# got wrong: at b52e1f5 those compaction.test.ts lines are
# `createCompactionEntry`, an entry with neither `content` nor `customType`).
# Without it that entry is a turn with zero blocks: the text is in
# `native`, so nothing is lost, but it is not in canonical JSON, not in the
# index, and not counted in `skipped` either — invisible in the one direction
# the accounting rule exists to prevent.
_PROSE_KEYS = ("summary", "shortSummary", "content", "data", "text")

# One level deeper, for the entries that wrap their prose in an object. pi's
# `context_edit` is the only one: `{type, targetId, replacement: {content} |
# null}` (`src/core/session-manager.ts:174-180` @a8ed4977), where `replacement`
# is the text that *supersedes* the target's contribution to model context and
# `null` means drop the target entirely. Reading only the top level left the
# replacement in `native` and the superseded text canonical — the store would
# hand back the stale version and the new one was unfindable. Nested rather
# than folded into `_PROSE_KEYS` because the keys collide: `replacement`
# carries `content`, and so does a `custom_message`.
_NESTED_PROSE_KEYS = ("replacement",)


def _prose(obj: dict):
    """The entry's own text, from the top level or one wrapper in.

    No fixture in either fork contains a `context_edit`, so the nested read is
    pinned by a synthetic transcript rather than the corpus — the shape comes
    from upstream's source, not from a line we have seen.
    """
    top = _get(obj, *_PROSE_KEYS)
    if top is not None:
        return top
    for key in _NESTED_PROSE_KEYS:
        inner = obj.get(key)
        if isinstance(inner, dict):
            nested = _get(inner, *_PROSE_KEYS)
            if nested is not None:
                return nested
    return None


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


def _with_error(
    blocks: list[tuple[str, str, str | None, dict]], obj: dict
) -> list[tuple[str, str, str | None, dict]]:
    """Append an entry's `errorMessage` as prose, if it has one.

    An aborted or errored call writes its explanation next to `content` rather
    than in it, and `content` is then usually `[]`. In the shipped corpus that
    is 19 and 22 assistant messages per *fixture* — 41 per fork — 13 and 14 of
    them with no content at all: turns with zero blocks whose only prose
    ("Request was aborted.") reached `native` and stopped there, so a search
    for why the transcript goes quiet returned nothing. Appended rather than
    substituted, because an entry can carry both.

    Applied to every entry shape that can declare the field rather than to
    assistant messages alone. `ModelUsageEntry` declares it too
    (`session-entries.ts:89` @b52e1f5) and has no `message` at all, so the
    whole reason a background call failed lived only in `native`; the corpus
    happens to contain none, which is why only a synthetic pins it.

    `native` is otherwise always the file's own object, so a block gitmemory
    composed says so instead of carrying a bare `{}` that reads as "this entry
    had no fields".
    """
    error = obj.get("errorMessage")
    if not isinstance(error, str) or not error:
        return blocks
    return [*blocks, ("text", error, None, {"gitmemory_synthesized": "errorMessage"})]


def _message_blocks(message: dict, native_role) -> list[tuple[str, str, str | None, dict]]:
    """Blocks for one `type: "message"` entry, whatever its role keeps them in."""
    if native_role == "bashExecution":
        # No `content` key at all: the command and its output are message
        # fields. Skipping the entry would lose real transcript text, and
        # concatenating the two would make the command unfindable separately
        # from a 10,000-line output, which is the search that matters.
        return _with_error(
            [
                ("tool_use", _flatten(message.get("command")), "bash", {}),
                ("tool_result", _flatten(message.get("output")), None, {}),
            ],
            message,
        )
    blocks = _blocks(message.get("content"))
    if native_role == "toolResult":
        # The content is a list of plain `text` blocks; only the message level
        # says it is a tool return. Without this the index sees a tool's stdout
        # as prose. The tool name is carried on the block — unlike Claude Code,
        # where a tool_result genuinely does not know it and the name has to be
        # joined from the matching tool_use.
        name = message.get("toolName")
        tool = name if isinstance(name, str) else None
        return _with_error([("tool_result", t, tool, nat) for _k, t, _n, nat in blocks], message)
    return _with_error(blocks, message)


def _compose_model(obj: dict) -> str | None:
    """`provider` + `modelId`/`model` read as the one `"provider/model"` string.

    Four kinds of line name a model and they do not agree on how — but they do
    agree on one thing, which is what makes this function short: a line either
    carries a `provider` next to a *bare* id, or carries the joined string and
    no `provider` at all. The session header splits it into `provider` and
    `modelId`; an assistant message and a `model_usage` entry split it into
    `provider` and `model` (`agent-session-stats.test.ts:21-39` @b52e1f5
    passes `model: target.id`; the rev matters, because the same path in pi at
    `a8ed4977` is a `createUsage` helper with no `model` field at all); a
    `model_change` does whichever the build was current
    for — every one of the twelve in the shipped fixtures splits it, while
    oh-my-pi's own tests write it joined and drop `provider`
    (`sdk-model-selection.test.ts:1218,1331`). Reading only the joined spelling
    is how `model_change` silently did nothing on all four corpus files.

    So the join is unconditional. An earlier revision guarded it with
    `name.startswith(provider + "/")` on the theory that `model_usage` wrote
    both; it does not, no upstream literal anywhere writes both, and
    instrumenting the branch over all four fixtures fires it 0 times in 1,890
    compositions. What it *did* reach was the opposite shape: a provider-
    qualified id that is the model's literal name — `provider: "mock"` with
    `model: "mock/mock"` (`unexpected-stop-classifier.test.ts:20-21`
    @b52e1f5).

    Collapsing that is not merely lossy, it resolves to a different model.
    oh-my-pi's own catalog ships two distinct entries under `openrouter`:
    `auto` (name `"Auto"`, cost 0/0) and `openrouter/auto` (name
    `"Auto Router"`, cost -1000000/-1000000) — `packages/catalog/src/models.json`
    @b52e1f5. So `provider: "openrouter"` with `model: "openrouter/auto"`
    collapses to `openrouter/auto`, and upstream's own resolver splits a
    reference on its *first* slash (`config/model-resolver.ts:689-692`
    @b52e1f5), reading that back as provider `openrouter`, id `auto` — the
    other entry, with the other cost row, in a field `dash_spend` groups by.
    The uncollapsed `openrouter/openrouter/auto` round-trips to the right one.
    A doubled prefix on some future writer is visibly wrong; a collapsed one
    is silently wrong, and this store's whole claim is that its strings are
    the file's.
    """
    name = _get(obj, "modelId", "model")
    if not isinstance(name, str) or not name:
        return None
    provider = obj.get("provider")
    if not isinstance(provider, str) or not provider:
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
            # The header is rewritten in place on every model switch, so this
            # seeds the carry-forward with the *last* model the session used,
            # not the first. Turns before the first `model_change` therefore
            # carry a post-hoc label. Preferred anyway: the alternative is null
            # until the first switch, and a wrong-but-plausible model on the
            # opening turns is the smaller error than no model at all — the
            # per-turn reads above override it wherever the line says otherwise.
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
        content = message.get("content") if raw_message is not None else _prose(obj)

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
                else _with_error(_blocks(content), obj)
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
            # Only the message branch falls back to the carry-forward. A
            # `model_usage` line is by construction a call some *other* model
            # made — `role: "tiny"`/`"smol"` (`session-entries.ts:82-83`
            # @b52e1f5) — so the conversation model is not a weak guess for it,
            # it is the one answer known to be wrong. An earlier revision
            # applied the fallback to both branches on the reasoning that a
            # call that happened should not read as "no model"; that labels an
            # auto-title generated by a tiny model as the session's opus turn,
            # in a field `dash_spend` groups by. Null is the honest answer.
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


def _glob_hits(session_id: str, root: str) -> set[str]:
    """Every path the two filename shapes reach under `root`. No validation.

    Split out of `find_session` so the hostile-id test's negative control can
    ask the implementation what it would find instead of restating the
    patterns. A restated control drifts the moment a shape changes here, and
    it drifts *green*: the fixture stops planting anything the glob can reach,
    every hostile id returns `None` for the wrong reason, and the test that
    exists to prove the charset guard works proves nothing. [review: tests]
    """
    base = _glob.escape(root)
    esc = _glob.escape(session_id)
    hits: set[str] = set()
    for name in (f"{esc}.jsonl", f"*_{esc}.jsonl"):
        # Two *filename* shapes, one depth pattern. `**` matches zero or more
        # directories, so this already covers `base/*/name`. [pair review]
        hits |= set(_glob.glob(os.path.join(base, "*", "**", name), recursive=True))
    return hits


def _names_session(path: str, session_id: str) -> bool:
    """Whether a globbed filename really names this session, not one ending in it.

    `*_<id>.jsonl` is a suffix match, so it is satisfied by any id the target
    id happens to end after an underscore. The prefix upstream actually writes
    is `fileTimestamp` — `timestamp.replace(/[:.]/g, "-")`
    (`session-manager.ts:1079` @a8ed4977) — which cannot contain `_`, so the
    first underscore is the separator and everything after it is the id.
    """
    stem = os.path.basename(path)[: -len(".jsonl")]
    return stem == session_id or stem.partition("_")[2] == session_id


def find_session(session_id: str, projects_root: str) -> str | None:
    """Locate a transcript by session id under a sessions root.

    pi names a file `<ISO-timestamp>_<session-id>.jsonl` inside a directory
    named after an encoded cwd, so the id is a *suffix* of the stem rather than
    the stem itself. Both shapes are globbed; the bare one costs a pattern and
    covers a renamed file or a build that drops the prefix.

    The glob is not the whole test. `*_<id>.jsonl` is a suffix match and an id
    may itself contain `_` — `assertValidSessionId` (`session-manager.ts:268`
    @a8ed4977) permits it explicitly — so a search for `beta` also matches
    `…Z_alpha_beta.jsonl`, which is session `alpha_beta`, and the function used
    to hand back *a different session's transcript* rather than nothing.
    `_names_session` re-checks each hit against the two shapes the glob was
    meant to express. It can do that exactly because the prefix is
    `fileTimestamp`, which upstream builds as `timestamp.replace(/[:.]/g, "-")`
    (`session-manager.ts:1079`) and which therefore cannot contain `_`: the
    first underscore is provably the separator.

    `session_id` is untrusted — nothing in-tree calls this yet, and the caller
    it is written for is a hook payload. So it is charset-checked before use,
    glob-escaped so metacharacters cannot widen the search, and every hit is
    confirmed to resolve inside the root. Newest mtime wins when a
    resume-rewrite left duplicates, with the path breaking ties so the answer
    does not depend on set iteration order.

    The returned path is the globbed one, not the resolved one, so a symlink
    inside the root is followed by the caller as the user intended — the
    containment proof is on the target, the answer is the name. That leaves the
    usual TOCTOU window, and `isfile` does not close any half of it: it is one
    more check before the same unsynchronised open, and the name can be
    replaced between the two. What it buys is that the *ordinary* wrong shapes
    — a directory or a FIFO named `<id>.jsonl` — are not returned as
    transcripts, so the reader blocks or raises only if someone is racing it.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    root = os.path.realpath(projects_root)
    inside = [
        h
        for h in _glob_hits(session_id, root)
        if _names_session(h, session_id)
        and os.path.realpath(h).startswith(root + os.sep)
        and os.path.isfile(h)
    ]
    if not inside:
        return None

    def newest(p: str) -> tuple[float, str]:
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (-1.0, p)

    return max(inside, key=newest)
