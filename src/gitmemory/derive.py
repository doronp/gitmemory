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
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field

from . import records, redact, store
from .index import parse_generation
from .records import Session, canonical_json

__all__ = ["Decision", "Stats", "build", "decisions", "derived_dir", "ideas", "timeline"]

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
# Neither cap bounded the *time*, which is the third cost and the one nobody
# had measured: 2 000 sentences of 25 all-distinct words sits exactly on both
# caps, is 333 KB, and cost 30 s of CPU. The caps were not wrong — the loops
# under them were, and `_sumy`'s `LexRank` subclass hoists them. Worst case at
# these same numbers is now 9.1 s, and it is a *measured* ceiling rather than
# an argued one. Raising MAX_SENTENCES raises it superlinearly; read the note
# on the subclass before touching either number. [E7 index-F8]
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

    class LexRank(LexRankSummarizer):
        """sumy's LexRank with two loops hoisted. Same matrix, bit for bit.

        Not an improvement on the algorithm — the same algorithm, with the
        work that does not depend on the loop variable moved out of the loop.
        Both overrides are checked against the originals by
        `test_the_hoisted_lexrank_is_the_same_matrix_sumy_computes`, which
        compares the matrices element-wise and the picks, on random documents.

        The caps above bound the *shape* of the input; without these they did
        not bound its *cost*, because the cost is in two places neither cap
        could see. Measured on this tree, worst case at each cap:

            2000 sentences x 25 all-distinct words   29.80 s -> 1.37 s
            2000 sentences x 25 identical words      23.10 s -> 9.11 s
            2000 sentences x 5 identical words        7.31 s -> 2.70 s

        `_compute_idf` asks `sum(1 for s in sentences if term in s)` once per
        distinct term, and `s` is a *list* despite `_to_words_set`'s name — so
        it is O(distinct x sentences x length) where a document-frequency
        count is O(total words). That is the first column above.

        `_create_matrix` calls `cosine_similarity`, which builds
        `frozenset(sentence)` for both arguments and recomputes both vector
        norms, for every one of the n^2 pairs — so each sentence's set is
        rebuilt 2n times and its norm 2n times. Hoisting both leaves the
        genuinely pairwise part: the intersection and the dot product over it,
        which is what LexRank is. That is the residue in the second column,
        and it is the real ceiling: at the caps, `sum over pairs of
        min(len_i, len_j)` maximises at equal lengths, 2000^2 x 25 = 10^8
        term-multiplications, which is the 9.11 s.

        ponytail: the symmetry is not exploited. `cos(i,j) == cos(j,i)`, so
        half the loop is redundant and 9.11 s could be 4.6 s — but the two
        intersections iterate in different orders, so the sums round
        differently in the last bit, and a pair either side of the threshold
        would then depend on which triangle computed it. Bit-identical to
        sumy is worth more than the 2x. [E7 index-F8]
        """

        @staticmethod
        def _compute_idf(sentences):
            df = Counter()
            for sentence in sentences:
                df.update(set(sentence))
            count = len(sentences)
            return {term: math.log(count / (1 + n_j)) for term, n_j in df.items()}

        def _create_matrix(self, sentences, threshold, tf_metrics, idf_metrics):
            count = len(sentences)
            unique = [frozenset(s) for s in sentences]
            # sumy's `denominator1`/`denominator2`, square-rooted once here
            # rather than 2n times each inside the loop.
            norms = [
                math.sqrt(sum((tf[t] * idf_metrics[t]) ** 2 for t in u))
                for u, tf in zip(unique, tf_metrics, strict=True)
            ]
            matrix = numpy.zeros((count, count))
            degrees = numpy.zeros((count,))
            for row in range(count):
                u1, tf1, norm1 = unique[row], tf_metrics[row], norms[row]
                if norm1 <= 0:  # sumy's `denominator1 > 0`, which returns 0.0
                    continue
                for col in range(count):
                    norm2 = norms[col]
                    if norm2 <= 0:
                        continue
                    common = u1 & unique[col]
                    if not common:  # a zero numerator never exceeds a positive
                        continue    # threshold, and this is the common case
                    tf2 = tf_metrics[col]
                    numerator = sum(tf1[t] * tf2[t] * idf_metrics[t] ** 2 for t in common)
                    if numerator / (norm1 * norm2) > threshold:
                        matrix[row, col] = 1.0
                        degrees[row] += 1
            for row in range(count):
                # `or 1` is sumy's `if degrees[row] == 0: degrees[row] = 1`,
                # which it does inside a second n^2 loop.
                matrix[row] /= degrees[row] or 1
            return matrix

    return (
        ObjectDocumentModel,
        Paragraph,
        Sentence,
        LexRank,
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
    decisions: int = 0
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


# Text that arrived in a human's turn without a human typing it. Three shapes,
# all of them whole-block: the CLI's markup notices, this adapter's own
# placeholders, and the canonical JSON it writes for a block kind it does not
# recognise.
#
# Measured, not guessed. On the secondary set — real third-party sessions —
# **39% of the prose in the user role was injected**, and 19 of the extractor's
# 30 false positives came from it. One sentence did most of the damage:
# `<ide_opened_file>… This may or may not be related to the current task.`, which
# the editor injects on every file opened and in which `_PROHIBIT` matches
# `may not`. No probe could find this: every probe item is a sentence somebody
# wrote on purpose. See `docs/benchmarks/E5-secondary-set.md`.
#
# Structural rather than a list of tag names, because the list is the CLI's to
# change and it has changed before: `<ide_opened_file>`, `<ide_selection>`,
# `<command-name>`, `<local-command-stdout>`, `<bash-input>`, `<command-args>`
# and whatever ships next.
#
# Two conditions, and the second one is a correction. **A human writing prose
# does use tags** — `<rules>…</rules>`, `<instructions>…</instructions>`,
# `<context>…</context>` is the prompt style the model vendor's own
# documentation teaches, and the first version of this pattern silenced every
# one of them. Losing a standing rule is the worse failure of the two this
# predicate can make, so the tag name now has to carry a `-` or a `_`. Every
# injected tag above does; the single-word tags a person reaches for do not.
# It is a discriminator and not a proof — somebody will one day write
# `<coding-rules>` — and the residue is recorded in `_injected`.
#
# The close tag must also *be* the open tag. `<a>x</zzz>` matched before, and
# with `re.S` the `.*` spanning the whole block, one stray `</…>` anywhere in a
# long human turn was enough to swallow all of it. [E5 review of fix 1]
#
# One *element*, not the whole block, because the CLI concatenates: a real block
# on the secondary set is `<bash-stdout>…</bash-stdout><bash-stderr></bash-stderr>`,
# which a single backreferenced element cannot match and which nobody typed. The
# whole-block question is `_all_markup`, which walks elements end to end rather
# than wrapping this in `(?:…)+` — a nested quantifier around `.*?` backtracks
# exponentially on a crafted block, and blocks come from transcripts we do not
# write.
_MARKUP = re.compile(r"<([a-z][a-z0-9]*[_-][a-z0-9_-]*)>.*?</\1>\s*", re.S)


def _all_markup(text: str) -> bool:
    """Whether the block is machine tags end to end and nothing else.

    Linear: each element is claimed by the first close tag that matches its own
    open tag, and the walk never goes back. A block that is tags followed by a
    sentence stops at the sentence and is somebody's — which is the failure this
    is anchored against, not the one where a notice survives.
    """
    pos = 0
    while m := _MARKUP.match(text, pos):
        pos = m.end()
    return pos > 0 and pos == len(text)

# `[image image/png 1046384 chars]` from `_image_text`, `[Request interrupted by
# user]` from the CLI. Whole-block and bracket-delimited, so a sentence that
# merely contains a bracket is untouched.
#
# The first character inside the bracket has to be a letter, and the notice has
# to be one line. Both are corrections found by re-running the review's JSON
# case: `["never use pickle", "always use uv"]` opens with `[`, closes with `]`
# and holds no bracket in between, so the old pattern classified every
# whole-block JSON *array* as a placeholder — before the JSON branch below ever
# saw it, which is why narrowing that branch alone did not free the array. A
# digit or a quote after the bracket means data; a letter means one of the two
# notices above. The residue is a whole-turn bracketed aside — `[note: never
# commit to main]` — and it is recorded in `_injected`.
_NOTICE = re.compile(r"\A\[[A-Za-z][^\[\]\n]*\]\Z")

# The CLI's own preamble on a local-command turn, which says in English that
# what follows is not addressed to the reader.
#
# One paragraph, and that is the correction: the pattern was anchored at `\A`
# with nothing at the other end, so anything appended to the caveat went with
# it. Unlike the tag case this fires on the CLI's everyday shape, which makes it
# the more likely of the two to have eaten somebody's sentence. A blank line
# ends the notice; what comes after it is somebody's. [E5 review of fix 1]
_CAVEAT = re.compile(
    r"\ACaveat: The messages below were generated by the user[^\n]*(?:\n[^\n]+)*\n?\Z", re.I
)


def _injected(text: str) -> bool:
    """Whether a block was put there by a program rather than typed.

    Applied to **every** role, not only the user's. The measured problem is in
    the user role — that is where a rule gets read out of a notice — but a
    `[image image/png …]` placeholder and the adapter's canonical JSON are no
    more somebody's writing in the assistant's turn than in anyone's.

    The JSON case asks whether the block is byte-for-byte what *this* writer
    would have produced: parse it, re-serialise it through the same
    `canonical_json` the adapter used, and compare. Asking only "does it parse"
    was wider than the sentence justifying it — `{"note": "never use sqlite in
    prod"}` and `["never use pickle", "always use uv"]` are things a person
    pastes, and both were silenced. Canonical form is sorted, separator-tight
    and ASCII-escaped; almost nothing typed or copied out of an editor matches
    it, and everything this adapter writes does, exactly. [E5 review of fix 1]

    `RecursionError` is caught with the parse errors because `json.loads` raises
    it rather than `ValueError` on deeply nested input, and it is not a
    hypothetical: `"["*20000 + "]"*20000` in one user block took the exception
    all the way out through `decisions()` and cost the whole generation its
    derived artifacts — no ideas, no timeline, no graph. The docstring here used
    to claim "blocks are bounded by the adapter, so the parse is bounded too",
    and that was simply false; the text branch copies `item["text"]` with no
    bound at all.

    What this deliberately does not try to catch: a slash command's *expansion*
    (`/init` puts "Please analyze this codebase and create a CLAUDE.md file…"
    into the user role as ordinary prose) and a subagent dispatch prompt. Both
    are machine-authored, both were labelled `machine` by all three annotators
    on the secondary set, and neither carries a marker. Catching them needs the
    preceding `<command-name>` block as context, which is a change to the
    adapter rather than to a predicate over one block's text.

    And what it knowingly lets through, after the review of fix 1: an unknown
    block whose canonical JSON a later writer re-formats, and a machine tag
    without a `-` or `_` in it. Both leave a notice in the prose stream, which
    is the cheaper of this predicate's two failures. Still eaten, in the other
    and worse direction: a turn that is nothing but a bracketed aside opening
    with a letter — `[note: never commit to main]` — which `_NOTICE` cannot tell
    from `[Request interrupted by user]` without reading it.
    """
    if _all_markup(text) or _NOTICE.match(text) or _CAVEAT.match(text):
        return True
    if text.startswith(("{", "[")):
        try:
            return canonical_json(json.loads(text)) == text.encode()
        except (ValueError, RecursionError):
            return False
    return False


def _prose(session: Session):
    """`(turn, block)` for every block a human wrote or read, in document order.

    `tool_use` and `tool_result` are machine data — including them makes the
    top-ranked "idea" a JSON payload, measured on the MIT corpus at E2 where
    tool traffic was the bulk of all indexed text.

    So is a block the CLI injected into somebody's turn — see `_injected`. The
    kind is `text` and the role is `user`, and neither of those makes it
    somebody's writing. It stays in `raw/`, it stays in the index, and it is
    findable; what it stops being is a sentence anybody said.

    The turn comes back with the block because `decisions` needs the role and
    there must not be a second copy of this admission rule to keep in step with
    it: a reader that admitted a different set of blocks than the ranker would
    attribute decisions to bytes `ideas.json` says are not prose.

    ponytail: `thinking` is excluded too. It is often where the reasoning
    actually is, and it is also not what anyone said; revisit when the dashboard
    has a reader who asks for it, and label it separately when it lands.
    """
    for turn in session.turns:
        for block in turn.blocks:
            text = block.text.strip()
            if block.kind == "text" and text and not _injected(text):
                yield turn, block


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
    for _turn, block in _prose(session):
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


# --- the decision extractor -------------------------------------------------
#
# Two labels, and both of them are a *pair*:
#
#   directive — a user block that names both sides of a standing constraint,
#               what to do and what not to do.
#   reversal  — an assistant block that gives up something already in play and
#               names what takes its place.
#
# So every rule below is a pair detector, and that is the whole design. A
# one-sided sentence is an instruction ("make sure the tests pass") or a retry
# ("let me run it again") or a report ("we moved off CSV last year"), and those
# are the bulk of any transcript. An extractor that fires on one side fires on
# all of them.
#
# The lexicons are *classes of word*, named for the job the word does in the
# sentence, not for the phrasings of any one transcript. Extend a class when a
# new word does the same job; adding a literal because it appeared in a corpus
# is how this becomes a memoriser of that corpus.
#
# ponytail: sentence-level shape only — no parse, so "we are not going to avoid
# X" reads as a prohibition, a constraint split across two turns is missed
# entirely, and a replacement stated as a bare declarative about the successor
# ("dropping the cache; the stdlib covers it") has no marker to find. The
# upgrade path is a dependency parse, which is a model download, which is the
# one thing this module may not do (DESIGN.md §2.6); the honest alternative is
# to widen the corpus until the shapes that fail are known.

# Degree and stance adverbs: the modifiers that attach to a verb phrase without
# changing what it asserts. English puts them on either side of the auxiliary —
# "I *really* don't think" and "I don't *really* think" are the same hedge — so a
# frame that opens the slot in one position and not the other has a hole the
# grammar does not have, and a hedge read as a rule is what falls through it.
# `\w+ly` is the productive form; the rest are the adverbs with no suffix, and
# writing it this way is what retires the hand-picked `-ly` literals that `_MID`
# and `_OPINION` each kept a private copy of.
_ADV = (
    r"(?:\w+ly|quite|rather|somewhat|even|much|all that|at all|so|too|just|ever"
    r"|still|half|altogether)"
)

# An adverb/auxiliary slot, for use in the middle of a frame. A fixed two-word
# sequence is a surface form; the same frame with a slot in it is a class, and
# every frame below that can take a modifier is written with one. The absence of
# this slot is what let "as we discussed" through while catching "as discussed".
# The members are the words a slot holds that `_ADV` does not: perfect and
# aspectual auxiliaries, and the temporal and focus adverbs.
_MID = (
    rf"(?: (?:{_ADV}|have|has|had|already|previously|earlier|before|also|again"
    r"|keep|kept|been|all|only|both|indeed))*"
)

# Verbs of saying. A class of saying-verbs has a couple of dozen members, not
# four; the job the word does is "report an earlier speech act", so every verb
# that reports one belongs here whether or not any transcript used it.
_SAYING = (
    r"(?:noted|noting|mentioned|mentioning|stated|stating|said|say|says|saying"
    r"|discussed|discussing|agreed|agreeing|decided|deciding|established"
    r"|covered|covering|explained|explaining|asked|asking|requested|requesting"
    r"|specified|specifying|flagged|flagging|pointed out|indicated|indicating"
    r"|emphasi[sz]ed|stressed|highlighted|underlined|described|outlined"
    r"|set out|laid out|spelled out|spelt out|wrote|written|told you|put it"
    r"|observed|remarked|reiterated|repeated|confirmed|clarified|warned"
    r"|instructed|directed|insisted|required|advised|suggested|promised)"
)

# Adversative and concessive connectives: the words that join a conceded clause
# to the point being made. They are here because they are the one thing that can
# stand between a unit's punctuation and the connective that opens it — "sorry
# to nag, but as we agreed …" — and without them the boundary is unreachable.
_CONCESSIVE = (
    r"(?:but|however|although|though|still|yet|anyway|regardless|nonetheless"
    r"|nevertheless|that said|even so|in any case|either way|mind you|all the same)"
)

# The positions a discourse connective can open from. This used to be offset 0
# alone, matched with `_BACKREF.match`, and offset 0 is not where chat puts it: a
# block is several sentences, the frame opens the second or the fifth of them,
# and it follows an apology or a concession when it opens the first. Every one of
# those was a restatement read as a fresh directive.
#
# Still a *position* and not a bag of words, which is the other half of getting
# this right. The frame has to open a unit — the block, a sentence, a clause
# after its punctuation, or the concessive that joins one — because a
# saying-verb loose inside a clause is reporting rather than restating ("write
# the ADR that records what we agreed"), and suppressing those costs real
# directives.
_OPENS_A_UNIT = rf"(?:\A|[.!?;:,\n)]|[—–]|\s-\s|\b{_CONCESSIVE}\b)"

# Back-reference: the speaker is restating something already agreed, which is
# not a new decision however imperative it sounds.
_BACKREF = re.compile(
    rf"{_OPENS_A_UNIT}\s*(?:"
    # The citation frame: "as <subject>? <slot>* <saying-verb>". One frame with
    # two open positions, rather than one list per subject — keeping two lists
    # in step is what failed, and "as we established" fell in the gap.
    rf"as(?: (?:i|we|you|they|it))?{_MID} {_SAYING}\b"
    r"|as (?:previously|already|before|earlier|above|discussed|agreed|per)\b"
    r"|as you (?:know|are aware|(?:will|may|might) recall)\b"
    # Citation by reference rather than by verb: "per the spec", "per my note".
    r"|per (?:my|our|your|the|that|this|a) [\w-]+"
    # Explicit repetition, with the politeness and hedge slots open in front.
    r"|(?:just |simply |only )?to (?:recall|reiterate|repeat|restate|remind)\b"
    # A reminder *frame* introduces what is being restated, so the noun is
    # followed by the thing it introduces. Without that lookahead "reminder" and
    # "recall" are ordinary words of this domain — a reminder job, a recall
    # figure beside a precision one — and unanchoring the guard put both of them
    # one comma away from suppressing a real directive.
    r"|(?:just |simply )?(?:a |an |one |another |the )?"
    r"(?:quick |friendly |gentle |final |small |little |brief |last )*"
    r"remind(?:er|ing)(?=\s*[:,;—–]|\s+(?:that|to)\b)"
    # `keep X in mind` and `bear X in mind` are separable, and the fixed strings
    # below only ever matched the un-separated form — "please keep this
    # instruction in mind" walked straight past a guard that claims exactly it.
    # No lookahead on this one: the particle `in mind` is the whole
    # disambiguation, so nothing after it has to be checked. [E5 root cause 4]
    r"|(?:please |just )?(?:bear|keep)(?:s|ing)? [\w ]{1,30}?in mind\b"
    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)"
    r"(?=\s*[:,;—–]|\s+(?:that|to|we|you|i|it|this|these|the|our|your|what"
    r"|when|how|why|never|no|always)\b)"
    r"|(?:please )?(?:do ?n[o']t|never) forget\b"
    r"|(?:i'?m |i am |just )?(?:repeating|restating|reiterating|echoing)\b"
    r"|(?:let me|i'?ll|i will) (?:repeat|restate|reiterate|remind|echo"
    r"|say (?:it|this|that) again)\b"
    r"|(?:once |yet )?again[,:]|once more[,:]|one more time[,:]"
    # `same <noun>` only where the noun is elliptical for "as before" — a rule,
    # a point, a thing, a deal are all *the same one we had*. `request` was in
    # this list and is not that idiom: "for each client, same request, and never
    # reuse the connection" describes what is sent, and the guard swallowed the
    # rule beside it. [round 4, Gemini F1]
    r"|same (?:as (?:before|above|last time)|rule|point|thing|deal)\b"
    r"|for the record\b|as a reminder\b|if you recall\b"
    r"|like (?:i|we|you) (?:said|mentioned|noted|asked|agreed|put it)\b"
    r"|you'?(?:ll|ve) (?:recall|remember|heard)\b"
    r")",
    re.I,
)

