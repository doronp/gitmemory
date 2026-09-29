# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
version may break the store format; the entry will say so, and `verify` will
tell you.

## [Unreleased]

### Added

- **`recall --policy`** decides which of two disagreeing hits comes first:
  `latest-user` (the user's own words newest first, then the rest), `latest`
  (newest first, whoever said it), `relevance` (best match first), or your own
  `module:function`. Also settable with `GITMEMORY_POLICY`. Nothing is deleted.
  See [USAGE.md](docs/USAGE.md#when-memories-disagree).
- `recall` prints each hit's date, and `Hit` carries the turn's `ts`.
- **Recall skill.** The Claude Code plugin ships a skill, `/gitmemory:recall`,
  that has the agent run `gitmemory recall` and quote what it returns, each
  line with its address, when you refer to a decision or a rule from before a
  compaction or from an earlier session. It runs `gitmemory index` when the
  index is missing or stale, and writes nothing else. See
  [docs/USAGE.md](docs/USAGE.md#put-it-back-in-front-of-the-agent). What it
  reads goes to the model provider, past no redaction gate. The plugin's
  version is still 0.1.0, so a copy installed from 0.1.0 does not get the skill
  from `claude plugin update`; reinstall it, or wait for the next release
  ([hook/README.md](hook/README.md#as-a-claude-code-plugin)).

### Changed

- `recall` now lists the user's own words newest first by default, not best
  match first. Pass `--policy relevance` for the old order. `index.search` is
  unchanged.

## [0.1.0] - 2026-09-28

The first public release. What it contains:

### Added

- **Capture.** An append-only segment store with generations, and a contiguity
  proof: the segments tile `[0, size)` and hash to a recorded digest.
  `gitmemory capture`, `gitmemory verify`.
- **Watcher and hook.** `gitmemory watch` tails configured roots and commits
  what grew. An optional POSIX `sh` hook shim makes capture happen at the
  compaction boundary. The repository is also a Claude Code plugin
  (`/plugin install gitmemory@gitmemory`) that registers the shim on
  `PreCompact` and `SessionEnd`, passing the event name in `args` (Claude Code
  2.1.139 and later).
- **Adapters.** Claude Code, and pi / oh-my-pi (one format family, two
  dialects). They pass 328 conformance cases drawn from three third-party MIT
  corpora.
- **Retrieval.** A SQLite FTS5 (BM25) index rebuilt from raw bytes, and
  `gitmemory recall`. The optional `hybrid` extra installs what the
  benchmarks' dense and rerank arms need; `recall` itself stays BM25.
- **Derivation.** `gitmemory derive` produces key ideas and a timeline.
  Decision-graph extraction is opt-in (`--graph`).
- **Dashboard.** `gitmemory dashboard` runs Datasette over the index,
  read-only, on loopback, behind a single-use sign-in URL.
- **Egress gate.** `gitmemory push` runs the redaction gate over everything a
  push would transmit. It does not send yet.
- **Benchmarks.** Harnesses for LongMemEval and LoCoMo, under each benchmark's
  official protocol and under peer protocols, plus an eval-only reader and judge
  QA harness. See [docs/RESULTS.md](docs/RESULTS.md).

### Security

- Two review rounds before publication: E7 (68 findings) and E7b (22 findings).
  See [SECURITY.md](SECURITY.md).

[Unreleased]: https://github.com/doronp/gitmemory/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/doronp/gitmemory/releases/tag/v0.1.0
