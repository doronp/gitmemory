# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
version may break the store format; the entry will say so, and `verify` will
tell you.

## [Unreleased]

### Added

- **`gitmemory init claude-code`** (or `pi`) adds that agent's standard
  `[[watch]]` table to `config.toml`, so setup no longer means writing TOML by
  hand. Watch roots still have no default: the root is written only because you
  named the agent. It appends without changing a byte already there, because
  the file also holds the `[remote.*]` push opt-ins. It does nothing if the
  table is already there, and it refuses, writing nothing and exiting 1, a root
  the watcher would skip or another agent already watches, and a `config.toml`
  that is a symlink or a hard link. See
  [docs/watching.md](docs/watching.md#configure-what-to-watch).
- **`gitmemory service install`** writes a systemd user unit (Linux) or a
  LaunchAgent (macOS) that keeps `gitmemory watch` running, and prints the
  commands that start it, check it, follow its log, restart it after an
  upgrade and remove it. It starts nothing itself, and it refuses, exiting 1,
  to overwrite a file that is already there. The file names the Python, the
  store and git's directory outright, because a user service inherits none of
  the shell's. See
  [docs/watching.md](docs/watching.md#keep-it-running).
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
- **`gitmemory --version`** prints the installed release. The recall skill
  runs it first and tells you when the CLI is older than the plugin; when the
  CLI lacks a subcommand or a flag the skill needs, the skill stops and says
  which instead of retrying without it. See
  [hook/README.md](hook/README.md#as-a-claude-code-plugin).
- **`recall` marks a subagent's turns.** A turn a subagent or workflow agent
  wrote prints its role as `subagent:<role>`, as in `subagent:user/text`, in
  the same column, so a line has as many fields as before. `Hit.is_sidechain`
  carries the mark, read per turn from the transcript's `isSidechain`. See
  [docs/USAGE.md](docs/USAGE.md#find-something).
- **Main sessions only.** [docs/watching.md](docs/watching.md#main-sessions-only-claude-code)
  gives a Claude Code watch pattern, `pattern = "*/*.jsonl"`, that leaves
  subagent and workflow transcripts out, and what that costs: those turns are
  never captured, and the dashboard's spend leaves out what they cost.
- **`recall` says when no watcher is collecting.** When a hook record has
  waited in the spool for more than an hour, `recall` prints one line on
  stderr before the results, naming the store and asking whether
  `gitmemory watch` is running for it. The time since the last capture cannot
  tell a stopped watcher from an idle one; a record no pass has drained can.
  Not under `--db`, and never on a store the hook does not ring. See
  [docs/watching.md](docs/watching.md#check-it).

### Changed

- `recall` now lists the user's own words newest first by default, not best
  match first. Pass `--policy relevance` for the old order. `index.search` is
  unchanged.
- **Install commands name Python 3.13.** Every documented `uv tool install`
  passes `--python 3.13`, so where the machine's own Python is older (Ubuntu
  24.04 ships 3.12) uv builds the tool on 3.13. Left to choose, uv can settle
  on the older one and refuse (it does with Python downloads off). pipx is
  down to a footnote: it installs into the Python it runs under, so it needs a
  3.13 interpreter. A test reads the floor from `pyproject.toml` and holds
  every install line to it. See
  [README.md](README.md#quickstart).

### Fixed

- **`latest-user` no longer takes a subagent's instructions for the user's
  words.** A subagent's first turn is user-role text that the parent agent
  wrote, its dispatch prompt, and while it was newer than the user's rule it
  led the default ahead of that rule. It now ranks with the agent's turns.
  `index.search` order is unchanged.

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
