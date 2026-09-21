# E5 — derivation and the decision graph

This file is written **before** anything is measured. The gate in `docs/DESIGN.md`
§2.6 is pre-registered, and a pre-registered bar declared after seeing the
numbers is not one.

## What ships

| | Artifact | Where |
|---|---|---|
| 1 | Key ideas — extractive sentences, LexRank over prose blocks | `derived/<session>/ideas.json` |
| 2 | Timeline — a fold over `Event` + `Turn` | `derived/<session>/timeline.json` |
| 3 | Decision graph — our nodes, graphify's graph | `derived/<session>/graph.json`, `graph.html` |
| 4 | `gitmemory derive` | rebuilds all of the above from the store |

Every artifact is a **function of committed bytes only**. Nothing in `derived/`
is a source of truth, nothing in it is the only copy of anything, and deleting
the whole directory costs a rebuild and nothing else. That is the property the
determinism test enforces: build twice, `git diff --exit-code` on `derived/`.

No keyphrase extractor, no LLM anywhere on the automatic path. The reasons are
in `E5-dependency-verification.md`; the short version is that a 4B model
inventing a rationale nobody wrote is the worst failure this product could have.

## The gate, pre-registered

**Claim under test:** that a deterministic extractor can identify decisions in a
transcript — a choice made, with the alternative it displaced — precisely enough
to be worth drawing.

**Unit.** One decision node. A node is a `(kind, source_ref)` pair where
`source_ref` is a `Block.block_id` — content-derived, so it names exactly one
block of exactly one committed transcript. A node with no `source_ref` is not a
node; the writer refuses it, and graphify's own schema helps here —
`source_file` is a required field on every node *and* every edge in
`validate_extraction`, so an unprovenanced node cannot even reach the graph.

**Labels.** Two kinds, defined by observable surface rather than by intent,
because intent is what a 4B model would be needed to read:

- **`directive`** — a *user* block that constrains how the work is done by
  naming both what to do and what not to do. Anchored on that user block.
- **`reversal`** — an *assistant* block that abandons an approach the same
  session had already stated or attempted, and names its replacement. Anchored
  on the assistant block that states the switch.

Everything else is a negative, and the ones that look like positives are the
whole difficulty: a tool failure followed by a retry of the *same* approach, a
user weighing two options without choosing, an assistant listing alternatives
inside a plan it had not yet committed to. Gemini's original objection to this
epoch was precisely that a naive redirect-after-tool-result heuristic scores
these as rejected alternatives, so the corpus is required to be full of them.

The vocabulary is shared between the two halves of the work — it has to be, or a
disagreement about naming reads as a disagreement about extraction — and nothing
else is.

**Match rule — strict.** A predicted node matches a gold node iff the
`source_ref` is byte-equal and the `kind` is equal, one-to-one. No span overlap,
no fuzzy window, no credit for "close", no second prediction matching an
already-matched gold node. Overlap scoring is how an extractor that flags every
tool retry gets a passing grade.

**Metric.** Micro-averaged over the whole labelled set, not per session then
averaged: precision = matched / predicted, recall = matched / gold. A session
with two decisions must not weigh the same as one with forty. A run that
predicts nothing at all scores precision 0, not 1 — an undefined denominator is
reported as a failure, never as a perfect score.

**Bar: precision ≥ 0.85, recall ≥ 0.60.**

**Below the bar it does not ship.** The decision graph is dropped from E5,
`gitmemory derive` emits ideas and timeline only, and the README states the
measured numbers and says the graph did not clear. This is written down now so
that the temptation to move the bar later has something to bump into.

## Who builds what, and why it is split that way

An extractor and the corpus that grades it, written by the same author, produce
a number that means nothing: every disagreement gets resolved in the direction
that makes the score go up, and the author cannot tell that from progress.

So:

- **Gemini builds the labelled corpus and the gate harness.** Synthetic sessions
  whose decisions are planted, so ground truth is exact and free, plus the
  scoring script. It does not see the extractor.
- **Claude builds the extractor.** It does not see the generator's plant records
  or its source until the first scored run.
- **The harness hands the extractor nothing the generator knows.** One argument,
  the `Session` the ordinary adapter parses out of the transcript — no gold
  nodes, no seed, no plant records, no generator metadata riding along in a
  field nobody reads. This is the same narrowing `bench/` still needs, where the
  factory receives a whole `Instance` and an arm could read `inst.question`.
- Then we review each other's lines, as on every other epoch.

The one interface both halves compile against, fixed here so neither has to see
the other's source:

```python
# src/gitmemory/derive.py — Claude
@dataclass(frozen=True, slots=True)
class Decision:
    kind: str  # "directive" | "reversal"
    source_ref: str  # a Block.block_id belonging to this session


def decisions(session: Session) -> list[Decision]: ...
```

**Two splits, from disjoint seeds.** `dev` is readable by the extractor's author
and is what the heuristic is developed against. `test` is scored **once**, at the
end, and the number that goes in the README is that one. An extractor iterated
against the set that grades it has measured its author's persistence, not its own
precision. If `test` comes in far below `dev`, that gap gets published too.

**The corpus is synthetic or public. It is never the owner's transcripts.** The
secondary set of 30 "real-shaped" sessions in §2.6 means *authored to look like
real sessions*, not *taken from real sessions*. `tests/test_no_owner_data.py`
enforces this over tracked and untracked files and is not to be weakened.

## The graphify seam, verified today

Checked against the installed wheel, not the note: `graphifyy` **0.9.65**, the
same version `E5-dependency-verification.md` resolved the licence from.

```
graphify.build.build_from_json(extraction: dict, *, directed=False, root=None) -> nx.Graph
graphify.validate.REQUIRED_NODE_FIELDS = {"id", "label", "file_type", "source_file"}
graphify.validate.REQUIRED_EDGE_FIELDS = {"source", "target", "relation", "confidence", "source_file"}
graphify.validate.VALID_FILE_TYPES     = {"code", "document", "paper", "image", "rationale", "concept"}
graphify.validate.VALID_CONFIDENCES    = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
```

Two of those rows decide our node model rather than merely permitting it.
`rationale` and `concept` are already valid file types, so a decision node does
not need a fork or a custom schema. And `VALID_CONFIDENCES` is the vocabulary
this project would have had to invent: a node we read directly off a structural
marker is `EXTRACTED`, a node we joined across turns is `INFERRED`, and there is
no third state where we quietly guess. We do not emit `AMBIGUOUS` into the
automatic graph at all — it is what the gate is for.

`derive` is declared as `sumy>=0.13, numpy>=2.0, graphifyy>=0.9.65`. `networkx`
comes in under graphify and is not ours to pin; `numpy` is, because LexRank
refuses to run without it and the old table did not say so.

## Order of work

1. This brief, and the corpus brief for Gemini. *(pre-registration)*
2. `derive.py`: ideas + timeline + the canonical writer + `gitmemory derive`.
   These have no gate — they are folds — and they are what makes `derived/`
   exist for the graph to land in.
3. Determinism test and the `source_ref`-or-nothing test, before the extractor,
   so the extractor is born inside the constraint rather than fitted to it.
4. Extractor and corpus, in parallel and apart.
5. Scored run. One number, published either way.
