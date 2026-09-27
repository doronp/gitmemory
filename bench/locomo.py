"""LoCoMo evidence-retrieval benchmark, run through the shipped pipeline. [E9]

LoCoMo (snap-research/locomo, CC BY-NC 4.0) is never vendored: `fetch_locomo.sh`
downloads a pinned revision into a temp directory, and only scores are
published. Each conversation is replayed as one Claude Code transcript, the
candidate arms retrieve turns for each question, and the score is whether the
annotated evidence turns (`dia_id`s) came back.

Scored the way the published retrieval numbers are: categories 1-4 only
(category 5 is the adversarial set, which has no answer to retrieve), turn
budget k, recall = fraction of a question's evidence turns in the top k,
averaged over questions. Hit (any) and All are reported beside it for the
reason `score.py` reports them for LongMemEval: which one a peer publishes is
rarely stated, and they differ by tens of points.

    python -m bench.locomo --dataset "$GITMEMORY_LOCOMO"
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

SCORED_CATEGORIES = (1, 2, 3, 4)
_SESSION_KEY = re.compile(r"session_(\d+)")
# `D1:3`, and the three malformed spellings the pinned release actually carries:
# several ids in one string (`D8:6; D9:17`, `D9:1 D4:4`), a stray colon
# (`D:11:26`), and a zero-padded turn (`D30:05`). Repaired rather than dropped:
# each is unambiguous, and dropping them removes nine evidence turns from the
# questions that are hardest to answer (they are the multi-hop ones).
_DIA_ID = re.compile(r"D:?(\d+):0*(\d+)")


@dataclass(frozen=True)
class Question:
    conversation: str
    question: str
    answer: str
    category: int
    evidence: frozenset[str]


@dataclass(frozen=True)
class Conversation:
    sample_id: str
    transcript: bytes
    line_starts: list[int]
    dia_ids: list[str]


def parse_evidence(raw: list[str]) -> set[str]:
    """Normalise LoCoMo evidence strings into canonical `D<session>:<turn>` ids."""
    return {f"D{s}:{t}" for item in raw for s, t in _DIA_ID.findall(item)}


def _timestamp(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), "%I:%M %p on %d %B, %Y").isoformat() + "Z"
    except ValueError:
        return None


def _turn_text(turn: dict) -> str:
    # The image caption is part of what the speaker said as far as the dataset
    # is concerned: several category-1 answers are only in it. Rendered the way
    # the LoCoMo authors' own baselines render it.
    text = f"{turn['speaker']}: {turn['text']}"
    if turn.get("blip_caption"):
        text += f" [shares an image: {turn['blip_caption']}]"
    return text


def to_conversation(sample: dict) -> Conversation:
    """Replay one LoCoMo sample as a Claude-Code-shaped JSONL transcript."""
    conv = sample["conversation"]
    speaker_a = conv["speaker_a"]
    sessions = sorted(
        (int(m.group(1)), key) for key in conv if (m := _SESSION_KEY.fullmatch(key))
    )
    out = bytearray()
    starts: list[int] = []
    ids: list[str] = []
    parent: str | None = None
    for n, key in sessions:
        sid = f"{sample['sample_id']}-s{n}"
        ts = _timestamp(conv.get(f"{key}_date_time", ""))
        for turn in conv[key]:
            role = "user" if turn["speaker"] == speaker_a else "assistant"
            uuid = f"{sid}-{turn['dia_id']}"
            line = {
                "type": role,
                "uuid": uuid,
                "sessionId": sid,
                "timestamp": ts,
                "parentUuid": parent,
                "message": {"role": role, "content": [{"type": "text", "text": _turn_text(turn)}]},
            }
            starts.append(len(out))
            ids.append(turn["dia_id"])
            out.extend((json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8"))
            parent = uuid
    return Conversation(sample["sample_id"], bytes(out), starts, ids)


# MemPalace's LoCoMo protocol (benchmarks/locomo_bench.py in MemPalace/mempalace,
# lines 932-948), replicated so its published session R@10 is compared like for
# like: all five categories, gold = sessions named by `re.match(r"D(\d+):")` on
# each raw evidence string (first id only; `D:11:26` yields nothing), and a
# question whose gold is empty scores 1.0. Session metrics only.
_MP_SESSION = re.compile(r"D(\d+):")


def load_mempalace(path: str) -> list[Question]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [
        Question(
            sample["sample_id"],
            qa["question"],
            str(qa.get("answer", qa.get("adversarial_answer", ""))),
            qa["category"],
            frozenset(
                f"D{m.group(1)}" for e in qa.get("evidence", []) if (m := _MP_SESSION.match(e))
            ),
        )
        for sample in data
        for qa in sample["qa"]
    ]


def load(path: str) -> tuple[list[Conversation], list[Question], dict[str, int]]:
    """Conversations, the scored questions, and counts of what was left out and why."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    convs, questions = [], []
    dropped: dict[str, int] = defaultdict(int)
    for sample in data:
        conv = to_conversation(sample)
        known = set(conv.dia_ids)
        convs.append(conv)
        for qa in sample["qa"]:
            if qa["category"] not in SCORED_CATEGORIES:
                dropped["category 5 (adversarial)"] += 1
                continue
            evidence = parse_evidence(qa.get("evidence", []))
            dropped["evidence ids naming no turn"] += len(evidence - known)
            evidence &= known
            if not evidence:
                dropped["questions with no resolvable evidence"] += 1
                continue
            questions.append(
                Question(
                    sample["sample_id"],
                    qa["question"],
                    str(qa.get("answer", "")),
                    qa["category"],
                    frozenset(evidence),
                )
            )
    return convs, questions, dict(dropped)


