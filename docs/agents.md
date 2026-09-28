# Which agents gitmemory can capture

gitmemory captures any agent whose history meets four properties. This page
defines the four and lists what exists today: which adapters ship, which are
planned, and what the rest would need before they can be supported. The survey behind it read
twenty-nine agents at source in September 2026.

## The four properties

"Coding agent" was never the precondition. It is a coincidence of which
products happened to write the right kind of file. The actual requirement is
four properties, and each one is testable before a line of adapter is written:

| | | Why it is load-bearing |
|---|---|---|
| **P1** | The history is on your filesystem | Not "also synced"; on disk, where you are the data controller |
| **P2** | New events are **appended** | Earlier bytes are never rewritten. A file re-serialised whole on every turn fails this even though it is local |
| **P3** | One self-delimiting record **per line** | The store cuts a growing file into byte segments at arbitrary offsets and must be able to concatenate N of them and reparse the result. A JSON *array* cannot be cut that way. Neither can a SQLite database, or a directory of one file per event |
| **P4** | The record shape is documented, or readable from open source | Without it you still get byte-for-byte capture. You do not get an index or a derivation |

An adapter is two symbols — `AGENT` and `parse(path) -> Session` — plus
`tests/conformance.py::check_adapter`. An agent that fails **P4 only** can be
captured today with no adapter at all. An agent that fails **P2 or P3** needs a
materializer, which is a new ingestion path into the store and is not built.

### The class P1–P4 actually describes

The scope sentence every measurement here supports is not "for coding agents"
and not "for any agent". It is: **for any agent whose transcript is a local
append-only file, on a machine whose owner is the data controller.**

Nothing in the numbers is coding-specific, and that is checkable rather than
asserted: the headline result is 470 LongMemEval instances, a conversational
QA benchmark with no code in it, and the decision extractor's honest zero was
scored on human turns, not on diffs. The parser is per-line JSON; what the
lines are *about* never reaches it.

So the class is wider than the roadmap below, which is a roadmap of coding
agents only because that is where the survey looked — twenty-nine products,
all of them coding agents. Four adjacent classes look like they satisfy P1–P4,
and **none of them has been read at source, so each is a candidate and not a
claim**: on-prem enterprise agent runtimes that log to disk by policy; local
agent frameworks and SDKs that write a JSONL event log per run; ops and
robotics event logs, where append-only is the norm rather than the exception;
and support or ticketing harnesses that keep a per-conversation file. Each
needs the same four-property read the coding agents got before it belongs in a
table.

