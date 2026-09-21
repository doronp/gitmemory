"""Scoring for the decision gate, and a runner that needs no generator.

Split out of `bench/decisions.py` for one reason: an extractor developed with
the corpus generator open is fitted to a generator rather than to the task, and
the gate it then passes measures nothing. This module scores decisions and
knows nothing about how the corpus was made, so it can be handed to an author
who has not read `decisions.py` — together with a frozen dump of the dev split
(`bench/fixture.py`) and nothing else.

`decisions.py` imports the scorer from here, so there is one implementation
rather than two that can drift.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from gitmemory import adapters
from gitmemory.records import Session

# Pre-registered before anything was measured. docs/DESIGN.md is the record.
PRECISION_FLOOR = 0.85
RECALL_FLOOR = 0.60

SLICES = ("directives", "post_failure_reversals", "other_reversals")


def _pair(d: Any) -> tuple[str, str]:
    """A decision as (kind, source_ref). Accepts any object with those two
    attributes, or a plain pair — the extractor under test defines its own
    `Decision` type and is not obliged to import ours."""
    if hasattr(d, "kind") and hasattr(d, "source_ref"):
        return (d.kind, d.source_ref)
    return (d[0], d[1])


def score_predictions(predicted: Sequence[Any], gold: Sequence[Any]) -> dict[str, Any]:
    """Strict one-to-one matched micro-averaged scorer.

    One-to-one matters: without it, predicting the same correct decision twice
    would be scored twice and precision would reward repetition.
    """
    gold_counts = Counter(_pair(g) for g in gold)
    matched = 0
    for p in predicted:
        key = _pair(p)
        if gold_counts[key] > 0:
            matched += 1
            gold_counts[key] -= 1

    n_pred = len(predicted)
    n_gold = len(gold)
    precision = matched / n_pred if n_pred else 0.0
    recall = matched / n_gold if n_gold else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "passed": precision >= PRECISION_FLOOR and recall >= RECALL_FLOOR,
        "matched": matched,
        "predicted": n_pred,
        "gold": n_gold,
    }


def post_failure_blocks(session: Session) -> dict[str, bool]:
    """Which blocks sit in the first assistant turn after a failed tool result.

    Read off the session, not off the generator, so the slice split means the
    same thing on a real transcript as it does on the corpus.
    """
    post_failure_turns = set()
    for idx, turn in enumerate(session.turns):
        if turn.role != "user":
            continue
        if not any(
            b.kind == "tool_result" and ("failed" in b.text or "Exit code 1" in b.text)
            for b in turn.blocks
        ):
            continue
        for later in session.turns[idx + 1 :]:
            if later.role == "assistant":
                post_failure_turns.add(later.uuid or later.byte_offset)
                break

    out: dict[str, bool] = {}
    for turn in session.turns:
        is_pf = (turn.uuid or turn.byte_offset) in post_failure_turns
        for block in turn.blocks:
            out[block.block_id] = is_pf
    return out


def get_decision_slice(d: Any, block_is_post_failure: dict[str, bool]) -> str:
    """Which population a decision belongs to, for the per-slice breakdown."""
    kind, source_ref = _pair(d)
    if kind == "directive":
        return "directives"
    if kind == "reversal":
        if block_is_post_failure.get(source_ref, False):
            return "post_failure_reversals"
        return "other_reversals"
    return "other"


def score_with_slices(
    predicted: Sequence[Any], gold: Sequence[Any], block_is_post_failure: dict[str, bool]
) -> dict[str, Any]:
    """The overall score, plus the same score computed within each slice.

    The overall figure is the gate. The breakdown is there because a
    micro-average over three populations lets an easy one carry a hard one:
    every baseline in this benchmark scores zero on two of the three.
    """
    overall = score_predictions(predicted, gold)
    buckets: dict[str, dict[str, list[Any]]] = {name: {"preds": [], "gold": []} for name in SLICES}
    for g in gold:
        sl = get_decision_slice(g, block_is_post_failure)
        if sl in buckets:
            buckets[sl]["gold"].append(g)
    for p in predicted:
        sl = get_decision_slice(p, block_is_post_failure)
        if sl in buckets:
            buckets[sl]["preds"].append(p)

    overall["slices"] = {
        name: {k: v for k, v in score_predictions(b["preds"], b["gold"]).items() if k != "passed"}
        for name, b in buckets.items()
    }
    return overall


def score_fixture(fixture_dir: Path, extractor: Callable[[Session], list[Any]]) -> dict[str, Any]:
    """Score an extractor against a dumped split.

    A fixture is `gold.json` — `{stem: [[kind, source_ref], ...]}` — beside a
    `sessions/` directory of `.jsonl` transcripts. Nothing else, and in
    particular nothing that says how any of it was generated.
    """
    gold_by_stem = json.loads((fixture_dir / "gold.json").read_text())
    adapter = adapters.get("claude-code")

    all_gold: list[Any] = []
    all_preds: list[Any] = []
    is_pf: dict[str, bool] = {}

    for stem in sorted(gold_by_stem):
        session = adapter.parse(str(fixture_dir / "sessions" / f"{stem}.jsonl"))
        all_gold.extend(tuple(pair) for pair in gold_by_stem[stem])
        all_preds.extend(extractor(session))
        is_pf.update(post_failure_blocks(session))

    return score_with_slices(all_preds, all_gold, is_pf)


def format_report(score: dict[str, Any]) -> str:
    lines = [
        f"precision {score['precision']:.4f}   recall {score['recall']:.4f}   "
        f"matched {score['matched']}  predicted {score['predicted']}  gold {score['gold']}",
        f"gate (P >= {PRECISION_FLOOR}, R >= {RECALL_FLOOR}): "
        f"{'PASS' if score['passed'] else 'FAIL'}",
        "",
    ]
    for name in SLICES:
        s = score["slices"][name]
        lines.append(
            f"  {name:<24} P {s['precision']:.4f}  R {s['recall']:.4f}  "
            f"matched {s['matched']:<5} predicted {s['predicted']:<5} gold {s['gold']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fixture_dir = Path(argv[0]) if argv else Path("bench/fixture-dev")
    if not (fixture_dir / "gold.json").exists():
        print(f"no fixture at {fixture_dir} — see bench/fixture.py", file=sys.stderr)
        return 2

    try:
        from gitmemory.derive import decisions
    except ImportError:
        print(
            "gitmemory.derive has no `decisions(session) -> list[Decision]` yet — "
            "that is the thing being scored",
            file=sys.stderr,
        )
        return 2

    print(format_report(score_fixture(fixture_dir, decisions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
