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
import re
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
    `Decision` type and is not obliged to import ours.

    A `str` is a sequence of two characters and a dict is subscriptable, so
    both used to come back as nonsense pairs or a `KeyError` from inside the
    scorer. An extractor author gets told what they returned instead. [E5:R2]
    """
    if hasattr(d, "kind") and hasattr(d, "source_ref"):
        return (d.kind, d.source_ref)
    if isinstance(d, (tuple, list)) and len(d) == 2:
        return (d[0], d[1])
    raise TypeError(
        "a decision must be (kind, source_ref) or have those attributes, "
        f"got {type(d).__name__}: {d!r}"
    )


def scoped(decisions: Sequence[Any], scope: str) -> list[tuple[str, str]]:
    """Decision keys namespaced to one transcript.

    `block_id` is content-derived, so two transcripts that share a session id
    and replay a turn verbatim — which is what a fork-from-compaction is, and
    the adapter says so itself — share block ids. Pooled flat, a prediction
    against file B then satisfies gold planted in file A: measured at 1.0/1.0
    on a run that found nothing. The scope is the file it was read out of.
    [E5:R1]
    """
    return [(kind, f"{scope}\x00{ref}") for kind, ref in (_pair(d) for d in decisions)]


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


# Exit code first, because it is the one unambiguous thing a runner reports.
# The word tests are the fallback for output that has no exit line, and they
# have to survive the sentence a *green* run prints: pytest, jest and npm all
# say "0 failed" on success, and the substring test that used to be here called
# every one of them a failure while missing pytest's real marker — uppercase
# FAILED with exit code 2. The two reversal slices swapped on real output.
# [E5:R3]
_EXIT_CODE = re.compile(r"exit code[: ]+(\d+)", re.IGNORECASE)
_COUNTED = re.compile(r"\b(?!0\b)\d+ (?:failed|failures|errors)\b", re.IGNORECASE)
# Case-sensitive, and that is the whole point of keeping it separate: pytest's
# marker is uppercase FAILED, and matching it case-insensitively makes it fire
# on the "0 failed" in every green run — which is the bug this replaced.
_FAILED_MARKER = re.compile(r"\bFAILED\b")
_FAILURE_PHRASE = re.compile(
    r"\bfatal\b|Traceback \(most recent call last\)|\b(?:command|build|test)s? failed\b",
    re.IGNORECASE,
)


def looks_failed(block: Any) -> bool:
    """Whether a tool_result block reports a failure.

    `native` is the adapter's verbatim record, and Claude Code puts `is_error`
    on a tool result that failed — so the structural answer is taken when it is
    there and the text is only read when it is not.

    ponytail: the text fallback is a heuristic over free-form runner output and
    always will be. The upgrade path is more adapters carrying an explicit
    error flag, not more regexes.
    """
    flag = block.native.get("is_error") if isinstance(block.native, dict) else None
    if isinstance(flag, bool):
        return flag
    m = _EXIT_CODE.search(block.text)
    if m:
        return m.group(1) != "0"
    return bool(
        _COUNTED.search(block.text)
        or _FAILED_MARKER.search(block.text)
        or _FAILURE_PHRASE.search(block.text)
    )


def post_failure_blocks(session: Session) -> dict[str, bool]:
    """Which blocks sit in the first assistant turn after a failed tool result.

    Read off the session, not off the generator, so the slice split means the
    same thing on a real transcript as it does on the corpus.
    """
    post_failure_turns = set()
    for idx, turn in enumerate(session.turns):
        if turn.role != "user":
            continue
        if not any(b.kind == "tool_result" and looks_failed(b) for b in turn.blocks):
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
    # Anything the three slices do not hold. A prediction with a `kind` outside
    # the vocabulary lands in no bucket, and the breakdown then read three
    # perfect slices under a gate of 0.20 with nothing saying that 8 of 10
    # predictions were not shown. Counted here and printed when non-zero.
    # [E5:R4]
    overall["unslotted"] = {
        "predicted": len(predicted) - sum(len(b["preds"]) for b in buckets.values()),
        "gold": len(gold) - sum(len(b["gold"]) for b in buckets.values()),
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

    # A transcript with no entry in `gold.json` was never parsed and never
    # scored, and nothing said so — a dump half-written by an interrupted run
    # would quietly score against the half that landed. [E5:R5]
    on_disk = {p.stem for p in (fixture_dir / "sessions").glob("*.jsonl")}
    if on_disk != set(gold_by_stem):
        raise ValueError(
            f"{fixture_dir}: sessions/ and gold.json disagree — "
            f"unlabelled {sorted(on_disk - set(gold_by_stem))}, "
            f"missing {sorted(set(gold_by_stem) - on_disk)}"
        )

    all_gold: list[Any] = []
    all_preds: list[Any] = []
    is_pf: dict[str, bool] = {}

    for stem in sorted(gold_by_stem):
        session = adapter.parse(str(fixture_dir / "sessions" / f"{stem}.jsonl"))
        all_gold.extend(scoped(gold_by_stem[stem], stem))
        all_preds.extend(scoped(extractor(session), stem))
        is_pf.update({f"{stem}\x00{k}": v for k, v in post_failure_blocks(session).items()})

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
        # A slice with no gold is not a slice scored zero, and printing 0.0000
        # for both said it was. [E5:R6]
        figures = (
            "no gold in this slice"
            if not s["gold"]
            else f"P {s['precision']:.4f}  R {s['recall']:.4f}"
        )
        lines.append(
            f"  {name:<24} {figures:<22} "
            f"matched {s['matched']:<5} predicted {s['predicted']:<5} gold {s['gold']}"
        )
    left = score.get("unslotted", {"predicted": 0, "gold": 0})
    if left["predicted"] or left["gold"]:
        lines += [
            "",
            f"  outside every slice        predicted {left['predicted']}  gold {left['gold']} "
            "— a `kind` the breakdown does not know",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fixture_dir = Path(argv[0]) if argv else Path("bench/fixture-dev")
    if not (fixture_dir / "gold.json").exists():
        print(f"no fixture at {fixture_dir} — see bench/fixture.py", file=sys.stderr)
        return 2

    # Import the module and look, rather than catching ImportError around the
    # `from`: any import failure inside `derive` — a missing numpy, a typo in a
    # dependency — raised ImportError too, and got reported as "no decisions
    # yet" with the real cause swallowed. [E5:R7]
    from gitmemory import derive

    if not hasattr(derive, "decisions"):
        print(
            "gitmemory.derive has no `decisions(session) -> list[Decision]` yet — "
            "that is the thing being scored",
            file=sys.stderr,
        )
        return 2

    print(format_report(score_fixture(fixture_dir, derive.decisions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