def measure(ranked: list[str], evidence: frozenset[str], k: int) -> tuple[float, float, float]:
    """(recall, hit, all) of the first k distinct items against the evidence."""
    if not evidence:  # only reachable under the MemPalace protocol, which scores it 1.0
        return 1.0, 1.0, 1.0
    top = set(ranked[:k])
    found = len(top & evidence)
    return found / len(evidence), float(found > 0), float(found == len(evidence))


def ranked_turns(conv: Conversation, offsets: list[int]) -> list[str]:
    """Byte offsets to `dia_id`s in rank order, distinct, offsets past a line dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for off in offsets:
        i = bisect.bisect_right(conv.line_starts, off) - 1
        if i < 0 or off >= len(conv.transcript):
            continue
        if conv.dia_ids[i] not in seen:
            seen.add(conv.dia_ids[i])
            out.append(conv.dia_ids[i])
    return out


def sessions(ids) -> list[str]:
    """`D3:5` -> `D3`, distinct, order kept: the session-level view peers publish."""
    return list(dict.fromkeys(i.split(":")[0] for i in ids))


def in_split(sample_id: str, split: str) -> bool:
    """Fixed dev/test halves by conversation, so tuning never sees what is reported."""
    if split == "all":
        return True
    dev = int(hashlib.sha256(sample_id.encode()).hexdigest(), 16) % 2 == 0
    return dev == (split == "dev")


def run(
    path: str, arm_names: list[str], ks: list[int], split: str = "all", protocol: str = "turn"
) -> dict:
    from bench import arms as arms_mod

    convs, questions, dropped = load(path)
    if protocol == "mempalace":
        questions, dropped = load_mempalace(path), {}
    convs = [c for c in convs if in_split(c.sample_id, split)]
    questions = [q for q in questions if in_split(q.conversation, split)]
    by_conv: dict[str, list[Question]] = defaultdict(list)
    for q in questions:
        by_conv[q.conversation].append(q)
    depth = max(ks)
    # sums[arm][(category, k)] = [recall, hit, all] at turn level, then the same
    # three at session level; category 0 is overall.
    sums: dict[str, dict[tuple[int, int], list[float]]] = {
        a: defaultdict(lambda: [0.0] * 6) for a in arm_names
    }
    counts: dict[int, int] = defaultdict(int)
    for q in questions:
        counts[0] += 1
        counts[q.category] += 1
    for conv in convs:
        for name in arm_names:
            retriever = getattr(arms_mod, f"{name}_factory")(conv.sample_id, conv.transcript)
            try:
                for q in by_conv[conv.sample_id]:
                    ranked = ranked_turns(conv, retriever(q.question, depth))
                    ranked_s = sessions(ranked)
                    for k in ks:
                        if protocol == "mempalace":  # gold is already sessions
                            m = (0.0, 0.0, 0.0) + measure(ranked_s, q.evidence, k)
                        else:
                            gold_s = frozenset(sessions(q.evidence))
                            m = measure(ranked, q.evidence, k) + measure(ranked_s, gold_s, k)
                        for cat in (0, q.category):
                            acc = sums[name][(cat, k)]
                            for i in range(6):
                                acc[i] += m[i]
            finally:
                if hasattr(retriever, "close"):
                    retriever.close()
    return {
        "n": counts[0],
        "per_category": {c: counts[c] for c in sorted(counts) if c},
        "dropped": dropped,
        "metrics": {
            name: {
                f"{cat}@{k}": [v / counts[cat] for v in vals]
                for (cat, k), vals in sums[name].items()
            }
            for name in arm_names
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.locomo")
    ap.add_argument("--dataset", default=os.environ.get("GITMEMORY_LOCOMO"))
    ap.add_argument("--arms", default="candidate")
    ap.add_argument("--k", default="5,10")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--protocol", choices=("turn", "mempalace"), default="turn")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    if not args.dataset or not os.path.exists(args.dataset):
        print("error: pass --dataset or set GITMEMORY_LOCOMO (bench/fetch_locomo.sh)",
              file=sys.stderr)
        return 1
    arm_names = ["gitmemory" if a == "candidate" else a for a in args.arms.split(",")]
    ks = [int(k) for k in args.k.split(",")]
    res = run(args.dataset, arm_names, ks, args.split, args.protocol)
    cats = "1-5, MemPalace protocol" if args.protocol == "mempalace" else "1-4"
    print(f"# LoCoMo evidence retrieval — {args.split}, n = {res['n']} (categories {cats})")
    print()
    for why, n in sorted(res["dropped"].items()):
        print(f"- excluded: {why}: {n}")
    print()
    names = ("Recall", "Hit", "All", "S-Recall", "S-Hit", "S-All")
    # MemPalace gold is sessions only; its turn columns would print as 0.0000. [E9]
    shown = range(3, 6) if args.protocol == "mempalace" else range(6)
    cols = [f"{names[i]}@{k}" for k in ks for i in shown]
    print("| Arm | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name in arm_names:
        cells = [f"{res['metrics'][name][f'0@{k}'][i]:.4f}" for k in ks for i in shown]
        print(f"| {name} | " + " | ".join(cells) + " |")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
