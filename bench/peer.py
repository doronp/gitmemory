# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""LongMemEval-S session retrieval, scored the way the peers score it. [E9]

`score.py` retrieves turns from a transcript padded with fabricated tool noise;
the published numbers rank sessions over the raw dataset. This replays each
instance with `plain=True` (no tool noise) through the shipped pipeline, maps
the ranked turns to their sessions in rank order, and scores the session list
under one of two protocols, each copied from the code that produced the
numbers we compare against:

- `official`: xiaowu0162/LongMemEval `src/retrieval/`. Abstention questions are
  skipped, gold is an answer session with at least one `has_answer` user turn
  (others are renamed `noans`, run_retrieval.py:209), a question with no gold
  is skipped (run_retrieval.py:396-402), and NDCG is eval_utils.py's, whose DCG
  discounts ranks 1 and 2 alike and whose ideal comes from every gold session.
- `mempalace`: MemPalace/mempalace `benchmarks/longmemeval_bench.py:53-80`. All
  500 questions, gold = `answer_session_ids`, and an NDCG whose ideal is the
  retrieved top k re-sorted, which can only read higher.

recall_any@k and recall_all@k are the same code in both.

    python -m bench.peer --dataset "$GITMEMORY_LONGMEMEVAL" --split dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile

from gitmemory.adapters import claude_code as cc

from . import arms
from .longmemeval import Instance
from .longmemeval import load as load_dataset
from .score import map_offset_to_turn
from .synth import to_transcript

ARMS = ("candidate", "dense", "rerank", "rerank12", "hybrid")


def split_of(question_id: str) -> str:
    """Fixed forever: tuning only ever sees `dev`, and `test` is what is reported."""
    return "dev" if int(hashlib.sha256(question_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


def gold(inst: Instance, protocol: str) -> list[str] | None:
    """The sessions a question is scored against, or None if the protocol skips it."""
    if protocol == "mempalace":
        return list(inst.answer_session_ids)
    if inst.question_id.endswith("_abs"):
        return None
    answer = set(inst.answer_session_ids)
    found = [
        sid
        for sid, sess in zip(inst.haystack_session_ids, inst.haystack_sessions, strict=True)
        if sid in answer and any(t.role == "user" and t.has_answer for t in sess)
    ]
    return found or None


def _dcg_official(rels: list[float]) -> float:
    # eval_utils.py:4-9: rel[0] + sum(rel[i] / log2(i + 1)) for i >= 1, so ranks 1, 2 tie.
    return rels[0] + sum(r / math.log2(i) for i, r in enumerate(rels[1:], start=2)) if rels else 0.0


def _dcg(rels: list[float]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def metrics(
    ranked: list[str], gold_ids: list[str], k: int, protocol: str
) -> tuple[float, float, float]:
    """(recall_any@k, recall_all@k, ndcg@k) of a ranked session list."""
    top = ranked[:k]
    rels = [1.0 if s in gold_ids else 0.0 for s in top]
    if protocol == "official":
        ideal = _dcg_official([1.0] * min(len(set(gold_ids)), k))
        ndcg = _dcg_official(rels) / ideal if ideal else 0.0
    else:
        ideal = _dcg(sorted(rels, reverse=True))
        ndcg = _dcg(rels) / ideal if ideal else 0.0
    return float(any(g in top for g in gold_ids)), float(all(g in top for g in gold_ids)), ndcg


def ranked_sessions(offsets: list[int], starts: list[int], turns: list) -> list[str]:
    """Turn offsets to session ids, first occurrence wins, offsets matching no turn dropped."""
    out: dict[str, None] = {}
    for off in offsets:
        turn = map_offset_to_turn(off, starts, turns)
        if turn is not None:
            out.setdefault(turn.session_id)
    return list(out)


def run(path: str, arm_names: list[str], ks: list[int], split: str, protocol: str,
        depth: int, limit: int | None = None) -> dict:
    sums = {a: {f"{m}@{k}": 0.0 for k in ks for m in ("recall_any", "recall_all", "ndcg")}
            for a in arm_names}
    n = skipped = 0
    for inst in load_dataset(path):
        if split != "all" and split_of(inst.question_id) != split:
            continue
        gold_ids = gold(inst, protocol)
        if gold_ids is None:
            skipped += 1
            continue
        if limit is not None and n >= limit:
            break
        n += 1
        tx = to_transcript(inst, seed=42, compaction=None, plain=True)
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as tmp:
            tmp.write(tx.bytes_data)
            tmp.flush()
            turns = sorted(cc.parse(tmp.name).turns, key=lambda t: t.byte_offset)
        starts = [t.byte_offset for t in turns]
        for name in arm_names:
            factory = getattr(arms, f"{'gitmemory' if name == 'candidate' else name}_factory")
            retriever = factory(inst.question_id, tx.bytes_data)
            try:
                ranked = ranked_sessions(retriever(inst.question, depth), starts, turns)
            finally:
                if hasattr(retriever, "close"):
                    retriever.close()
            for k in ks:
                for m, v in zip(("recall_any", "recall_all", "ndcg"),
                                metrics(ranked, gold_ids, k, protocol), strict=True):
                    sums[name][f"{m}@{k}"] += v
    return {"n": n, "skipped": skipped, "split": split, "protocol": protocol, "depth": depth,
            "metrics": {a: {m: v / n for m, v in s.items()} for a, s in sums.items()} if n else {}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.peer")
    ap.add_argument("--dataset", default=os.environ.get("GITMEMORY_LONGMEMEVAL"))
    ap.add_argument("--arms", default="candidate")
    ap.add_argument("--k", default="1,3,5,10")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--protocol", choices=("official", "mempalace"), default="official")
    ap.add_argument("--depth", type=int, default=200, help="turns retrieved before session dedupe")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    arm_names = args.arms.split(",")
    if not args.dataset or not os.path.exists(args.dataset) or set(arm_names) - set(ARMS):
        print(f"error: need --dataset and --arms from {','.join(ARMS)}", file=sys.stderr)
        return 1
    ks = [int(k) for k in args.k.split(",")]
    res = run(args.dataset, arm_names, ks, args.split, args.protocol, args.depth, args.limit)
    print(f"# LongMemEval-S sessions — {args.protocol} protocol, {args.split}, n = {res['n']}"
          f" ({res['skipped']} skipped by the protocol), depth {args.depth} turns")
    print()
    cols = [f"{m}@{k}" for k in ks for m in ("recall_any", "recall_all", "ndcg")]
    print("| Arm | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name in arm_names:
        print(f"| {name} | " + " | ".join(f"{res['metrics'][name][c]:.4f}" for c in cols) + " |")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
