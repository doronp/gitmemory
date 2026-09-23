# Third-party code and data

gitmemory is Apache-2.0. Both licenses below are inbound-compatible with it.
Nothing here was rewritten to avoid attribution: where prior art solved a
problem correctly, we took the solution and named the source.

**The MIT licence requires the permission notice itself to travel with the
code, not merely the copyright line.** Verbatim upstream texts:

| Source | File |
|---|---|
| fable | [`licenses/fable-MIT.txt`](licenses/fable-MIT.txt) |
| claude-code-log | [`licenses/claude-code-log-MIT.txt`](licenses/claude-code-log-MIT.txt) |

## Vendored source

### fable — MIT, Copyright (c) 2026 Anoop Grover
<https://github.com/grooverLab/fable>

- `src/gitmemory/jsonl.py` — copy of `fable/jsonl.py` with **one local change**,
  marked `[E3]` in the source and listed below.
  Kept rather than rewritten for two reasons a reimplementation gets wrong:
  `surrogateescape` decoding keeps byte offsets true on invalid UTF-8
  (`errors="replace"` inflates them, U+FFFD being 3 bytes), and the
  `raw_decode` loop recovers multiple JSON objects concatenated onto one
  physical line — which occurs in real Claude Code transcripts.

  Two defects were found in it during our E1 review. Both are to be reported
  upstream at RC1; one is now also patched here.

  1. **Patched locally at E3.** `byte_off` re-encoded the whole line prefix for
     every object on that line, so a line holding *N* concatenated objects cost
     O(N²) — exactly the concatenated-objects case the module exists to handle.
     E1 called it latent because our own corpus tops out at a handful per line.
     E3 measured it and changed the verdict: 6,400 objects on one line take
     165 ms and the curve is quadratic, so a single ~175 MB line is minutes of
     CPU. A transcript is untrusted input on the ingest path, and the store's
     whole premise is accepting whatever bytes an agent wrote — a superlinear
     cost the input chooses is not a perf nit. The fix carries a byte cursor
     instead of recomputing it, is O(N), and yields byte-identical offsets;
     `test_multibyte_before_a_second_object_on_the_same_line` pins that.
  2. **Not patched.** A `JSONDecodeError` `break`s out of the whole physical
     line, so a valid object concatenated *after* a malformed one is lost. It
     is counted (our adapter reports `json_decode_error`), so it is a loss we
     can see rather than a silent one — and there is no reliable resync point
     in a JSON stream: every "skip to the next `{`" heuristic can resume inside
     a string literal and invent an object. A visible loss beats an invented
     turn in a store whose value is that its bytes are the agent's bytes.

### claude-code-log — MIT, Copyright (c) 2025 Daniel Demmel
<https://github.com/daaain/claude-code-log>

- `SILENT_SKIP_TYPES` in `src/gitmemory/adapters/claude_code.py` — the set of
  Claude Code line types carrying no DAG role. Field knowledge, not code.
  Kept byte-identical to upstream's set: an earlier version of that constant
  had drifted (it invented `artifact-publish` and omitted `frame-link`), which
  is how a vendored constant quietly stops being the thing it cites. Local
  additions, if any, go below a marked line in the source.
- `test/test_data/` (162 `.jsonl` fixtures) is used as gitmemory's conformance
  corpus, fetched at test time by `tests/fetch_fixtures.sh` rather than
  redistributed here. It is third-party and public, which is what makes it a
  legitimate test corpus: gitmemory is never tested against its author's own
  transcripts.
- `dev-docs/dag.md` documents the six shapes that legitimately appear as DAG
  roots. Read, not copied.

### pi — MIT, Copyright (c) 2025 Mario Zechner
<https://github.com/earendil-works/pi>

- `packages/coding-agent/src/core/session-manager.ts` is the source of truth
  for the entry union as pi ships it, including `ContextEditEntry` — the
  rewrite path that makes the format *not* strictly append-only. Read to write
  `adapters/pi.py`; no code copied. Every claim the adapter's docstring makes
  about the format cites a file and line at the pinned rev, so a reader can
  check it rather than trust it. The `src/session/` paths an earlier revision
  of this file credited to pi do not exist at `a8ed4977`; they are oh-my-pi's,
  and are credited there.
- `packages/coding-agent/test/fixtures/` (2 `.jsonl` fixtures) joins the
  conformance corpus, fetched at test time by `tests/fetch_fixtures.sh`. These
  in particular could not be vendored even if the licence invited it: each one
  carries its author's home directory in a `cwd` field, and this repository
  does not carry anybody's absolute paths, its own owner's least of all.

### oh-my-pi — MIT, Copyright (c) 2025 Mario Zechner, (c) 2025-2026 Can Bölük, (c) 2026 Stencil Labs, Inc.
<https://github.com/can1357/oh-my-pi>

- `packages/coding-agent/src/session/session-entries.ts` and
  `session-manager.ts` are where the fork moved and extended the entry union,
  and are the source for the 256-byte rewritable `title` slot
  (`SESSION_TITLE_SLOT_BYTES`, `session-entries.ts:15` @`b52e1f5`). Read, not
  copied.
- A fork of pi writing the same JSONL dialect, which is why one adapter reads
  both and `"omp"` is an alias rather than a second entry in `ADAPTERS`. Its
  `packages/coding-agent/test/fixtures/` (2 `.jsonl`) is the other half of the
  pi corpus, fetched the same way and for the same reason — the two forks'
  fixtures differ in bytes, so replaying both is what proves the dialect claim
  rather than assuming it.

### agentcairn — Apache-2.0, Copyright 2026 Charles C. Figueiredo
<https://github.com/ccf/agentcairn>

- The fail-closed structural event taxonomy in
  `ingest/harness/claude_code.py:classify_claude_code` informed this project's
  line classification. No code copied; the approach is credited.

## Depended on, not vendored

Not redistributed here, so no licence text travels with this repository — but
the project's rule is that attribution lands when the first line of code
depends on something, and `src/gitmemory/derive.py` now does.

| Package | Version verified | Licence | Used for |
|---|---|---|---|
| `sumy` | 0.13.0 | Apache-2.0 | LexRank over prose blocks (`derive.ideas`) |
| `numpy` | ≥2.0 | BSD-3-Clause | LexRank's matrix; sumy does not declare it |
| `graphifyy` | 0.9.65 | Apache-2.0 | the decision graph (`graph.build` → `build_from_json`) |

`sumy`'s default tokenizer downloads an `nltk` `punkt` model on first use.
gitmemory never reaches it: `derive._Tok` supplies the two methods sumy's
`Sentence` actually calls, so the derivation path stays offline. That is a
property, not an accident — see `docs/tasks/E5-dependency-verification.md`.

`graphifyy`'s licence is Apache-2.0 by `License-Expression` in the wheel. **PyPI's
JSON licence field is empty**, which is a packaging gap rather than a licence
gap, and is one of the things to report upstream at RC1: a downstream tool that
reads licences from the index rather than the wheel sees no licence at all.

The division of labour is deliberate and is recorded in DESIGN.md §"The graph
layer is graphify, not ours": graphify gets a nodes/edges dict and supplies the
graph, the community detection and the rendering; gitmemory decides what a
decision *is*, which is the part that can be wrong, and keeps the gate on it.
`tests/test_graph.py` asserts our emitted dict against graphify's own
`validate_extraction` rather than against our reading of its schema, so an
upgrade that moves a required field fails a test instead of a diagram.

## Not taken

Storage layers, indexes and ingest pipelines were read and deliberately not
reused. See DESIGN.md §6 for why, per tool.
