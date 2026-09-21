"""The decision graph: gitmemory supplies the claims, graphify supplies the graph.

`derive.decisions` says *which blocks are decisions*, which is the part that can
be wrong and the part the E5 gate measures. Everything after that — assembling a
graph, finding communities, drawing it — is a solved problem under a compatible
licence, so this module is an emitter and nothing more. It turns decisions into
the nodes/edges dict `graphify.build_from_json` already accepts and gets out of
the way. See `docs/DESIGN.md` §"The graph layer is graphify, not ours".

The field names here are not guessed. They are `REQUIRED_NODE_FIELDS` and
`REQUIRED_EDGE_FIELDS` from the installed `graphify/validate.py`, and
`test_graph.py` asserts the emitted dict passes that validator rather than
trusting this comment to stay true across an upgrade.

Two rules this module exists to keep:

- **A node is a block, not a paraphrase.** `id` and `source_file` are both the
  `source_ref` — a content-derived `block_id` — so every node on the diagram
  names bytes somebody can go and read. A node that cannot be traced back to a
  committed block is the fiction this whole epoch is built to avoid.
- **An edge says how sure it is.** graphify's `confidence` vocabulary is exactly
  the distinction we refuse to blur, so the only edges emitted here are the ones
  the bytes actually support.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from gitmemory.derive import Decision, _leaks, decisions
from gitmemory.records import Session

__all__ = ["LABEL_CHARS", "NO_TEXT", "REDACTED", "build", "extraction"]

# Long enough to read on a node, short enough that the diagram is not a wall of
# text. The full block is one lookup away by `source_ref`, which is the point of
# carrying it.
LABEL_CHARS = 120

# What a node wears when the block it names has no visible text. Parenthesised
# so it cannot be read as the block's content, and a fixed string rather than
# anything derived from the bytes, because the point is to describe the absence
# rather than to invent something to show. A node with an empty label is a blank
# node, which is the thing the missing-ref check above refuses. [review: Gemini 2]
NO_TEXT = "(no visible text)"

# What a node wears when the block it names quotes a secret. The node stays —
# dropping it would lose a decision and break the chain of edges through it —
# and `source_ref` still names the bytes, so the block is one lookup away in
# `raw/`, which is the unredacted copy by design.
#
# Without this the write door in `derive._write` would refuse the whole
# artifact, and a single leaked key in one decision block would cost a
# generation every derived file it has. `ideas()` already takes the milder
# route for the same reason: it drops the offending sentence, not the run.
REDACTED = "(redacted: this block quotes a secret)"

_WS = re.compile(r"\s+")


def _label(text: str) -> str:
    """One line of the block, for the node to wear.

    Whitespace is collapsed first because a label is a single line by the time
    anything draws it, and a newline inside one is the kind of thing that
    renders as a blank node three layers downstream with no clue why.

    The secret scan runs on the whole block, before the cut, not on the label it
    returns. A key that straddles `LABEL_CHARS` survives the cut as a fragment
    no detector matches, so scanning afterwards would pass a truncated key
    through and call it clean.
    """
    if _leaks(text.encode("utf-8", "surrogatepass")):
        return REDACTED
    flat = _WS.sub(" ", text).strip()
    if not flat:
        return NO_TEXT
    if len(flat) <= LABEL_CHARS:
        return flat
    # Cut on a space so the label ends on a word, and fall back to a hard cut
    # for text that has no space in range — a long identifier or a URL.
    cut = flat.rfind(" ", 0, LABEL_CHARS)
    return flat[: cut if cut > 0 else LABEL_CHARS].rstrip() + "…"


def _blocks_by_id(session: Session) -> dict[str, str]:
    return {b.block_id: b.text for t in session.turns for b in t.blocks}


def extraction(
    sessions: Iterable[Session],
    *,
    extract: Callable[[Session], list[Decision]] = decisions,
) -> dict:
    """Decisions from many transcripts as one graphify extraction dict.

    `extract` is injected so the graph can be scored against a fixed extractor
    without monkeypatching, and so a caller can build the diagram from decisions
    it got some other way. It defaults to the one this project stands behind.

    **A `block_id` is content-derived, so a block replayed into a second
    transcript is the same id and therefore the same node.** That is deliberate:
    a fork from a compaction boundary is the same conversation, and drawing the
    decision twice would say two were made. It is also the only cross-transcript
    edge in the graph that costs nothing and asserts nothing — the two chains
    meet at the shared node because they genuinely share the bytes.

    Node and edge lists are sorted before returning, so the same store emits the
    same bytes whatever order the caller walked it in.
    """
    nodes: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()

    for session in sessions:
        text_of = _blocks_by_id(session)
        found = extract(session)
        for d in found:
            # Silently labelling a node we cannot quote would put a decision on
            # the diagram with no bytes under it. An extractor that returns a
            # ref from a different session is a bug in the extractor, and this
            # is where it becomes visible instead of becoming a blank node.
            if d.source_ref not in text_of:
                raise ValueError(
                    f"{session.source_path}: decision names block "
                    f"{d.source_ref!r}, which is not in this transcript"
                )
            nodes.setdefault(
                d.source_ref,
                {
                    "id": d.source_ref,
                    "label": _label(text_of[d.source_ref]),
                    "file_type": "rationale",
                    "source_file": d.source_ref,
                    "kind": d.kind,
                },
            )
        # `decisions` returns in the order the decisions occur, which is byte
        # order — a mutation row pins that — so consecutive pairs are the
        # succession the transcript records.
        for a, b in zip(found, found[1:], strict=False):
            if a.source_ref != b.source_ref:
                edges.add((a.source_ref, b.source_ref))

    return {
        # `ideas()` reports `sentences_redacted`; this surface reported nothing,
        # so a store where the gate misfires on every block — one false positive
        # is a block that merely *names* a PEM header — looked exactly like a
        # store full of secrets. A count is the difference between a redaction
        # you can audit and a redaction you can only discover. [E7]
        "labels_redacted": sum(1 for n in nodes.values() if n["label"] == REDACTED),
        "nodes": [nodes[i] for i in sorted(nodes)],
        "edges": [
            {
                "source": s,
                "target": t,
                # The bytes say this decision came after that one in the same
                # transcript, and that is the whole claim. EXTRACTED is
                # therefore honest and cheap.
                #
                # ponytail: no `supersedes` edge. The nearest-preceding
                # heuristic is one line away and would make a far better
                # picture, but a reversal about caching does not supersede the
                # directive about timestamps that happened to precede it, and
                # an INFERRED edge nothing evaluates is exactly the fiction the
                # node rule above exists to prevent. The upgrade is evidence —
                # overlap between the two blocks, scored the way the node
                # extractor was — not a guess drawn confidently.
                "relation": "follows",
                "confidence": "EXTRACTED",
                "source_file": s,
            }
            for s, t in sorted(edges)
        ],
    }


def build(
    sessions: Iterable[Session],
    *,
    extract: Callable[[Session], list[Decision]] = decisions,
    **kw,
):
    """The extraction, assembled by graphify. Returns a `networkx.Graph`.

    `extract` is ours; everything else in `**kw` is graphify's — `directed`,
    `root`. The first version funnelled all of `**kw` into `extraction`, so
    `build(sessions, directed=True)` died on a `TypeError` from a function the
    caller had never heard of, which is the worst way to learn that an argument
    went to the wrong place. [review: Gemini 1]

    Imported here rather than at module scope so `gitmemory.graph` is importable
    without the `derive` extra installed — the emitter above is pure stdlib and
    is what the tests spend most of their time on.
    """
    from graphify.build import build_from_json

    return build_from_json(extraction(sessions, extract=extract), **kw)
