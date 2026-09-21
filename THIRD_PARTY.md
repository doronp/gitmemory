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

`sumy`'s default tokenizer downloads an `nltk` `punkt` model on first use.
gitmemory never reaches it: `derive._Tok` supplies the two methods sumy's
`Sentence` actually calls, so the derivation path stays offline. That is a
property, not an accident — see `docs/tasks/E5-dependency-verification.md`.

`graphifyy` 0.9.65 (Apache-2.0 by `License-Expression` in the wheel; PyPI's
JSON licence field is empty, which is a packaging gap and not a licence gap)
is declared in the `derive` extra and gets its row here when the decision graph
clears its gate and the first line of code imports it.

## Not taken

Storage layers, indexes and ingest pipelines were read and deliberately not
reused. See DESIGN.md §6 for why, per tool.
