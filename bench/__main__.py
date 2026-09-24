"""Benchmark sweep runner and reporter."""

from __future__ import annotations

import argparse
import functools
import importlib.util
import os
import sys

from bench import arms as arms_mod
from bench import longmemeval as lm
from bench import score
from bench.arms import gitmemory_factory
from gitmemory.index import Weights


def _weights(text: str) -> Weights:
    parts = text.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected prose,tool_use,tool_result,paths")
    try:
        prose, tool_use, tool_result, paths = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not four numbers: {exc}") from exc
    return Weights(prose=prose, tool_use=tool_use, tool_result=tool_result, paths=paths)


def _optional_arms() -> tuple[dict[str, score.Factory], list[str]]:
    """The arms whose dependencies are an extra, and a line each for the ones missing.

    Reported rather than skipped in silence: which arms ran is part of what a
    report means, and "BM25 was the best arm" reads very differently once you
    know the dense arm was never installed. [E3]

    The probe is the third-party module, not the factory symbol. Importing
    `bench.arms` always succeeds — the factories import numpy and flashrank
    lazily inside themselves — so probing the symbol registered an arm that then
    raised `ImportError` on the first instance and took the whole sweep down
    with it, 470 instances in. Found by running it. [E3]
    """
    needs = {"dense": ("numpy", "model2vec"), "rerank": ("flashrank",)}
    arms: dict[str, score.Factory] = {}
    skipped: list[str] = []
    for name, modules in needs.items():
        missing = [m for m in modules if importlib.util.find_spec(m) is None]
        if missing:
            skipped.append(f"{name}: no {', '.join(missing)} (pip install 'gitmemory[hybrid]')")
            continue
        arms[name] = getattr(arms_mod, f"{name}_factory")
    return arms, skipped


def _table(header: list[str], rows: list[list[str]]) -> None:
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def _metric_cells(m: dict[str, float]) -> list[str]:
    return [
        f"{m['turn_recall']:.4f}",
        f"{m['turn_hit']:.4f}",
        f"{m['turn_recall_all']:.4f}",
        f"{m['turn_mrr']:.4f}",
        f"{m['session_recall']:.4f}",
        f"{m['session_hit']:.4f}",
        f"{m['session_recall_all']:.4f}",
        f"{m['session_mrr']:.4f}",
        f"{m['unmatched_per_query']:.2f}",
    ]


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark sweep and print a report."""
    ap = argparse.ArgumentParser(prog="python -m bench")
    ap.add_argument("--dataset", default=os.environ.get("GITMEMORY_LONGMEMEVAL"))
    ap.add_argument("--k", type=int, default=10, help="retrieval budget")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=0, help="first N instances; 0 means all")
    ap.add_argument(
        "--compaction",
        action="append",
        choices=["none", "before_evidence", "after_evidence", "every_n"],
        help="repeatable; defaults to all four",
    )
    ap.add_argument(
        "--weights", type=_weights, default=None, help="prose,tool_use,tool_result,paths"
    )
    args = ap.parse_args(argv)

    if not args.dataset:
        print("error: pass --dataset or set GITMEMORY_LONGMEMEVAL", file=sys.stderr)
        return 1
    if not os.path.exists(args.dataset):
        print(f"error: dataset path {args.dataset!r} does not exist", file=sys.stderr)
        return 1

    print(f"loading LongMemEval from {args.dataset}", file=sys.stderr)
    instances = list(lm.load(args.dataset))
    if args.limit:
        instances = instances[: args.limit]
    print(f"loaded {len(instances)} instances", file=sys.stderr)

    modes = (
        [None if c == "none" else c for c in args.compaction]
        if args.compaction
        else score.COMPACTION_MODES
    )
    candidate = (
        functools.partial(gitmemory_factory, weights=args.weights)
        if args.weights
        else gitmemory_factory
    )
    extra, skipped = _optional_arms()
    for line in skipped:
        print(f"arm not available — {line}", file=sys.stderr)

    results = score.score(
        instances,
        candidate,
        k=args.k,
        seed=args.seed,
        compaction_modes=modes,
        extra_arms=extra,
    )

    # Calibration first, and the scores only if it held. A report that prints
    # the candidate's numbers above a failed gate is a report whose numbers get
    # quoted without the caveat; refusing to print them is the only version of
    # "the gate failed" that survives a copy-paste. [E3]
    print("# LongMemEval benchmark")
    print()
    print(
        f"k = {results['k']} · seed = {results['seed']} · "
        f"instances = {results['num_instances_evaluated']} · arms = {', '.join(results['arms'])}"
    )
    if skipped:
        for line in skipped:
            print(f"- arm not available — {line}")
    print()
    print("## Calibration")
    print()
    _table(
        ["Check", "Compaction", "Passed", "Detail"],
        [
            [
                c["check"],
                "none" if c["compaction"] is None else c["compaction"],
                "yes" if c["passed"] else "**NO**",
                c["detail"],
            ]
            for c in results["calibration_checks"]
        ],
    )

    if not results["calibration_passed"]:
        print("**Calibration failed. Scores withheld: they would not mean anything.**")
        print()
        for c in results["calibration_checks"]:
            if not c["passed"]:
                print(f"- {c['check']}: {c['detail']}")
        print("calibration failed", file=sys.stderr)
        return 1

    print("Calibration passed.")
    print()
    # `Hit@k` and `All@k` bracket `Turn recall` deliberately: the fractional
    # mean in the middle is ours, the two either side are what other systems
    # publish, and printing them adjacent is what stops a reader comparing a
    # fractional recall against someone else's hit rate.
    cols = [
        "Turn recall",
        f"Hit@{results['k']}",
        f"All@{results['k']}",
        "Turn MRR",
        "Session recall",
        f"S-Hit@{results['k']}",
        f"S-All@{results['k']}",
        "Session MRR",
        "Unmatched/query",
    ]

    print("## Overall")
    print()
    _table(
        ["Compaction", "Arm", *cols],
        [
            ["none" if comp is None else comp, arm, *_metric_cells(m)]
            for comp in results["overall_metrics"]
            for arm, m in results["overall_metrics"][comp].items()
        ],
    )

    print("## By question type")
    print()
    _table(
        ["Compaction", "Question type", "Arm", *cols],
        [
            [
                "none" if e["compaction"] is None else e["compaction"],
                e["question_type"],
                arm,
                *_metric_cells(e[arm]),
            ]
            for e in results["breakdowns"]
            for arm in results["arms"]
        ],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
