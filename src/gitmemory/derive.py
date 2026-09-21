"""Derivation: key ideas and a timeline, rebuilt from committed bytes alone.

Three rules, and the whole module is downstream of them:

1. **No LLM, ever, on this path.** A 4B model inventing a rationale nobody wrote
   is the worst failure a decision record can have. Everything here is a fold or
   a matrix. DESIGN.md §2.6.
2. **Nothing without provenance.** Every idea carries the `block_id` it was read
   out of; every timeline mark carries the `turn_id` that caused it. An artifact
   that cannot be traced back to a committed block is fiction with a filename.
3. **Rebuildable and diffable.** `derived/` is not a source of truth and is not
   the only copy of anything. Delete it and `gitmemory derive` puts it back,
   byte for byte — that is what the determinism test checks.

`sumy` is an optional dependency (`pip install -e ".[derive]"`) and is imported
lazily, so `capture`, `verify` and `recall` stay dependency-free. It is also the
one place `nltk` could sneak a network fetch into an offline product: sumy's
default tokenizer downloads `punkt` on first use. `_Tok` below is why we never
reach it. See docs/tasks/E5-dependency-verification.md.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass, field

from . import store
from .index import parse_generation
from .records import Session, canonical_json

__all__ = ["Stats", "build", "derived_dir", "ideas", "timeline"]

DEFAULT_IDEAS = 8

# LexRank is O(n^2) in sentences: 2 000 sentences is a 32 MB float64 matrix and
# 10 000 is 800 MB, which is a session turning into an OOM.
# ponytail: hard cap, and the shortfall is reported rather than hidden. Raise it
# by ranking per compaction span instead of per generation if a real session
# ever exceeds it — the spans are already in the manifest.
MAX_SENTENCES = 2000

# Sentence split and word split, both deliberately dumb. The alternative is
# nltk's punkt, which is a download.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9']+")


class _Tok:
    """The two methods `sumy.models.dom.Sentence` actually calls.

    Supplying it keeps `nltk` installed-but-unimported: sumy's summarizers want
    a document, not a tokenizer, and a document is `Paragraph`s of `Sentence`s
    each holding one of these.
    """

    def to_sentences(self, text: str) -> tuple[str, ...]:
        return tuple(s for s in (p.strip() for p in _SENT_RE.split(text)) if s)

    def to_words(self, sentence: str) -> tuple[str, ...]:
        return tuple(_WORD_RE.findall(sentence.lower()))


@dataclass(slots=True)
class Stats:
    generations: int = 0
    ideas: int = 0
    marks: int = 0
    # Reason per generation, never a bare count: a derivation that silently
    # skipped half the store and reported a clean run is the failure mode.
    skipped: list[str] = field(default_factory=list)


def derived_dir(home: str, stored: store.Stored) -> str:
    """`derived/<agent>/<session_id>/gNN/`, mirroring `raw/`.

    Built from the manifest's own path, not from `Stored.key`: `agent` and
    `session_id` come out of untrusted JSON, and the manifest is already sitting
    at a location the store's own path guard vetted.
    """
    session_dir, gen_file = os.path.split(stored.manifest)
    agent_dir, session = os.path.split(session_dir)
    agent = os.path.basename(agent_dir)
    return os.path.join(home, "derived", agent, session, os.path.splitext(gen_file)[0])


def _prose(session: Session):
    """Blocks a human wrote or read, in document order.

    `tool_use` and `tool_result` are machine data — including them makes the
    top-ranked "idea" a JSON payload, measured on the MIT corpus at E2 where
    tool traffic was the bulk of all indexed text.

    ponytail: `thinking` is excluded too. It is often where the reasoning
    actually is, and it is also not what anyone said; revisit when the dashboard
    has a reader who asks for it, and label it separately when it lands.
    """
    for turn in session.turns:
        for block in turn.blocks:
            if block.kind == "text" and block.text.strip():
                yield block


def ideas(session: Session, *, count: int = DEFAULT_IDEAS) -> dict:
    """Extractive key sentences by LexRank, each with the block it came from.

    Returns the ranked sentences *in document order*, which is how they read,
    plus the two counts that make a truncated run visible.
    """
    try:
        from sumy.models.dom import ObjectDocumentModel, Paragraph, Sentence
        from sumy.summarizers.lex_rank import LexRankSummarizer
        from sumy.utils import get_stop_words
    except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise RuntimeError(
            "derivation needs the `derive` extra: pip install -e '.[derive]'"
        ) from exc

    tok = _Tok()
    paragraphs, owners, texts = [], [], []
    seen = 0
    for block in _prose(session):
        sentences = [Sentence(s, tok) for s in tok.to_sentences(block.text)]
        seen += len(sentences)
        room = MAX_SENTENCES - len(owners)
        if room <= 0:
            continue
        sentences = sentences[:room]
        if not sentences:
            continue
        paragraphs.append(Paragraph(sentences))
        owners.extend([block.block_id] * len(sentences))
        texts.extend(str(s) for s in sentences)

    picked: list[dict] = []
    # Speed, not safety: LexRank returns an empty tuple for an empty document,
    # so this only skips loading 580 stop words for a tool-only generation.
    if owners:
        summarizer = LexRankSummarizer()
        summarizer.stop_words = get_stop_words("english")
        document = ObjectDocumentModel(paragraphs)
        # `_get_best_sentences` sorts by rating with a stable sort and then back
        # into document order, so ties keep document order and the result is a
        # subsequence of `texts`. Match it with a cursor rather than `.index()`:
        # sumy's `Sentence` compares by text, so a repeated sentence would
        # otherwise always be attributed to its first occurrence.
        cursor = 0
        for chosen in summarizer(document, count):
            text = str(chosen)
            while cursor < len(texts) and texts[cursor] != text:
                cursor += 1
            if cursor >= len(texts):  # pragma: no cover - not a subsequence
                break
            picked.append({"text": text, "source_ref": owners[cursor], "rank": len(picked)})
            cursor += 1

    return {
        "ideas": picked,
        "sentences_seen": seen,
        "sentences_ranked": len(owners),
    }


def timeline(session: Session) -> dict:
    """The event spine, with how much transcript sits between each mark.

    A fold over `Event` and `Turn`, not a re-listing of the transcript: what a
    reader wants from a timeline is where the compactions were and how much
    happened in between. The raw bytes are three directories away if they want
    the rest.

    `Event.meta` is deliberately dropped. It is adapter-shaped, untrusted, and
    every value in it would have to survive canonical JSON; the kind and the
    anchor are what a spine is made of.
    """
    turns = sorted(session.turns, key=lambda t: (t.byte_offset, t.seq))
    marks = sorted(session.events, key=lambda e: (e.byte_offset, e.seq, e.event_id))

    out: list[dict] = []
    i = 0
    for event in marks:
        turns_since = blocks_since = 0
        # An event with no byte offset (-1) sorts first and consumes nothing,
        # which is right: we do not know where it sat, so we may not claim a
        # span for it.
        while i < len(turns) and turns[i].byte_offset < event.byte_offset:
            turns_since += 1
            blocks_since += len(turns[i].blocks)
            i += 1
        out.append(
            {
                "kind": event.kind,
                "byte_offset": event.byte_offset,
                "source_ref": event.anchor,
                "event_id": event.event_id,
                "turns_since": turns_since,
                "blocks_since": blocks_since,
            }
        )

    # Every turn is accounted for or the fold is lying. The tail is the turns
    # after the last event, and it is emitted even when empty so that summing
    # `turns_since` over the whole timeline always equals `turns`.
    out.append(
        {
            "kind": "tail",
            "byte_offset": -1,
            "source_ref": "",
            "event_id": "",
            "turns_since": len(turns) - i,
            "blocks_since": sum(len(t.blocks) for t in turns[i:]),
        }
    )
    return {"marks": out, "turns": len(turns)}


def _write(path: str, payload: object) -> None:
    """Canonical JSON, published by rename.

    No fsync. `derived/` is rebuildable by definition, so the durability the
    store pays for here would buy nothing: a torn file loses a rebuild, and the
    rebuild is the command that just ran.
    """
    data = canonical_json(payload)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".deriving-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def build(home: str | None = None, *, count: int = DEFAULT_IDEAS) -> Stats:
    """Rebuild `derived/` from the store. Full rebuild, every generation.

    One bad generation costs its own artifacts and nothing else — same rule as
    `index.build` and `verify`, and for the same reason: the store deliberately
    preserves bytes no parser can love, and a reader that gave up on the whole
    tree when it met one would make that preservation worthless.
    """
    resolved = store.resolve_home(home)
    stats = Stats()
    for stored in store.sessions(resolved):
        try:
            session = parse_generation(stored)
            payload_ideas = ideas(session, count=count)
            payload_timeline = timeline(session)
        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data
            stats.skipped.append(f"{stored.key}: {exc!r}")
            continue
        out = derived_dir(resolved, stored)
        _write(os.path.join(out, "ideas.json"), payload_ideas)
        _write(os.path.join(out, "timeline.json"), payload_timeline)
        stats.generations += 1
        stats.ideas += len(payload_ideas["ideas"])
        stats.marks += len(payload_timeline["marks"])
    return stats
