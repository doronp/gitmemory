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

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gitmemory import derive, gitrepo, index, store
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
    for name in ("ideas.json", "timeline.json"):
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


def test_a_write_failure_costs_one_generation_and_leaves_nothing_torn(
    home, src, tmp_path, monkeypatch
):
    """Both `_write` calls sat outside the `try`: a failure on the second aborted
    the whole build with no skip entry, and left a fresh ideas.json beside a
    stale timeline.json. [E5:7]
    """
    other = tmp_path / "src" / "other.jsonl"
    write(src, conversation())
    write(str(other), conversation())
    store.capture(src, "claude-code", "sess-a", home=home)
    store.capture(str(other), "claude-code", "sess-b", home=home)

    real = derive._write

    def fail_on_the_second_write(path, payload):
        if "sess-a" in path and path.endswith("timeline.json"):
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
