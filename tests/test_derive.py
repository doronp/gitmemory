"""E5: derivation — key ideas and the timeline.

The decision extractor has its own gate and its own corpus; these are the tests
for the part that has no gate, because it is a fold rather than a guess.

Two of them are the constraints the extractor has to be born inside, and they
are written before it exists on purpose:

  - **`derived/` rebuilds byte-identically**, in a fresh interpreter, under a
    different hash seed. If it does not, the committed tree churns on every
    rebuild and the diffability that is the whole product is gone.
  - **nothing without a `source_ref`.** Every idea names the block it was read
    out of and every mark names the turn that caused it. An artifact that cannot
    be traced back to a committed block is fiction with a filename.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gitmemory import derive, gitrepo, index, records, store
from gitmemory.__main__ import main

# Distinct vocabularies, so LexRank has something to rank and a test can tell
# which block an idea came out of by reading it.
PROSE = [
    "The append-only store copies bytes out of the transcript and never writes back. "
    "Segments tile the whole file with no hole and no overlap. "
    "Contiguity is arithmetic here, not a heuristic anyone has to trust.",
    "Retrieval is BM25 over the raw bytes, which SQLite ships in the standard library. "
    "A dense arm has to earn its dependency against that baseline. "
    "Nothing is indexed that the store cannot hand back verbatim.",
    "A compaction boundary is an offset the adapter supplies and the manifest carries. "
    "Bytes outrank boundaries whenever the two disagree. "
    "Capturing bytes with no boundaries beats capturing nothing at all.",
]


def write(path: str, lines: list[dict], mode: str = "ab") -> int:
    with open(path, mode) as fh:
        for obj in lines:
            fh.write(json.dumps(obj).encode() + b"\n")
    return os.path.getsize(path)


def user(uid: str, text: str) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "sessionId": "s1",
        "message": {"role": "user", "content": text},
    }


def assistant(uid: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "sessionId": "s1",
        "message": {"role": "assistant", "model": "claude-sonnet-4-5", "content": blocks},
    }


def text(body: str) -> dict:
    return {"type": "text", "text": body}


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "store"
    h.mkdir()
    return str(h)


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src" / "sess.jsonl"
    p.parent.mkdir()
    return str(p)


def stored(home: str, src: str, lines: list[dict], **kw):
    """Write, capture, derive. Returns the one `Stored` generation."""
    write(src, lines)
    store.capture(src, "claude-code", "sess", home=home, **kw)
    derive.build(home)
    return store.sessions(home)[0]


def loaded(home: str, name: str) -> dict:
    hit = list(Path(home, "derived").rglob(name))
    assert len(hit) == 1, f"expected one {name}, found {hit}"
    return json.loads(hit[0].read_bytes())


def conversation() -> list[dict]:
    return [
        user("u1", PROSE[0]),
        assistant("a1", [text(PROSE[1])]),
        user("u2", PROSE[2]),
    ]


def compacted() -> list[dict]:
    """A conversation with a compaction in the middle of it — so, two events.

    The real shape: a `system`/`compact_boundary` line, then the summary flagged
    `isCompactSummary`. The adapter calls the first `compaction` and the second
    `fork`, and a fixture that planted only the summary would be checking a
    shape Claude Code never writes.

    `conversation()` has no events at all, so every timeline it produces is the
    tail and nothing else — which is how the "every mark names its turn" test
    came to iterate an empty list and pass whatever `source_ref` held.
    """
    return [
        user("u1", PROSE[0]),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "b1",
            "sessionId": "s1",
            "parentUuid": None,
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s1",
            "isCompactSummary": True,
            "message": {"role": "user", "content": PROSE[1]},
        },
        user("u3", PROSE[2]),
    ]


# --------------------------------------------------------------------------- #
# nothing without provenance
# --------------------------------------------------------------------------- #


def test_every_idea_names_a_block_that_exists_in_the_session(home, src):
    """The floor the whole epoch rests on.

    A key sentence whose `source_ref` does not resolve is a sentence this
    product asserts and cannot show you, which is the failure mode §2.6 says the
    gate exists to prevent. Checked against the real block ids, not against
    "looks like a hash": a sixty-four-character string is easy to produce and
    proves nothing.
    """
    gen = stored(home, src, conversation())
    session = index.parse_generation(gen)
    real = {b.block_id for t in session.turns for b in t.blocks}

    out = loaded(home, "ideas.json")
    assert out["ideas"], "no ideas derived from three paragraphs of prose"
    for idea in out["ideas"]:
        assert idea["source_ref"] in real, idea


def test_an_idea_is_attributed_to_the_block_it_was_read_out_of(home, src):
    """Not merely *a* real block — the right one.

    The test above passes if every sentence is attributed to the first block in
    the session, so the fixture here is built around the one input that can tell
    the two apart: **the same sentence said twice, in different blocks.** sumy's
    `Sentence` compares by text, so `texts.index(...)` returns the first
    occurrence for both copies and the second block is never cited. The cursor
    walk in `ideas` is what makes the second citation land.

    Without the repetition this test is decorative, which is exactly the shape
    the E4 vacuity audit was about.
    """
    echo = "Bytes outrank boundaries whenever the two disagree."
    lines = [
        user("u1", f"{PROSE[0]} {echo}"),
        assistant("a1", [text(f"{PROSE[1]} {echo}")]),
    ]
    gen = stored(home, src, lines)
    session = index.parse_generation(gen)
    by_id = {b.block_id: b.text for t in session.turns for b in t.blocks}

    ideas = loaded(home, "ideas.json")["ideas"]
    for idea in ideas:
        assert idea["text"] in by_id[idea["source_ref"]], idea

    cited = {i["source_ref"] for i in ideas if i["text"] == echo}
    assert len(cited) == 2, f"both copies of {echo!r} should be cited, got {cited}"


def test_every_timeline_mark_names_the_turn_that_caused_it(home, src):
    """The fixture has to contain events or this test iterates nothing.

    It did, for one commit: `conversation()` produces no events, so the loop
    below ran zero times and the assertion inside it was never evaluated —
    setting `source_ref` to the empty string for every mark changed nothing and
    no test noticed. A negative control caught it before it was committed, which
    is the entire argument for running them. [E5]
    """
    gen = stored(home, src, compacted())
    session = index.parse_generation(gen)
    real = {t.turn_id for t in session.turns}

    marks = loaded(home, "timeline.json")["marks"]
    anchored = [m for m in marks if m["kind"] != "tail"]
    assert len(anchored) == 2, f"the fixture is meant to have two events: {marks}"
    for mark in anchored:
        assert mark["source_ref"] in real, mark


def test_the_timeline_accounts_for_every_turn(home, src):
    """A fold that drops turns is a fold that lies about the span between marks.

    The tail exists for this: without it the turns after the last event are in
    no span at all, and nothing in the file says how many were left over.
    """
    gen = stored(home, src, conversation())
    session = index.parse_generation(gen)

    out = loaded(home, "timeline.json")
    assert out["turns"] == len(session.turns)
    assert sum(m["turns_since"] for m in out["marks"]) == len(session.turns)
    assert sum(m["blocks_since"] for m in out["marks"]) == sum(len(t.blocks) for t in session.turns)


def test_a_compaction_splits_the_timeline_where_the_boundary_is(home, src):
    """The mark has to sit between the turns it separates, not merely exist.

    Counting turns before the event's byte offset is the only part of this fold
    that can be wrong in a way an eyeball misses, so it is checked by arithmetic
    rather than by the mark's presence.
    """
    stored(home, src, compacted())

    marks = loaded(home, "timeline.json")["marks"]
    assert [m["kind"] for m in marks] == ["compaction", "fork", "tail"], marks
    assert marks[0]["turns_since"] == 1, "only the first turn precedes the boundary line"
    assert marks[1]["turns_since"] == 1, "the boundary line is itself a turn"
    assert marks[2]["turns_since"] == 2, "the summary and the turn after it"


# --------------------------------------------------------------------------- #
# determinism — the property `derived/` exists to have
# --------------------------------------------------------------------------- #


def test_deriving_twice_changes_nothing_in_git(home, src):
    """Build, commit, build again, `git diff --exit-code`.

    The check every derived artifact in this project has to survive, run through
    git rather than through a Python comparison because git is what stores it
    and a tree object is what churns.
    """
    gitrepo.init(home)
    stored(home, src, conversation())
    assert gitrepo.commit(home, "derived") is not None

    derive.build(home)
    proc = subprocess.run(
        ["git", "-C", home, "diff", "--exit-code", "--", "derived/"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout


def test_derived_bytes_are_identical_in_a_fresh_interpreter(home, src, tmp_path):
    """Two processes, two hash seeds, byte-for-byte.

    An in-process rebuild shares every interned string, every dict order and
    every module-level cache with the build it is compared against — this
    project has already shipped one determinism test that compared an object
    with itself. Set-iteration order is the specific hazard: it varies with
    `PYTHONHASHSEED` across processes and not at all within one.
    """
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)

    runs = []
    for seed in ("0", "12345"):
        out = tmp_path / f"out-{seed}"
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;from gitmemory import derive;derive.build(sys.argv[1])",
                home,
            ],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"},
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            capture_output=True,
        )
        out.mkdir()
        for path in sorted(Path(home, "derived").rglob("*.json")):
            (out / path.name).write_bytes(path.read_bytes())
        runs.append({p.name: p.read_bytes() for p in sorted(out.iterdir())})

    assert runs[0] == runs[1]
    assert runs[0], "derived nothing, so the comparison was vacuous"


def test_derived_is_committed_rather_than_ignored(home):
    """`derived/` is diffable and rebuildable; `index/` is only rebuildable.

    The determinism test above is worth nothing if the directory it diffs is not
    in the repository, and `GITIGNORE` is a string that has grown a line in
    every epoch so far.

    It grew one for `derived/` at E5:8 — the `.deriving-*` temp — so "the word
    `derived` does not appear in .gitignore" stopped being the right question
    and was replaced by asking git itself, one path at a time. That is the
    stronger check anyway: a pattern can mention `derived` and ignore nothing,
    or mention nothing and ignore everything.
    """
    gitrepo.init(home)
    assert "/index/" in Path(home, ".gitignore").read_text(), "checking the wrong file"

    def ignored(rel: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", home, "check-ignore", "-q", "--no-index", rel],
                check=False,
            ).returncode
            == 0
        )

    gen = "derived/claude-code/sess/g00"
    assert not ignored(f"{gen}/ideas.json"), "derived/ is the diffable layer"
    assert not ignored(f"{gen}/timeline.json"), "derived/ is the diffable layer"
    assert not ignored("derived/"), "the whole tree must reach a commit"
    assert ignored(f"{gen}/.deriving-abc123.json"), "a half-written artifact was committable"
    assert ignored("index/bm25.sqlite3"), "the control: index/ is the ignored layer"


# --------------------------------------------------------------------------- #
# what does and does not become an idea
# --------------------------------------------------------------------------- #


def test_a_tool_payload_never_becomes_a_key_idea(home, src):
    """Machine data outranks prose on any term-frequency measure, because it
    repeats itself. Measured at E2: image blocks alone were 61.7% of all indexed
    text before the index started eliding them.
    """
    payload = "column_a column_b column_a column_b " * 40
    lines = [
        user("u1", PROSE[0]),
        assistant(
            "a1",
            [
                text(PROSE[1]),
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": payload}},
            ],
        ),
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": payload}],
            },
        },
    ]
    stored(home, src, lines)

    for idea in loaded(home, "ideas.json")["ideas"]:
        assert "column_a" not in idea["text"], idea


def test_ideas_come_back_in_the_order_they_were_said(home, src):
    """Ranked by LexRank, presented in document order — that is how they read.

    `rank` is the presentation index, so it is also the assertion that the list
    was not shuffled on the way out.
    """
    gen = stored(home, src, conversation())
    session = index.parse_generation(gen)
    order = {
        b.block_id: i for i, (t, b) in enumerate((t, b) for t in session.turns for b in t.blocks)
    }

    ideas = loaded(home, "ideas.json")["ideas"]
    positions = [order[i["source_ref"]] for i in ideas]
    assert positions == sorted(positions), ideas
    assert [i["rank"] for i in ideas] == list(range(len(ideas)))


def test_a_session_past_the_sentence_cap_says_so(home, src, monkeypatch):
    """The cap is a real ceiling and a silent one would be a lie in a file.

    LexRank is O(n^2) in sentences, so there has to be a cap; `sentences_seen`
    against `sentences_ranked` is what makes hitting it visible to a reader of
    the artifact rather than only to whoever wrote the constant.
    """
    monkeypatch.setattr(derive, "MAX_SENTENCES", 4)
    stored(home, src, conversation())

    out = loaded(home, "ideas.json")
    assert out["sentences_ranked"] == 4
    assert out["sentences_seen"] == 9, "three paragraphs of three sentences each"


def test_a_session_with_no_prose_still_gets_its_artifacts_written(home, src):
    """A transcript of nothing but tool traffic is an ordinary transcript.

    The claim is that it gets `ideas.json` like any other generation, holding an
    empty list — not that it is skipped, and not that it is an error. Skipping
    it is the plausible wrong implementation (why write an empty file?) and it is
    wrong because a reader then cannot tell "derived nothing" from "not derived
    yet", which is the distinction the whole store exists to keep.

    The `if owners:` guard in `ideas` is not what holds this up: LexRank returns
    an empty tuple for an empty document, so removing the guard changes nothing
    observable. The guard is there to skip loading 580 stop words per tool-only
    generation, and that is all it is there for.
    """
    lines = [
        assistant("a1", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"c": "ls"}}]),
    ]
    stored(home, src, lines)

    out = loaded(home, "ideas.json")
    assert out["ideas"] == []
    assert out["sentences_seen"] == 0


# --------------------------------------------------------------------------- #
# failure is per generation, never per store
# --------------------------------------------------------------------------- #


def test_one_unparseable_generation_does_not_cost_the_others(home, src, tmp_path):
    """Same rule as `index.build` and `verify`, and for the same reason: the
    store preserves bytes no parser can love, and a reader that gave up on the
    whole tree when it met one would make that preservation worthless.
    """
    stored(home, src, conversation())
    other = tmp_path / "src" / "two.jsonl"
    write(str(other), conversation())
    store.capture(str(other), "claude-code", "two", home=home)

    # Break the parse of exactly one generation, at the manifest's agent field,
    # so the segments are untouched and the store still verifies.
    manifest = Path(store.sessions(home)[0].manifest)
    man = json.loads(manifest.read_bytes())
    man["agent"] = "no-such-agent"
    manifest.write_bytes(json.dumps(man).encode())

    stats = derive.build(home)
    assert stats.generations == 1
    assert len(stats.skipped) == 1
    assert "no-such-agent" in stats.skipped[0]


def test_a_skipped_generation_is_reported_on_stderr(home, src, capsys):
    stored(home, src, conversation())
    manifest = Path(store.sessions(home)[0].manifest)
    man = json.loads(manifest.read_bytes())
    man["agent"] = "no-such-agent"
    manifest.write_bytes(json.dumps(man).encode())

    assert main(["--home", home, "derive"]) == 0
    assert "skipped" in capsys.readouterr().err


def test_cli_derive_refuses_a_path_that_is_not_a_store(tmp_path, capsys):
    """`0 generation(s)` on a typo'd path reads as a clean run over an empty
    store. The floor `verify` and `index` already have. [E4, review: CLI 3]
    """
    absent = str(tmp_path / "nope")
    assert main(["--home", absent, "derive"]) == 2
    assert "not a store" in capsys.readouterr().err
    assert not os.path.exists(absent)


def test_derived_mirrors_the_manifest_path_not_the_manifest_contents(home, src):
    """`agent` and `session_id` come out of untrusted JSON; the manifest's own
    location was vetted by the store's path guard when it was written. Deriving
    the output directory from the field rather than the path is how a hostile
    manifest writes outside `derived/`.
    """
    gen = stored(home, src, conversation())
    man = json.loads(Path(gen.manifest).read_bytes())
    man["session_id"] = "../../escaped"
    man["agent"] = "../.."
    Path(gen.manifest).write_bytes(json.dumps(man).encode())

    out = derive.derived_dir(home, store.sessions(home)[0])
    assert os.path.commonpath([os.path.realpath(out), os.path.realpath(home)]) == os.path.realpath(
        home
    )
    assert out.endswith(os.path.join("derived", "claude-code", "sess", "g00"))


# --------------------------------------------------------------------------- #
# E5 standalone review: the fix list, each fix pinned by its own mutant
#
# Everything below answers a numbered finding in
# docs/reviews/E5-derive-standalone-review.md. Finding 1 is the reason the rest
# of this section exists at all: fourteen behaviours could be deleted from
# derive.py with the whole suite still green, including two of the module's
# three self-declared rules.
# --------------------------------------------------------------------------- #


def test_the_caps_are_the_values_their_comment_was_measured_against(home, src):
    """The cap tests monkeypatch the constants, so nothing else pins the numbers.

    Without this, `MAX_WORDS = 1_000_000_000` passes every test in the file —
    the same self-defeating shape the E4 elision-cap test had, where deriving
    the fixture size from the constant made the test move with the mutant.
    """
    assert derive.MAX_SENTENCES == 2000, "the O(n^2) matrix ceiling was measured at 2000"
    assert derive.MAX_WORDS == 50_000, "the O(U*n*L) idf ceiling was measured against 50k words"


# --- finding 1: `count` reaches the summariser, and reaches it from the CLI --- #


def test_ideas_returns_the_number_asked_for(home, src):
    gen = stored(home, src, conversation())
    session = index.parse_generation(gen)
    assert len(derive.ideas(session, count=2)["ideas"]) == 2
    assert len(derive.ideas(session, count=5)["ideas"]) == 5
    assert derive.DEFAULT_IDEAS != 2, "the fixture has to differ from the default to prove it"


def test_the_ideas_flag_reaches_the_artifact(home, src):
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)
    assert main(["--home", home, "derive", "--ideas", "2"]) == 0
    assert len(loaded(home, "ideas.json")["ideas"]) == 2


def test_a_negative_ideas_count_yields_no_ideas_rather_than_almost_all(home, src):
    """sumy's `ItemsCount` is `sequence[:count]`, so `--ideas -1` meant "every
    sentence but the last" — 8 of 9 sentences instead of the 8 best. [E5:6]
    """
    gen = stored(home, src, conversation())
    session = index.parse_generation(gen)
    assert derive.ideas(session, count=0)["ideas"] == []
    assert derive.ideas(session, count=-1)["ideas"] == []
    assert derive.ideas(session, count=-3)["ideas"] == []
    assert derive.ideas(session, count=1)["ideas"], "the fixture ranks nothing; test is vacuous"


# --- finding 1: rule 3, rebuildable and diffable --- #


def test_artifacts_are_canonical_json_on_disk(home, src):
    """Rule 3 of the module docstring, which nothing pinned: swap the serializer
    for `json.dumps(indent=2, sort_keys=False)` and 800 tests stayed green.
    """
    from gitmemory.records import canonical_json

    stored(home, src, compacted())
    for name in ("ideas.json", "timeline.json", "graph.json"):
        hit = list(Path(home, "derived").rglob(name))
        raw = hit[0].read_bytes()
        assert raw == canonical_json(json.loads(raw)), f"{name} is not canonical bytes"
        assert b"\n" not in raw, "pretty-printed: a rebuild would churn the diff"
        raw.decode("ascii")  # ensure_ascii, or the bytes are locale-dependent


def test_every_generation_is_derived_not_only_the_newest(home, src):
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)
    # A rewrite, not an append: an append extends g00, a divergence opens g01.
    write(src, [user("u9", PROSE[1]), assistant("a9", [text(PROSE[2])])], mode="wb")
    store.capture(src, "claude-code", "sess", home=home)

    stats = derive.build(home)
    assert stats.generations == 2, "a second capture made a second generation"
    gens = sorted(p.name for p in Path(home, "derived", "claude-code", "sess").iterdir())
    assert gens == ["g00", "g01"]
    for g in gens:
        assert Path(home, "derived", "claude-code", "sess", g, "ideas.json").exists()


def test_the_counts_stats_reports_are_the_counts_on_disk(home, src):
    write(src, compacted())
    store.capture(src, "claude-code", "sess", home=home)
    stats = derive.build(home)
    assert stats.ideas == len(loaded(home, "ideas.json")["ideas"]) > 0
    assert stats.marks == len(loaded(home, "timeline.json")["marks"]) > 1


# --- finding 1: what a mark is made of --- #


def test_a_mark_carries_the_offset_and_the_id_of_the_event_it_stands_for(home, src):
    gen = stored(home, src, compacted())
    session = index.parse_generation(gen)
    marks = derive.timeline(session)["marks"]
    real = [m for m in marks if m["kind"] != "tail"]
    assert len(real) == len(session.events) == 2, "the fixture lost its events"
    for mark, event in zip(real, sorted(session.events, key=lambda e: e.byte_offset), strict=True):
        assert mark["byte_offset"] == event.byte_offset > 0, "offset zeroed or unset"
        assert mark["event_id"] == event.event_id != "", "the mark cannot be traced back"


def _turn(seq: int, offset: int, blocks: int = 1):
    from gitmemory.records import Block, Turn

    t = Turn(session_id="s1", seq=seq, role="user", byte_offset=offset, byte_len=10)
    t.blocks = [Block(turn_id="t", seq=i, kind="text", text="x") for i in range(blocks)]
    return t


def _event(seq: int, offset: int):
    from gitmemory.records import Event

    return Event(session_id="s1", seq=seq, kind="compaction", byte_offset=offset, anchor="a")


def test_the_timeline_sorts_its_own_inputs_by_bytes(home):
    """Handed turns and events in the wrong order — which no adapter does today,
    which is exactly why nothing caught `sorted(...)` being deleted.
    """
    from gitmemory.records import Session

    s = Session(session_id="s1", agent="x", source_path="/dev/null")
    s.turns = [_turn(2, 200), _turn(0, 0), _turn(1, 100)]
    s.events = [_event(1, 250), _event(0, 150)]

    marks = derive.timeline(s)["marks"]
    assert [m["byte_offset"] for m in marks] == [150, 250, -1], "marks not in byte order"
    assert [m["turns_since"] for m in marks] == [2, 1, 0], "turns not in byte order"
    assert sum(m["turns_since"] for m in marks) == 3, "the fold lost a turn"


def test_the_tail_is_emitted_even_when_there_is_nothing_after_the_last_event(home):
    from gitmemory.records import Session

    s = Session(session_id="s1", agent="x", source_path="/dev/null")
    s.turns = [_turn(0, 0)]
    s.events = [_event(0, 100)]
    marks = derive.timeline(s)["marks"]
    assert marks[-1]["kind"] == "tail", "summing turns_since stops equalling turns"
    assert marks[-1]["turns_since"] == 0 and marks[-1]["blocks_since"] == 0


# --- finding 1: what becomes an idea, and who it is attributed to --- #


def test_thinking_is_not_admitted_to_the_prose_stream(home, src):
    """`_prose`'s ponytail comment says thinking is excluded on purpose. Deleting
    the exclusion changed no test, so the comment was the only thing enforcing it.
    """
    secret = (
        "Zebra mandolin quarantine parsnip. "
        "Obelisk trombone kumquat harpsichord. "
        "Marzipan xylophone tessellate wombat."
    )
    gen = stored(
        home,
        src,
        [
            user("u1", PROSE[0]),
            assistant("a1", [{"type": "thinking", "thinking": secret}, text(PROSE[1])]),
        ],
    )
    session = index.parse_generation(gen)
    assert any(b.kind == "thinking" for t in session.turns for b in t.blocks), "no thinking block"
    payload = derive.ideas(session, count=20)
    assert "Zebra" not in json.dumps(payload), "a thinking block reached the artifact"


def test_an_adjacent_duplicate_sentence_is_attributed_to_its_own_block(home, src):
    """The `cursor += 1` at derive.py's match, which nothing pinned.

    The committed attribution test separates its two copies by three sentences,
    so the cursor is already past the first by the time the second matches. The
    shape that needs it is the *adjacent* duplicate across a block boundary:
    without the advance both copies are attributed to the earlier block, which
    is an idea naming a block it did not come from — a rule-2 violation.
    """
    echo = "The manifest is the only writeable file in the store."
    gen = stored(
        home,
        src,
        [
            user("u1", "Segments tile the transcript with no hole and no overlap. " + echo),
            assistant("a1", [text(echo + " Retrieval is BM25 over the raw bytes.")]),
        ],
    )
    session = index.parse_generation(gen)
    blocks = [b for t in session.turns for b in t.blocks if b.kind == "text"]
    assert len(blocks) == 2, "the fixture is not two blocks"

    picked = derive.ideas(session, count=4)["ideas"]
    owners = [i["source_ref"] for i in picked if i["text"] == echo]
    assert len(owners) == 2, f"both copies must be ranked for this to mean anything: {picked}"
    assert owners == [blocks[0].block_id, blocks[1].block_id], "both copies blamed on one block"


# --- finding 3: redaction at the derived boundary --- #


def test_a_key_quoted_in_prose_never_reaches_derived(home, src):
    """DESIGN.md names `derived/` as a redaction boundary; `derive` did not
    import `redact` at all, so a key in prose was ranked, written, and committed
    a second time. [E5:3]
    """
    key = "AKIAZZZZQQQQWWWW1234"  # synthetic, right shape
    gen = stored(
        home,
        src,
        [
            user("u1", PROSE[0]),
            assistant("a1", [text(f"The deploy failed because the key {key} was rejected.")]),
        ],
    )
    session = index.parse_generation(gen)
    payload = derive.ideas(session, count=20)
    assert payload["sentences_redacted"] == 1, "the sentence under test was never scanned"
    assert key not in json.dumps(payload), "the key was ranked as an idea"

    blob = b"".join(p.read_bytes() for p in Path(home, "derived").rglob("*.json"))
    assert key.encode() not in blob, "the key reached the committed artifact tree"
    raw = b"".join(p.read_bytes() for p in Path(home, "raw").rglob("*.jsonl"))
    assert key.encode() in raw, "raw/ is the unredacted copy; the push gate is what holds it"


def test_the_write_door_refuses_a_secret_no_matter_who_built_the_payload(home):
    """The gate is at `_write`, not only in `ideas()`, because every artifact
    this module grows later goes through the same door.
    """
    target = os.path.join(home, "derived", "claude-code", "sess", "g00", "ideas.json")
    with pytest.raises(ValueError, match="refusing to write a secret"):
        derive._write(target, {"ideas": [{"text": "ghp_" + "A" * 36}]})
    assert not os.path.exists(target)
    derive._write(target, {"ideas": []})  # the control: a clean payload still writes
    assert os.path.exists(target)


@pytest.mark.parametrize("before", ["é", "漢", "🙂"], ids=["latin1", "cjk", "astral"])
def test_the_write_door_does_not_open_for_a_non_ascii_character(home, before):
    """One `é` used to be enough to walk a token past the last gate.

    The bytes written must be canonical, canonical means `ensure_ascii`, and
    `ensure_ascii` renders every non-ASCII character as `\\uXXXX` — which ends
    in a hex digit. A hex digit is a `\\w`, so it annihilates the leading `\\b`
    that every high-tier rule anchors on, and `token éghp_AAAA…` scanned clean
    as canonical bytes while scanning dirty as UTF-8.

    Nothing leaked, because `ideas()` and `graph._label` scan raw text upstream.
    But this function's docstring promises to be *the single door* that any
    artifact added later can rely on, and a door that opens for `é` is not one.
    The ASCII case passing is not evidence: it passed throughout. [E7 carry-in]
    """
    from gitmemory.records import canonical_json

    target = os.path.join(home, "derived", "claude-code", "sess", "g00", "ideas.json")
    payload = {"ideas": [{"text": f"token {before}ghp_" + "A" * 36}]}
    # The precondition is the whole finding: the canonical bytes are clean.
    assert not derive._leaks(canonical_json(payload)), "precondition: the escape hides it"
    with pytest.raises(ValueError, match="refusing to write a secret"):
        derive._write(target, payload)
    assert not os.path.exists(target)


# --- finding 4: a missing extra is an environment fault, not per-generation --- #


def test_a_missing_derive_extra_fails_the_build_instead_of_skipping_everything(
    home, src, monkeypatch
):
    """Raised inside the per-generation `try`, "this install cannot derive
    anything" became N skip lines and an exit code of 0. [E5:4]
    """
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)

    def no_extra():
        raise RuntimeError("derivation needs the `derive` extra: pip install -e '.[derive]'")

    monkeypatch.setattr(derive, "_sumy", no_extra)
    with pytest.raises(RuntimeError, match="derive. extra"):
        derive.build(home)


# --- finding 5: the cap that binds on the axis that costs --- #


def test_a_terminator_free_block_is_capped_by_words_not_by_sentences(home, src, monkeypatch):
    """A block with no `.`/`!`/`?` is one sentence however long it is, so the
    sentence cap never fired on the input that drives `_compute_idf` superlinear:
    256 000 words in one sentence measured 133 s with `sentences_seen == 1`.
    """
    monkeypatch.setattr(derive, "MAX_WORDS", 20)
    gen = stored(home, src, [user("u1", " ".join(f"w{i}" for i in range(100)))])
    session = index.parse_generation(gen)
    payload = derive.ideas(session, count=5)
    assert payload["sentences_seen"] == 1, "the fixture grew a terminator"
    assert payload["sentences_ranked"] == 0, "the word cap did not bind"
    assert payload["words_ranked"] <= 20


def test_the_word_cap_binds_part_way_through_a_document(home, src, monkeypatch):
    monkeypatch.setattr(derive, "MAX_WORDS", 12)
    body = " ".join(f"alpha{i} beta{i} gamma{i} delta{i} epsilon{i}." for i in range(4))
    gen = stored(home, src, [user("u1", body)])
    payload = derive.ideas(index.parse_generation(gen), count=5)
    assert payload["sentences_seen"] == 4, "the fixture is not four sentences"
    assert payload["sentences_ranked"] == 2, "the cap let through the wrong number"
    assert payload["words_ranked"] == 10


# --- finding 7 and 2: a failure costs its own generation and nothing else --- #


@pytest.mark.parametrize("artifact", ["ideas.json", "timeline.json", "graph.json"])
def test_a_write_failure_costs_one_generation_and_leaves_nothing_torn(
    home, src, tmp_path, monkeypatch, artifact
):
    """Both `_write` calls sat outside the `try`: a failure on the second aborted
    the whole build with no skip entry, and left a fresh ideas.json beside a
    stale timeline.json. [E5:7]

    Parametrised over all three artifacts once the graph joined them. Pinning
    one name pins the position it happened to hold, and the interesting position
    is the last one: a failure there leaves two good files, which is the torn
    generation that looks most like a whole one.
    """
    other = tmp_path / "src" / "other.jsonl"
    write(src, conversation())
    write(str(other), conversation())
    store.capture(src, "claude-code", "sess-a", home=home)
    store.capture(str(other), "claude-code", "sess-b", home=home)

    real = derive._write

    def fail_on_the_second_write(path, payload):
        if "sess-a" in path and path.endswith(artifact):
            raise OSError(28, "No space left on device")
        return real(path, payload)

    monkeypatch.setattr(derive, "_write", fail_on_the_second_write)
    stats = derive.build(home)

    assert stats.generations == 1, "the failure took the other generation down with it"
    assert len(stats.skipped) == 1 and "sess-a" in stats.skipped[0], stats.skipped
    torn = Path(home, "derived", "claude-code", "sess-a", "g00")
    assert not torn.exists(), (
        f"torn generation left behind: {sorted(p.name for p in torn.iterdir())}"
    )
    assert Path(home, "derived", "claude-code", "sess-b", "g00", "timeline.json").exists()


def test_a_generation_that_stops_parsing_loses_its_stale_artifacts(home, src):
    """`derived/` stopped being a function of the committed bytes: the skip line
    went to stderr and the old artifacts stayed on disk asserting facts about a
    generation `derive` had just said it could not read. [E5:2]
    """
    gen = stored(home, src, conversation())
    out = Path(derive.derived_dir(home, gen))
    assert (out / "ideas.json").exists(), "nothing was derived, so nothing can go stale"

    man = json.loads(Path(gen.manifest).read_bytes())
    man["agent"] = "no-such-agent"
    Path(gen.manifest).write_bytes(json.dumps(man).encode())

    stats = derive.build(home)
    assert stats.generations == 0 and len(stats.skipped) == 1, stats
    assert not out.exists(), "stale artifacts survived a run that could not read the generation"


def test_a_symlinked_derived_leaf_is_written_through_not_followed(home, src, tmp_path):
    """`store._mkdir` tested with `os.path.isdir`, which follows symlinks, so a
    leaf that was a symlink was "already a directory" and all three artifacts
    went to the link's target — outside `$GITMEMORY_HOME`, with nothing skipped
    and nothing reported. The leaf name is predictable from the store's own
    contents and does not exist before the first derive, so any process running
    as the user can plant one and wait. [E7]"""
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)
    victim = tmp_path / "victim"
    victim.mkdir()
    out = Path(derive.derived_dir(home, next(iter(store.sessions(home)))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.symlink_to(victim)

    stats = derive.build(home)
    assert sorted(p.name for p in victim.iterdir()) == [], "the artifacts left the store"
    assert stats.generations == 0
    assert any("symbolic link" in s for s in stats.skipped), stats.skipped


def test_a_rollback_that_cannot_run_says_so(home, src, tmp_path):
    """`ignore_errors=True` was doing two jobs. The wanted one: most skips happen
    before anything is written and `rmtree` on an absent path raises. The other:
    `rmtree` refuses a symlink outright, and the flag swallowed that — so stale
    artifacts stayed on disk while the run reported the generation skipped,
    which is the exact invariant the comment above the call asserts. [E7]"""
    write(src, conversation())
    store.capture(src, "claude-code", "sess", home=home)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "ideas.json").write_text('{"stale": true}')
    out = Path(derive.derived_dir(home, next(iter(store.sessions(home)))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.symlink_to(victim)

    stats = derive.build(home)
    assert (victim / "ideas.json").exists(), "precondition: the rollback cannot clear this"
    assert any("rollback left artifacts behind" in s for s in stats.skipped), stats.skipped


# --- E7 index F5/F7: what the artifacts may carry --- #


def _artifact_strings(home: str) -> dict[str, str]:
    """Every string in every derived artifact, keyed by artifact name."""
    out = {}
    for path in sorted(Path(home, "derived").rglob("*.json")):
        out[path.name] = json.dumps(json.loads(path.read_bytes()), ensure_ascii=False)
    return out


def test_a_non_utf8_byte_in_a_transcript_does_not_reach_the_artifacts(home, src):
    """A lone surrogate in `derived/` is a file our own consumers cannot re-encode.

    The capture path keeps non-UTF-8 bytes on purpose — `jsonl` decodes with
    `surrogateescape` so offsets stay true — and the index neutralises them at
    its door with `_encodable`. `derived/` had no equivalent, so `\\xff\\xfe` in
    a tool result reached `graph.json` and `ideas.json` as `\\udcff\\udcfe`. The
    files are valid ASCII (canonical JSON escapes them), so nothing fails at
    write time; the break lands on whoever loads the JSON and re-encodes it. [E7]
    """
    bad = b"Never use \xff\xfe pickle for untrusted input, decided we switch to json."
    write(
        src,
        [
            user("u1", bad.decode("utf-8", "surrogateescape")),
            assistant("a1", [text(PROSE[1])]),
            user("u2", PROSE[2]),
        ],
    )
    store.capture(src, "claude-code", "sess", home=home)
    assert derive.build(home).skipped == []

    for name, blob in _artifact_strings(home).items():
        assert not any(0xD800 <= ord(c) <= 0xDFFF for c in blob), f"{name} carries a surrogate"
        blob.encode("utf-8")  # what a strict consumer does, and what used to raise


def test_a_terminal_escape_in_a_transcript_does_not_reach_the_artifacts(home, src):
    """`derived/` is rendered by a second tool, so it gets the terminal's rules.

    `graph._label` collapsed whitespace and `\\s` matches none of these, so an
    ANSI erase-line, a bidi override, NUL, DEL and a zero-width space all went
    into the committed artifacts verbatim. `ideas.json` never passed through
    `_label` at all. Both are fixed at the one door they share. [E7]
    """
    hostile = "We decided \x1b[2K to ‮esrever‬ the order \x00 and drop\x7f the ​cache."
    write(src, [user("u1", PROSE[0]), assistant("a1", [text(hostile)]), user("u2", PROSE[2])])
    store.capture(src, "claude-code", "sess", home=home)
    assert derive.build(home).skipped == []

    strings = _artifact_strings(home)
    assert any("esrever" in blob for blob in strings.values()), "the hostile block was dropped"
    for name, blob in strings.items():
        for ch in "\x1b‮‬\x00\x7f​":
            assert ch not in blob, f"{name} carries U+{ord(ch):04X}"
    # Spelled out, not deleted: a reader can still see what was there.
    assert any("\\u202e" in blob for blob in strings.values()), strings


# --- finding 8: the temp file, swept and ignored --- #


def test_a_half_written_artifact_is_swept_by_the_next_build(home, src):
    stored(home, src, conversation())
    out = Path(home, "derived", "claude-code", "sess", "g00")
    litter = out / ".deriving-killed.json"
    litter.write_text("{")
    derive.build(home)
    assert not litter.exists(), "a SIGKILL'd write stays until someone commits it"
    assert (out / "ideas.json").exists(), "the sweep took the artifacts with it"


def test_a_failed_publish_leaves_neither_a_partial_artifact_nor_a_temp(home, monkeypatch):
    """Pins both halves of `_write`: publish by rename, and unlink on failure.

    The first version asserted `not target.exists()` against a fresh directory,
    and the mutation index reported it MISSED: writing straight to the target
    and then unlinking on failure leaves exactly the empty directory a clean
    rename-publish leaves, so the assertion passed under both. It was the unlink
    half wearing the rename half's name.

    What only rename buys is that an artifact already published survives a
    rebuild that dies part way: `O_TRUNC` destroys it before the first byte of
    the replacement is written, and the failure path then unlinks what is left.
    So publish once, fail the second publish, and read the first one back.
    """
    target = Path(home, "derived", "claude-code", "sess", "g00", "ideas.json")
    derive._write(str(target), {"ideas": [{"text": "the published one"}]})
    published = target.read_bytes()

    def boom(src_path, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        derive._write(str(target), {"ideas": [{"text": "the replacement"}]})
    assert target.exists(), "written in place: the published artifact was destroyed"
    assert target.read_bytes() == published, "a reader can see a half-built artifact"
    assert not list(target.parent.glob(".deriving-*")), "the temp outlived its failure"


# --- finding 9: derived/ inherits the store's permission stance --- #


def test_derived_directories_are_owner_only(home, src):
    """0755 under the default umask, 0777 under `umask 0` — and a world-writable
    directory lets a local user swap `ideas.json` for their own, which is then
    committed. `store._mkdir` has said 0700 since E3. [E5:9]
    """
    stored(home, src, conversation())
    made = Path(home, "derived", "claude-code", "sess", "g00")
    for d in (made, made.parent, made.parent.parent, made.parent.parent.parent):
        assert d.stat().st_mode & 0o777 == 0o700, f"{d.name} is {oct(d.stat().st_mode & 0o777)}"
    assert (made / "ideas.json").stat().st_mode & 0o777 == 0o600


# --- finding 10: prose with nothing to rank --- #


def test_prose_with_no_rankable_word_ranks_nothing_and_warns_about_nothing(home, src):
    """LexRank divides by a zero norm when every sentence is content-free, so a
    turn of `... !!! ???` printed `RuntimeWarning: invalid value encountered in
    divide` and wrote NaN-ranked selections. Under `PYTHONWARNINGS=error` the
    same store reported the generation as skipped instead. [E5:10]
    """
    import warnings

    gen = stored(home, src, [user("u1", "... !!! ??? ... !!!")])
    session = index.parse_generation(gen)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        payload = derive.ideas(session, count=5)
    assert [str(w.message) for w in caught] == [], "the warning still escapes"
    assert payload["ideas"] == [], "NaN ratings were ranked and written"
    assert payload["sentences_seen"] == 5, "the fixture never reached the summariser"


def test_one_content_word_is_enough_to_rank_the_rest(home, src):
    """The guard is per document, not per sentence: a mixed document must still
    rank its content-free sentences rather than quietly dropping them.
    """
    gen = stored(home, src, [user("u1", "!!! The append-only store never writes back. ???")])
    payload = derive.ideas(index.parse_generation(gen), count=3)
    assert payload["sentences_ranked"] == 3, "content-free sentences were dropped, not ranked"
    assert len(payload["ideas"]) == 3


def test_two_sentences_sharing_no_vocabulary_rank_nothing_and_warn_about_nothing(home, src):
    """The degenerate case is wider than "no content words", which is how the
    first fix for finding 10 was caught being wrong by its own sibling test.

    sumy's idf is `log(n / (1 + df))`, so with two sentences and no shared term
    every idf is exactly `log(1) == 0`: every tf-idf vector is zero, every
    cosine is zero, the matrix is zero, and `power_method` divides by a zero
    norm — with a content word in every sentence. The condition is arithmetic,
    not vocabulary, which is why the guard asks numpy instead of guessing.
    """
    import warnings

    gen = stored(home, src, [user("u1", "Segments tile the transcript. Retrieval ranks bytes.")])
    session = index.parse_generation(gen)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        payload = derive.ideas(session, count=2)
    assert [str(w.message) for w in caught] == [], "the warning still escapes"
    assert payload["sentences_ranked"] == 2, "the fixture never reached the summariser"
    assert payload["ideas"] == [], "NaN-ranked selections were written as ideas"


# --------------------------------------------------------------------------- #
# decisions
#
# Every fixture below is hand-written. None of it is copied out of
# `bench/fixture-dev`: a test whose input came from the corpus the extractor was
# tuned against tests the tuning, and would go green on an extractor that had
# memorised a hundred templates and understood nothing.
# --------------------------------------------------------------------------- #


def parsed(home, src, lines):
    """The `Session` for a hand-written transcript."""
    return index.parse_generation(stored(home, src, lines))


def labelled(session) -> list[tuple[str, str]]:
    """Each decision as `(kind, the text it was read out of)`.

    Resolving the id back to text is the point: a test that asserted only on
    `kind` would pass for an extractor that found the right number of decisions
    in the wrong blocks.
    """
    by_id = {b.block_id: b.text for t in session.turns for b in t.blocks}
    return [(d.kind, by_id[d.source_ref]) for d in derive.decisions(session)]


def test_a_user_constraint_is_a_directive(home, src):
    """The first of the two labels. A standing rule the user imposes on the work
    — both halves named, what to do and what not to do — is the thing a decision
    record exists to carry forward into the next session.
    """
    rule = "Keep every timestamp in UTC; no local time anywhere in the tree."
    session = parsed(home, src, [user("u1", rule)])
    assert labelled(session) == [("directive", rule)]


def test_an_assistant_changing_course_is_a_reversal(home, src):
    """The second label, and the one that decays fastest out of context: six
    turns later the transcript still contains the abandoned approach, and a
    reader who does not know it was dropped will reintroduce it.

    This fixture used to read "I'll replace the polling loop with an inotify
    watch." and it was the wrong sentence. Replacing one thing with another is
    what writing code *is*; nothing in that sentence says the polling loop was
    ever the assistant's idea rather than the code it was handed. Three
    adjudicators reading the same shape on real sessions called it narration
    every time. The sentence below abandons something instead of substituting
    it, which is the shape the label is actually for. See
    `test_a_bare_substitution_from_the_assistant_is_not_a_reversal` and
    `docs/benchmarks/E5-secondary-set.md`.
    """
    turn = "Giving up on the polling loop; I'll use an inotify watch."
    session = parsed(
        home, src, [user("u1", "Why is the watcher so slow?"), assistant("a1", [text(turn)])]
    )
    assert labelled(session) == [("reversal", turn)]


def test_ordinary_conversation_yields_no_decisions(home, src):
    """Almost every block of a real transcript is neither label. An extractor
    that fires on plain narration produces a decision log nobody can read,
    which is worse than an empty one because it looks complete.
    """
    session = parsed(
        home,
        src,
        [
            user("u1", "Can you run the tests and tell me what fails?"),
            assistant("a1", [text("The suite passes; I'll paste the diff below.")]),
            user("u2", "Nice, that reads much better now."),
        ],
    )
    assert derive.decisions(session) == []


def test_retrying_a_failed_command_is_not_a_reversal(home, src):
    """Repeating something is not abandoning it, and the post-failure turn is
    where that confusion is most expensive: every failure in a transcript is
    followed by an assistant turn, so an extractor that reads "the command
    failed, running it again" as a course change reports one reversal per
    failure and its precision collapses on exactly the slice the gate watches.
    """
    lines = [
        user("u1", "Run the migration."),
        assistant(
            "a1",
            [
                text("Running the migration now."),
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "migrate"}},
            ],
        ),
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "connection refused; Exit code 1",
                    }
                ],
            },
        },
        assistant("a2", [text("That failed because the daemon was down. Let me run it again.")]),
    ]
    assert derive.decisions(parsed(home, src, lines)) == []


def test_an_empty_session_has_no_decisions(home, src):
    """A transcript with nothing in it is the shape a fresh capture has, and the
    daemon derives on every capture — so this runs on the happy path before the
    user has typed anything.

    Deliberately asked *after* a session that does have decisions in it: an
    accumulator that outlives the call answers the empty session with the
    previous one's nodes, and a test that only ever saw the empty case would
    read that as correct.
    """
    populated = parsed(home, src, [user("u1", "Use the vendored parser, never the system one.")])
    assert len(derive.decisions(populated)) == 1
    assert derive.decisions(records.Session("s1", "claude-code", "/nowhere.jsonl")) == []


def test_the_same_sentence_twice_yields_two_distinct_source_refs(home, src):
    """A decision is an event in a transcript, not a string, and the same rule
    restated in a later turn is a second event that has to be citable on its
    own. De-duplicating on text would silently drop it, and the strict
    one-to-one scorer would then mark the survivor's twin a miss.
    """
    rule = "Use the vendored parser, never the system one."
    session = parsed(home, src, [user("u1", rule), user("u2", rule)])
    out = derive.decisions(session)
    assert [d.kind for d in out] == ["directive", "directive"]
    assert out[0].source_ref != out[1].source_ref, "the two statements collapsed into one node"


def test_every_decision_names_a_block_that_exists_in_the_session(home, src):
    """The same floor the ideas path stands on: a node whose `source_ref` does
    not resolve is an assertion this product cannot show you. Checked against
    the real ids rather than against "looks like a hash", because a
    sixty-four-character string is easy to produce and proves nothing.
    """
    lines = [
        user("u1", "Write it to Parquet rather than CSV."),
        assistant(
            "a1",
            [
                text("I'll drop the CSV writer and use pyarrow instead."),
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "rm -rf csv/ # use parquet instead of csv"},
                },
            ],
        ),
    ]
    session = parsed(home, src, lines)
    real = {b.block_id for t in session.turns for b in t.blocks}
    prose = {b.block_id for t in session.turns for b in t.blocks if b.kind == "text"}
    out = derive.decisions(session)
    assert len(out) == 2
    for d in out:
        assert d.source_ref in real, "a decision names a block that is not in the session"
        assert d.source_ref in prose, "a decision was anchored on machine output"


def test_decisions_come_back_in_the_order_they_occur(home, src):
    """A decision log is read top to bottom, and the last word on a subject is
    the one that stands. Out of order, a reversal can appear to precede the
    approach it reverses.

    Asserted on the texts, and on a sequence of labels that is not a palindrome:
    with two directives around one reversal, reversing the list is invisible,
    and the first version of this test was caught being exactly that by its own
    negative control.
    """
    first = "Configuration comes from the environment, not from a dotfile."
    second = "Also: never log the raw token."
    third = "On second thought, one table beats the three I sketched."
    session = parsed(
        home,
        src,
        [
            user("u1", first),
            assistant("a1", [text("Understood. Reading the loader now.")]),
            user("u2", second),
            assistant("a2", [text(third)]),
        ],
    )
    assert labelled(session) == [
        ("directive", first),
        ("directive", second),
        ("reversal", third),
    ]


def test_the_same_sentence_is_labelled_by_who_said_it(home, src):
    """The two labels are defined by speaker, not by wording: from the user
    "drop the CSV writer" is a rule to obey, from the assistant it is a course
    change already taken. Collapsing them loses the distinction the per-slice
    breakdown is built on.
    """
    line = "Drop the CSV writer and use Parquet instead."
    session = parsed(home, src, [user("u1", line), assistant("a1", [text(line)])])
    assert [k for k, _ in labelled(session)] == ["directive", "reversal"]


def test_a_bare_substitution_from_the_assistant_is_not_a_reversal(home, src):
    """The asymmetry is sharper than the test above shows, and it is fix 2.

    "Store it in Parquet instead of CSV" is a directive from the user — it
    governs later work however CSV got there — and from the assistant it is
    *nothing*, because a substitution frame does not say the thing being put
    down was ever the assistant's own position. Writing code is substitution
    all day long. On real sessions this one shape was 31 of 52 wrong nodes.
    """
    line = "Store it in Parquet instead of CSV."
    session = parsed(home, src, [user("u1", line), assistant("a1", [text(line)])])
    assert [k for k, _ in labelled(session)] == ["directive"]


def test_restating_an_agreed_rule_is_not_a_new_decision(home, src):
    """A back-reference is the commonest distractor in a long session and it
    quotes the rule verbatim, so anything keying on the rule's own words fires
    on it. The tell is the discourse connective in front, not the rule behind.
    """
    session = parsed(home, src, [user("u1", "As we agreed, never touch the vendored tree.")])
    assert derive.decisions(session) == []


def test_weighing_two_approaches_is_not_choosing_between_them(home, src):
    """Deliberation names both candidates in one sentence, which is the exact
    shape of a reversal, and it is what an assistant does immediately before
    every real decision. Reading it as the decision dates the record one turn
    early and attributes it to a sentence that committed to nothing.
    """
    session = parsed(
        home,
        src,
        [assistant("a1", [text("The options are a queue or a semaphore instead of the lock.")])],
    )
    assert derive.decisions(session) == []


def test_narrating_an_old_switch_is_not_making_one(home, src):
    """ "We moved off CSV last year" is a report, and it carries the substitution
    frame in full. Without the retrospective guard the extractor mines project
    history for decisions and files them under today's session.
    """
    session = parsed(
        home,
        src,
        [assistant("a1", [text("Historically we replaced the CSV reader with a parser.")])],
    )
    assert derive.decisions(session) == []


# --------------------------------------------------------------------------- #
# the guards, as classes
#
# Each test below gives a guard several members of its class — a different
# saying-verb, an adverb dropped into the middle of the frame, the polite form
# of the same request — and one bare control that *is* a decision. The control
# is load-bearing: without it a guard that suppressed everything would pass, and
# the assertion on the whole list is what makes each suppressed line a claim
# rather than a hope.
#
# The members were written before the regexes were widened to admit them. A test
# that only fails for the one phrasing its rule was built around is a test a
# memoriser passes, which is the failure these exist to catch.
# --------------------------------------------------------------------------- #


def test_a_typo_correction_from_the_user_is_not_a_directive(home, src):
    """A repair borrows the substitution frame whole — "I meant uv, not pip" —
    and the user mistypes as readily as the assistant does. Guarding repairs on
    one role leaves the other side unguarded, and the user side is where every
    directive in the corpus is scored.
    """
    control = "Never commit the generated files."
    session = parsed(
        home,
        src,
        [
            user("u1", "Sorry, typo — never `pip`, always `uv`."),
            user("u2", "Correction: no raw SQL in the handlers."),
            user("u3", "My mistake — never the system parser, always the vendored one."),
            user("u4", "Ignore my last message, I mistyped: no local time anywhere."),
            user("u5", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_citation_keeps_its_shape_when_words_are_added(home, src):
    """A citation is a frame with open positions — "as <someone> <had already>
    <said>" — not a fixed pair of words. Any verb of saying fills the last slot
    and any adverb fills the middle one, so a rule keyed on the two-word form
    reads every longer citation as a fresh instruction.
    """
    control = "No secrets in the repository."
    session = parsed(
        home,
        src,
        [
            user("u1", "As we have already discussed, never commit secrets."),
            user("u2", "As you kindly flagged, no raw SQL in the handlers."),
            user("u3", "As I explicitly spelled out, avoid global mutable state."),
            user("u4", "As they specified, only the scheduler may write to that table."),
            user("u5", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_being_asked_to_remember_a_rule_is_not_a_new_rule(home, src):
    """The same back-reference in its polite and imperative clothes. "Please
    remember", "don't forget", "just a reminder" all do the job "as we agreed"
    does, and they are commoner in a long session because the speaker is
    exasperated by then.
    """
    control = "Never force-push to main."
    session = parsed(
        home,
        src,
        [
            user("u1", "Please remember, no secrets in the repository."),
            user("u2", "Don't forget: never edit the generated files."),
            user("u3", "Just a friendly reminder: never force-push to main."),
            user("u4", "Bear in mind that logs must never carry a token."),
            user("u5", "For the record, we never ship on a Friday."),
            user("u6", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_an_unsettled_comparison_is_not_a_decision(home, src):
    """Deliberation is any verb of comparing over any preposition that takes the
    alternatives, plus the bare disjunctions — "either ... or", "X versus Y",
    "up in the air". The set being still open is the whole tell, and it survives
    the choice of verb.
    """
    control = "Forbid raw SQL in the handlers."
    session = parsed(
        home,
        src,
        [
            user("u1", "I'm still deciding whether to forbid raw SQL."),
            user("u2", "Comparing between a queue and a lock, no strong leaning."),
            user("u3", "Either we forbid the cache, or we allow it everywhere."),
            user("u4", "Postgres versus SQLite; no ORM either way."),
            user("u5", "That one is still up in the air, so do not build on it."),
            user("u6", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_background_framing_is_not_an_instruction(home, src):
    """A clause announced as background — "for context", "FYI", "back in 2019",
    "since then" — reports how things were. The frame marks it, and it marks it
    even when the tense of the clause behind is ambiguous, which is why the
    frame is what the guard keys on.

    The two framed clauses are prohibitions, and that is the [E7 pair review]
    half of this test. They were *"the old build never ran the tests"* and
    *"the previous team never wrote tests"* — reports of the past that the
    extractor does not read as decisions in the first place, so deleting the
    `for context|fyi|fwiw` branch this test is named for left it green. A guard
    can only be tested on an input it is the only thing stopping.
    """
    control = "No CI-less merges from here on."
    session = parsed(
        home,
        src,
        [
            user("u1", "For context, we don't vendor dependencies."),
            user("u2", "FYI, never rebase a shared branch."),
            user("u3", "Back in 2019 we had no CI whatsoever."),
            user("u4", "Since then we never rebuilt the index."),
            user("u5", "Early on we decided not to use an ORM."),
            user("u6", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_hedged_opinion_is_still_an_opinion(home, src):
    """ "I don't think so" imposes nothing, and neither does "I don't *really*
    think so" or "I don't buy it". The adverb in the middle and the choice of
    attitude verb are free; the first-person negation is the class.
    """
    control = "Don't add a cache here."
    session = parsed(
        home,
        src,
        [
            user("u1", "I don't really think that is the right call."),
            user("u2", "We don't necessarily want it that way."),
            user("u3", "I don't buy the argument that it is slower."),
            user("u4", "I don't follow why that would matter."),
            user("u5", "I'm not convinced we should forbid the cache."),
            user("u6", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_an_attitude_joined_to_a_rule_suppresses_only_itself(home, src):
    """The reason to give a rule is usually an attitude, and English joins the
    two with a comma and a coordinator: *"we don't want another dependency, so
    never add one without asking"*. While `_OPINION` suppressed the whole block
    the reason cancelled the rule, and the longer the message the more it cost —
    one "I'm not sure" anywhere in it and nothing in it was read at all.

    The three that must still be declined are the point of the pairing. A
    boundary is a sentence end, a semicolon, or a comma *followed by a
    coordinator*; a bare comma is an aside, and an aside inside an attitude
    ("I don't think, given the deadline, that we should never use pickle") is
    not a second clause. Nor is a subordinate `that`-clause, which is where the
    sentence this guard exists for keeps its rule-shaped words.
    [round 4, Gemini F2]
    """
    kept = [
        "We don't want another dependency, so never add one without asking.",
        "I'm not sure it matters; never commit generated files anyway.",
    ]
    session = parsed(
        home,
        src,
        [
            user("u1", "I don't think we need a rule that we never commit generated files."),
            user("u2", "I don't think, given the deadline, that we should never use pickle."),
            user("u3", "We are not at all sure we should never rewrite this."),
            *[user(f"k{i}", text) for i, text in enumerate(kept)],
        ],
    )
    assert labelled(session) == [("directive", text) for text in kept]


def test_reporting_never_having_seen_it_is_not_forbidding_it(home, src):
    """The sharpest pair in the file: "we have never used pickle" and "we never
    use pickle" differ by an auxiliary, and one is a report of what happened
    while the other is a rule about what may happen. Without the tense cue the
    extractor turns every recollection into a prohibition.
    """
    control = "We never commit secrets."
    session = parsed(
        home,
        src,
        [
            user("u1", "I have never seen that particular failure."),
            user("u2", "We've never needed that option."),
            user("u3", "I'd never used pyarrow before this week."),
            user("u4", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


# --------------------------------------------------------------------------- #
# round 4 — the pair review
#
# Five defects, four of them reported by the reviewer and one found while
# reproducing its examples. The fifth is the largest: "We never had that
# problem." is four words, has none of the reviewer's `earlier` in it, and was
# read as a standing prohibition. The gate scored 1.0000 before and after every
# one of these; `bench/probes.py` is what moved. `docs/reviews/` has the record,
# including what was declined.
# --------------------------------------------------------------------------- #


def test_a_report_of_what_never_happened_is_not_a_rule(home, src):
    """ "We never had that problem" wears the same word as "we never commit
    secrets" and means the opposite kind of thing: one says what did not happen,
    the other says what may not. The cue is the tense of the clause `never`
    opens, which is also why `avoided` had to go — "we avoided threads" is the
    same report in a different verb.

    The two controls are the exceptions that make the cue a tense test rather
    than a word list. A present-tense copula in front turns the participle into
    a passive rule, and English spells a handful of present-tense verbs with a
    final `-ed`, so a bare `\\w+ed` would delete "never exceed 100 rows" too.
    """
    passive = "Raw SQL is never allowed in the handlers."
    regular = "Never exceed 100 rows per page."
    session = parsed(
        home,
        src,
        [
            user("u1", "We never had that problem."),
            user("u2", "That code path never ran in production."),
            user("u3", "The cache never worked on Windows."),
            user("u4", "We avoided threads entirely."),
            user("u5", "Raw SQL was never allowed in the handlers."),
            user("u6", passive),
            user("u7", regular),
        ],
    )
    assert labelled(session) == [("directive", passive), ("directive", regular)]


def test_an_early_stage_dates_a_report_and_scopes_a_rule(home, src):
    """ "In the first implementation" points both ways. Behind a report it dates
    it; in front of an imperative it scopes a rule over an implementation nobody
    has written yet. The frame cannot tell them apart, so the finite past verb
    in the clause does.

    Deleting "the first" from the frame instead was tried and refused: it makes
    the first line below a directive, which trades one false positive for
    another.
    """
    control = "In the first implementation of the module, never use unsafe code."
    session = parsed(
        home,
        src,
        [
            user("u1", "In the first version we had no CI at all."),
            user("u2", "In the first implementation there was no retry logic."),
            user("u3", "In earlier versions we used cron instead of a systemd timer."),
            user("u4", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_question_about_a_rule_is_not_the_rule(home, src):
    """A question quotes the rule it asks about. "Should we avoid raw SQL?"
    proposes one and "why do we never run tests?" complains about one, and
    neither imposes anything — but both carry the prohibition whole, so the
    extractor read the question mark as emphasis.

    Interrogative shape, not the mark: the two controls end in one and are
    directives anyway. A tag question does not invert an auxiliary to the front,
    and "do not" opens the only imperative that looks like it does.
    """
    tag = "Use Parquet instead of CSV, ok?"
    imperative = "Do not use pip here — clear?"
    session = parsed(
        home,
        src,
        [
            user("u1", "Should we avoid raw SQL in the handlers?"),
            user("u2", "Why do we never run tests?"),
            user("u3", "Is there any reason we still use pip?"),
            user("u4", "What about the rule that we never commit generated files?"),
            user("u5", tag),
            user("u6", imperative),
        ],
    )
    assert labelled(session) == [("directive", tag), ("directive", imperative)]


def test_a_condition_is_not_the_rule_it_carries(home, src):
    """Both halves of a conditional are real, which is why the protasis is cut
    out rather than used to suppress the block. "If we never release the lock,
    the database hangs" states a consequence and was read as a rule; "if the
    build fails, never retry more than twice" *is* a rule, and suppressing every
    `if` to fix the first loses it.

    The three controls put the rule on each side of the cut — after the clause,
    before it, and wrapped around it — because a fix that only handled the
    fronted case would pass on the first of them alone.
    """
    after = "If the build fails, never retry more than twice."
    before = "Never retry more than twice if the build fails."
    around = "Configure it so that if the queue is full, never block the caller."
    session = parsed(
        home,
        src,
        [
            user("u1", "If we never release the lock, the database hangs."),
            user("u2", "The database hangs if we never release the lock."),
            user("u3", after),
            user("u4", before),
            user("u5", around),
        ],
    )
    assert labelled(session) == [
        ("directive", after),
        ("directive", before),
        ("directive", around),
    ]


def test_the_same_request_is_not_the_same_rule(home, src):
    """ "Same rule", "same point", "same deal" are all elliptical for *the same
    one as before*, which is what makes them back-references. "Same request" is
    not that idiom — it describes what is sent — and it took the rule beside it
    down with it.
    """
    control = "For each client, same request, and never reuse the connection."
    session = parsed(
        home,
        src,
        [
            user("u1", "Same rule as the other service: never reuse the connection."),
            user("u2", "Same thing as before: no network calls in unit tests."),
            user("u3", "Same deal here, never commit generated files."),
            user("u4", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_having_no_opinion_is_not_forbidding_one(home, src):
    """ "I have no strong view" is the speaker declining to constrain anything,
    and it is written with the same determiner as "no raw SQL". The noun after
    it is what separates them: a view, an objection, a recollection is something
    the speaker holds, not something the code does.
    """
    control = "No raw SQL in the handlers."
    session = parsed(
        home,
        src,
        [
            user("u1", "I have no strong view on it."),
            user("u2", "We have no strong feelings about that approach."),
            user("u3", "I have no context on that decision."),
            user("u4", "I have no memory of agreeing to that."),
            user("u5", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_compound_noun_is_still_a_prohibition(home, src):
    """The other direction, and the reason the guards are written with word
    boundaries: "time" heads the list of nouns that make "no" a formula rather
    than a ban, but "no time-based tests" is a ban. A stop-list matched on a
    prefix quietly deletes a whole family of real directives, and the recall it
    costs shows up on no dev fixture that happens to lack the compound.
    """
    lines = [
        "No time-based tests in the suite.",
        "No issue-tracker links in commit messages.",
        "No view-model logic in the template.",
    ]
    session = parsed(home, src, [user(f"u{i}", t) for i, t in enumerate(lines)])
    assert labelled(session) == [("directive", t) for t in lines]


def test_a_restatement_is_one_wherever_its_sentence_starts(home, src):
    """The guard was pinned to offset 0, and chat does not oblige.

    A block is several sentences. The speaker thanks you and *then* restates the
    rule; or concedes something and restates it after the "but". The frame was
    there every time and the guard could not reach it, so the commonest
    distractor in a long session came back as a fresh directive.

    The other half of this is what must not change: the frame still has to
    *open* something — the block, a sentence, a clause after its punctuation, or
    the concessive that joins one. A saying-verb loose in the middle of a clause
    is reporting, not restating, and the control below is that sentence.
    """
    control = "Write the ADR that records what we agreed, and never merge red."
    session = parsed(
        home,
        src,
        [
            user("u1", "Thanks for the patch. As we agreed, never touch the vendored tree."),
            user("u2", "Sorry to keep on about it, but as I said, no raw SQL in the handlers."),
            user("u3", "That reads well! For the record, we never ship on a Friday."),
            user("u4", "Fine by me; per the spec, avoid global mutable state."),
            user("u5", "Looks right — as you flagged in review, never edit the generated files."),
            user("u6", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_reminder_is_a_frame_and_not_a_noun(home, src):
    """Reaching into the middle of a block put two ordinary words one comma away
    from deleting a rule: this project is measured in precision and *recall*, and
    a *reminder* is a thing a scheduler sends. A reminder frame introduces what
    it restates — a colon, a comma, "that", "to" — and with nothing following it
    the word is only a word.
    """
    kept = [
        "The reminder job must never fire twice; use the idempotency key.",
        "Report precision, recall and F1; never round them to two places.",
        "Set a reminder and never skip the nightly backup.",
    ]
    session = parsed(
        home,
        src,
        [
            user("u0", "Nice. A gentle reminder that we never ship on a Friday."),
            user("u1", "Right. Keep in mind we never log the raw token."),
            *(user(f"u{i + 2}", t) for i, t in enumerate(kept)),
        ],
    )
    assert labelled(session) == [("directive", t) for t in kept]


def test_a_hedge_is_a_hedge_on_either_side_of_the_auxiliary(home, src):
    """English takes the modifier before the auxiliary as readily as after it:
    "I really don't think" and "I don't really think" are one sentence written
    twice. The slot was open on one side only, so the frame missed, the negation
    was left sitting there on its own, and the hedge was filed as a rule.

    The two perfect-tense lines carry a participle that is spelled like a bare
    verb — *run*, *let* — and that is the [E7 pair review] half of this test.
    The one they replace was *"I honestly have never needed that flag"*, which
    nothing reads as a rule with or without the frame, so deleting both adverb
    slots from the perfect frame left the test green. *"We have never run
    migrations by hand"* is the sentence the frame exists for: strip it and the
    directive class sees `never run` and files a report of the past as an order.

    `u4` used to read *"We are not at all sure, so no rewrite this week."* — the
    hedge and a rule-shaped tail, because a hedge with nothing rule-shaped after
    it scores `None` whether the frame matches or not and the line would hold
    nothing. The tail was in a clause of its own, and once `_OPINION` became
    clause-scoped that clause is a directive and *should* be: the uncertainty is
    the reason for the instruction, not a cancellation of it. Same job, one
    clause: strip the adverb slot and `never rewrite` is filed as an order.
    [round 4, Gemini F2]
    """
    control = "Don't add a cache in the request path."
    session = parsed(
        home,
        src,
        [
            user("u1", "I really don't think that buys us anything."),
            user("u2", "We honestly don't want another moving part."),
            user("u3", "I'm genuinely not convinced we should forbid the cache."),
            user("u4", "We are not at all sure we should never rewrite this."),
            user("u5", "I honestly have never run migrations by hand."),
            user("u6", "We have honestly never let a secret into the repo."),
            user("u7", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_preferring_one_thing_to_another_is_not_one_verb(home, src):
    """ "X over Y" names a winner and a loser whichever verb takes it, and only
    one verb could reach the frame. Two of the rest were worse off than missing:
    a verb of choosing followed by "over" was read as *deliberation*, so a
    decision already taken was filed as a decision still open. The control is the
    sentence that really is still open, and it shares the verb.
    """
    kept = [
        "Favour composition over inheritance in the new module.",
        "Pick Parquet over CSV for anything we keep.",
        "Prioritise correctness over throughput here.",
        "Recommend uv over pip in the contributor guide.",
    ]
    session = parsed(
        home,
        src,
        [
            user("u0", "I am still choosing between a queue and a lock."),
            *(user(f"u{i + 1}", t) for i, t in enumerate(kept)),
        ],
    )
    assert labelled(session) == [("directive", t) for t in kept]


def test_a_block_that_dates_itself_to_the_past_is_a_report(home, src):
    """Tense needs a parser and the adjunct that says *when* does not.

    A block that dates itself and then describes a practice is a report about
    that date, and it was being read as a rule on the strength of whatever
    contrast word the description happened to carry. This does not lift the
    tense ceiling; it takes the subset of it that announces itself out loud.
    """
    control = "From now on, write Parquet instead of CSV."
    session = parsed(
        home,
        src,
        [
            user("u1", "A year ago we swapped the writer for pyarrow instead of fixing it."),
            user("u2", "Back when I joined, the team used a spinlock rather than a mutex."),
            user("u3", "Yesterday we dropped the cache, not the queue."),
            user("u4", "In older releases we avoided threads entirely."),
            user("u5", "Three sprints back the loader read JSON, not Parquet."),
            user("u6", "We started out with a flat file instead of a database."),
            user("u7", "Earlier, we swapped the lock for a queue rather than fixing the load."),
            user("u8", "Last Tuesday we moved the runner to podman instead of docker."),
            user("u9", control),
        ],
    )
    assert labelled(session) == [("directive", control)]


def test_a_decision_is_frozen_and_hashable(home, src):
    """The scorer counts decisions in a `Counter`, so a `Decision` has to be
    usable as a dict key; and a node whose `source_ref` can be reassigned after
    it was read off the block is provenance in name only.
    """
    d = derive.Decision("directive", "deadbeef")
    assert len({d, derive.Decision("directive", "deadbeef")}) == 1, "not usable as a scorer key"
    with pytest.raises(AttributeError):
        d.source_ref = "cafe"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the graph as a published artifact
#
# `graph.extraction` had tests from the day it was written and nothing called
# it: the emitter was complete, scored, and reachable only from a test file, so
# `gitmemory derive` produced two artifacts out of three and the README said so.
# --------------------------------------------------------------------------- #


DECIDED = [
    user("u1", "Keep every timestamp in UTC; no local time anywhere in the tree."),
    assistant("a1", [text("Giving up on the polling loop; I'll use an inotify watch.")]),
]


def test_the_decision_graph_is_published_beside_the_ideas(home, src):
    """The artifact is the extraction dict, not a drawing: graphify assembles it
    on demand, and writing a picture instead would put a rendering decision into
    a tree whose whole promise is that it rebuilds byte-identically.
    """
    from gitmemory import graph

    gen = stored(home, src, DECIDED)
    payload = loaded(home, "graph.json")
    assert payload == graph.extraction([index.parse_generation(gen)]), (
        "the published artifact is not what the emitter emits"
    )
    assert {n["kind"] for n in payload["nodes"]} == {"directive", "reversal"}
    assert len(payload["edges"]) == 1, "two decisions in one transcript are one succession"


def test_the_decision_count_stats_reports_is_the_count_on_disk(home, src):
    """The sibling of the ideas/marks check above, and the reason `build` counts
    nodes rather than calling `decisions` a second time: a stat computed from
    anything but the payload that was written can disagree with the file.
    """
    write(src, DECIDED)
    store.capture(src, "claude-code", "sess", home=home)
    stats = derive.build(home)
    assert stats.decisions == len(loaded(home, "graph.json")["nodes"]) == 2


def test_a_key_in_a_decision_block_costs_its_label_and_not_the_generation(home, src):
    """The write door refuses a secret, and a node label is the first artifact
    that carries block text through it verbatim. Refusing is right and skipping
    the generation is not: one leaked key in one decision would take that
    generation's ideas and timeline down with it, having derived nothing.

    So the label goes and the node stays. `source_ref` still names the block,
    and `raw/` still holds the bytes — the push gate is what keeps those home.
    """
    from gitmemory import graph

    key = "AKIAZZZZQQQQWWWW1234"  # synthetic, right shape
    rule = f"Never hardcode {key} in the tree; read it from the environment."
    stored(home, src, [user("u1", rule)])

    payload = loaded(home, "graph.json")
    assert [n["label"] for n in payload["nodes"]] == [graph.REDACTED], payload
    assert loaded(home, "ideas.json") and loaded(home, "timeline.json"), (
        "the whole generation was skipped over one label"
    )
    blob = b"".join(p.read_bytes() for p in Path(home, "derived").rglob("*.json"))
    assert blob, "derived nothing, so the scan below is vacuous"
    assert key.encode() not in blob, "the key reached the committed artifact tree"


# --------------------------------------------------------------------------- #
# E7 index-F8: the caps bound the shape of the input, not its cost
# --------------------------------------------------------------------------- #


def _ranked(n: int, per: int, *, distinct: bool = True) -> str:
    """`n` sentences of `per` words, every word distinct unless told otherwise.

    All-distinct is the shape that costs `_compute_idf`; identical is the shape
    that costs the pairwise loop. Both sit inside the caps.
    """
    out, seen = [], 0
    for _ in range(n):
        words = [f"w{seen + j}" if distinct else f"v{j}" for j in range(per)]
        seen += per
        out.append(" ".join(words) + ".")
    return " ".join(out)


def test_the_hoisted_lexrank_is_the_same_matrix_sumy_computes():
    """The two overrides are an optimisation or they are a fork, and the
    difference is this test. Element-wise on the matrix, not just the picks: a
    matrix can differ in a cell that no threshold comparison reaches on a
    particular document, and then the picks agree by luck. [E7 index-F8]
    """
    import random

    from sumy.models.dom import ObjectDocumentModel, Paragraph, Sentence
    from sumy.summarizers.lex_rank import LexRankSummarizer

    hoisted = derive._sumy()[3]
    assert hoisted is not LexRankSummarizer, "the subclass is not in the seam"
    tok = derive._Tok()
    vocab = ["alpha", "beta", "gamma", "delta", "lock", "await", "the", "held"]
    rng = random.Random(42)

    for trial in range(8):
        texts = [
            " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 12))) + "."
            for _ in range(rng.randint(2, 40))
        ]
        document = ObjectDocumentModel([Paragraph([Sentence(t, tok) for t in texts])])
        stock, fast = LexRankSummarizer(), hoisted()
        for s in (stock, fast):
            s.stop_words = ()
        words = [stock._to_words_set(s) for s in document.sentences]
        tf = stock._compute_tf(words)
        assert fast._compute_idf(words) == stock._compute_idf(words), trial
        stock_matrix = stock._create_matrix(words, stock.threshold, tf, stock._compute_idf(words))
        fast_matrix = fast._create_matrix(words, fast.threshold, tf, fast._compute_idf(words))
        assert (stock_matrix == fast_matrix).all(), f"trial {trial}: the matrices differ"
        assert [str(x) for x in fast(document, 5)] == [str(x) for x in stock(document, 5)], trial


def test_a_document_that_sits_exactly_on_both_caps_is_ranked_in_seconds():
    """800 sentences of 25 all-distinct words is inside both caps and cost
    4.60 s in the summariser before the loops were hoisted, 0.22 s after. At
    the caps themselves — 2000 sentences — it was 31 s, which is the finding.

    All-distinct, because that is the shape that stresses *both* overrides at
    once: 20 000 distinct terms is what made sumy's per-term `_compute_idf`
    scan quadratic, and 800 sentences is what made its per-pair frozensets
    quadratic. Reverting one override costs 1.86 s through `ideas`, reverting
    the other 3.26 s, against 0.39 s with both — which is the separation the
    bound has to sit inside.

    The bound is 1 s: 2.5x the measured 0.39 s, under the cheaper of the two
    reverts. Tighter than a timing test would like, and `process_time` rather
    than wall clock for exactly that reason — it counts this process's CPU, so
    a busy machine slows the test without failing it. If it ever does flake,
    widen the *fixture* and not the bound; 1200 sentences roughly doubles the
    gap. [E7 index-F8]
    """
    text = _ranked(800, 25)
    session = records.Session(
        session_id="s1",
        agent="claude-code",
        source_path="/x/s1.jsonl",
        turns=[
            records.Turn(
                session_id="s1",
                seq=0,
                role="assistant",
                byte_offset=0,
                byte_len=len(text),
                turn_id="t" * 12,
                blocks=[
                    records.Block(
                        turn_id="t" * 12, seq=0, kind="text", text=text, block_id="b" * 12
                    )
                ],
            )
        ],
    )
    started = time.process_time()
    out = derive.ideas(session)
    spent = time.process_time() - started
    assert out["sentences_ranked"] == 800, out
    assert spent < 1.0, f"ranking 800 sentences took {spent:.2f} s of CPU"


# --- E5 secondary set: text in a human's turn that no human typed --- #


def _user_blocks(uid: str, bodies: list[str]) -> dict:
    """A user turn whose content is a list, which is how the CLI sends one when
    it appends its own notice to what somebody typed.
    """
    return {
        "type": "user",
        "uuid": uid,
        "sessionId": "s1",
        "message": {"role": "user", "content": [text(b) for b in bodies]},
    }


IDE_NOTICE = (
    "<ide_opened_file>The user opened the file /tmp/demo/index.html in the IDE. "
    "This may or may not be related to the current task.</ide_opened_file>"
)


def test_an_editor_notice_in_the_user_role_is_not_a_directive(home, src):
    """The single worst defect the secondary set found, and the one that cost
    the most: 19 of 30 false positives came from blocks no person typed, and
    most of those from this one sentence. `_PROHIBIT` matches **`may not`** in
    "This may or may not be related to the current task", which the editor
    injects into the user role on *every file opened*.

    No probe could have found it. Every probe item is a sentence somebody wrote
    on purpose, so the whole category is absent from A, B, C and D — it took
    somebody else's real sessions. See `docs/benchmarks/E5-secondary-set.md`.
    """
    gen = stored(home, src, [_user_blocks("u1", [IDE_NOTICE])])
    session = index.parse_generation(gen)
    assert derive._decision_kind(IDE_NOTICE, "user") == "directive", (
        "the fixture no longer reproduces the defect; the rule stopped matching"
    )
    assert derive.decisions(session) == [], "the editor's notice became a decision"


def test_an_editor_notice_is_not_a_key_idea_either(home, src):
    """`_prose` feeds the ranker and the extractor from one place, on purpose,
    so the fix has to hold on both sides. A top-ranked "idea" reading "The user
    opened the file … in the IDE" is the same defect wearing the other hat.
    """
    gen = stored(home, src, [_user_blocks("u1", [IDE_NOTICE, PROSE[0]])])
    session = index.parse_generation(gen)
    payload = derive.ideas(session, count=20)
    assert "ide_opened_file" not in json.dumps(payload), "the notice reached the artifact"
    assert payload["ideas"], "the fixture's real sentence was dropped with it"


def test_the_notice_is_dropped_and_the_sentence_beside_it_is_kept(home, src):
    """Per block, not per turn. The CLI appends its notice as a *second* block
    on a turn whose first block is what somebody typed, and suppressing the turn
    would lose the instruction that the notice is attached to.
    """
    rule = "Never commit to main, always open a branch."
    gen = stored(home, src, [_user_blocks("u1", [rule, IDE_NOTICE])])
    session = index.parse_generation(gen)
    got = derive.decisions(session)
    assert [d.kind for d in got] == ["directive"], got
    blocks = {b.block_id: b.text for t in session.turns for b in t.blocks}
    assert blocks[got[0].source_ref] == rule, "the decision is anchored on the wrong block"


def test_a_sentence_that_merely_contains_markup_is_still_prose(home, src):
    """The predicate is whole-block and anchored at both ends. A person writing
    about markup — which is most of what anyone types at a coding agent — must
    not be silenced by mentioning a tag.
    """
    said = "Never emit a bare <script> tag; the sanitiser must escape it [see the ticket]."
    gen = stored(home, src, [user("u1", said)])
    session = index.parse_generation(gen)
    assert [d.kind for d in derive.decisions(session)] == ["directive"], "a real rule was dropped"


def test_a_block_that_opens_with_a_tag_and_goes_on_in_english_is_prose(home, src):
    """The closing anchor, which is the half that can silence somebody.

    The CLI does not always send its notice as a block of its own — a wrapper
    tag followed by what the person actually typed arrives as one block, and a
    predicate that only checked the opening tag would drop the instruction along
    with the wrapper. Losing a rule is a worse failure than keeping a notice.
    """
    said = "<command-name>/review</command-name>\nNever merge to main without a green run."
    gen = stored(home, src, [user("u1", said)])
    session = index.parse_generation(gen)
    assert [d.kind for d in derive.decisions(session)] == ["directive"], "a real rule was dropped"


def test_the_local_command_caveat_is_not_a_directive(home, src):
    """The CLI's own preamble on a local-command turn says, in English, that
    what follows is not addressed to the reader — and contains "DO NOT respond",
    which `_PROHIBIT` reads as a standing rule. It is on the secondary set once,
    and it is on every local-command turn in every session.
    """
    caveat = (
        "Caveat: The messages below were generated by the user while running "
        "local commands. DO NOT respond to these messages or otherwise consider "
        "them in your response unless the user explicitly asks you to."
    )
    gen = stored(home, src, [user("u1", caveat)])
    session = index.parse_generation(gen)
    assert derive._decision_kind(caveat, "user") == "directive", (
        "the fixture no longer reproduces the defect; the rule stopped matching"
    )
    assert derive.decisions(session) == [], "the CLI's caveat became a decision"


def test_the_adapters_own_image_placeholder_is_not_prose(home, src):
    """`_image_text` writes `[image image/png 1046384 chars]` as a *text* block
    so the index can find that an image was there. It is this repository's own
    string, and it was reaching LexRank and being offered as a key idea.
    """
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [
                {"type": "image", "source": {"media_type": "image/png", "data": "A" * 2048}},
                text(PROSE[0]),
            ],
        },
    }
    gen = stored(home, src, [line])
    session = index.parse_generation(gen)
    kinds = [b.text for t in session.turns for b in t.blocks if b.kind == "text"]
    assert any(k.startswith("[image ") for k in kinds), "the fixture made no placeholder"
    assert "[image " not in json.dumps(derive.ideas(session, count=20)), "placeholder ranked"


def test_the_canonical_json_of_an_unknown_block_is_not_prose(home, src):
    """The adapter's `else` branch writes canonical JSON into a `text` block,
    deliberately, so an unrecognised block stays searchable. Searchable is not
    the same as said: a JSON payload ranked as a key idea is the E2 finding
    that put `tool_use` out of the prose stream in the first place.
    """
    line = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [
                {"type": "wombat", "never": "use pickle", "avoid": "threads"},
                text(PROSE[1]),
            ],
        },
    }
    gen = stored(home, src, [line])
    session = index.parse_generation(gen)
    assert any(
        b.text.startswith("{") for t in session.turns for b in t.blocks if b.kind == "text"
    ), "the fixture made no JSON block"
    assert derive.decisions(session) == [], "an unknown block's JSON became a decision"
    assert "wombat" not in json.dumps(derive.ideas(session, count=20)), "JSON ranked as an idea"


# --- E5 fix 2: a substitution is not a reversal without a recant --- #
#
# 52 of the 61 `reversal` nodes the extractor wrote on real sessions were
# narration, and the substitution frame on its own was 31 of them. Writing code
# is substitution all day long; what makes it a *reversal* is that the thing
# being put down was the assistant's own position, and a predicate over one
# block only knows that if the block says so. These fix the three things that
# count as saying so, and the two `_ABANDON` branches that were not
# abandonments. See `docs/benchmarks/E5-secondary-set.md`.


def _assistant_says(home, src, body):
    """The decisions in a one-block assistant turn.

    A fresh store *and* a fresh transcript per call. `write` opens the source
    `ab`, so a second call appends a second line to the same file — and both
    lines carry uuid `a1`, which the parser dedupes. Reusing the fixtures would
    therefore have re-scored the *first* body every time, and the loops below
    would have asserted three times about one item.
    """
    tag = hashlib.sha256(body.encode()).hexdigest()[:8]
    home = f"{home}-{tag}"
    Path(home).mkdir()
    src = str(Path(src).with_name(f"{tag}.jsonl"))
    return derive.decisions(parsed(home, src, [assistant("a1", [text(body)])]))


def test_the_assistant_conceding_a_point_and_substituting_is_a_reversal(home, src):
    """A concession presupposes a position that was contested. With a
    substitution frame beside it, the block says which position and what
    replaces it — which is the whole shape, in one block, with no context.
    """
    assert [d.kind for d in _assistant_says(home, src, "You're right. Parquet, not CSV.")] == [
        "reversal"
    ]


def test_the_assistant_trying_a_different_approach_is_a_reversal(home, src):
    """"A different approach" is a comparative, and a comparative presupposes a
    salient prior member of the class. Narrower than it looks: "a different
    file" or "a different value" is not here, because only the plan can be the
    thing reversed.
    """
    body = "The checker chokes on that. Let me try a different approach, a plain return instead."
    assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"]


def test_the_assistant_saying_it_will_not_work_and_substituting_is_a_reversal(home, src):
    """A verdict that the thing does not work presupposes it was being relied
    on to work. The sentence that motivated this one is probe B's "Hmm, that's
    not going to work. Dropping the thread pool and doing it synchronously
    instead."
    """
    body = "That's not going to work. I'll do it synchronously instead."
    assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"]


def test_narrating_an_edit_is_not_a_reversal(home, src):
    """The negative half, and the expensive one. All three of these were
    emitted on real sessions and all three were adjudicated narration: a repair,
    the next step of the user's own plan, and a bug report. Nothing in any of
    them says the assistant is leaving a position it held.
    """
    for body in (
        "I need to pass a Path object instead of a string. Let me fix this:",
        "Now I'll replace the complex cross-session navigation with a simple link.",
        "It seems like the code is still falling back to the regular generate_html "
        "function instead of using the combined link version.",
    ):
        assert _assistant_says(home, src, body) == [], body


def test_a_cleanup_justified_by_no_longer_needing_it_is_not_a_reversal(home, src):
    """`no longer` is resultative: it describes the state after a change rather
    than announcing one, which is why it fired five times on real sessions and
    never on an abandonment. Both of these are cleanups whose *reason* is given
    in the frame.
    """
    for body in (
        "Now I need to remove the unused import since we're no longer using convert_jsonl_to_html.",
        "Let me also remove the TODO comment that's no longer needed:",
    ):
        assert _assistant_says(home, src, body) == [], body


def test_a_directory_called_scratch_is_not_an_abandonment(home, src):
    """`scratch` is a verb in "scratch the cron job" and a very common directory
    name everywhere else. It matched `scratch/` on real sessions, so it takes an
    object now. (Not `main.py.oldscratch`, which this said for three commits: the
    token is in the corpus but `\\b` never let the branch reach it. See
    `_ABANDON`.)

    Found while writing this: "Scratch that" never reaches `_PIVOT`, which lists
    it. `_REPAIR` claims `scratch that` first — as the speaker striking their own
    slip of the keyboard — and the guards run before the rules. Recorded, not
    fixed here: one fix per commit, and this one is a guard ordering question
    rather than a wording one.

    **Probe A's ceiling item is not this.** "Scratch the cron approach; a systemd
    timer is the right tool here" was cited here and in the benchmark write-up as
    the same guard-ordering casualty, and it is not: `_REPAIR`'s object list is
    `that|this|my last|my previous|the last|the previous`, and "the cron" is none
    of them, so `_REPAIR` never fires on it at all. It failed one layer down —
    `_ABANDON` matched "Scratch the" and neither `_ADOPT` nor `_COMMIT` fired,
    because "is the right tool here" names the replacement with no adoption verb
    and no first-person commitment. Reordering the guards, which is what the
    recorded diagnosis implies, would not have moved it. [E5 fix 2 review, F7]

    **That ceiling is now lifted, and the correction above is why it could be.**
    Probe E found the nominal cancel marker — *"Scratch the SEARCH ALL"* — going
    nowhere, and the repair is in `_PIVOT` rather than in the guard order,
    exactly where F7's diagnosis said the problem was not. `_PIVOT` is sufficient
    on its own, so the missing second half stops mattering and the item passes.
    The deictic is untouched: `_REPAIR` still claims `scratch that`, still runs
    first, and the assertion below still holds. [E5 probe E]
    """
    body = "Let me check what scratch/ contains - it looks like a dev artifact. I'll use the cache."
    assert _assistant_says(home, src, body) == []
    assert _assistant_says(home, src, "Scratch that, I'll use the cache.") == [], "_REPAIR won"
    assert [
        d.kind for d in _assistant_says(home, src, "Dropping that, I'll use the cache.")
    ] == ["reversal"], "the abandonment pair still fires"

    # The corrected diagnosis, asserted rather than narrated: a `== []` here
    # would hold under either story, and the wrong one survived three commits
    # precisely because nothing distinguished them. Kept after the ceiling was
    # lifted, because what makes the item pass now is `_PIVOT` and the two
    # predicates F7 was wrong about are still the ones that do not reach it.
    ceiling = "Scratch the cron approach; a systemd timer is the right tool here"
    assert [d.kind for d in _assistant_says(home, src, ceiling)] == ["reversal"]
    assert not derive._REPAIR.search(ceiling), "_REPAIR was never the reason"
    assert not derive._ADOPT.search(ceiling) and not derive._COMMIT.search(ceiling), (
        "the stop/start pair still cannot reach it; `_PIVOT` is what does"
    )
    assert derive._PIVOT.search(ceiling), "and it needs no second half"


# --- E5 fix 2, reviewed: an object list that admitted an idiom --- #


def test_scratching_the_surface_is_not_abandoning_it(home, src):
    """Giving the verb an object fixed the directory and admitted an idiom.

    `scratch(es|ed|ing)? (that|the|this|…)` matches *"scratches the"* in "this
    only scratches the surface", which is not an abandonment of anything — it is
    a remark about how far the work got. Joined to any first-person plan, which
    is most of what an assistant says next, the stop/start pair fires and the
    block is written into the graph as a reversal.

    The object is what makes the verb an abandonment, so the idiom is excluded by
    its object rather than by dropping the inflections: "he scratched the plan"
    is a real abandonment in the same tense. [E5 fix 2 review, F6]
    """
    assert _assistant_says(home, src, "This only scratches the surface — let's use a "
                                      "deeper scan.") == []
    assert _assistant_says(home, src, "That barely scratches the surface of the "
                                      "problem, so let's start again.") == []

    # The ceiling the one-word exclusion buys, asserted so it is visible rather
    # than discovered. `surface` is refused in the object position whatever it
    # heads, so a real abandonment of a thing called "the surface …" is declined
    # with the idiom. Telling them apart needs to know whether `surface` is the
    # head noun or a modifier, which is a parser and not a lookahead; the idiom
    # is common in chat and the noun phrase is not, so the trade is taken.
    assert _assistant_says(home, src, "Scratch the surface probes; I'll use the "
                                      "full scan.") == []

    # The rest of the object list is untouched, which is what makes the
    # exclusion narrow rather than a retreat from the whole branch.
    assert [
        d.kind
        for d in _assistant_says(home, src, "Scratch the cron job; I'll use a systemd "
                                            "timer.")
    ] == ["reversal"]


def test_the_objects_the_verb_actually_takes_include_the_plural_ones(home, src):
    """`them`, `those` and `these` were left out of the list, silently.

    A list written by hand from one corpus gets the objects that corpus happened
    to contain. These three are the same construction as `that` — the speaker
    striking something already in play — and all three came back as nothing.
    """
    for body in (
        "Scratch those, I'll use a deque.",
        "Scratch them — I'll use a bounded queue.",
        "Scratch these, we'll use the cache.",
    ):
        assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"], body


# --- E5 fix 1, reviewed: `_injected` was wider than its own comment --- #
#
# The filter that keeps the CLI's own text out of the prose stream was reported
# as silencing people. Every case below was reproduced through `decisions()`
# before it was fixed, and each one is a *standing rule* that the filter ate —
# the expensive direction, because a lost rule is gone from the artifact and a
# kept notice is only noise in it.


def _user_says(home, src, body):
    """The decisions in a one-block user turn. Fresh store and fresh transcript
    per call, for the reason given in `_assistant_says`.
    """
    tag = hashlib.sha256(body.encode()).hexdigest()[:8]
    home = f"{home}-{tag}"
    Path(home).mkdir()
    src = str(Path(src).with_name(f"{tag}.jsonl"))
    return [d.kind for d in derive.decisions(parsed(home, src, [user("a1", body)]))]


def test_a_person_writing_a_tagged_prompt_keeps_the_rule(home, src):
    """`<rules>…</rules>` is the prompt style the model vendor's own
    documentation teaches, and the first version of this filter silenced every
    one of them — the tag was enough. The tag name now has to carry a `-` or a
    `_`, which every injected tag has and these do not.
    """
    for body in (
        "<rules>\nNever commit to main. Always run the tests first.\n</rules>",
        "<instructions>\nDo not use pickle anywhere in this project.\n</instructions>",
        "<context>\nNever use pickle.\n</context>",
    ):
        assert _user_says(home, src, body) == ["directive"], body


def test_an_uppercase_tag_is_a_person_too(home, src):
    """The discriminator is a `-` or a `_`, so a person who writes
    `<IMPORTANT_RULES>` has one. Case is what saves them: the CLI's tags are
    lowercase, and matching case-insensitively would take this rule back.
    """
    body = "<IMPORTANT_RULES>\nNever use pickle.\n</IMPORTANT_RULES>"
    assert _user_says(home, src, body) == ["directive"]


def test_a_block_that_opens_and_closes_on_different_tags_is_not_one_notice(home, src):
    """The close tag has to *be* the open tag. Without the backreference the
    `.*` spans everything between the first tag and the last `</…>` in the
    block, so a rule typed between two CLI notices went with them.
    """
    body = (
        "<command-name>/review</command-name>\n"
        "Never merge to main without a green run.\n"
        "<local-command-stdout>ok</local-command-stdout>"
    )
    assert _user_says(home, src, body) == ["directive"]


def test_a_tag_that_does_not_close_itself_is_somebodys_typo(home, src):
    """An element ends at *its own* close tag. A person who writes a tagged
    prompt and mistypes the separator on the way out has written a block that no
    CLI would emit, and the block holds their rule.

    Thin on its own — the dash in `<important-rules>` is already most of the way
    to being read as a notice, and that residue is recorded in `_injected`. It
    is here because without it nothing pins the backreference.
    """
    body = "<important-rules>\nNever commit to main.\n</important_rules>"
    assert _user_says(home, src, body) == ["directive"]


def test_two_machine_tags_in_one_block_are_still_a_notice(home, src):
    """The backreference alone was too strict, and a real session said so: the
    CLI writes `<bash-stdout>…</bash-stdout><bash-stderr></bash-stderr>` as one
    block. Requiring the close to match the open turned that into prose and cost
    a false positive back. The whole-block question is therefore "tags end to
    end", not "one tag".
    """
    body = (
        "<bash-stdout>5 failed, 174 passed\nThis may or may not be related to the "
        "current task.</bash-stdout><bash-stderr></bash-stderr>"
    )
    assert _user_says(home, src, body) == []
    assert derive._decision_kind(body, "user") == "directive", (
        "the fixture no longer reproduces the defect; the rule stopped matching"
    )


def test_the_local_command_caveat_ends_at_the_blank_line(home, src):
    """The caveat was anchored at the front and nowhere else, so everything
    appended to it was swallowed too. This is the likelier of the filter's two
    over-reaches to have cost somebody a sentence: it fires on the CLI's
    everyday shape, not on an unusual one.
    """
    body = (
        "Caveat: The messages below were generated by the user while running "
        "local commands. DO NOT respond to these messages or otherwise consider "
        "them in your response unless the user explicitly asks you to."
        "\n\nAlso, from now on never commit straight to main."
    )
    assert _user_says(home, src, body) == ["directive"]


def test_pasted_json_is_not_the_adapters_own_json(home, src):
    """"Does it parse" was wider than the sentence justifying it. The adapter
    writes `canonical_json`, which is sorted and separator-tight, so the test is
    a round trip and not a parse — and a config fragment somebody pasted out of
    an editor, spaces and all, is theirs again.

    The list is the one that shows the filter had two holes in the same place:
    narrowing the JSON branch alone left it silenced, because `_NOTICE` claimed
    it first for opening with `[` and closing with `]`.
    """
    for body in (
        '{"note": "never use sqlite in prod", "when": "always"}',
        '["never use pickle", "always use uv"]',
    ):
        assert _user_says(home, src, body) == ["directive"], body


def test_a_bracket_notice_needs_its_closing_bracket(home, src):
    """The closing anchor on the notice pattern, which is the half that can
    silence somebody: `[image …]` is the whole block or it is not a notice.
    """
    assert _user_says(home, src, "[note] never commit straight to main.") == ["directive"]


def test_text_that_opens_with_a_brace_and_is_not_json_is_still_prose(home, src):
    """Failing to parse means it was typed, not that it was generated. The
    branch used to return `True` from the same place it returns `False` now.
    """
    assert _user_says(home, src, "{never use pickle}") == ["directive"]


def test_a_deeply_nested_block_does_not_cost_the_generation_its_artifacts(home, src):
    """`json.loads` raises `RecursionError`, not `ValueError`, on deep nesting,
    and nothing above `_injected` caught it: one pasted block took out
    `decisions()`, and with it the whole generation's ideas, timeline and graph.

    The docstring used to argue the parse was bounded because "blocks are
    bounded by the adapter". They are not — the text branch copies the block
    through at whatever length it arrives.
    """
    rule = "From now on never commit straight to main."
    stored(home, src, [user("u1", "[" * 20000 + "]" * 20000), user("u2", rule)])
    stats = derive.build(home)
    assert stats.skipped == [], stats.skipped
    written = sorted(p.name for p in Path(home, "derived").rglob("*") if p.is_file())
    assert written == ["graph.json", "ideas.json", "timeline.json"], written


# --- E5 fix 2, reviewed: the conjunction was scoped to the block --- #


def test_a_concession_three_paragraphs_from_a_substitution_licenses_nothing(home, src):
    """Both nodes that survived fix 2 on real sessions were this shape.

    "You're absolutely right." opens the message; four hundred characters later
    an `instead` turns up inside a description of the *bug* — the template "is
    now only showing the combined transcript link instead of the session
    navigation". Nothing joins the two but the message boundary. A conjunction
    over a whole block will pair any concession with any substitution the same
    message happens to contain, and on a chat transcript that is most of them.
    """
    body = (
        "Oh no! You're absolutely right.\n\n"
        "The issue is likely that the template is now only showing the combined "
        "transcript link instead of the session navigation."
    )
    assert _assistant_says(home, src, body) == []


def test_a_concession_offered_with_the_substitution_is_still_a_reversal(home, src):
    """The control, and it is load-bearing: a guard that suppressed every
    concession would pass the test above and take all seven true reversals with
    it. Same two cues, one paragraph.
    """
    body = "You're absolutely right — I'll use a bounded queue instead of the lock."
    assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"]


def test_cutting_an_attitude_out_does_not_reflow_the_message(home, src):
    """`_without_opinion` splits on the whitespace after a sentence end, which
    swallows a blank line, and rejoining with a space handed the branch above
    one paragraph where the message had two — so the scope fix did nothing for
    any message whose first paragraph ends in an opinion, which is the shape of
    the concession it was written for.

    The opinion here is in the first paragraph and the substitution is in the
    second; the paragraph break has to survive the cut.
    """
    body = (
        "Oh no! You're absolutely right. I don't like how that turned out.\n\n"
        "The template is now only showing the combined link instead of the nav."
    )
    assert _assistant_says(home, src, body) == []


# --- E5 fix 2, reviewed: an alternative no input could reach --- #


def test_owning_a_slip_is_a_repair_whichever_predicate_gets_there_first(home, src):
    """`_RECANT` listed `my (mistake|bad|error)` and no input could reach it.

    `_REPAIR` lists `my (mistake|bad|error|fault)` — the same three words and one
    more — and `_REPAIR` is a guard, so it returns `None` before the assistant
    branch is entered at all. The alternative was unreachable by inclusion, not
    by accident of the corpus, and it is deleted.

    **This fix has no mutation row, and cannot have one.** Dead code is exactly
    code every single-edit mutant of which survives: restoring the alternative
    to `_RECANT` changes no output, because `_REPAIR` still gets there first;
    removing `_REPAIR`'s branch changes no output either, because `_RECANT` no
    longer lists the words. Only both edits together bring the behaviour back —
    measured, and it comes back as `reversal` on all three bodies below. A row
    is one find/replace in one file, so the negative control this deserves is
    unexpressible in the harness and is written down here instead.

    The deletion is therefore a small hardening as well as a tidy: before it, a
    future narrowing of `_REPAIR` would have silently promoted these to
    reversals through a predicate nobody was looking at. Owning a slip of the
    keyboard is not withdrawing a position.
    """
    for body in (
        "My mistake — I'll use a bounded queue instead of the lock.",
        "My bad. Replacing the lock with a queue.",
        "My error — switching to a deque instead.",
    ):
        assert _assistant_says(home, src, body) == [], body


# --- E5 fix 2, reviewed: eight alternatives no test was holding --- #
#
# A mutation sweep over `_RECANT`, one mutant per alternative, replacing each
# with a token that cannot match — not deleting it, because a deleted
# alternative leaves an empty branch, which matches everywhere and measures
# nothing. Nine of twelve survived the whole suite. Only `you're right`, `a
# different approach` and `that's not going to work` were held by anything, and
# one of those nine was `my (mistake|bad|error)`, which survived because it was
# dead; it is gone, and the test above says why. That leaves the eight below.
#
# The obvious reading — "eight branches are untested, write eight tests" — is
# worth one more question first: are they untested, or unused? Counted, over
# every population this repository has:
#
#   alternative              real  dev42  test31337  probes
#   you're right               14      0          0       0   pinned
#   good catch                  2      0          0       0
#   good point                  2      0          0       0
#   good observation            3      0          0       0
#   i was wrong                 0      0          0       0*
#   i apologi[sz]e              0      0          0       0
#   i'?m sorry                  0      0          0       0
#   a different approach        4      0          0       0   pinned
#   that's not going to work    0      0          0       1   pinned
#   that won't work             0      0          0       0
#   that approach fails         0      0          0       0
#
# "real" is 559 distinct assistant text blocks, sha256-deduped, `agent-*.jsonl`
# and `subagents/` excluded. (*) `i was wrong` does occur in probe D, but on a
# *user* turn, and `_RECANT` is read only in the assistant branch, so nothing
# reaches it.
#
# Two things fall out of that table, and the second is the larger one.
#
# **The synthetic corpus contains none of this vocabulary at all.** Not one
# alternative fires on either split, including the three that tests hold. So
# every "no change on the gate" measurement recorded for fix 2 and fix 3 is
# true but vacuous for this branch: the gate cannot see it. What has ever
# measured the assistant substitution branch is the real-corpus adjudication
# (61 emitted, 7 kept) and probe B's single case — and that is the whole of it.
#
# **Six alternatives fire nowhere.** The tempting move is to delete the five of
# those that no test holds, the way `my (mistake|bad|error)` was deleted. It is
# the wrong move, and the reason is the distinction that deletion was built on:
# that one was *dead* — no input could reach it, because a guard returned first
# — and these are merely *unobserved*. A mutant of dead code survives because
# the code cannot run; a mutant of unobserved code survives because nobody
# wrote the input. Only the first is a fact about the program.
#
# I did look for a cost to keeping them, and did not find one that is theirs.
# The apology frames are the weakest members — you can apologise for a delay
# without leaving any position, so they fail the class's own stated criterion —
# and they do turn "I'm sorry, the build is slow because of the cold cache
# rather than the linker" into a reversal. But so does "You're right, …", and
# so does "Good catch, …", on the same sentence: a contrast between two *facts*
# read as a contrast between two plans is a `_CONTRAST` limitation shared by
# every member of the class, including the ones that fire fourteen times. It is
# recorded in the secondary set, not fixed here, and it is not evidence against
# these five. Deleting them on the strength of an argument I could not measure
# is the mistake the `oldscratch` correction was about.
#
# So: pin all eight. A test per alternative would be memorisation if the
# sentences came from the corpus that tuned the predicate; these come from the
# class's stated criterion instead, one sentence each, which is what a
# specification looks like. Eight mutation rows point here.


def test_every_recant_alternative_is_held_by_something(home, src):
    """One sentence per `_RECANT` alternative that no other test was holding.

    Each pairs the frame with a substitution, because the frame alone is not
    sufficient and never was — this class only ever qualifies a substitution
    that is already there. So each body below is the minimum input that
    distinguishes "this alternative exists" from "this alternative does not",
    and a mutant of any one of them fails exactly one assertion here.

    *Minimum* is the load-bearing word, and the first draft got it wrong. The
    `good catch` body was "Good catch. Dropping the retry wrapper and using the
    built-in backoff instead", which reads like a clean example and is not one:
    `Dropping` is `_ABANDON` and `using` is `_ADOPT`, so the stop/start pair
    reaches `reversal` on its own and the verdict is the same with the
    alternative deleted. The mutation row came back MISSED and said so. A body
    that exercises a branch and a body that *depends* on it are different
    things, and only the second pins anything — every body below was checked by
    deleting its own alternative and confirming the block goes to nothing.

    Grouped by what the counts above say about them, because the two groups
    carry different weight: the first three were observed on real transcripts
    and their absence would be a measured recall loss, the last five have never
    been observed anywhere and their absence would be a loss nobody has yet
    seen. Both are pinned; only the first three are evidenced.
    """
    observed = (
        "Good catch — using the built-in backoff instead.",
        "Good point — I'll key on the check package ID rather than the tail number.",
        "Good observation; using a deque instead of the list.",
    )
    declared = (
        "I was wrong about the lock; using a bounded queue instead.",
        "I apologise — writing to the staging bucket instead of prod.",
        "I'm sorry, I'll run the full scan instead of the sample.",
        "That won't work — switching to a systemd timer instead of cron.",
        "That approach fails on empty input; I'll use a sentinel instead.",
    )
    for body in observed + declared:
        assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"], body


# --- E5 probe E: the cancel marker that only worked with a pronoun --- #


def test_cancelling_a_named_thing_is_a_pivot_and_not_only_cancelling_a_pronoun(home, src):
    """*"Scratch the SEARCH ALL"* — the form you write when the thing is not the
    last thing you said.

    `_PIVOT`'s entire cancel marker was `scratch that`, which could never fire:
    `_REPAIR` claims the demonstratives and runs first, so the alternative was
    dead in the F5 sense — a mutant of it survives because the code cannot be
    reached, not because nobody wrote the input. The reachable form is the one
    that names its object, and nothing claimed it.

    Two things this does *not* change, asserted because both were load-bearing
    decisions before it:

      - the deictic is still `_REPAIR`'s, which is the recorded guard-ordering
        trade and not something this touches;
      - `surface` is still refused in the object position, the same exclusion
        `_ABANDON` carries for the same idiom. The inflected forms cannot reach
        `_PIVOT` at all — it wants the bare verb — but *"let me scratch the
        surface here"* is the bare imperative and would otherwise be a reversal.

    The gain over the stop/start pair is that `_PIVOT` is sufficient alone, so a
    cancellation whose replacement is named without an adoption verb now lands.
    That is probe A's ceiling item, and it is the same sentence as probe E's.
    """
    for body in (
        "Scratch the SEARCH ALL. The table isn't guaranteed ascending.",
        "Scratch the cron approach; a systemd timer is the right tool here.",
        "Strike my earlier answer on the sort card.",
        "Scratch our two-pass design. One pass, totals in a table.",
    ):
        assert [d.kind for d in _assistant_says(home, src, body)] == ["reversal"], body

    assert _assistant_says(home, src, "Scratch that, I'll use the cache.") == [], "_REPAIR's"
    assert _assistant_says(home, src, "Let me scratch the surface here first.") == [], "the idiom"


# --- E5 probe E follow-up: the contracted negation, and `never mind` --- #


def test_a_contracted_dont_needs_the_imperative_and_doesnt_has_no_such_position(home, src):
    """*"Still doesn't work"* is not a rule about working.

    `_PROHIBIT` listed `don't` and `doesn't` beside `do not` and `does not`, and
    on the 89 real user prose blocks in `bench/secondary.py` the contracted form
    occurs 21 times: 18 of them report that something is broken and 3 forbid
    something. The three are all imperatives — nothing in front of the verb —
    and the eighteen all have a subject, which is what makes them reports.

    So the contracted form is admitted in imperative position only. `doesn't`
    gets no position at all: it is third-person singular present, English has no
    third-person imperative, and there is therefore no sentence in which it is a
    command. That is the same call the module makes about `cannot` — inability
    and prohibition share the word, and inability is the commoner one.

    The uncontracted forms are untouched and the first two bodies say why: a
    written rule reaches for them, and both a synthetic gate template and a
    probe E directive are phrased that way.
    """
    rules = (
        "Make sure we do not use pickle for the cache. Rely on the standard json module.",
        "The grandmaster belongs to the console — our code reads it and does not set it.",
        "Don't merge without a green build.",
        "Run the tests first. Then don't push until CI is green.",
        "Go ahead with the branch and the commits (but don't push).",
    )
    for body in rules:
        assert _user_says(home, src, body) == ["directive"], body

    reports = (
        "Still doesn't work, maybe there's a step needed to have the submodule checked out?",
        "The exported types don't accurately represent the request body structure.",
        "Would I be able to redirect so the old URL doesn't break?",
        "The fixtures don't even agree on what a timeout means.",
    )
    for body in reports:
        assert _user_says(home, src, body) == [], body


def test_never_mind_is_a_person_dropping_a_request(home, src):
    """*"Never mind!"* is a withdrawal, and it was scoring a standing rule.

    The tense cue cannot reach it — `mind` is a bare infinitive, not a past —
    so the exclusion is by name, which is how the determiner `no` already
    handles "no rush" and "no worries" one block below.

    The control is the whole reason it is two words rather than one: the
    exclusion is the formula, so a `never` anywhere else in the same block is
    still a prohibition. Suppressing the block on the formula would be the
    `options` mistake, where a guard swallows a real rule sitting beside it.
    """
    assert _user_says(home, src, "Never mind! Add a details element around it.") == []
    assert _user_says(home, src, "Never mind that. Never commit generated files.") == ["directive"]


# --- E5 root cause 4: the positive standing rule, in the corner a regex has -- #


def test_a_rule_can_say_a_thing_stays_the_way_it_is(home, src):
    """*"Patch file stays YAML."* is a rule, and nothing claimed it.

    The user branch reads a substitution frame and a prohibition, so every rule
    it sees is one that names something rejected. A rule stated positively —
    what a thing *is*, and goes on being — had no home, and probes C, D and E
    miss 24 user directives between them mostly of that shape.

    The persistence verbs are the corner of the class that carries the meaning
    in the verb rather than in the syntax: saying a thing stays as it is *is*
    constraining future work on it. The rest of the hole needs to tell "every
    task card carries the AMM reference" from "the exported types don't
    represent the request body", which is a subject and a simple-present verb
    in both, and that is a parse.
    """
    rules = (
        "Patch file stays YAML.",
        "Deferral records stay in UTC.",
        "While you're in the cue loader anyway, index zero stays reserved for blackout.",
        "Part serial numbers keep their leading zeros.",
        "Put the region on the JOB card once and keep it off the individual steps.",
        "The grandmaster remains a console concern.",
    )
    for body in rules:
        assert _user_says(home, src, body) == ["directive"], body


def test_the_persistence_verb_needs_the_position_the_report_does_not_have(home, src):
    """*"I keep getting build errors"* is a complaint about repetition.

    With a subject in front the verb turns aspectual: `keep` plus a participle
    says a thing happens over and over, which is what you write when something
    is broken. The rules above have either a noun phrase the rule is *about*
    ("patch file stays") or no subject at all, because they are imperatives.
    That is the same discriminator the contracted negation uses two classes up,
    and for the same reason.

    `keep-alive` is the other one, and it is not subtle: it arrives five to a
    block inside pasted HAR files. `good to keep as milestone information` is
    the third — with no object between the verb and `as`, the frame is
    appraising the thing rather than constraining it.
    """
    reports = (
        "I keep getting mysterious build errors when MDX files have URLs in brackets.",
        'The HAR shows { "name": "Connection", "value": "keep-alive" } on every request.',
        'The above "status report" is very interesting and good to keep as milestone information.',
    )
    for body in reports:
        assert _user_says(home, src, body) == [], body

    # The object is what separates the appraisal from the rule, so putting one
    # back brings the rule back.
    assert _user_says(home, src, "Keep it as YAML.") == ["directive"]


def test_keep_this_in_mind_is_the_same_reminder_as_keep_in_mind(home, src):
    """`keep X in mind` is separable and the guard only had the joined form.

    `_BACKREF` exists to stop a restated instruction being recorded a second
    time, and it listed `keep in mind` and `bear in mind` as fixed strings. A
    reminder that names what it is about — "please keep this instruction in
    mind" — puts the object between the verb and the particle and walked past
    it. Invisible until the persistence class arrived, because nothing else
    read `keep`; found by the held-out gate, where it cost 14 points of
    precision on the test split.

    No lookahead on the new alternative, unlike its neighbours: bare `remember`
    and `recall` are ordinary words that need one, and `in mind` is not.
    """
    for body in (
        "Please keep this instruction in mind. Deferral records stay in UTC.",
        "Bear the deadline in mind — the cache stays warm between runs.",
    ):
        assert _user_says(home, src, body) == [], body

    # The particle is doing the work, so a `keep` with no `in mind` after it is
    # still a rule.
    assert _user_says(home, src, "Keep the deadline in the ticket.") == ["directive"]


def test_two_cases_side_by_side_are_a_deliberation(home, src):
    """*"There's a case for dropping the cache and a case for keeping it warm."*

    Probe A files this under `guard: deliberation` and the guard did not have
    it — `on the one hand … on the other` was there, the same frame in other
    words was not. It cost nothing while no rule read `keeping`, which is how a
    hole like this stays open.

    The control is the single case, which is an argument for one thing and not
    a weighing of two.
    """
    assert _user_says(
        home, src,
        "There's a case for dropping the cache entirely and a case for keeping it warm.",
    ) == []
    assert _user_says(
        home, src, "There's a case for dropping the cache. The index stays in memory.",
    ) == ["directive"]


# --- E5 review round: the rules model a space, the input carries whitespace --- #
#
# Three findings from the standalone review of fix 7, one cause. Each was
# reproduced through `decisions()` before `_flatten` was written, and each is
# input nobody has typed into a corpus we own — which is the point: no board
# here moves, and all three are wrong anyway.


def test_a_windows_line_ending_still_ends_a_paragraph(home, src):
    """`_PARAGRAPH` is `\\n[ \\t]*\\n`, and in `\\r\\n\\r\\n` the `\\r` between
    the newlines is neither a space nor a tab. So a message typed on Windows —
    or pasted out of one — was one paragraph however many it had, and the
    assistant's conjunction scoped over all of it: the concession in the first
    paragraph licensed the `instead` in the second, which is exactly the false
    positive the paragraph scope exists to stop.

    Same body as `test_a_concession_three_paragraphs_from_a_substitution_licenses_nothing`,
    with the other line ending.
    """
    body = (
        "Oh no! You're absolutely right.\r\n\r\n"
        "The issue is likely that the template is now only showing the combined "
        "transcript link instead of the session navigation."
    )
    assert _assistant_says(home, src, body) == []

    # The control: one paragraph, CRLF or not, is still a reversal.
    assert [
        d.kind
        for d in _assistant_says(
            home, src, "You're absolutely right —\r\nI'll use a bounded queue instead."
        )
    ] == ["reversal"]


def test_two_spaces_do_not_get_a_report_past_the_persistence_guard(home, src):
    """`_PERSIST`'s lookbehinds are fixed-width because Python's `re` allows no
    other kind, so each one checks exactly one space. "I  keep getting build
    errors" — the shape the guard was written for, typed with a double space —
    walked straight past it and came back a standing rule.

    A tab and a non-breaking space are the same hole. The last is not a typo: it
    is what a copy out of a rendered page puts in the message.
    """
    for body in (
        "I  keep getting mysterious build errors when MDX files have URLs in brackets.",
        "I\tkeep getting mysterious build errors when the MDX has brackets.",
        "I\u00a0keep getting mysterious build errors when the MDX has brackets.",
        "I keep getting mysterious build errors when the MDX has brackets.",
    ):
        assert _user_says(home, src, body) == [], body

    # The control, because a guard that ate every `keep` would pass the above.
    assert _user_says(home, src, "Keep  it as YAML.") == ["directive"]


def test_a_directive_typed_with_extra_spaces_is_still_a_directive(home, src):
    """`_PROHIBIT` admits at most three characters between a coordinator and the
    contraction, which is a real bound — the alternative is `don't` anywhere in
    the block, and on real sessions that is a complaint 18 times in 21. Four
    spaces is still a person typing a rule.
    """
    assert _user_says(home, src, "please    don't push to main") == ["directive"]
    assert _user_says(home, src, "and\t\tdon't touch the vendored tree") == ["directive"]
