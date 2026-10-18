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
this conversation and goes to the model provider with the rest of it. The
redaction gate stands at `gitmemory push`, not here. `--redact` masks the
credential shapes that gate knows in what `recall` and `show` print, as
`sk-a…[103 bytes]`. Pass it to both whenever `echo "${GITMEMORY_REDACT:-}"`
prints anything but nothing or `0`, and whenever the user asks you to keep
credentials out of the conversation. Masking covers only what those two print,
so while you are masking, read nothing under the store by hand: `raw/`, the
manifests under `sessions/` and the index all hold it unmasked. If either
command rejects the flag, do not run it without it.

The store is `$GITMEMORY_HOME`, or `~/.gitmemory` when that is unset; the CLI
reads it, so leave it as this session has it. If the user keeps the store
somewhere else, pass their path before the subcommand, as in
`gitmemory --home <path> recall "<topic>"`, and the same for `index` and
`show`. `<store>` below means that directory: the path the user gave, or what
`echo "${GITMEMORY_HOME:-$HOME/.gitmemory}"` prints. Do not type `~/.gitmemory`
in its place from memory.

## The CLI's version

The plugin and the CLI are installed separately, so they can come from
different releases. This skill is written for gitmemory 0.2.0. Once per
session, before the first search, run:

```sh
gitmemory --version
```

