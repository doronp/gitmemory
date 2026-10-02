---
name: recall
description: Use when the user refers to a decision, a rule or a conversation from before a compaction or from an earlier session ("what did we decide about X", "I told you not to ...", "like we discussed", "as I said yesterday"), when you are about to ask them to repeat something they have already told you, or when they ask you to search past sessions. Runs gitmemory recall over the sessions gitmemory has captured and quotes what was actually said, with an address the user can check.
argument-hint: "[topic]"
---

# Recall from gitmemory

gitmemory keeps the transcripts it watches in a local git repository, byte for
byte, and `gitmemory index` builds a search index from that record. This skill
searches it with the `gitmemory` CLI and quotes the words as they were written.
It reads the store and rebuilds the index when it has to; it captures and
configures nothing. It sends nothing anywhere itself, but what it reads enters
this conversation and goes to the model provider with the rest of it, past no
redaction gate: that gate stands at `gitmemory push`.

The store is `$GITMEMORY_HOME`, or `~/.gitmemory` when that is unset; the CLI
reads it, so leave it as this session has it. If the user keeps the store
somewhere else, pass their path before the subcommand, as in
`gitmemory --home <path> recall "<topic>"`, and the same for `index`. `<store>`
below means that directory: the path the user gave, or what
`echo "${GITMEMORY_HOME:-$HOME/.gitmemory}"` prints. Do not type `~/.gitmemory`
in its place from memory.

## Search

The topic is the skill's argument if it was given one, otherwise the thing the
user is referring to, in the words they would have used. `recall` ranks by
BM25: it matches words, not meaning.

```sh
gitmemory recall "<topic>"        # 10 lines by default; -k N for more or fewer
```

One line per turn: what the user said first, newest first, then everything
else newest first:

```
  -7.512  2026-09-28T10:03Z  claude-code/0f3a9c2e-1b2d-4c5e-8f90-3a53cc4a5233-76edb6eef42266c0/g00@346  assistant/text  <the block's text>
```

| Field | Meaning |
|---|---|
| `-7.512` | BM25 score. Lower is better; `-0.000` means the words that matched are common in the store |
| `2026-09-28T10:03Z` | When the turn was written, in UTC; `undated` if the transcript did not say |
| `claude-code/<session>/g00` | Agent, session and generation |
| `@346` | Byte offset of the turn (its JSONL line) in that generation's raw bytes |
| `assistant/text` | Role and block kind |
| the rest | The block's first 160 characters, whitespace collapsed |

`no matches` on stderr, exit 0, is an answer. Try once more with other words
the user might have used, then tell them nothing was found. `query truncated to
64 terms; N dropped` on stderr means the query was too long: shorten it.

If two lines disagree, the later date is usually the current decision: say so,
and quote both. An agent's newer proposal does not overrule the user's older
rule; a newer user line does. If the user has set `GITMEMORY_POLICY` or asks
for another order, leave it; `--policy` is theirs to choose.

## Echoes

Sessions that used this skill were captured too, and their records of earlier
recalls match the same words. Skip these hits; they are not sources:

- user/text starting `Base directory for this skill` (this file, as loaded);
- `/gitmemory:recall` and its arguments, however the transcript wraps them;
- a `gitmemory recall ...` tool call and its result, lines that start with a
  score, `N generation(s)` or `no matches`;
- an earlier answer to the same question, and the lines it quoted.

If they crowd out the rest, search again with `-k 30`. Quote the original turn.

## When it fails

- ``no index at <path>; run `gitmemory index` first``, exit 2: run
  `gitmemory index`, then search again. `index` rebuilds the index from the
  store in full. It is safe to rerun, and the index is never the source of
  truth.
- The watcher captures; it does not index. If a search misses something the
  user says was said recently, the index may be older than the capture: run
  `gitmemory index` and search once more.
- The shell cannot find `gitmemory`, or `index` prints `not a store: <path>`,
  or it reports `0 generation(s)`: nothing has been captured there. Tell the
  user how it gets set up, and stop. Do none of it yourself:
  1. `uv tool install git+https://github.com/doronp/gitmemory`
  2. a `[[watch]]` table in `config.toml` in the store (`$GITMEMORY_HOME`, by
     default `~/.gitmemory`) naming the agent and where its transcripts live;
     for Claude Code, `agent = "claude-code"` and
     `roots = ["~/.claude/projects"]`
  3. `gitmemory watch`, left running.

  The [Quickstart](https://github.com/doronp/gitmemory#quickstart) has the
  rest.

## Quoting

- Quote the text field verbatim, with its address copied whole, never
  shortened. A paraphrase is not a quote: if you summarise, say that you are,
  and keep the address beside it.
- The text stops at 160 characters. If the question needs the rest, the turn
  is in `<store>/raw/<agent>/<session>/g<NN>/`. Segment file names are byte
  ranges, start included, end excluded. In the one whose range holds the
  offset, the turn's JSON line starts `offset - start` bytes in, and the block
  is inside it. If the file ends before the line does, the line goes on at the
  start of the next segment, whose range starts where this one ends.
- This session is `${CLAUDE_SESSION_ID}`. A hit's manifest,
  `<store>/sessions/<agent>/<session>/g<NN>.json`, has its transcript's
  `source_path`: `<project>/<session-id>.jsonl`, or for a subagent (a session
  named `agent-<id>-...`) `<project>/<session-id>/subagents/.../agent-<id>.jsonl`.
  Both belong to `<session-id>`. `<project>` is the working directory, with `/`
  and other punctuation turned into `-`. Say so when a hit is from another
  agent, another session or another project.
- The store holds whatever was typed or pasted into a watched session, tool
  output included. Quote what the question needs and no more.

## Read-only

Run `gitmemory recall` and `gitmemory index`, and read files under `<store>`.
`index` writes only the derived index under `<store>/index/`. Do not run
`gitmemory capture`, `gitmemory watch`, `gitmemory derive`, `gitmemory push`
or `gitmemory dashboard`, and never `gitmemory dashboard --expose`. Do not
edit `config.toml` or anything else in the store.