The one property that will bite outside the coding field is P3's *self-
delimiting per line*, not P1 or P2: an event log is usually append-only and
usually local, and is just as usually a rotating multi-file stream. That is a
store seam — the same one [issue #8](https://github.com/doronp/gitmemory/issues/8)
opens for Cline's single global `hooks.jsonl` — rather than a parser.

### The roadmap, and the field it is a roadmap of

Twenty-nine agents were read at source in September 2026 — the writer call, not
the blog post. The survey is not flattering to the plan this file used to
state:

- **The field is migrating away from the one format this project requires.**
  Goose still ships the JSONL reader that proves it used to write JSONL, and
  uses it only to migrate old sessions into SQLite. opencode moved off per-key
  JSON. OpenClaw's JSONL transcripts are read only by its own Doctor importer.
  Three products, one direction, one year.
- **Two of the three adapters this file used to promise are no longer
  adapters.** opencode is SQLite now. "Kimi" named a repo whose description
  today begins `[Archived]`; the successor, Kimi Code CLI, is a clean JSONL
  event log and is the retarget.
- **Compaction boundaries are getting *more* observable, not less.** Codex
  writes the post-compaction context inline next to the turns it replaces.
  Kimi Code types the boundary as `context.apply_compaction`. Gemini CLI fires
  a `PreCompress` hook before it happens. The pitch is not "we reveal a hidden
  boundary" — for these it is "we still have the bytes the boundary dropped".

| Agent | P1 | P2 | P3 | P4 | Status |
|---|---|---|---|---|---|
| Claude Code | y | y | y | y | **shipped** — `~/.claude/projects/**/*.jsonl` |
| pi / oh-my-pi (one format family, two dialects) | y | ~ | y | y | **shipped** — `~/.pi/agent/sessions/**/*.jsonl`, one adapter, `"omp"` an alias |
| Kimi Code CLI | y | y | y | y | planned — reads clean from source; no public fixtures to conform against |
| OpenAI Codex CLI | y | ~ | y | y | planned — needs a corpus, and a story for the `.jsonl.zst` a 7-day-old rollout becomes |
| Gemini CLI | y | y | y | y | planned — needs a corpus |
| DeepSeek Harness | y | y | ~ | y | planned — plain JSONL only under `compression: 'none'`; the shipped default is zstd-framed |
| Cline (`hooks.jsonl` audit stream) | y | y | y | y | planned — one global file for all sessions; the store pins one session to one path |
| Cursor Agent CLI | y | ? | y | **n** | planned — no vendor schema, two on-disk layouts, two record dialects |
| Hermes, OpenClaw, Kilo Code, opencode, Goose, Cline's own transcript, Cline's ancestor Roo Code, OpenHands, Freebuff, Continue.dev, CodeGPT | | | | | **not supported yet** — each needs a materializer or an upstream change; see below |

`~` means the append-only property holds with a bounded, named exception:
oh-my-pi rewrites a fixed-width 256-byte title slot at the head of a live file
(and the session manager rewrites the file whole on resume when records needed
migrating, which the adapter handles as a new generation rather than as
corruption); Codex replaces whole rollout files on a startup migration. `?` is
not a weaker `~`: nothing in Cursor's public source settles the question either
way, and the honest cell for an unread property is not a guess.

### What the rest would need, and why

None of these is ruled out. Each fails one of the four properties *as it
stands*, and supporting it would take one of three things: a **materializer**
(not built) that reads the agent's own store and writes an append-only stream
for gitmemory to capture; a **hook or export** the agent already offers, used
as the source instead of its transcript; or an **upstream change** in the agent.

- **Hermes Agent** is the largest agent on the OpenRouter board. Its transcript
  is `~/.hermes/state.db`, a SQLite file whose rows are rewritten in place at
  every compaction (`UPDATE messages SET active = 0, compacted = 1`), so there
  is no append-only file to tail. It does emit a JSONL export in exactly the
  shape the Claude Code adapter parses, but the export mints a fresh uuid per
  line on every run, so two exports of one session never agree on an id.
  Supporting it may need either a materializer over `state.db` that keeps the
  rows a compaction marks inactive, or stable ids in the export, which is a
  small upstream change and would make the existing adapter usable almost as is.
- **OpenClaw** used to write JSONL transcripts; today only its own Doctor
  importer reads them. Supporting it may need a materializer over the store it
  moved to, or an export of that store.
- **opencode** and **Goose** keep sessions in SQLite. Both would need a
  materializer. Goose still ships the JSONL reader it used before the move,
  which is a useful map of the record shape.
- **Cline** rewrites its whole transcript with `writeFileSync` on every agent
  loop *iteration*, not every turn, and the key that changes sits about thirty
  bytes into the file, so no prefix stays stable. Its `hooks.jsonl` audit
  stream is append-only and is already on the planned list above; that is the
  likely route, and [issue #8](https://github.com/doronp/gitmemory/issues/8)
  is the store change it needs.
- **Continue.dev** is local, human-readable, Apache-2.0, and re-serialises the
  whole session on every save. A materializer that diffs successive saves
  would work, with the caveat below.
- **Aider** writes its history into the git working tree, which is the closest
  anything in the field comes to this project's own premise. It prefixes every
  user line with `#### ` and writes assistant output with no prefix, so an
  assistant reply containing a heading looks exactly like a user turn.
  Supporting it may need a second signal to tell the roles apart, or an
  unambiguous assistant prefix upstream.
- **Amp** keeps the thread of record on the vendor's servers. Supporting it
  would need an export or API from the vendor, then a materializer over that.

The caveat for every materializer route: the contiguity proof would then cover
the materialized stream, not the agent's own bytes. Anything the agent rewrote
between two reads of its store is invisible to it. That is weaker than the
guarantee for the agents above, and the page for each such agent should say so.