# Weighing alternatives is the opposite of choosing between them, and it wears
# the same vocabulary — two named approaches in one sentence. The tell is that
# the set is still open: "options", "alternatives", "we could".
_DELIBERATION = re.compile(
    # The set, named as a set.
    r"\b(?:options|alternatives|candidates|choices|possibilities"
    r"|(?:one|another|the other|a third|a better|the best) option"
    r"|under (?:consideration|discussion|debate)|open question"
    r"|to be (?:decided|determined)|up in the air"
    # The comparison frame: any verb of comparing or choosing, an open adverb
    # slot, and the preposition that takes the alternatives as its object. Two
    # verb groups, because they do not share "over": after a verb of pondering it
    # means "about" and the set stays open ("still mulling it over"), but after a
    # verb of choosing it names the loser — "pick Parquet over CSV" is a decision
    # taken, and one list read it as a decision deferred.
    r"|(?:choos|select|pick|decid)\w* (?:\w+ )?(?:between|among|amongst"
    r"|about whether|whether)"
    r"|(?:deliberat|debat|compar|evaluat|assess|mull|agonis|agoniz|wonder|think"
    r"|ponder)\w* (?:\w+ )?(?:between|among|amongst|over|about whether|whether)"
    # Two cases laid side by side and neither taken. Same shape as "on the one
    # hand … on the other", and the guard had the second and not the first.
    # [E5 root cause 4]
    r"|a case (?:for|against) .{1,60}? (?:and|or) a case (?:for|against)"
    r"|torn between|going back and forth|on the (?:one|other) hand"
    r"|weigh(?:s|ing|ed)?|pros and cons|trade-?offs?|upsides? and downsides?"
    r"|(?:we|i) (?:could|might|may want)|(?:we|you|i) can either"
    r"|still (?:deciding|thinking|unsure|undecided|considering|open)"
    r"|(?:a couple|a few|several|two|three) (?:of )?"
    r"(?:ways|options|approaches|routes|choices)"
    r"|would you (?:rather|prefer)|thoughts on"
    r"|which (?:one|option|approach|way|of|do|would|should)"
    r"|what (?:are the (?:options|choices|alternatives)|do you (?:think|prefer)))\b"
    # The bare contrastive comparison, which is a set of two and nothing else.
    r"|\b(?:vs\.?|versus)\b"
    # "either X or Y" — the open disjunction.
    r"|\beither\b[^.;:!?]{1,60}\bor\b",
    re.I,
)

