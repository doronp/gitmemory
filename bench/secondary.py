"""The secondary set: every human turn in somebody else's real sessions.

Every extraction number before this one was written by an agent. The gate
fixture is synthetic by construction — that is what makes its labels exact —
and probes A through D are hand-written fiction, four vocabularies invented to
attack the rules from outside. `docs/DESIGN.md` promised a secondary set of
real-shaped sessions against that, and fiction is not it. A person asking for
work at 1am does not write like a probe author.

This is the set. The text is real Claude Code transcripts belonging to somebody
else — claude-code-log's `test_data/real_projects`, MIT, already pinned in this
repository for the parser conformance run. Not one byte of it is the owner's,
which is the same rule every other corpus here is held to.

**The corpus is not vendored.** The manifest carries a sha256 per item and
the label; the text is read back out of the clone, and an item whose digest has
moved is a hard error rather than a silent relabelling. That is the LongMemEval
arrangement — the corpus is fetched, never vendored — and here it also settles
the question of republishing a stranger's sessions.

It does not settle quoting one. `docs/benchmarks/E5-secondary-set.md` reproduces
eight runs of 40 characters or more verbatim, and `main()` below prints 140
characters of every miss, because a labelling argument cannot be made without
the sentence. This docstring used to say "no transcript text is committed",
which was true of the manifest and false of the write-up. [E7b L4-F3]

**Census, not sample.** Of the user-role blocks in those sessions, 1095 are tool
results and 20 are empty; the extractor reads none of them. The remaining 219
are prose, 140 of them distinct, and all 140 are labelled. There is no sampling
step to argue with, and nothing the extractor reads is outside the set.

The assistant side is scored separately and only for precision. Its items are
not a census of anything — they are exactly the blocks the extractor called
`reversal`, adjudicated after the fact — so a reversal it never flagged is
invisible there by construction.

Run: `python -m bench.secondary`
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from gitmemory import derive
from gitmemory.adapters import claude_code

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs/benchmarks/E5-secondary-manifest.json"
FIXTURES = ROOT / ".conformance/claude-code-log/test/test_data"
SUBTREE = "real_projects"

# Gold labels. `reversal-by-user` is held aside from the class score for the
# same reason probe C's `ceiling` group is: the extractor declares in
# `docs/DESIGN.md` that role is definitional and a user revoking their own
# instruction is out of scope. Scoring a declared limit inside the headline
# number hides how often it actually costs something, which is the one thing
# real text can tell us and no probe can.
GOLD = ("directive", "reversal-by-user", "none")
ASIDE = frozenset({"reversal-by-user"})

EXPECTED = {"directive": "directive", "none": None}


def corpus_root() -> Path | None:
    """`real_projects` inside the pinned clone, or None if it is not here."""
    # `or`, not a `get` default — see `_corpus` in tests/test_claude_code.py.
    # An empty env var is the shape of an unexpanded shell variable, and here it
    # would make the bench report a missing clone that is right there. [E7b L4-F5]
    root = Path(os.environ.get("GITMEMORY_CC_FIXTURES") or str(FIXTURES)) / SUBTREE
    return root if root.is_dir() else None


def transcripts(root: Path) -> list[Path]:
    """Every session transcript under `root`, and nothing outside it.

    Resolved and bounded because a corpus is somebody else's directory and
    `store._open_source` opens the same shape of file with `O_NOFOLLOW`: a
    `*.jsonl` symlink planted in it is read by whatever it points at, and the
    one thing this repository must never read is the machine's own history.
    The walk does not descend symlinked directories, so the leaf is the whole
    hole. [E7b L4-F4]

    Subagent transcripts are skipped here rather than at each of the three call
    sites, which is where the rule used to live in triplicate.
    """
    base = root.resolve()
    return [
        path
        for path in sorted(base.rglob("*.jsonl"))
        if path.resolve().is_relative_to(base)
        and not path.name.startswith("agent-")
        and path.parent.name != "subagents"
    ]


def _blocks(root: Path) -> list[tuple[str, str, str, str]]:
    """`(sha256, file, block_id, text)` for every text block in a user turn.

    **Deliberately not `derive._prose`.** The population is a property of the
    corpus, not of the code under test: the first fix this set produced was one
    that makes `_prose` decline 51 of these blocks, and routing the harness
    through it would have shrunk the denominator from 140 to 89 and reported
    the improvement as a smaller world. A benchmark whose population moves with
    the fix is how a regression gets laundered. Blocks the extractor no longer
    reads score `None` here, which is a result and, for the 135 negatives, the
    right one.

    Subagent transcripts are excluded: an `agent-*.jsonl` opens with a dispatch
    prompt in the user role that an orchestrator wrote, not a person, and a
    benchmark of what people say should not be scored on what a program said to
    another program.

    Deduped on the text's digest, because a resumed session replays earlier
    turns verbatim and the same sentence labelled twice is one fact counted
    twice. The first occurrence keeps the identity.
    """
    seen: set[str] = set()
    out = []
    for path in transcripts(root):
        session = claude_code.parse(str(path))
        for turn in session.turns:
            if turn.role != "user":
                continue
            for block in turn.blocks:
                if block.kind != "text":
                    continue
                text = block.text.strip()
                digest = hashlib.sha256(text.encode()).hexdigest()
                if not text or digest in seen:
                    continue
                seen.add(digest)
                out.append((digest, str(path.relative_to(root)), block.block_id, text))
    return out


def _emitted_assistant(root: Path) -> list[tuple[str, str, str, str]]:
    """`(sha256, file, block_id, text)` for the assistant blocks scored `reversal`.

    The production path, not the rule in isolation: `derive.decisions()` is what
    a real session actually writes into the graph. Deduped the same way, and two
    nodes on one block count once.
    """
    seen: set[str] = set()
    out = []
    for path in transcripts(root):
        session = claude_code.parse(str(path))
        prose = {b.block_id: (t.role, b.text.strip()) for t, b in derive._prose(session)}
        for node in derive.decisions(session):
            role, text = prose.get(node.source_ref, ("", ""))
            digest = hashlib.sha256(text.encode()).hexdigest()
            if role != "assistant" or not text or digest in seen:
                continue
            seen.add(digest)
            out.append((digest, str(path.relative_to(root)), node.source_ref, text))
    return out


def _nodes(root: Path) -> dict[str, str]:
    """`{sha256 of the block's text: node kind}` for the whole corpus.

    The production path and nothing beside it. `derive.decisions()` is what a
    real session actually writes into the graph, so the score is of the product
    rather than of a rule called in isolation — which matters the moment a fix
    lands anywhere other than in the rule, as the first one did: it changed
    which blocks are *read*, and a harness calling `_decision_kind` directly
    would have scored every one of them exactly as before.
    """
    out: dict[str, str] = {}
    for path in transcripts(root):
        session = claude_code.parse(str(path))
        prose = {b.block_id: b.text.strip() for _t, b in derive._prose(session)}
        for node in derive.decisions(session):
            text = prose.get(node.source_ref, "")
            if text:
                out.setdefault(hashlib.sha256(text.encode()).hexdigest(), node.kind)
    return out


def _join(
    rows: list[dict],
    found: list[tuple[str, str, str, str]],
    pin: str,
    *,
    may_shrink: bool = False,
) -> list[dict]:
    """Manifest rows with their text, or a hard error if the corpus has moved.

    Three ways this raises rather than scoring something else: an item in the
    manifest that the corpus no longer produces, a block the corpus produces
    that the manifest does not cover, and a digest that has changed file or
    block. Each of them means the pin moved, and a benchmark that quietly
    rescores itself against a changed corpus is worse than one that does not
    run.

    `may_shrink` is for the assistant side alone, whose population *is* the
    extractor's output: a fix that stops emitting an adjudicated block is the
    point, and the row survives with no text so the withdrawal stays visible and
    countable. It does *not* hold the precision denominator — see
    `assistant_score`. An *extra* block is still fatal in both directions — it is
    output nobody adjudicated, and scoring it against nothing would count an
    unlabelled guess as a win.
    """
    by_digest = {row["sha256"]: row for row in rows}
    have = {digest: (file, block_id, text) for digest, file, block_id, text in found}

    missing = sorted(set(by_digest) - set(have))
    extra = sorted(set(have) - set(by_digest))
    if extra or (missing and not may_shrink):
        raise RuntimeError(
            f"the corpus has moved off pin {pin}: "
            f"{len(missing)} labelled items are gone, {len(extra)} unlabelled blocks appeared"
        )

    out = []
    for digest, row in by_digest.items():
        if digest not in have:
            out.append({**row, "text": None})
            continue
        file, block_id, text = have[digest]
        if (file, block_id) != (row["file"], row["block_id"]):
            raise RuntimeError(f"{digest[:12]} moved: {row['file']} -> {file}")
        out.append({**row, "text": text})
    return out


def _manifest() -> dict:
    root = corpus_root()
    if root is None:
        raise RuntimeError(
            "the pinned claude-code-log clone is not here; see README.md for the "
            "two lines that fetch it, or set GITMEMORY_CC_FIXTURES"
        )
    return json.loads(MANIFEST.read_text())


def items() -> list[dict]:
    """The labelled user-side census, with text and the node the product emits."""
    manifest = _manifest()
    root = corpus_root()
    assert root is not None  # _manifest() raised otherwise
    nodes = _nodes(root)
    return [
        {**row, "emits": nodes.get(row["sha256"])}
        for row in _join(manifest["items"], _blocks(root), manifest["pin"])
    ]


def assistant_items() -> list[dict]:
    """The adjudicated assistant-side output, text joined back on."""
    manifest = _manifest()
    root = corpus_root()
    assert root is not None
    return _join(
        manifest["emitted_assistant"],
        _emitted_assistant(root),
        manifest["pin"],
        may_shrink=True,
    )


def score(labelled: list[dict]) -> dict:
    """Directive precision and recall, plus the two things a probe cannot show.

    `machine` counts false positives raised on blocks no human typed — slash
    command wrappers, IDE notices, command stdout. The user *role* is not the
    user, and this is the only corpus here where that distinction exists at all.

    `emits` is what `derive.decisions()` actually wrote for the block, not what
    `_decision_kind` says about its text in isolation. The difference is a block
    the product never reads, which scores as no node — the whole of the first
    fix this set produced lives in that gap.
    """
    tp = fp = fn = 0
    aside_n = aside_seen = 0
    machine_fp = 0
    misses = []
    for item in labelled:
        got = item["emits"]
        gold = item["gold"]
        if gold in ASIDE:
            aside_n += 1
            aside_seen += got is not None
            if got is not None:
                misses.append((gold, got, item))
            continue
        want = EXPECTED[gold]
        if got == "directive" and want == "directive":
            tp += 1
        elif got == "directive":
            fp += 1
            machine_fp += bool(item["machine"])
        elif want == "directive":
            fn += 1
        if got != want:
            misses.append((gold, got, item))
    return {
        "n": len(labelled),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "aside_n": aside_n,
        "aside_labelled_something": aside_seen,
        "machine_fp": machine_fp,
        "misses": misses,
        "gold": Counter(i["gold"] for i in labelled),
        "machine": sum(i["machine"] for i in labelled),
    }


def assistant_score(adjudicated: list[dict]) -> dict:
    """Precision only, and the word is meant literally.

    Every item here is something the extractor emitted, so there is no negative
    class and no recall to compute. `right` over `n` is the share of the
    assistant-side output that survived three adjudicators.

    `n` is the adjudicated population, held fixed at whatever the extractor
    emitted when the labels were written. A fix that stops emitting a block
    leaves the row with `text: None`, so it drops out of `still` and out of
    `right`.

    **`precision` is `right / still_emitted`, and that denominator shrinks.**
    It is the honest measure of what the extractor writes *now*, and it is also
    trivially gameable: a predicate that silenced every adjudicated block would
    read 1.0000 on an empty output. Nothing here can detect that, so it is the
    caller's job — `bench/test_secondary.py` pins `still_emitted` and `right`
    alongside `n`, and a fix that withdraws more than it should fails on the
    count rather than passing on the ratio.
    """
    still = [row for row in adjudicated if row["text"] is not None]
    right = sum(row["gold"] == "reversal" for row in still)
    return {
        "n": len(adjudicated),
        "still_emitted": len(still),
        "right": right,
        "precision": right / len(still) if still else 1.0,
        "unanimous": sum(row["votes"] == 3 for row in adjudicated),
        "wrong": [row for row in still if row["gold"] != "reversal"],
        "withdrawn": [row for row in adjudicated if row["text"] is None],
    }


def main() -> int:
    if corpus_root() is None:
        print("secondary set: corpus absent, nothing scored", file=sys.stderr)
        return 0
    s = score(items())
    a = assistant_score(assistant_items())
    print(f"secondary set: {s['n']} items  {dict(s['gold'])}  machine-authored {s['machine']}")
    print(f"  directive precision {s['precision']:.4f}  recall {s['recall']:.4f}")
    print(f"  tp {s['tp']}  fp {s['fp']} (machine-authored {s['machine_fp']})  fn {s['fn']}")
    print(
        f"  aside: user-reverses-own {s['aside_n']}, "
        f"of which the extractor labelled something {s['aside_labelled_something']}"
    )
    print(
        f"  assistant `reversal`: {a['right']}/{a['still_emitted']} adjudicated real, "
        f"precision {a['precision']:.4f}  "
        f"({a['n']} adjudicated, {len(a['withdrawn'])} no longer emitted, "
        f"{a['unanimous']}/{a['n']} unanimous)"
    )
    for gold, got, item in s["misses"]:
        head = item["text"].replace("\n", " ⏎ ")[:140]
        print(f"  [{gold}] got {got}  {item['file']}\n    {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
