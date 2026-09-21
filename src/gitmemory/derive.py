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
import glob as _glob
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field

from . import redact, store
from .index import parse_generation
from .records import Session, canonical_json

__all__ = ["Stats", "build", "derived_dir", "ideas", "timeline"]

DEFAULT_IDEAS = 8

# Two caps, because there are two costs and the obvious one is the smaller.
#
# LexRank's similarity matrix is O(n^2) in sentences: 2 000 sentences is a 32 MB
# float64 matrix and 10 000 is 800 MB. That is what MAX_SENTENCES bounds.
#
# The cost that actually bites is `_compute_idf`, O(U*n*L) in distinct words,
# and a block with no `.`/`!`/`?` in it is *one* sentence however long it is —
# so the sentence cap never fires on the input that needs it. Measured on one
# terminator-free block: 64 000 words 8.5 s, 128 000 words 33 s, 256 000 words
# 133 s — clean x4 per doubling, so ~9 hours at 48 MB, which one agent
# transcript can reach. MAX_WORDS is the cap on that axis. [E5:5]
#
# ponytail: two hard caps, and the shortfall is reported rather than hidden
# (`sentences_seen` vs `sentences_ranked`, `words_ranked`). Raise them by
# ranking per compaction span instead of per generation if a real session ever
# exceeds them — the spans are already in the manifest.
MAX_SENTENCES = 2000
MAX_WORDS = 50_000

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


def _sumy():
    """The `sumy` pieces, imported lazily and in exactly one place.

    Lazy because `capture`, `verify` and `recall` must stay dependency-free. One
    place because `build` calls it up front: a missing extra is an environment
    fault identical for every generation, and raising it *inside* the
    per-generation `try` laundered "this install cannot derive anything" into N
    skip lines and an exit code of 0. [E5:4]
    """
    try:
        import numpy
        from sumy.models.dom import ObjectDocumentModel, Paragraph, Sentence
        from sumy.summarizers.lex_rank import LexRankSummarizer
        from sumy.utils import get_stop_words
    except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise RuntimeError(
            "derivation needs the `derive` extra: pip install -e '.[derive]'"
        ) from exc
    return (
        ObjectDocumentModel,
        Paragraph,
        Sentence,
        LexRankSummarizer,
        get_stop_words("english"),
        numpy,
    )


