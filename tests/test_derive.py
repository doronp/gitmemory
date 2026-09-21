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
    """
    gitrepo.init(home)
    ignore = Path(home, ".gitignore").read_text()
    assert "/index/" in ignore, "the fixture is checking the wrong file"
    assert "derived" not in ignore


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