If it prints that release or a later one, go on. If it prints an earlier one,
or exits 2 with `the following arguments are required: cmd` (a CLI from before
`--version` existed), the CLI is older than this skill: tell the user so in
one line, with the fix under [When it fails](#when-it-fails), and go on. If
what `recall` then prints is not laid out as below, say so, and do not judge
which of two lines is the current decision from their order.

## Search

The topic is the skill's argument if it was given one, otherwise the thing the
user is referring to, in the words they would have used. `recall` ranks by
BM25: it matches words, not meaning.

```sh
gitmemory recall "<topic>"            # 10 lines by default; -k N for more or fewer
gitmemory recall --redact "<topic>"   # the same, masked
```

If the topic starts with `-`, put `--` before it, after any flags, as in
`gitmemory recall --redact -- "<topic>"`. Otherwise `recall` reads it as a
flag, and the error that follows says nothing about the CLI's age.

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
| `subagent:user/text` | A subagent's turn, marked `subagent:` before any role/kind (`subagent:assistant/text` too, and `subagent:user` in a `show` header). Its user text is the instructions the parent agent gave it, not the user's words |
| the rest | The block's first 160 characters, whitespace collapsed |

`no matches` on stderr, exit 0, is an answer. Try once more with other words
the user might have used, then tell them nothing was found. `query truncated to
64 terms; N dropped` on stderr means the query was too long: shorten it.

If two lines disagree, the later date is usually the current decision: say so,
and quote both. An agent's newer proposal does not overrule the user's older
rule, and neither does a subagent's line; a newer user line does. If the user
has set `GITMEMORY_POLICY` or asks for another order, leave it; `--policy` is
theirs to choose. The order is applied to the top `-k` matches only, so if
echoes (below) or `subagent:` lines crowd out the rest, search again with
`-k 50`.

## Echoes

Sessions that used this skill were captured too, and their records of earlier
recalls match the same words. Skip these hits; they are not sources:

- user/text starting `Base directory for this skill` (this file, as loaded);
- `/gitmemory:recall` and its arguments, however the transcript wraps them;
- a `gitmemory recall ...` tool call and its result, lines that start with a
  score, `N generation(s)` or `no matches`;
- a `gitmemory --version` tool call and the one line it printed;
- a `gitmemory show ...` tool call and its result, text that starts `> ` or
  `- ` immediately followed by an `<agent>/<session>/gNN@<offset>` address
  and a date, which is `show`'s header. It holds the whole turn, dated later
  than the turn was: the source is the turn at the address in its `> `
  header, not the echo's own address;
- an earlier answer to the same question, and the lines it quoted.

Quote the original turn.

## When it fails

- A `gitmemory` command exits 2, what it prints starts `usage: gitmemory`, and
  its last line says `invalid choice`, or `unrecognized arguments` naming a
  flag (a word starting with `-`) that is not your topic: the CLI is older
  than this skill and lacks that subcommand or flag. Do not run it again
  without the flag or under another name. Tell the user which one is missing, and that running step 1
  below again updates the CLI, a pinned install included. If
  `unrecognized arguments` names only plain words, the CLI is not the problem:
  the topic went in unquoted. Put it in one pair of quotes and search again.
- ``no index at <path>; run `gitmemory index` first``, exit 2: run
  `gitmemory index`, then search again. `index` rebuilds the index from the
  store in full. It is safe to rerun, and the index is never the source of
  truth.
- The watcher captures; it does not index. If a search misses something the
  user says was said recently, the index may be older than the capture: run
  `gitmemory index` and search once more.
- ``<N> hook record(s) uncollected, the oldest for <age>: is `gitmemory watch` running for <store>? These results may be missing everything said since.``
  on stderr, before the results, exit 0: the hook has rung and no watcher has
  run a pass on that store since, so nothing said after that is being captured
  there. Answer from the results, say they may be missing everything since, and
  tell the user to start `gitmemory watch` with the `GITMEMORY_HOME` this
  session has, and to keep it running with `gitmemory service install` (step 3
  below). If they have installed a service already, it is not started, it is
  failing (its log says why), or it watches another store. Do not run any of
  it yourself. Reindexing cannot bring back what was said since; if the hits
  miss something older, the bullet above still applies.
- The shell cannot find `gitmemory`, or `index` prints `not a store: <path>`,
  or it reports `0 generation(s)`: nothing has been captured there. Tell the
  user how it gets set up, and stop. Do none of it yourself:
  1. `uv tool install --python 3.13 git+https://github.com/doronp/gitmemory`
  2. `gitmemory init claude-code`, which adds the `[[watch]]` table for
     `~/.claude/projects` to `config.toml` in the store (`$GITMEMORY_HOME`, by
     default `~/.gitmemory`)
  3. `gitmemory service install`, which writes a user service that keeps
     `gitmemory watch` running and prints the line that starts it; or
     `gitmemory watch` in a terminal left open.

  The [Quickstart](https://github.com/doronp/gitmemory#quickstart) has the
  rest.

## Quoting

- Quote the text field verbatim, with its address copied whole, never
  shortened. A paraphrase is not a quote: if you summarise, say that you are,
  and keep the address beside it.
- The text stops at 160 characters. If the question needs the rest, run
  `gitmemory show <address>` with the address copied whole, or
  `gitmemory show --context 2 <address>` for the two turns either side too,
  and `gitmemory show --redact <address>` if you passed `--redact` to
  `recall`. A masked address (one with `…[` in it) is not one `show` can
  look up: say the line stops there and give the address as printed. A
  header starts `>` for the turn at the address and `-` for a neighbour, and
  only headers start in column 0; quote the text under one without its
  indent. If the CLI is older than `show` (exit 2, `invalid choice: 'show'`),
  tell the user as [When it fails](#when-it-fails) says. If you are masking,
  say the line stops at 160 characters and give the address. Otherwise read
  the turn by hand: it is the JSON line that starts `offset - start` bytes
  into the segment under `<store>/raw/<agent>/<session>/g<NN>/` whose name,
  the byte range `start-end` with the end excluded, holds the offset, and it
  may run on into the next segment.
- This session is `${CLAUDE_SESSION_ID}`. Unless you are masking, a hit's
  manifest, `<store>/sessions/<agent>/<session>/g<NN>.json`, has its
  transcript's `source_path`: `<project>/<session-id>.jsonl`, or for a subagent (a session
  named `agent-<id>-...`) `<project>/<session-id>/subagents/.../agent-<id>.jsonl`.
  Both belong to `<session-id>`. `<project>` is the working directory, with `/`
  and other punctuation turned into `-`. While masking, go by the address
  alone. Say so when a hit is from another agent, another session or another
  project.
- Say so, too, when a quote is a `subagent:` line, and never give one as what
  the user said. When it and a main-session line say the same thing, quote the
  main-session line.
- The store holds whatever was typed or pasted into a watched session, tool
  output included. Quote what the question needs and no more.

## Read-only

Run `gitmemory --version`, `gitmemory recall`, `gitmemory show` and
`gitmemory index`, and, when you are not masking, read files under
`<store>`. `index` writes only the derived index under `<store>/index/`. Do
not run `gitmemory init`, `gitmemory capture`, `gitmemory watch`,
`gitmemory service`, `gitmemory derive`, `gitmemory push` or
`gitmemory dashboard`, and never `gitmemory dashboard --expose`. Do not edit
`config.toml` or anything else in the store. Leave `GITMEMORY_REDACT` as this
session has it.
