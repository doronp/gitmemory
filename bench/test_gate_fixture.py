"""Tests for the fixture dump and the generator-free scorer."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from bench import fixture, gate
from gitmemory import adapters

ROOT = Path(__file__).resolve().parents[1]


def small_fixture(tmp_path: Path, n: int = 4) -> Path:
    out = tmp_path / "fx"
    fixture.dump(out, seed=42, split="dev", n=n)
    return out


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
