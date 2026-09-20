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

- `src/gitmemory/jsonl.py` — verbatim copy of `fable/jsonl.py`.
  Kept rather than rewritten for two reasons a reimplementation gets wrong:
  `surrogateescape` decoding keeps byte offsets true on invalid UTF-8
  (`errors="replace"` inflates them, U+FFFD being 3 bytes), and the
  `raw_decode` loop recovers multiple JSON objects concatenated onto one
  physical line — which occurs in real Claude Code transcripts.

  Two defects found in it during our E1 review, **not yet patched locally**
  because the file is vendored verbatim and diverging costs more than either
  currently does. Both are to be reported upstream at RC1:

  1. `byte_off` re-encodes the whole line prefix for every object on that line,
     so a line holding *N* concatenated objects costs O(N²) — exactly the
     concatenated-objects case the module exists to handle. Our corpus tops out
     at a handful per line, so this is latent, not live.
  2. A `JSONDecodeError` `break`s out of the whole physical line, so a valid
     object concatenated *after* a malformed one is lost. It is counted (our
     adapter reports `json_decode_error`), so it is a loss we can see rather
     than a silent one.

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

## Not taken

Storage layers, indexes and ingest pipelines were read and deliberately not
reused. See DESIGN.md §6 for why, per tool.
