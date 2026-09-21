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
    """`(turn, block)` for every block a human wrote or read, in document order.

    `tool_use` and `tool_result` are machine data — including them makes the
    top-ranked "idea" a JSON payload, measured on the MIT corpus at E2 where
    tool traffic was the bulk of all indexed text.

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
            if block.kind == "text" and block.text.strip():
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

# An adverb/auxiliary slot, for use in the middle of a frame. A fixed two-word
# sequence is a surface form; the same frame with a slot in it is a class, and
# every frame below that can take a modifier is written with one. The absence of
# this slot is what let "as we discussed" through while catching "as discussed".
_MID = (
    r"(?: (?:have|has|had|already|previously|earlier|before|just|also|again"
    r"|clearly|explicitly|repeatedly|specifically|originally|initially|indeed"
    r"|keep|kept|been|all|only|both))*"
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

# Sentence-initial back-reference: the speaker is restating something already
# agreed, which is not a new decision however imperative it sounds. Anchored at
# the start because that is where a discourse connective lives; "as noted" in
# the middle of a clause is doing a different job.
_BACKREF = re.compile(
    r"\s*(?:"
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
    r"|(?:just |simply )?(?:a |an |one |another |the )?"
    r"(?:quick |friendly |gentle |final |small |little |brief |last )*"
    r"remind(?:er|ing)\b"
    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)\b"
    r"|(?:please )?(?:do ?n[o']t|never) forget\b"
    r"|(?:i'?m |i am |just )?(?:repeating|restating|reiterating|echoing)\b"
    r"|(?:let me|i'?ll|i will) (?:repeat|restate|reiterate|remind|echo"
    r"|say (?:it|this|that) again)\b"
    r"|(?:once |yet )?again[,:]|once more[,:]|one more time[,:]"
    r"|same (?:as (?:before|above|last time)|rule|point|thing|request|deal)\b"
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
    # slot, and the preposition that takes the alternatives as its object.
    r"|(?:choos|select|pick|decid|deliberat|debat|compar|evaluat|assess|mull"
    r"|agonis|agoniz|wonder|think)\w* (?:\w+ )?(?:between|among|amongst|over"
    r"|about whether|whether)"
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

# A report about the past, not a decision taken now. The give-away is a
# retrospective time adverbial: the clause says when it happened, which a
# commitment never needs to.
_RETROSPECTIVE = re.compile(
    r"\b(?:historically|originally|formerly|initially|previously|early on"
    r"|in the past|back then|at first|at one point|once upon a time"
    r"|at the (?:time|outset|start|beginning)|in the (?:beginning|old days)"
    r"|earlier (?:in|today|this|on|we|i|you|they|the|that|it)"
    r"|a (?:while|long time|moment|minute) (?:back|ago)|way back|long ago"
    r"|back in (?:the day|\d{4})|in \d{4}\b"
    r"|used to"
    r"|last (?:year|month|week|quarter|sprint|time|release|cycle|session"
    r"|iteration|night|day)"
    # A past-tense copula or auxiliary right after a retrospective determiner is
    # the one tense cue available without a parse: "the old setup *had* no CI".
    r"|the (?:old|previous|original|former|first) [\w-]+ (?:was|were|had|did|used)"
    r"|(?:years|months|weeks|days|hours|releases|sprints|quarters|versions) ago"
    r"|(?:up )?until (?:now|recently|last|then)|up to now|till recently"
    r"|since then|ever since|by then|at that time"
    # Framing markers that announce the clause as background rather than as an
    # instruction. The frame is the tell, not the tense.
    r"|for (?:context|background|history)|as (?:context|background)"
    r"|fyi|fwiw|just so you know|for what it'?s worth)\b",
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

# Negated auxiliaries, as a class rather than as the five that turned up first.
_NOT = (
    r"(?:(?:do|did|does|ca|could|wo|would|should|have|has|had|was|were|is|are|ai)"
    r" ?n[o']t|'?m not|am not|'?re not|are not|'?ve not|'?d not|'?ll not)"
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
_OPINION = re.compile(
    # The negated-attitude frame, with an adverb slot in the middle: "I do not
    # *really* think", "we don't *necessarily* want".
    rf"\b(?:i|we) ?{_NOT}"
    rf" (?:(?:\w+ly|quite|even|much|all that|so|too|just) )?{_MIND}\b"
    # The perfect is a report of experience; the bare present is a rule. "We
    # have never used pickle" says what happened, "we never use pickle" orders.
    r"|\b(?:i|we)(?:'?ve| have|'?d| had) never\b"
    # Negated certainty about a proposition, which asserts nothing about it.
    rf"|\b(?:i|we) ?{_NOT} (?:sure|certain|convinced|persuaded|sold|clear"
    r"|positive|confident|fussed|bothered|keen)\b"
    # An attitude or knowledge noun under "have no" is a report of not holding
    # one, not a prohibition on holding one.
    r"|\b(?:i|we)(?:'?ve| have| had|'?d) no (?:\w+ )?"
    r"(?:view|opinion|preference|objection|idea|clue|issue|problem|feelings?"
    r"|thoughts?|comment|complaint|doubt|memory|recollection|experience"
    r"|visibility|insight|say|stake|context|sense)\b",
    re.I,
)

# The rejected side of a constraint: a deontic prohibition. Sufficient on its
# own for a directive — "never touch the vendored tree" governs later work with
# no second clause — which is why the class is kept to unambiguously deontic
# forms. Bare "cannot" is out on purpose: inability and prohibition share the
# word and inability is the commoner one in a transcript.
_PROHIBIT = re.compile(
    r"\b(?:never|not to|do(?:es)? not|don'?t|doesn'?t|must ?n[o']t|may not"
    r"|shall not|should ?n[o']t|no longer|avoid(?:s|ed|ing)?"
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
    r"|\bonly [\w ]{1,30}?(?:may|can|should|is allowed|are allowed)\b",
    re.I,
)

# A purpose clause naming what is being kept out. Weaker than a prohibition —
# it is the *reason* for a rule rather than the rule — so it needs the required
# half stated beside it.
_PREVENT = re.compile(r"\b(?:prevent(?:s|ed|ing)?|guard(?:s|ed|ing)? against)\b", re.I)

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
    r"|\bprefer\w*\b[^.;:!?]{1,40}\bover\b",
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

# Putting something down that was already in play.
_ABANDON = re.compile(
    r"\b(?:stop(?:s|ped|ping)? (?:using|with)|stop(?:s|ped|ping)? [\w-]+ing"
    r"|ceas(?:e|es|ed|ing)|quit [\w-]+ing|drop(?:s|ped|ping)?|abandon(?:s|ed|ing)?"
    r"|discard(?:s|ed|ing)?|ditch(?:es|ed|ing)?|scrap(?:s|ped|ping)?"
    r"|scratch(?:es|ed|ing)?|(?:has|have|had|needs?) to go|must go"
    r"|retir(?:e|es|ed|ing)|shelv(?:e|es|ed|ing)|forget(?:s|ting)? (?:about )?the"
    r"|mov(?:e|es|ed|ing) (?:away from|off|on from)|back(?:s|ed|ing)? out of"
    r"|back away from|walk(?:s|ed|ing)? back|roll(?:s|ed|ing)? back"
    r"|revert(?:s|ed|ing)?|giv(?:e|es|ing) up on|gave up on|get rid of"
    r"|let go of|part ways with|rip(?:s|ped|ping)? out|tear(?:s|ing)? out"
    r"|tore out|throw(?:s|ing)? out|threw out|forgo(?:es|ing)?"
    r"|step(?:s|ped|ping)? away from|no longer [\w-]+)\b",
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
_PIVOT = re.compile(
    r"\b(?:on second thought|second thoughts|change of plan|new plan"
    r"|scratch that|chang(?:e|ed|ing) (?:my|our) mind|pivot(?:s|ed|ing)?"
    r"|(?:chang|alter|revers|shift)(?:e|es|ed|ing)? (?:our |the )?"
    r"(?:course|direction|approach|tack|plan)"
    r"|course.correct\w*|backtrack\w*|u-turn|start over|on reflection)\b",
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
    # Five ways of writing a sentence that is *about* a decision without being
    # one, all of which borrow the vocabulary of the thing they describe. They
    # are checked for both roles: a user mistypes a flag and corrects it in the
    # substitution frame exactly as the assistant does, and the correction is a
    # repair either way.
    if (
        _BACKREF.match(text)
        or _DELIBERATION.search(text)
        or _RETROSPECTIVE.search(text)
        or _OPINION.search(text)
        or _REPAIR.search(text)
    ):
        return None

    if role == "user":
        # A substitution frame carries both sides in one phrase; a prohibition
        # is a standing rule by itself ("never touch the vendored tree"); a
        # purpose clause is only the reason for a rule, so it needs the rule.
        if _CONTRAST.search(text) or _PROHIBIT.search(text):
            return "directive"
        if _PREVENT.search(text) and _REQUIRE.search(text):
            return "directive"
        return None

    if role == "assistant":
        # Three constructions that carry the whole stop/start pair by
        # themselves: the two-argument replacement verb, the substitution frame,
        # and the announced change of course.
        if _SWITCH.search(text) or _CONTRAST.search(text) or _PIVOT.search(text):
            return "reversal"
        # Otherwise the pair has to be assembled from two clauses: something put
        # down, and something taken up in its place. Neither half counts alone —
        # `_ABANDON` alone is a complaint and `_ADOPT` alone is just a plan.
        if _ABANDON.search(text) and (_ADOPT.search(text) or _COMMIT.search(text)):
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
