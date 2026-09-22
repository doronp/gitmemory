"""Tests for the fixture dump and the generator-free scorer."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bench import fixture, gate
from gitmemory import adapters

ROOT = Path(__file__).resolve().parents[1]


def small_fixture(tmp_path: Path, n: int = 4) -> Path:
    out = tmp_path / "fx"
    fixture.dump(out, seed=42, split="dev", n=n)
    return out


def transcript(path: Path, turns: list[tuple[str, str, str]]) -> Path:
    """A minimal Claude-Code transcript: (role, kind, text) per turn.

    Written by hand rather than taken from the dump — these tests are about the
    scorer meeting real runner output, which the corpus does not contain.
    """
    lines, parent = [], None
    for i, (role, kind, text) in enumerate(turns):
        uuid = f"u{i}"
        if kind == "tool_result":
            message = {"role": "user", "content": [{"type": "tool_result", "content": text}]}
        elif role == "user":
            message = {"role": "user", "content": text}
        else:
            message = {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": text}],
            }
        lines.append(
            json.dumps(
                {
                    "cwd": "/w",
                    "message": message,
                    "parentUuid": parent,
                    "sessionId": "s0",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "type": role,
                    "uuid": uuid,
                    "version": "1.2.0",
                }
            )
        )
        parent = uuid
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_dumped_gold_ref_names_a_block_that_is_in_the_dumped_bytes(tmp_path):
    """The fixture is two files that have to agree. If the transcripts were
    written from one generation and the labels from another, every source_ref
    would still look plausible and nothing downstream could tell."""
    out = small_fixture(tmp_path)
    gold = json.loads((out / "gold.json").read_text())
    assert gold, "the dump produced no labels at all"

    for stem, decisions in gold.items():
        session = adapters.get("claude-code").parse(str(out / "sessions" / f"{stem}.jsonl"))
        present = {b.block_id for turn in session.turns for b in turn.blocks}
        for kind, ref in decisions:
            assert kind in ("directive", "reversal"), kind
            assert ref in present, f"{stem}: gold ref {ref} is in no block of the dumped session"


def test_the_gold_labels_carry_no_hint_of_how_the_corpus_was_made(tmp_path):
    """The fixture exists to keep the extractor's author blind. A stray field —
    a template id, a session index, a family name — would hand back exactly
    what removing the generator was meant to withhold."""
    out = small_fixture(tmp_path)
    gold = json.loads((out / "gold.json").read_text())
    for decisions in gold.values():
        for entry in decisions:
            assert len(entry) == 2, f"a third field appeared in the label: {entry}"
    assert sorted((out).iterdir()) == sorted([out / "gold.json", out / "sessions"])


def test_a_perfect_extractor_scores_one_and_an_empty_one_scores_zero(tmp_path):
    """Pins both ends of the scorer against a real dump. A scorer that silently
    matched nothing would look identical to an extractor that found nothing."""
    out = small_fixture(tmp_path)
    gold = json.loads((out / "gold.json").read_text())
    by_ref = {ref: kind for decisions in gold.values() for kind, ref in decisions}

    def perfect(session):
        return [
            (by_ref[b.block_id], b.block_id)
            for turn in session.turns
            for b in turn.blocks
            if b.block_id in by_ref
        ]

    score = gate.score_fixture(out, perfect)
    assert score["precision"] == 1.0 and score["recall"] == 1.0
    assert score["passed"] is True
    assert score["gold"] == sum(len(v) for v in gold.values())
    assert set(score["slices"]) == set(gate.SLICES)
    assert sum(s["gold"] for s in score["slices"].values()) == score["gold"], (
        "a gold decision fell outside every slice"
    )

    empty = gate.score_fixture(out, lambda session: [])
    assert empty["precision"] == 0.0 and empty["recall"] == 0.0
    assert empty["passed"] is False


def test_a_prediction_cannot_satisfy_gold_planted_in_another_transcript(tmp_path):
    """block_id is content-derived, so a fork-from-compaction — two files with
    the same session id replaying the same turn — gives two files the same ids.
    Pooled flat, an extractor that found nothing in the labelled file scored a
    perfect pass off the other one."""
    out = tmp_path / "fx"
    (out / "sessions").mkdir(parents=True)
    src = small_fixture(tmp_path / "src")
    gold = json.loads((src / "gold.json").read_text())
    stem = next(s for s in sorted(gold) if gold[s])
    body = (src / "sessions" / f"{stem}.jsonl").read_bytes()
    (out / "sessions" / "000.jsonl").write_bytes(body)
    (out / "sessions" / "001.jsonl").write_bytes(body)
    (out / "gold.json").write_text(json.dumps({"000": gold[stem], "001": []}))

    calls = []

    def only_the_unlabelled_file(session):
        calls.append(1)
        return [tuple(p) for p in gold[stem]] if len(calls) == 2 else []

    score = gate.score_fixture(out, only_the_unlabelled_file)
    assert score["matched"] == 0, "a prediction against 001 was credited to gold in 000"
    assert score["passed"] is False


def test_a_green_test_run_is_not_a_failure_and_a_red_one_is(tmp_path):
    """The two reversal slices are split on this predicate. pytest, jest and npm
    all print "0 failed" when they pass, and pytest's real failure marker is
    uppercase FAILED with exit code 2 — the substring test this replaced called
    every green run a failure and every pytest failure a success."""
    cases = [
        ("Exit code 0\n15 passed, 0 failed in 0.05s", False),
        ("Tests:  3 passed, 0 failed, 3 total", False),
        ("Exit code 2\nFAILED tests/test_x.py::test_y - AssertionError", True),
        ("Exit code 1\ncommand failed", True),
        ("Traceback (most recent call last):\n  File x\nValueError", True),
        ("2 failed, 3 passed", True),
    ]
    for i, (text, expected) in enumerate(cases):
        path = transcript(
            tmp_path / f"t{i}.jsonl",
            [("user", "text", "go"), ("user", "tool_result", text), ("assistant", "text", "ok")],
        )
        session = adapters.get("claude-code").parse(str(path))
        last = [b.block_id for turn in session.turns for b in turn.blocks][-1]
        assert gate.post_failure_blocks(session)[last] is expected, text


def test_predictions_the_slices_cannot_hold_are_reported(tmp_path):
    """A `kind` outside the vocabulary lands in no bucket. Silently dropped, the
    breakdown read three perfect slices under a gate of 0.20 with nothing saying
    that eight of ten predictions were not shown."""
    gold = [("directive", "aa"), ("reversal", "bb")]
    preds = gold + [("decision", f"x{i}") for i in range(8)]
    score = gate.score_with_slices(preds, gold, {"bb": True})

    assert score["predicted"] == 10
    assert sum(s["predicted"] for s in score["slices"].values()) == 2
    assert score["unslotted"] == {"predicted": 8, "gold": 0}
    assert "outside every slice" in gate.format_report(score)


def test_a_transcript_with_no_label_is_refused_rather_than_skipped(tmp_path):
    """An unlabelled .jsonl was never parsed and never scored. A dump half
    written by an interrupted run would have scored against the half that
    landed, and reported a clean number for it."""
    out = small_fixture(tmp_path)
    (out / "sessions" / "999.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="unlabelled"):
        gate.score_fixture(out, lambda session: [])


def test_a_decision_that_is_neither_a_pair_nor_an_object_is_named(tmp_path):
    """A str is a sequence of two characters and a dict is subscriptable, so
    both came back from the scorer as nonsense or a KeyError raised three frames
    from the extractor that caused it."""
    for bad in ("directive", {"kind": "directive", "source_ref": "aa"}, ("a", "b", "c")):
        with pytest.raises(TypeError, match="must be"):
            gate.score_predictions([bad], [("directive", "aa")])


def test_a_slice_with_no_gold_does_not_read_as_a_failed_one(tmp_path):
    """P 0.0000 R 0.0000 on an empty slice looks identical to total failure on a
    populated one, and the three slices are very different sizes."""
    score = gate.score_with_slices([], [("directive", "aa")], {})
    report = gate.format_report(score)
    assert "no gold in this slice" in report
    assert report.count("no gold in this slice") == 2, "directives has gold; the other two do not"


def test_a_derive_that_fails_to_import_is_not_reported_as_unwritten(tmp_path, monkeypatch):
    """`except ImportError` around the import reported "no decisions yet" for
    any failure inside derive — a missing numpy, a typo in a dependency — and
    swallowed the cause.

    `__file__` is a real string rather than another ImportError, and that is the
    whole difference between this test and the one it was. Raising on every
    attribute meant the E5:R8 line above the check — `print(f"scoring
    {derive.__file__}")`, which landed later — tripped first, so the check this
    test is named for was never reached. The mutation harness found it: reverting
    the check left the test green. Only `decisions` raises now. [E7 pair review]
    """
    out = small_fixture(tmp_path)

    class Broken:
        __file__ = "<broken>"

        def __getattr__(self, name):
            raise ImportError("numpy is required for LexRank and is not installed")

    import gitmemory
    import gitmemory.derive  # noqa: F401 - so there is an attribute to replace

    # Both, because `from gitmemory import derive` takes the package attribute
    # when the submodule is already imported and never looks at sys.modules.
    monkeypatch.setattr(gitmemory, "derive", Broken())
    monkeypatch.setitem(sys.modules, "gitmemory.derive", Broken())
    with pytest.raises(ImportError, match="numpy"):
        gate.main([str(out)])


def test_the_report_names_the_file_it_scored(tmp_path, capsys):
    """Run from a worktree, `python -m bench.gate` resolves `gitmemory` through
    the editable install — the main checkout — so an author can iterate on their
    own extractor all afternoon while the gate scores a different file. The
    extractor's author hit this and caught it by hand."""
    from gitmemory import derive

    out = small_fixture(tmp_path)
    gate.main([str(out)])
    assert derive.__file__ in capsys.readouterr().err


def test_the_scorer_runs_with_the_corpus_generator_unimportable(tmp_path):
    """The blindness is the point, and it is structural or it is nothing. An
    extractor author gets `bench/gate.py` and a dump; if the scorer reaches for
    `bench/decisions.py` the templates come back with it and the gate stops
    measuring anything."""
    out = small_fixture(tmp_path)
    script = textwrap.dedent(f"""
        import sys

        class Blocked:
            def find_spec(self, name, path=None, target=None):
                if name == "bench.decisions":
                    raise ImportError("bench.decisions is not available to the scorer")
                return None

        sys.meta_path.insert(0, Blocked())
        from pathlib import Path
        from bench import gate
        score = gate.score_fixture(Path({str(out)!r}), lambda session: [])
        assert "bench.decisions" not in sys.modules, "the scorer imported the generator"
        print(score["gold"])
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": f"{ROOT}:{ROOT / 'src'}", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) > 0, "the fixture scored against no gold at all"
