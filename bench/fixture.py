# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""Dump a split of the decision corpus to disk, generator not included.

    python -m bench.fixture bench/fixture-dev            # dev, seed 42
    python -m bench.fixture <dir> --split test --seed 20042

The output is a `sessions/` directory of transcripts and a `gold.json` of
`{stem: [[kind, source_ref], ...]}`. That is everything needed to score an
extractor and nothing that says how the corpus was built, which is the point:
`bench/decisions.py` spells out every template and every vocabulary it draws
from, and an extractor written with that open is fitted to the generator.
`bench/gate.py` reads a fixture and never imports this module.

The dump is not committed. It is regenerated from the committed seed, and
`tests/test_decisions_bench.py` holds the checksum that says the corpus behind
it has not moved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def dump(out_dir: Path, *, seed: int = 42, split: str = "dev", n: int = 100) -> int:
    from bench.decisions import generate, resolve_gold_for_case

    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    gold: dict[str, list[list[str]]] = {}
    for i, case in enumerate(generate(seed=seed, n=n, split=split)):
        stem = f"{i:03d}"
        (sessions_dir / f"{stem}.jsonl").write_bytes(case.transcript_bytes)
        gold[stem] = [[d.kind, d.source_ref] for d in resolve_gold_for_case(case)]

    (out_dir / "gold.json").write_text(json.dumps(gold, indent=1, sort_keys=True) + "\n")
    return sum(len(v) for v in gold.values())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(argv[0])
    split = argv[argv.index("--split") + 1] if "--split" in argv else "dev"
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 42
    total = dump(out_dir, seed=seed, split=split)
    print(f"{split} split, seed {seed}: 100 sessions and {total} gold decisions in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