def _leaks(data: bytes, path: str = "-") -> list[redact.Finding]:
    """High-confidence secret shapes, the only tier this boundary acts on.

    DESIGN.md names two places redaction applies, and one of them is anything
    written to `derived/` — `derive` did not do it, so a key quoted in prose was
    ranked as an idea and committed a second time. [E5:3]

    HIGH only, deliberately. The SUSPECT tier is `password: "..."`-shaped and
    fires on prose *about* configuration; dropping those would gut legitimate
    content to catch a shape the raw copy already carries anyway.
    """
    return [f for f in redact.scan_bytes(data, path) if f.tier == "high"]


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
    ObjectDocumentModel, Paragraph, Sentence, LexRankSummarizer, stop_words, numpy = _sumy()

    # sumy's `ItemsCount` is `sequence[:count]`, so a negative count means "all
    # but the last |count|" — `--ideas -1` wrote 8 of 9 sentences instead of the
    # 8 best. Same clamp and same reason as `index.search`'s `max(k, 0)`. [E5:6]
    count = max(count, 0)

    tok = _Tok()
    paragraphs, owners, texts = [], [], []
    seen = redacted = words_ranked = 0
    for block in _prose(session):
        kept = []
        for text in tok.to_sentences(block.text):
            seen += 1
            if _leaks(text.encode("utf-8", "surrogatepass")):
                redacted += 1
                continue
            words = tok.to_words(text)
            if len(owners) + len(kept) >= MAX_SENTENCES:
                continue
            if words_ranked + len(words) > MAX_WORDS:
                continue
            words_ranked += len(words)
            kept.append(Sentence(text, tok))
        if not kept:
            continue
        paragraphs.append(Paragraph(kept))
        owners.extend([block.block_id] * len(kept))
        texts.extend(str(s) for s in kept)

    picked: list[dict] = []
    if owners:
        summarizer = LexRankSummarizer()
        summarizer.stop_words = stop_words
        document = ObjectDocumentModel(paragraphs)
        # `_get_best_sentences` sorts by rating with a stable sort and then back
        # into document order, so ties keep document order and the result is a
        # subsequence of `texts`. Match it with a cursor rather than `.index()`:
        # sumy's `Sentence` compares by text, so a repeated sentence would
        # otherwise always be attributed to its first occurrence.
        #
        # A document LexRank finds no signal in — every term's idf is exactly
        # zero, which happens for "... !!! ???", for nothing but stop words, and
        # for two sentences sharing no vocabulary — makes the whole similarity
        # matrix zero, and `power_method` then normalises by a zero norm. The
        # ratings come back NaN, sumy's sort over them silently degrades to
        # input order, and a `RuntimeWarning: invalid value encountered in
        # divide` reaches the terminal. Ranking nothing is the honest answer:
        # `sentences_ranked > 0` with an empty `ideas` says "there was prose and
        # it had no signal", which is exactly what happened. Asking numpy is how
        # we detect it — the condition is a property of sumy's arithmetic, and
        # any reimplementation of it here would be a second thing to get wrong.
        # [E5:10]
        cursor = 0
        try:
            with numpy.errstate(invalid="raise", divide="raise"):
                chosen_sentences = summarizer(document, count)
        except FloatingPointError:
            chosen_sentences = ()
        for chosen in chosen_sentences:
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
        "sentences_redacted": redacted,
        "words_ranked": words_ranked,
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

    The redaction gate is here rather than only in `ideas()` because this is the
    single door into `derived/`: every artifact this module grows later goes
    through it too, and an artifact that reaches this point carrying a key is a
    bug upstream, not a sentence to quietly drop. [E5:3]
    """
    data = canonical_json(payload)
    leaks = _leaks(data, os.path.basename(path))
    if leaks:
        raise ValueError(f"refusing to write a secret into derived/: {leaks[0]}")
    parent = os.path.dirname(path)
    # Not `os.makedirs`: it applies its mode to the leaf only. `derived/` holds
    # the same transcript text the store does, so it gets the store's stance —
    # 0700, and never a mode a caller's umask chose. [E5:9]
    store._mkdir(parent)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".deriving-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sweep_temps(home: str) -> None:
    """Remove half-written artifacts a killed `derive` left behind.

    `_write` unlinks its own temp when the write fails, but not when the process
    is killed between `mkstemp` and `os.replace`. The leftover is a dotfile in
    the target directory, `gitrepo.commit` stages dotfiles, and a rebuild never
    touched it — so it was committed for ever. `.gitignore` stops the commit and
    this stops the litter, the same two-part answer the store already gives its
    own temps. [E5:8]
    """
    for stray in _glob.glob(os.path.join(home, "derived", "*", "*", "*", ".deriving-*")):
        with contextlib.suppress(OSError):
            os.unlink(stray)


def build(home: str | None = None, *, count: int = DEFAULT_IDEAS) -> Stats:
    """Rebuild `derived/` from the store. Full rebuild, every generation.

    One bad generation costs its own artifacts and nothing else — same rule as
    `index.build` and `verify`, and for the same reason: the store deliberately
    preserves bytes no parser can love, and a reader that gave up on the whole
    tree when it met one would make that preservation worthless.
    """
    resolved = store.resolve_home(home)
    # Before the loop, not inside it: see `_sumy`. [E5:4]
    _sumy()
    _sweep_temps(resolved)
    stats = Stats()
    for stored in store.sessions(resolved):
        out = derived_dir(resolved, stored)
        try:
            session = parse_generation(stored)
            payload_ideas = ideas(session, count=count)
            payload_timeline = timeline(session)
            # Inside the try, both of them. Outside, a failure on the second
            # write aborted the whole build with no skip entry and left the
            # generation torn: a fresh ideas.json beside a stale timeline.json.
            # [E5:7]
            _write(os.path.join(out, "ideas.json"), payload_ideas)
            _write(os.path.join(out, "timeline.json"), payload_timeline)
        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data
            # Leave nothing behind that still asserts facts about a generation
            # this run could not read or could not finish writing. A stale
            # artifact beside a skip line is a `derived/` tree that has quietly
            # stopped being a function of the committed bytes, and `git diff`
            # shows clean while it happens. [E5:2, E5:7]
            shutil.rmtree(out, ignore_errors=True)
            stats.skipped.append(f"{stored.key}: {exc!r}")
            continue
        stats.generations += 1
        stats.ideas += len(payload_ideas["ideas"])
        stats.marks += len(payload_timeline["marks"])
    return stats