# A quantity and a unit of time — the two halves of a measured distance into the
# past. Written as two classes because a list of whole phrasings is how the
# singular went missing: "years ago" and "a while back" were both admitted and
# "a year ago" fell between them.
_QTY = (
    r"(?:an?|one|two|three|four|five|six|seven|eight|nine|ten|\d+|some|many"
    r"|several|a few|a couple of|a handful of)"
)
_TIME_UNIT = (
    r"(?:(?:second|minute|hour|day|week|month|year|decade|release|sprint"
    r"|quarter|cycle|version|iteration|generation)s?|while|long time|moment)"
)

# Weekday and month names, a closed class. "last Tuesday" and "last March" date
# a clause exactly as "last week" does, and a list of generic units alone
# reaches none of the dated ones.
_CALENDAR = (
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun[e]?|jul[y]?"
    r"|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

# Things a project has earlier versions of. Only used behind a retrospective
# determiner, and enumerated rather than left as `[\w-]+` so that "in the first
# place" — which dates nothing — is not read as a date.
_ARTEFACT = (
    r"(?:versions?|releases?|iterations?|builds?|designs?|drafts?|cuts?"
    r"|revisions?|implementations?|days)"
)

# A report about the past, not a decision taken now. The give-away is a
# retrospective time adverbial: the clause says when it happened, which a
# commitment never needs to.
_RETROSPECTIVE = re.compile(
    r"\b(?:historically|originally|formerly|initially|previously|early on"
    r"|in the past|back then|at first|at one point|once upon a time"
    r"|at the (?:time|outset|start|beginning)|in the (?:beginning|old days)"
    # The punctuation is looked at rather than consumed: the wrapper's trailing
    # `\b` wants a word character, and a clause-final "earlier," has none.
    r"|earlier (?:in|today|this|on|we|i|you|they|the|that|it)|earlier(?=[,;])"
    r"|way back|long ago"
    r"|back in (?:the day|\d{4})|in \d{4}\b"
    r"|used to"
    rf"|last (?:year|month|week|quarter|sprint|time|release|cycle|session"
    rf"|iteration|night|day|{_CALENDAR})"
    # A past-tense copula or auxiliary right after a retrospective determiner is
    # the one tense cue available without a parse: "the old setup *had* no CI".
    r"|the (?:old|previous|original|former|first) [\w-]+ (?:was|were|had|did|used)"
    # A measured distance back. "back" only after a quantity, because "roll the
    # version back" is a direction and not a date.
    rf"|{_TIME_UNIT} ago|{_QTY} {_TIME_UNIT} (?:ago|back)"
    # The past named as a period rather than as a distance: a clause that opens
    # one, a deictic day, or an earlier edition of the thing under discussion.
    r"|back when|when (?:we|i) (?:first|originally|initially|started|began)"
    r"|yesterday|the other (?:day|week|night|morning|afternoon)"
    r"|started (?:out|off)|in the early days"
    r"|(?:up )?until (?:now|recently|last|then)|up to now|till recently"
    r"|since then|ever since|by then|at that time"
    # "before then", "prior to that", "beforehand" are deliberately absent: they
    # locate a clause relative to another time, not in the past, and every one
    # of them has a reading that points forward ("ship it before then").
    # Framing markers that announce the clause as background rather than as an
    # instruction. The frame is the tell, not the tense.
    r"|for (?:context|background|history)|as (?:context|background)"
    r"|fyi|fwiw|just so you know|for what it'?s worth)\b",
    re.I,
)

# The early-stage frame, out of `_RETROSPECTIVE` because it is the one time
# adverbial there that also scopes work not yet done. "In earlier versions we
# had no CI" dates a report; "in the first implementation of the module, never
# use unsafe code" scopes a rule over an implementation nobody has written.
# Same words, opposite direction, so the frame cannot decide on its own and is
# paired with `_PAST_FINITE` in `_decision_kind`. Dropping `the first` from the
# list instead was tried and refused: it makes "in the first version we had no
# CI at all" a directive, which trades one false positive for another.
#
# ponytail: the frame, not the class. `initially`, `at first` and `early on`
# have the same two readings — "initially, never use a cache" is a rule — and
# stay unconditional above, because the tense test costs a suppression on every
# verbless fragment ("back then, no CI") and they are commoner in that shape.
# [round 4, Gemini F4]
_RETRO_STAGE = re.compile(
    r"\bin (?:older|earlier|prior|previous|the original|the first|the early)"
    rf" {_ARTEFACT}\b",
    re.I,
)

# Repairing a mistaken token is not abandoning an approach, and it borrows the
# substitution frame wholesale ("replace `initilize` with `initialize`"). What
# is being replaced is a slip, and the sentence says so. Checked for both roles:
# a user mistypes a flag as readily as the assistant does, and "I meant uv, not
# pip" carries the substitution frame in full.
_REPAIR = re.compile(
    r"\b(?:typos?|mis-?spell\w*|mis-?typ(?:e|ed|ing)|mis-?nam(?:e|ed|ing)"
    r"|mis-?wrote|mis-?worded|mis-?read|spelling|syntax error|typing error"
    r"|off-by-one|autocorrect|fat.finger\w*|transposed"
    r"|correct(?:s|ed|ing)? (?:it|that|this) to"
    r"|fix(?:es|ed|ing)? the (?:typo|spelling|name|wording)"
    r"|(?:i|we) meant|meant to (?:say|write|type|put)"
    r"|(?:that|this|it) should (?:say|read|have (?:been|said|read))"
    r"|wrong (?:word|name|file|flag|spelling|variable|function|argument)"
    r"|my (?:mistake|bad|error|fault)|apologies|oops|whoops"
    r"|(?:please )?(?:ignore|disregard|scratch|strike) "
    r"(?:that|this|my (?:last|previous)|the (?:last|previous)))\b"
    # Announced as a correction, before the corrected token is even given. The
    # punctuation is the frame, so it sits outside the word-boundary group.
    r"|\bcorrection\s*[:!—-]"
    r"|\bs/[\w-]+/[\w-]*/",
    re.I,
)

# A block that is nothing but a question. A question quotes the rule it is
# asking about: "should we avoid raw SQL in the handlers?" proposes one and
# "why do we never run tests?" complains about one, and neither imposes
# anything on the work.
#
# Interrogative *shape*, not the mark alone — a wh-word or an inverted
# auxiliary at the front and the mark at the back, with nothing sentence-final
# in between. A directive with a tag question after it ("use Parquet instead of
# CSV, ok?") has the mark and none of the shape, and a question followed by an
# answer ("why do we never run tests? from now on, never merge without them")
# does not end on one. `do not` is excluded from the openers because it is the
# one that starts an imperative rather than a question. [round 4, Gemini F5]
_QUESTION = re.compile(
    r"\A\s*(?:who|what|when|where|why|how|which|whose|whom"
    r"|do(?! not\b)|does|did|is|are|was|were|am|will|would|shall|should"
    r"|can|could|may|might|must|have|has|had|any(?:one|body)?)\b"
    r"[^.!?]*\?\s*\Z",
    re.I | re.S,
)

# The protasis of a conditional: the circumstance a rule applies in, which is
# not the rule. Stripped rather than suppressed, because both halves are real —
# "if the build fails, never retry more than twice" is a directive, and "if we
# never release the lock, the database hangs" is a consequence — and blanket
# `if`-suppression loses the first to fix the second. What remains after the
# cut is the apodosis, wherever it sits: before the clause, after it, or
# wrapped around it.
#
# The span runs from the subordinator to the next comma or clause end, so the
# cut needs a comma when the protasis is fronted. Without one — "unless you
# have a reason never use raw SQL" — it eats the rule as well, which is the
# ceiling; the upgrade is a clause parser, and chat punctuates.
# [round 4, Gemini F5]
_PROTASIS = re.compile(
    r"\b(?:if|unless|in case|provided that|assuming|as long as|so long as"
    r"|whenever|when)\b[^,.;:!?]*(?:,|(?=[.;:!?]|\Z))",
    re.I,
)

# Independent-clause boundaries, for the one suppressor that is scoped to a
# clause rather than to the block.
#
# Deliberately not "every comma". A comma is as often an aside as a boundary,
# and "I don't think, given the deadline, that we should never use pickle" has
# to stay one clause — split it and the aside turns the sentence into the rule
# it declines. So a boundary is a sentence end, a semicolon, or a comma that is
# followed by a coordinator, which is the shape that actually joins two
# independent clauses in chat.
#
# The lookbehind is why the sentence end needs the space after it: `derive.py`
# and `v1.2` are not two clauses, and `[.!?]\s*` splits both.
#
# ponytail: three coordinators and a full stop, not a clause parser. The ceiling
# is the same one `_PROTASIS` has — an unpunctuated join ("we don't want pickle
# never import it") stays one clause — and the upgrade is the same parser.
# [round 4, Gemini F2]
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|,\s*(?:so|but|and|yet|then)\s+", re.I)

# Negated auxiliaries, as a class rather than as the five that turned up first.
# Written as auxiliary + slot + negator rather than as whole contracted forms,
# because that is the third place the modifier goes ("I'm *really* not sure") and
# a list of surface forms has nowhere to put one.
_NOT = (
    r"(?:(?:do|did|does|ca|could|wo|would|should|have|has|had|was|were|is|are|ai"
    rf"|am|'?m|'?re|'?ve|'?d|'?ll) ?(?:{_ADV} )?n[o']t)"
)

# Verbs of believing, knowing, perceiving and wanting — the ones that take a
# first-person subject and report a mental state rather than impose a rule.
_MIND = (
    r"(?:think|believe|know|knew|see|saw|feel|felt|care|mind|want|wanted|wish"
    r"|remember|recall|follow|understand|get|got|buy|tell|reckon|suppose|guess"
    r"|imagine|expect|agree|worry|bother|notice|reproduce|repro|like|love|hate"
    r"|trust|doubt|fancy|mean|intend|fully (?:get|follow)|quite (?:see|follow))"
)

# First-person epistemic negation — "I don't think that works" — which wears a
# prohibition's words and imposes nothing. Guarded on the subject, because the
# same verb with any other subject ("you don't touch that") is deontic.
#
# Clause-scoped, unlike the other guards here: this one is cut out of the block
# by `_without_opinion` rather than suppressing it. "We still don't want to use
# pickle, so never import it." is a want clause *and* an imperative, and while
# this was block-scoped the want clause suppressed both. Narrowing the class was
# never the repair — it flips "I don't think we need a rule that we never commit
# generated files" into a directive, which is the sentence this exists for.
# [round 4, Gemini F2 — the report blames `still`, which does nothing: the
# sentence scores the same without it. Deferred one round for the clause
# boundaries, which are `_CLAUSE`]
_OPINION = re.compile(
    # The negated-attitude frame, with the adverb slot open on both sides of the
    # auxiliary: "I *honestly* don't think", "I do not *really* think".
    rf"\b(?:i|we)(?: {_ADV})? ?{_NOT}"
    rf" (?:{_ADV} )?{_MIND}\b"
    # The perfect is a report of experience; the bare present is a rule. "We
    # have never used pickle" says what happened, "we never use pickle" orders.
    rf"|\b(?:i|we)(?: {_ADV})?(?:'?ve| have|'?d| had)(?: {_ADV})? never\b"
    # Negated certainty about a proposition, which asserts nothing about it.
    rf"|\b(?:i|we)(?: {_ADV})? ?{_NOT} (?:{_ADV} )?"
    r"(?:sure|certain|convinced|persuaded|sold|clear"
    r"|positive|confident|fussed|bothered|keen)\b"
    # An attitude or knowledge noun under "have no" is a report of not holding
    # one, not a prohibition on holding one.
    rf"|\b(?:i|we)(?: {_ADV})?(?:'?ve| have| had|'?d) no (?:\w+ )?"
    r"(?:view|opinion|preference|objection|idea|clue|issue|problem|feelings?"
    r"|thoughts?|comment|complaint|doubt|memory|recollection|experience"
    r"|visibility|insight|say|stake|context|sense)\b",
    re.I,
)

# The irregular past-tense forms a report about what happened reaches for.
# Shared by the two tense cues below, which want different halves of the past.
_PAST_IRREGULAR = (
    r"was|were|been|had|did|got|saw|ran|went|took|made|came|found|knew"
    r"|thought|said|broke|hit|felt|kept|left|lost|meant|sent|spent|told|wrote"
    r"|built|caught|brought|gave|held|paid|understood|won"
)

# The tense cue `never` is read against. Regulars carry most of it; the handful
# of present-tense verbs English spells with a final `-ed` are excluded by name,
# because "never exceed 100 rows" and "never embed credentials" are rules and a
# bare `\w+ed` deletes both.
#
# ponytail: a list, not a lemmatiser. The ceiling is an irregular nobody wrote
# down — "we never begun" is not English, but "it never bore fruit" would slip
# through — and the upgrade is a tagger, which is a model and is out of scope on
# this path by design. Adding a word to the list is the intended repair.
_PAST = (
    r"(?:(?!(?:need|proceed|exceed|embed|feed|seed|speed|succeed|breed|heed"
    r"|bleed|cede|indeed)\b)\w+ed"
    rf"|{_PAST_IRREGULAR})"
)

# A finite past-tense verb: the tense cue for a whole clause rather than for the
# word after `never`. `_PAST`'s `\w+ed` branch is deliberately not reused —
# across a whole block it matches participial adjectives, and "never use
# deprecated APIs" is a rule with no past clause in it that `deprecated` would
# report as one. `used` is the one regular worth the risk, because "we used X"
# is how half of these clauses are written.
_PAST_FINITE = re.compile(rf"\b(?:{_PAST_IRREGULAR}|used)\b", re.I)

# The rejected side of a constraint: a deontic prohibition. Sufficient on its
# own for a directive — "never touch the vendored tree" governs later work with
# no second clause — which is why the class is kept to unambiguously deontic
# forms. Bare "cannot" is out on purpose: inability and prohibition share the
# word and inability is the commoner one in a transcript.
_PROHIBIT = re.compile(
    # `never` with the tense of the clause it opens looked at, because a report
    # of what did not happen wears the same word as a rule about what may not:
    # "we never had that problem" is four words and was read as a standing
    # prohibition. [round 4]
    #
    # `never mind` is excluded by name and not by tense, because it has none:
    # it is a conversational formula for withdrawing a request, the same class
    # as the "no rush"/"no worries" list two blocks down, and it scored a
    # directive on somebody dropping the subject. [E5 probe E follow-up]
    rf"\b(?:never(?! mind\b)(?!\s+{_PAST}\b)"
    # The exception, and the reason the cue is the *clause's* tense rather than
    # the next word's: a present-tense copula in front makes what follows a
    # passive rule, not a report — "raw SQL is never allowed here". `was`/`were`
    # are deliberately not here, because "raw SQL was never allowed" is the
    # report. `be`/`been`/`being` are not either: they never precede `never`
    # ("has never been merged" puts `been` after it, where it reads as the past
    # tense it is), so listing them would be a lookbehind that cannot fire.
    r"|(?:(?<=\bis )|(?<=\bare ))never"
    # `avoided` is gone with it: "we avoided threads" is the same report in a
    # different verb. The cost is the passive "raw SQL is avoided here", which
    # is a weak rule and rare beside the report. [round 4]
    # The contracted forms are gone from here and `don'?t` comes back at the
    # bottom with a position attached; see the comment there.
    r"|not to|do(?:es)? not|must ?n[o']t|may not"
    r"|shall not|should ?n[o']t|no longer|avoid(?:s|ing)?"
    r"|refrain(?:s|ing)? from|steer clear of|stay away from|keep out of"
    r"|ban(?:s|ned|ning)?|forbid(?:s|den|ding)?|prohibit(?:s|ed|ing)?"
    r"|disallow(?:s|ed|ing)?|rule out|off limits|under no circumstances)\b"
    # Determiner "no" over a noun phrase — "no raw SQL in the handlers", "no
    # network calls in tests". Two exclusions, both for "no" asserting absence
    # rather than forbidding a practice: the existential frame ("there is no
    # X"), in all its tenses, and the light nouns that only ever appear in it or
    # in a conversational formula ("no rush", "no worries"). The inner lookahead
    # requires the whole word, so "no time-based tests" is still a prohibition
    # even though "time" is on the list.
    r"|(?<!there is )(?<!there are )(?<!there was )(?<!there were )(?<!there's )"
    r"(?<!i see )(?<!we see )(?<!makes )(?<!made )(?<!leaves )(?<!left )"
    r"\bno (?!(?:longer|problem|idea|luck|one|matter|doubt|need|more|less|other"
    r"|rush|hurry|harm|point|reason|way|difference|sign|objection|change"
    r"|worries|worry|thanks|offence|offense|pressure|surprise|sweat|stress"
    r"|trouble|joy|dice|big|biggie|clue|comment|complaint|guarantee|word"
    r"|further|such|question|choice|time|issue|view|opinion|preference)"
    r"(?![\w-]))[\w-]+\b"
    # Exclusive permission, which is a prohibition on everyone else.
    r"|\bonly [\w ]{1,30}?(?:may|can|should|is allowed|are allowed)\b"
    # The contracted negated `do`, which needs a position the uncontracted form
    # does not. `do not` and `does not` are the register a rule is written in —
    # "we do not abend on bad input data", "our code reads it and does not set
    # it" — but `don't` and `doesn't` are how a person says something is broken.
    # On the 89 real user prose blocks the contracted form occurs 21 times, and
    # 18 of them are reports: "still doesn't work", "they don't look that bad",
    # "the exported types don't accurately represent the request body". The
    # other three are imperatives and all three are real prohibitions.
    #
    # So the contracted form counts only where it cannot be a report, which is
    # the imperative: nothing in front of it, at the head of a clause, allowing
    # the one coordinator that joins it to the last one. `doesn't` gets no such
    # alternative, because English has no third-person imperative — there is no
    # position that rescues it, which is the same call `cannot` got at the top
    # of this comment and for the same reason.
    #
    # ponytail: clause-initial is approximated by punctuation and a short
    # coordinator, not parsed. The ceiling is an imperative opening a clause
    # this list does not punctuate — "check the lockfile then don't touch it" —
    # and the upgrade is `_CLAUSE`, which splits on sentence ends and so cannot
    # see the boundary either. Adding a coordinator is the intended repair.
    # [E5 probe E follow-up; docs/benchmarks/E5-secondary-set.md]
    r"|(?:^|[.;:!?\n(\[—])\W{0,4}(?:(?:but|and|so|also|please|now|then)\W{1,3})?"
    r"don'?t\b",
    re.I,
)

# The positive standing rule, in the one form that says so in a word. Asserting
# that something *stays* the way it is is asserting a constraint on future work
# — "patch file stays YAML", "deferral records stay in UTC", "part serial
# numbers keep their leading zeros" — which makes it the positive dual of
# `_PROHIBIT` rather than a second flavour of it: one names what may not be
# done, this names what may not be changed.
#
# It is one narrow corner of a wide hole. Probes C, D and E between them miss
# 24 user directives and most of them are positive standing rules with nothing
# lexical to match on at all — "Every task card carries the AMM reference",
# "Dispatch checks run the MEL first" — which is a subject noun phrase and a
# simple-present verb, and telling that from a bug report ("the exported types
# don't accurately represent the request body") wants a parse this module does
# not do. The persistence verbs are the subset that carries the meaning in the
# verb, so they are the subset a regex can have. [E5 root cause 4]
_PERSIST = re.compile(
    # Not `I keep getting build errors`. A subject in front turns the verb
    # aspectual — it reports repetition rather than requiring constancy — and
    # the imperative, which is how half of these rules are written, has none.
    # Same reasoning as the contracted negation in `_PROHIBIT`, and the same
    # shape: position, not vocabulary.
    r"(?<!\bi )(?<!\bwe )(?<!\bit )(?<!\bthey )(?<!\bthis )(?<!\bthat )"
    # Not `keep-alive`, which is a header value and arrives five at a time in
    # pasted HAR files.
    r"\b(?:stays?|remains?|keeps?|keeping)(?![\w-])"
    # Not `good to keep as milestone information`. With no object between the
    # verb and `as`, the frame appraises a thing rather than constraining it;
    # `keep it as YAML` has the object and still counts.
    r"(?!\s+as\b)",
    re.I,
)

# A purpose clause naming what is being kept out. Weaker than a prohibition —
# it is the *reason* for a rule rather than the rule — so it needs the required
# half stated beside it.
_PREVENT = re.compile(r"\b(?:prevent(?:s|ed|ing)?|guard(?:s|ed|ing)? against)\b", re.I)

# Verbs that take an `X over Y` complement in which `over` names the loser.
# Preference is not one verb: ranking one thing above another is a small open
# class and the frame means the same with any member of it. Verbs whose `over`
# is spatial or scopal are deliberately out — "use a lock over the whole map",
# "take the branch over the trunk" — because there the preposition is the same
# word doing a different job, and admitting them buys the preference frame at
# the price of every sentence about coverage.
_PREFER = (
    r"(?:prefer\w*|favou?r\w*|choos\w*|chose|chosen|pick(?:s|ed|ing)?"
    r"|select\w*|opt(?:s|ed|ing)?|prioriti[sz]\w*|privileg\w*|recommend\w*"
    r"|advocat\w*|rank\w*)"
)

# The substitution frame. Two-sided by construction — it cannot be written
# without naming both the thing taken up and the thing put down — which is why
# it is sufficient on its own for a directive.
_CONTRAST = re.compile(
    r"\b(?:instead|rather than|in place of|in lieu of|in favou?r of"
    r"|as opposed to|not \w+,? but)\b"
    # "tabs, not spaces" — the bare contrastive apposition, same frame with the
    # preposition elided.
    r"|,\s*(?:and )?not\b"
    # A preference stated as a comparison: "composition over inheritance".
    #
    # ponytail: the gap of up to 40 characters is what keeps "prefer X strongly
    # over Y" in, and it is also the hole. A preference verb can take an `over`
    # that is nobody's loser once something else intervenes: "we chose to go
    # over the design" and "we recommend running the tests over the network"
    # both score `directive` here, and both are reports. The two proposed
    # repairs were blocklists — of verbs, then of nouns after `over` — and a
    # list that admits the two sentences someone wrote down is the failure mode
    # this class is built against. The upgrade is a complement test on `over`,
    # which needs a parse. Declined twice now, with the sentences kept here so
    # the next reader argues with them rather than with the abstraction.
    # [round 4, Gemini F3 — see docs/reviews/E5-gemini-pair-review-derive.md]
    rf"|\b{_PREFER}\b[^.;:!?]{{1,40}}\bover\b",
    re.I,
)

# The required side: a deontic modal or an imperative that names what to do.
# Deliberately excludes bare "should" and "please", which mark a request for
# one action now rather than a rule that governs later work.
_REQUIRE = re.compile(
    r"\b(?:must|has to|have to|needs? to|needs|ensure(?:s|d)?|make sure|required?"
    r"|shall|always|only|enforce(?:s|d)?|stick to|standardise|standardize)\b",
    re.I,
)

# Taking something up. Broad on purpose: it is never sufficient alone, it only
# supplies the "and this instead" half of a pair.
_ADOPT = re.compile(
    r"\b(?:use|uses|used|using|adopt(?:s|ed|ing)?|leverage(?:s|d|ing)?"
    r"|employ(?:s|ed|ing)?|rely on|relying on|switch(?:es|ed|ing)? to"
    r"|mov(?:e|es|ed|ing) to|go(?:es|ing)? with|went with|opt(?:s|ed|ing)? for"
    r"|prefer(?:s|red|ring)?|reach for|deploy(?:s|ed|ing)?|stick(?:ing)? with"
    r"|appl(?:y|ies|ied|ying)|settle on|serialise to|serialize to)\b",
    re.I,
)

# Putting something down that was already in play. Every member presupposes the
# thing had been taken up — you cannot abandon what you never adopted — which is
# why the pair it forms with `_ADOPT` needs no further evidence of a prior
# commitment in the assistant role. See `_RECANT`.
#
# Two branches were removed after they fired eight times on real sessions and
# never once on an abandonment [E5 fix 2]:
#
#   - `no longer [\w-]+` matched "the TODO comment that's no longer needed",
#     "since we're no longer using X", "no longer depends". `no longer` is
#     resultative: it describes the state *after* a change, which is the
#     retrospective register this module already declines elsewhere, and none
#     of the five was announcing anything.
#   - bare `scratch` matched the directory `scratch/`, and the bare word where
#     a path is being named. The verb takes an object, so it now needs one; the
#     bare "scratch that" it used to cover is in `_PIVOT`.
#
#     This used to name `main.py.oldscratch` as the second example. That token
#     really is in the corpus, and the branch really did fire five times, but
#     never on that: the alternative sits inside `\b(?:…)\b`, and there is no
#     word boundary between `old` and `scratch`. An example that cannot occur,
#     attached to a defect that did, costs the next reader the half hour it
#     took to notice. [E5 fix 2 review, F7]
_ABANDON = re.compile(
    r"\b(?:stop(?:s|ped|ping)? (?:using|with)|stop(?:s|ped|ping)? [\w-]+ing"
    r"|ceas(?:e|es|ed|ing)|quit [\w-]+ing|drop(?:s|ped|ping)?|abandon(?:s|ed|ing)?"
    r"|discard(?:s|ed|ing)?|ditch(?:es|ed|ing)?|scrap(?:s|ped|ping)?"
    # The object is what makes this verb an abandonment, and the list was
    # written from one corpus, so it got that corpus's objects: `them`, `those`
    # and `these` were missing and came back as nothing, and `the` admitted
    # "only scratches the surface", which is a remark about how far the work got
    # rather than a decision to stop. The idiom is excluded by its object, not by
    # dropping the inflections — "he scratched the plan" is a real abandonment in
    # the same tense. [E5 fix 2 review, F6]
    r"|scratch(?:es|ed|ing)? (?:that|these|those|them|the|this|my|our|its?|all)"
    r"(?! surface\b)"
    r"|(?:has|have|had|needs?) to go|must go"
    r"|retir(?:e|es|ed|ing)|shelv(?:e|es|ed|ing)|forget(?:s|ting)? (?:about )?the"
    r"|mov(?:e|es|ed|ing) (?:away from|off|on from)|back(?:s|ed|ing)? out of"
    r"|back away from|walk(?:s|ed|ing)? back|roll(?:s|ed|ing)? back"
    r"|revert(?:s|ed|ing)?|giv(?:e|es|ing) up on|gave up on|get rid of"
    r"|let go of|part ways with|rip(?:s|ped|ping)? out|tear(?:s|ing)? out"
    r"|tore out|throw(?:s|ing)? out|threw out|forgo(?:es|ing)?"
    r"|step(?:s|ped|ping)? away from)\b",
    re.I,
)

# A two-argument replacement verb: "replace X with Y", "swap X for Y". The verb
# carries both halves by itself, so this one is sufficient alone. Abandonment
# verbs that take their replacement as a prepositional argument — "revert to Y",
# "fall back to Y" — are the same shape and live here rather than in `_ABANDON`,
# which is the one-sided class. The bounded gap keeps it inside a clause: across
# a sentence boundary the two arguments are not each other's. "for now" is
# excluded because it is a time adverbial, not the second argument.
_SWITCH = re.compile(
    r"\b(?:replac(?:e|es|ed|ing)|swap(?:s|ped|ping)?|substitut(?:e|es|ed|ing)"
    r"|exchang(?:e|es|ed|ing)|trad(?:e|es|ed|ing)|switch(?:es|ed|ing)?"
    r"|migrat(?:e|es|ed|ing)|revert(?:s|ed|ing)?|fall(?:s|ing)? back|fell back"
    r"|roll(?:s|ed|ing)? back)\b[^.;:!?]{0,80}?\b(?:with|for|to|from)\b(?! now\b)",
    re.I,
)

# A first-person commitment to do something next. Not a signal on its own —
# most of a transcript is the assistant saying what it will do — but it turns a
# bare abandonment into the stop/start pair: "scrap the regex; I'll hand-write
# the tokenizer" names the replacement in a clause with no adoption verb in it.
_COMMIT = re.compile(
    r"\b(?:i'?ll|i will|i am going to|i'm going to|we'?ll|we will|let'?s|let me"
    r"|going forward|from now on|next,? i)\b",
    re.I,
)

# An announced change of course: the speaker says the change has happened.
# Sufficient on its own, because the announcement presupposes both halves — you
# cannot change your mind without there being something to change it from.
# Deliberative verbs ("rethink", "reconsider") are deliberately *not* here: they
# propose thinking again, which is the opposite of having decided.
#
# The cancel marker takes a noun phrase, not only a pronoun. `scratch that` was
# the whole of it and could never fire: `_REPAIR` claims the deictics — `scratch
# that`, `scratch this`, `scratch the last` — and `_REPAIR` is a guard, checked
# first, so the alternative was dead in the F5 sense rather than merely unused.
# What nobody claimed was the nominal form. *"Scratch the SEARCH ALL"* names the
# thing being cancelled instead of pointing at it, which is the version you write
# when the thing is two turns back, and it fell through both. So the alternative
# is the complement of `_REPAIR`'s: a determiner that is not a demonstrative.
# Measured — 726 real blocks across both roles, every probe, both gate splits: no
# verdict moves anywhere except the probe E item that found it and probe A's
# ceiling item, which this lifts. The idiom is refused the same way `_ABANDON`
# refuses it and for the same reason, because one rule about `surface` is enough:
# the inflections cannot reach here at all, and the bare imperative would.
# [E5 probe E, and F6 for the exclusion]
_PIVOT = re.compile(
    r"\b(?:on second thought|second thoughts|change of plan|new plan"
    r"|(?:scratch|strike) (?:the|my|our)(?! surface\b)"
    r"|chang(?:e|ed|ing) (?:my|our) mind|pivot(?:s|ed|ing)?"
    r"|(?:chang|alter|revers|shift)(?:e|es|ed|ing)? (?:our |the )?"
    r"(?:course|direction|approach|tack|plan)"
    r"|course.correct\w*|backtrack\w*|u-turn|start over|on reflection)\b",
    re.I,
)

# Evidence that a position of the speaker's own is being *withdrawn*, as opposed
# to a thing merely being named. Three presupposition triggers, and the
# presupposition is the whole point of the class:
#
#   - a concession presupposes a position that was contested, and grants it;
#   - "a different approach" is a comparative, and a comparative presupposes a
#     salient prior member of the class it compares against;
#   - a verdict that the thing does not work presupposes the thing was being
#     relied on to work.
#
# None is sufficient alone: this class only ever qualifies a frame. First-person
# reference to earlier work is deliberately **not** here, though it was tried:
# "what I wrote", "my original code", "here is a summary of what I did"
# establish that a thing is the speaker's, which is the wrong presupposition. It
# is the register of a *report* on prior work, and admitting it put two
# retrospective summaries back into the output for one extra true reversal.
# Ownership is not withdrawal.
# [E5 fix 2 — see docs/benchmarks/E5-secondary-set.md]
# A blank line, which is what separates one assertion from the next in a chat
# message. Used by the assistant's substitution branch to scope its conjunction;
# see the comment there for why the block is the wrong unit and the sentence is
# too small a one.
_PARAGRAPH = re.compile(r"\n[ \t]*\n")

_RECANT = re.compile(
    r"\b(?:you(?:'re| are| were) (?:absolutely |completely |totally |quite )?right"
    r"|(?:good|great|nice|excellent) catch"
    r"|(?:good|great|fair|excellent) point"
    r"|(?:good|great|excellent|sharp) observation"
    # `my (mistake|bad|error)` was here and no input could reach it: `_REPAIR`
    # lists the same three words plus `fault`, and `_REPAIR` is a guard, so it
    # returns None before this branch is read. Deleted rather than left as
    # documentation — an alternative that cannot fire is a claim about the
    # predicate's coverage that is not true of its behaviour, and the next
    # reader prices it in. The behaviour is pinned by a test that names neither
    # regex. [E5 fix 2 review, F5]
    r"|i was wrong|i apologi[sz]e|i'?m sorry"
    r"|(?:a |an |some )?(?:different|another|alternative|new)"
    r" (?:approach|way|route|strategy|tack|plan|direction)"
    r"|(?:that|this|it)(?:'s| is| was)? (?:just )?(?:not|never) (?:going to |gonna )?work"
    r"|(?:that|this|it) (?:won'?t|doesn'?t|didn'?t|isn'?t going to|wasn'?t going to) work"
    r"|(?:that|this|the [\w-]+) (?:approach|idea|plan)"
    r" (?:fail\w*|doesn'?t work|isn'?t work\w*))\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision, and the block it was read out of.

    `source_ref` is a `Block.block_id` — content-derived, so it names exactly
    one block of exactly one committed transcript. Read it off the block; a
    reconstructed id is a node that cannot be traced back to bytes, which is
    the same fiction rule the rest of this module runs on.
    """

    kind: str  # directive | reversal
    source_ref: str


def _without_opinion(text: str) -> str:
    """`text` with every clause that reports an attitude cut out of it.

    The other six guards suppress the whole block, which is right for them: a
    block that is a question, or a report of what was agreed last week, is not
    partly a decision. An attitude is different, because English joins one to a
    rule with a comma and a coordinator all the time — "we don't want pickle, so
    never import it" — and suppressing the block loses the imperative that the
    attitude is the *reason* for. [round 4, Gemini F2]

    Paragraph by paragraph, and rejoined as paragraphs. `_CLAUSE` splits on the
    whitespace after a sentence end, which swallows the blank line between two
    paragraphs, and a single `" ".join` then handed the caller one long line —
    so the assistant's substitution branch, which scopes its conjunction to the
    paragraph, saw one paragraph where the message had three and kept a
    concession that licensed nothing. Cutting an attitude out of a message is
    not licence to reflow it. [E5 fix 2 review, F3]
    """
    return "\n\n".join(
        " ".join(c for c in _CLAUSE.split(part) if not _OPINION.search(c))
        for part in _PARAGRAPH.split(text)
    )


# Horizontal whitespace, newline excepted. `\S` is negated rather than `[ \t]`
# listed so the class covers the non-breaking space and the other invisible
# things that survive a copy out of a browser. [E5 review round, Gemini F1]
_HSPACE = re.compile(r"[^\S\n]+")

# The zero-width format characters. None of them is whitespace — `\S` matches
# every one — so the class above does not reach them, and a single one in front
# of a block moves the text off `\A`, where four of the seven guards are
# anchored. Soft hyphen and the Mongolian vowel separator are in the range for
# the same reason the rest are: invisible in the client, fatal to an anchor.
#
# Written as escapes and not as the characters themselves, which is not a style
# preference: a class of invisible characters typed invisibly cannot be read, a
# reviewer cannot tell which ones are in it, and a diff that drops one is blank.
_INVISIBLE = re.compile(r"[\xad\u180e\u200b-\u200f\u2060\ufeff]")

# The markup a chat client renders and a rule cannot see: list bullets, the
# blockquote and heading markers, a table pipe, an opening quote, emphasis. Per
# line, because `_BACKREF` admits `\n` as the start of an assertion and then
# expects the cue immediately after it.
_LINE_MARKUP = re.compile(r"^[ \t]*(?:[-*+>#|]+|\d{1,3}[.)]|[\"'“‘])[ \t]*", re.M)


def _flatten(text: str) -> str:
    """`text` as the rules assume it was typed: no formatting layer, one space.

    Every rule in this module is written for prose typed one space at a time,
    and three of them turn out to *depend* on that. `_PERSIST` rejects the
    aspectual reading with fixed-width lookbehinds — Python's `re` allows no
    other kind — so `(?<!\\bi )` checks exactly one space and "I  keep getting
    build errors" walks past it. `_PROHIBIT` admits at most three characters
    between a coordinator and the contraction, so "please    don't push" is
    missed. And `_PARAGRAPH` looks for `\\n[ \\t]*\\n`, which a Windows line
    ending breaks outright: the `\\r` is neither, so a CRLF message is one
    paragraph and the assistant's conjunction scopes over all of it.

    One normalisation instead of three patterns, because it is one defect — the
    rules model a space, the input carries whitespace — and widening each
    pattern to accept runs would leave the fourth instance of it for the next
    reader to find. Applied here and not in `_prose`, because this is the only
    consumer that pattern-matches: `_injected` reads markers the CLI writes, and
    the block text that reaches the graph is the bytes as they were committed.

    Nothing measurable moves. Probes A-E, the 140-turn census, the 61-item
    assistant adjudication and both gate splits are identical either side of it,
    which is the honest statement of what it is worth: no corpus here contains
    the input, and the three defects are real anyway. Found by review, held by
    tests rather than by a board. [E5 review round, Gemini F1-F3]

    There is no `\\r\\n` -> `\\n` here and there was, for one mutation run: the
    `\\r` is horizontal whitespace, so the class below already turns it into the
    space `_PARAGRAPH` admits between two newlines, and the replace changed no
    answer. It scored SURVIVED against the CRLF test that was written for it,
    which is what a redundant line looks like from the outside.

    **Two more of the same defect, found by asking what else is in front of the
    first word.** Four of the seven guards anchor on `\\A` or on a class of
    punctuation, so anything else in that position turns the guard off — and a
    guard that stops firing is a *false positive*, because the block it was
    suppressing is a restatement or a question that now reads as a new rule.
    A zero-width character does it, and so does the bullet in front of "- Per my
    earlier message, the worker must never write to the replica", which comes
    back `directive` today. All 32 items of probe A, B, C, D and E were swept
    under four variations to find these: a leading BOM flips four of them, and
    CRLF, a trailing newline and an indent flip none.

    ponytail: stripping `-` at the head of a line also strips it from the
    removed side of a pasted diff, and `_PROHIBIT` reading pasted machine text
    as a rule is an open item with three blocks left on it. Measured, that costs
    nothing here — the 140-turn census has 14 blocks with a markup line inside
    them and its false-positive count does not move — but it is the place this
    would show up, and the upgrade is to track fenced code rather than to narrow
    the class. Zero of those 140 blocks *begin* with a marker, so the census is
    evidence about the inner lines and silent about the leading one.
    """
    return _HSPACE.sub(" ", _LINE_MARKUP.sub("", _INVISIBLE.sub("", text)))


def _decision_kind(text: str, role: str) -> str | None:
    """The label for one block's text, or None. At most one per block.

    Role is not a shortcut here, it is the definition: a directive is something
    the *user* imposes on the work and a reversal is the *assistant* changing
    what it is doing. The same sentence means different things from the two
    mouths — "store it in Parquet instead of CSV" from the user is a rule to
    obey, from the assistant it is a course change it just made.

    ponytail: so a user who reverses their own earlier instruction, and an
    assistant that records a standing rule for itself, are both missed. The
    upgrade is a third label for "restated by the other party" rather than
    loosening these two, because loosening them makes every polite suggestion a
    directive.
    """
    # Whitespace first, before any pattern reads the text: three of them are
    # written for one space and get a different answer on two. See `_flatten`.
    text = _flatten(text)

    # Six ways of writing a sentence that is *about* a decision without being
    # one, all of which borrow the vocabulary of the thing they describe. The
    # seventh, the reported attitude, is below: it is the one that comes joined
    # to a real rule often enough to be cut out instead of obeyed. They
    # are checked for both roles: a user mistypes a flag and corrects it in the
    # substitution frame exactly as the assistant does, and the correction is a
    # repair either way.
    #
    # `_RETRO_STAGE` is the one that needs a second cue: the frame it matches
    # dates a report and scopes a rule with the same words, and the tense of the
    # block is what tells them apart. See the comment on it.
    if (
        _BACKREF.search(text)
        or _DELIBERATION.search(text)
        or _RETROSPECTIVE.search(text)
        or (_RETRO_STAGE.search(text) and _PAST_FINITE.search(text))
        or _REPAIR.search(text)
        or _QUESTION.search(text)
    ):
        return None

    # The attitude out, whatever it was joined to left behind. The seventh guard,
    # and the only one that cuts rather than suppresses — see `_without_opinion`
    # for why, and `_CLAUSE` for where it cuts. A block that is nothing but
    # attitude comes back empty and is declined here, which is what the block
    # scope used to do for every block that contained one. [round 4, Gemini F2]
    text = _without_opinion(text)
    if not text.strip():
        return None

    # The circumstance out, the rule left behind. After the guards, because a
    # guard scopes the whole sentence it opens — "as we agreed, if the build
    # fails, never retry" is still a restatement — and before the rules, because
    # the cues inside a protasis are not what the block decides.
    text = _PROTASIS.sub(" ", text)

    if role == "user":
        # A substitution frame carries both sides in one phrase; a prohibition
        # is a standing rule by itself ("never touch the vendored tree"); a
        # persistence verb is the same rule stated positively, about a property
        # rather than an act; a purpose clause is only the reason for a rule, so
        # it needs the rule.
        if _CONTRAST.search(text) or _PROHIBIT.search(text) or _PERSIST.search(text):
            return "directive"
        if _PREVENT.search(text) and _REQUIRE.search(text):
            return "directive"
        return None

    if role == "assistant":
        # Two constructions say by themselves that a prior position is being
        # left. The announced change of course lexicalises it — "on second
        # thought", "scratch that", "changing my mind" — and the stop/start
        # pair gets it from `_ABANDON`, every member of which presupposes the
        # thing had been taken up. The second half of the pair is still
        # required: `_ABANDON` alone is a complaint and `_ADOPT` alone is a
        # plan.
        if _PIVOT.search(text):
            return "reversal"
        if _ABANDON.search(text) and (_ADOPT.search(text) or _COMMIT.search(text)):
            return "reversal"
        # The substitution frames are different, and this is the whole of fix 2.
        # In the user role a frame is enough, because "use X instead of Y"
        # governs later work however Y arrived. From the assistant it is not:
        # **writing code is substitution all day long.** Passing a Path instead
        # of a string, replacing one function with another, swapping a loop for
        # a library call — on a coding transcript the substitution frame is the
        # ordinary register of the work, not a marker of a change of course, and
        # a predicate over one block cannot see which. So the block has to carry
        # the evidence itself: something the assistant had adopted is being put
        # down, not merely something being edited.
        #
        # Measured: 52 of 61 emitted nodes on real sessions were narration, and
        # `_CONTRAST` alone was 31 of them. It costs recall on bare substitution
        # — six probe items phrased as course changes with nothing in the
        # sentence to say so — and that trade is priced in
        # `docs/benchmarks/E5-secondary-set.md`. [E5 fix 2]
        #
        # **Scoped to the paragraph**, because a conjunction over a whole block
        # is not a conjunction over an assertion. Both survivors of fix 2 are
        # the same shape: a concession opening the message — "You're absolutely
        # right." — and then, three paragraphs of narration later, an `instead`
        # or a `, not` that belongs to a description of the *bug*, or to a
        # bullet in a summary of what was fixed. Nothing joins the two but the
        # message boundary. A concession licenses the substitution it is
        # *offered with*; at four hundred characters' distance it is licensing
        # somebody else's sentence.
        #
        # Measured on the nine that survived fix 2: the paragraph keeps 7 of 7
        # true and drops 2 of 2 false. The sentence would keep 1 of 7 — a
        # concession is its own sentence far more often than not, which is why
        # this is not scoped there. Splitting on every newline instead scores
        # identically on this evidence, so the choice between them is not
        # measured; the paragraph is the looser of the two and a hard-wrapped
        # line is a formatting artifact rather than a boundary.
        # [E5 fix 2 review, F3]
        #
        # ponytail: `_ABANDON` is not a third option here, and "Good catch.
        # Dropping the retry wrapper." therefore scores nothing. It is a
        # reversal on any reading — the concession names a prior position and
        # the verb puts it down — and the stop/start pair above cannot reach it
        # because nothing replaces the thing. Adding the disjunct was written
        # and measured rather than argued about: probes 23/25/14/14, secondary
        # user side 0/12/2, assistant side 7 of 7 with 54 withdrawn, gate dev
        # 1.0000/0.9319, held-out 1.0000/0.7428 — every board identical to
        # without it, because the construction does not occur in 559 real
        # assistant blocks or in either synthetic split.
        #
        # Declined on that. `_RECANT`'s alternatives are vocabulary inside a
        # construction this corpus does show (seven times); this would be a new
        # construction, and fix 2 itself was bought with 61 adjudicated items.
        # Retaining unobserved vocabulary and adding an unobserved rule are not
        # the same act. One observed instance flips it.
        # [E5 fix 2 review, M11]
        if any(
            _RECANT.search(part) and (_SWITCH.search(part) or _CONTRAST.search(part))
            for part in _PARAGRAPH.split(text)
        ):
            return "reversal"
    return None


def decisions(session: Session) -> list[Decision]:
    """The decisions in a session, in the order they occur.

    Deterministic and local, like everything else on this path: a 4B model
    inventing a rationale nobody wrote is the worst failure a decision record
    can have, so this is regex over prose and the structure of the record.

    Prose only, via `_prose`: a decision anchored on a `tool_result` would point
    at machine output nobody said, and a `tool_use` is the consequence of a
    decision rather than the statement of one.
    """
    out: list[Decision] = []
    for turn, block in _prose(session):
        kind = _decision_kind(block.text, turn.role)
        if kind:
            # `block.block_id` off the block, never rebuilt from its parts: the
            # id is what ties the node to committed bytes, and an id this
            # function computed is an id this function could get wrong.
            out.append(Decision(kind, block.block_id))
    return out


def _renderable(obj):
    """Every string in `obj`, run through `records.safe_text`. See `_write`.

    Keys are left alone: today every key in every artifact is a field name this
    module wrote or a block id, which is hex. ponytail: an artifact that keys a
    dict on transcript text wants them scrubbed too, and then it wants the
    collision check that scrubbing two near-identical keys into one needs.
    """
    if isinstance(obj, str):
        return records.safe_text(obj)
    if isinstance(obj, dict):
        return {k: _renderable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_renderable(v) for v in obj]
    return obj


def _write(path: str, payload: object) -> None:
    """Canonical JSON, published by rename.

    No fsync. `derived/` is rebuildable by definition, so the durability the
    store pays for here would buy nothing: a torn file loses a rebuild, and the
    rebuild is the command that just ran.

    The redaction gate is here rather than only in `ideas()` because this is the
    single door into `derived/`: every artifact this module grows later goes
    through it too, and an artifact that reaches this point carrying a key is a
    bug upstream, not a sentence to quietly drop. [E5:3]

    **Rendering safety is here for the same reason, and only here.** `graph`
    could clean its labels and `ideas` could clean its sentences, and then the
    next artifact would arrive without either. One door, one transform — and
    the transform runs *before* the redaction scan, so the bytes scanned are the
    bytes written, which is the property that makes the scan mean anything. [E7]
    """
    payload = _renderable(payload)
    data = canonical_json(payload)
    # Scanned twice, and the second scan is the one that catches things. The
    # bytes written have to be canonical, and canonical means `ensure_ascii`,
    # and `ensure_ascii` turns every non-ASCII character into a `\uXXXX` escape
    # that ends in a hex digit. A hex digit is a `\w`, so an `e` immediately
    # before a token annihilates the leading `\b` that every high-tier rule
    # anchors on: `token éghp_AAAA…` scans clean as canonical bytes and dirty as
    # UTF-8. The upstream gates at `ideas()` and `graph._label` scan raw, which
    # is why nothing leaks today — but this function's docstring promises to be
    # the single door, and a door that opens for `é` is not one. [E7 carry-in]
    leaks = _leaks(data, os.path.basename(path)) or _leaks(
        json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass"),
        os.path.basename(path),
    )
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
    # Local, because `graph` imports `decisions` from this module and a
    # module-scope import here is a cycle. Only the emitter is used, never
    # `graph.build`, so `derived/` stays buildable without the graphify extra:
    # the artifact is the extraction dict graphify consumes, not a drawing.
    from gitmemory import graph

    _sweep_temps(resolved)
    stats = Stats()
    for stored in store.sessions(resolved):
        out = derived_dir(resolved, stored)
        try:
            session = parse_generation(stored)
            payload_ideas = ideas(session, count=count)
            payload_timeline = timeline(session)
            payload_graph = graph.extraction([session])
            # Inside the try, all three. Outside, a failure on a later write
            # aborted the whole build with no skip entry and left the generation
            # torn: a fresh ideas.json beside a stale timeline.json. [E5:7]
            _write(os.path.join(out, "ideas.json"), payload_ideas)
            _write(os.path.join(out, "timeline.json"), payload_timeline)
            _write(os.path.join(out, "graph.json"), payload_graph)
        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data
            # Leave nothing behind that still asserts facts about a generation
            # this run could not read or could not finish writing. A stale
            # artifact beside a skip line is a `derived/` tree that has quietly
            # stopped being a function of the committed bytes, and `git diff`
            # shows clean while it happens. [E5:2, E5:7]
            #
            # `ignore_errors=True` was doing two jobs and only one of them was
            # wanted. The wanted one: most skips happen before anything is
            # written, and `rmtree` on a path that is not there raises. The
            # other one: when `out` is a symlink, `rmtree` refuses outright —
            # "Cannot call rmtree on a symbolic link" — and the flag swallowed
            # that, so the artifacts stayed on disk while the run reported the
            # generation skipped. The invariant the comment above asserts is
            # exactly the one that failed, and it failed in silence. [E7]
            try:
                shutil.rmtree(out)
            except FileNotFoundError:
                pass  # nothing was written yet, which is the ordinary skip
            except OSError as rm:
                stats.skipped.append(f"{stored.key}: rollback left artifacts behind: {rm!r}")
            stats.skipped.append(f"{stored.key}: {exc!r}")
            continue
        stats.generations += 1
        stats.ideas += len(payload_ideas["ideas"])
        stats.marks += len(payload_timeline["marks"])
        stats.decisions += len(payload_graph["nodes"])
    return stats
