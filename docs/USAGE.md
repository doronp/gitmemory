# Using gitmemory

The README gets it installed. This page is about getting value out of it: what
to run on a normal day, what the output means, and how to put the record back
in front of an agent that has forgotten it.

Every output below is real, from a four-turn transcript captured into an empty
store.

## What you get

An agent's context window is a cache. When it compacts, or a session ends,
whatever it dropped is gone from the agent, but not from the transcript on
disk. gitmemory keeps that transcript, byte for byte, in a local git
repository, and lets you search it. So:

- **"Why did we do it this way?"** has an answer that quotes the session where
  it was decided, not a summary of it.
- **A standing rule survives compaction.** "Don't add a retry loop" said forty
  turns ago is one `recall` away, with the byte offset it was said at.
- **You can tell a gap from silence.** `verify` proves the stored bytes tile the
  transcript with no hole, or names the hole. A memory you cannot check is a
  memory you cannot trust.
- **Nothing leaves the machine.** No network call, no LLM, no account.

## Set it up once

1. Install, as in the [README quickstart](../README.md#quickstart), then name
   what to watch:

   ```sh
   gitmemory init claude-code      # or: gitmemory init pi
   ```

   Watch roots have no default; `config.toml` names each agent and where its
   transcripts live, and `init` adds that table for you. [agents.md](agents.md)
   lists what can be captured.
2. Run the watcher and leave it running:

   ```sh
   gitmemory watch
   ```

   How it stays running is your choice. `gitmemory service install` writes a
   systemd user unit (Linux) or a LaunchAgent (macOS) that starts it at every
   login, and prints the line that starts it now; it starts nothing itself. On
   a Linux machine you reach over SSH, the `loginctl enable-linger` line it
   prints keeps it running after you log out
   ([watching.md](watching.md#keep-it-running)). A terminal you leave open
   works too. [watching.md](watching.md#run-it) covers the flags and how to
   tell a misconfigured watcher from an idle one.
3. Optional: install the [hook shim](../hook/README.md) so a capture happens at
   the compaction boundary itself rather than at the next sweep. The watcher
   alone is enough for correctness.

To check the loop end to end without waiting, run one pass:

```console
$ gitmemory watch --once
captured 1 +1.2 kB (1167 B) commit=f355b10ace4b872ffe77794cbf4183afb1dcf478
$ gitmemory verify
0 problem(s)
```

Each capture is one commit, so `git -C ~/.gitmemory log` is the history of what
was captured and when.

## Find something

Build the index, then search it. `index` is a full rebuild from the store and
can be rerun at any time; the index is never the source of truth.

```console
$ gitmemory index
1 generation(s)  4 turn(s)  4 block(s)  content=f99ac9c9b888
$ gitmemory recall "why did we drop the retry loop?"
  -0.000  2026-09-28T10:02Z  claude-code/0f3a9c2e-…-fc59494095426389/g00@0  user/text  Don't add a retry loop around the upload; the API is idempotent only per request id.
  -0.000  2026-09-28T10:03Z  claude-code/0f3a9c2e-…-fc59494095426389/g00@288  assistant/text  Understood. I dropped the retry loop and pass a request id instead, so a replay cannot double-charge.
```

One line per turn: what the user said first, newest first, then everything else
newest first (see [When memories disagree](#when-memories-disagree)):

| Field | Meaning |
|---|---|
| `-0.000` | The BM25 score. Lower is better; on a store this small every score rounds to zero |
| `2026-09-28T10:03Z` | When the turn was written, in UTC. `undated` if the transcript did not say |
| `claude-code/<session>/g00` | Agent, session and generation. A new generation starts only when the source file was rewritten |
| `@288` | The byte offset of the turn inside that generation's raw bytes |
| `assistant/text` | Role and block kind. `subagent:` before them, as in `subagent:user/text` or `subagent:assistant/text`, marks a subagent's or workflow agent's turn, which the transcript marks `isSidechain`: its user text is the instructions your agent gave it, not your words |

`-k` sets how many lines come back.

If the [hook](../hook/README.md) has rung and no watcher has collected the
record for an hour, `recall` says so on stderr, before the results it still
prints:
``1 hook record(s) uncollected, the oldest for 3 hours: is `gitmemory watch` running for <store>? These results may be missing everything said since.``
A running watcher drains the spool on every pass, so none has run on that store
since: start `gitmemory watch` with the hook's `$GITMEMORY_HOME`, and
`gitmemory service install` to keep it running
([watching.md](watching.md#keep-it-running)). [Check it](watching.md#check-it)
covers the other causes, such as a watcher that runs passes less often than
hourly. With `--db`, recall does not look.

## What recall searches

By default `recall` searches one project, and in it only what the user and the
assistant wrote as text. A recalled line goes back into an agent's context, so
what it searches by default is what reaches the agent.

- **The project** is the repository holding the current directory, or the
  directory `--project DIR` names: the nearest directory at or above it with a
  `.git`. A linked worktree, Claude Code's `.claude/worktrees/` included, joins
  its repository, even after it has been removed. A submodule or a clone
  vendored inside the project is a repository of its own. `~` and `/` are
  looked at like any other level, so a dotfiles repository in `~` is a project,
  and the search goes no higher. With one there, every directory under `~`
  that is in no other repository, `~/Downloads` as much as `~/notes`, is in
  that one project. Outside any repository a directory is only itself, not
  its subdirectories, so `~` does not gather every project under it. The `.git` entries are read from the filesystem; git is never run.
- **A turn's project** is that of the directory recorded on its own line. A
  session that `cd`s into another repository puts the lines after the `cd` in
  that repository's project. A line that records no directory is where its
  session was. pi records the directory only on its session header, so every pi
  turn is where its session started. A session that recorded none is in no
  project, and only `--all-projects` finds it.
- **The kinds** are user and assistant text. Tool calls, tool output, thinking
  and system lines are left out.

`--all-projects` searches every project, and the lines written in none.
`--all-kinds` adds every kind. `--project` and `--all-projects` cannot be
combined, and no environment variable sets any of the three, so a search
widens only when it is asked to. `recall --all-projects --all-kinds` searches
what 0.2.0's `recall` searched. The order can still differ under the default
`latest-user` policy: lines Claude Code marked as its own (`isMeta`, a
compaction summary) no longer lead as the user's words. `-k` counts turns
inside the scope.

When the scope left matches out, `recall` says how many on stderr, after the
results or after `no matches`:

```console
$ gitmemory recall "retry loop"
...
outside this scope: 3 matching turn(s) (--all-projects, --all-kinds to widen)
```

It counts distinct turns, as the results are counted, and names only the flags
you did not pass. One count covers both dimensions, so with neither flag it
can be this project's tool output alone. Nothing is printed when nothing was left out.

`show` is not scoped in this release: it prints any address you give it, and
`--context N` adds neighbouring turns of every kind.

### What the scope does not solve

- **Pasted and restated text.** Text the user pasted, and assistant text that
  restates a tool's output, are user or assistant text and pass. Role and kind
  record who wrote a line, not where its words came from.
- **Injected text that is not marked.** A user block that mixes typed words
  with a `<system-reminder>` carrying a cloned repository's `CLAUDE.md`, a
  skill load or slash-command expansion without `isMeta`, and pi's
  `custom_message` text all still read as the user's words.
- **Submodules and vendored clones.** Turns recorded while `cd`'d into a
  submodule or a vendored clone count as that repository's, not the parent's.
- **A work tree with no `.git` of its own**, as when a bare repository is
  checked out with `--git-dir` and `--work-tree`: nothing is guessed, and each
  directory is scoped by equality, without its subdirectories.
- **A removed worktree that lived outside its repository**
  (`git worktree add ../x`): nothing left on disk says whose it was, so it is
  only its own directory.
- **What bypasses `recall`.** `show`, the dashboard, and reading `raw/` by hand
  are not scoped.
- **An agent that widens anyway.** An agent can pass `--all-projects` or
  `--all-kinds`, and can follow what a recalled line says. The skill's rules
  against both are rules it follows, not gates
  ([SECURITY.md](../SECURITY.md#recall-output-is-untrusted-input)).
- **A stale rule in this project.** An instruction the user has since dropped
  still matches. Only the dates, and asking the user, settle it.

## Read the whole turn

A line stops at 160 characters. `show` prints the whole turn at an address,
read from the store rather than the index, so it needs no index and a stale
one cannot change what it prints. Copy the address whole: the ones above are
cut short with `…` to fit this page, and a shortened address is refused with
`not an address` and exit 2.

```console
$ gitmemory show claude-code/0f3a9c2e-1b2d-4c5e-8f90-3a53cc4a5233-fc59494095426389/g00@288 --context 1
- claude-code/0f3a9c2e-1b2d-4c5e-8f90-3a53cc4a5233-fc59494095426389/g00@0  2026-09-28T10:02Z  user
  text
    Don't add a retry loop around the upload; the API is idempotent only per request id.
> claude-code/0f3a9c2e-1b2d-4c5e-8f90-3a53cc4a5233-fc59494095426389/g00@288  2026-09-28T10:03Z  assistant
  text
    Understood. I dropped the retry loop and pass a request id instead, so a replay cannot double-charge.
- claude-code/0f3a9c2e-1b2d-4c5e-8f90-3a53cc4a5233-fc59494095426389/g00@592  2026-09-28T10:04Z  user
  text
    Good. Ship it.
```

Each turn starts with a header: `>` for the turn at the address, `-` for the
neighbours `--context N` (or `-C N`) adds either side, then the turn's own
address, date and role. A subagent's turns say `subagent:user` and
`subagent:assistant`. Under the header comes each block's kind, with the tool's
name for a tool call, and then its text, indented. Only headers start in
column 0, so nothing in a transcript can print a line that reads as one.

An offset inside a turn shows that turn, and its header gives where it starts.
An address the store does not hold (`not in the store`, `no turn at byte N`)
exits 1. `--redact` masks credentials in what it prints
([below](#masking-credentials-in-what-it-prints)).

## Masking credentials in what it prints

`recall` and `show` print what the transcript says, credentials included,
because the store is yours and on your disk. When their output is going
further, to an agent and with it to the agent's model provider, `--redact`
masks every shape the push gate knows, in the text and in the address, with
one exception for a session id ([below](#what-it-does-not-guarantee)). That is
twelve issuer-prefixed keys and four SUSPECT patterns, listed in
[`redact.py`](../src/gitmemory/redact.py). This sample is from a second
transcript, a one-turn session of its own:

```console
$ gitmemory recall --redact "staging key"
  -0.000  2026-10-02T09:41Z  claude-code/7c41d2e9-…-61a9a184ea213ec3/g00@0  user/text  Use AKIA…[20 bytes] for the staging key, never the prod one.
```

A masked span keeps its first four bytes and its length; in an address the
marker drops its space, so the address stays one field of a `recall` line or
a `show` header. A private key is masked from its BEGIN line to its END line,
or to the end of the text when there is none. Both commands mask the text as
written, and also find a header that a line break split, as a flattened line
would show it; `recall` then flattens its line and cuts it at 160 characters.
`show --redact` masks each block of the turn whole before it splits it into
lines, so a key's body goes with its header, and it masks a tool's name too.
A masked address is not one `show` can look up: it refuses it, exit 2, and
`recall` prints the full address when it is not masking.

`GITMEMORY_REDACT=1` makes it standing for both: unset, empty or `0` is off,
and any other value is on. There is no flag that turns a standing setting off;
for one call, run `GITMEMORY_REDACT=0 gitmemory recall ...`.

### What it does not guarantee

- It masks only those sixteen shapes. Anything else prints as written: a bare
  AWS secret key, `PGPASSWORD=…`, a custom `GH_TOKEN=…`, `glpat-` and `npm_`
  tokens, an Azure `AccountKey=…`, a password in prose, `curl -u user:pass`, a
  `.netrc` line.
- Nothing is decoded. Base64, URL-encoding and a token wrapped across lines get
  through. JSON `\u` escapes are masked, because both commands read the parsed
  text, not the raw bytes.
- It masks credentials; it does not make the output confidential. Hostnames,
  customer data, personal data and code are untouched.
- It covers only what `recall` and `show` print. `raw/`, the index database,
  the dashboard, `derived/` and any file an agent opens are not masked by it.
  The environment setting is a floor, not a boundary: an agent with a shell can
  unset it, or read `raw/`.
- A private key whose BEGIN line and body are in two blocks of one turn, such
  as two tool results, is masked to the end of the first block only; `show`
  prints the body under the second.
- The store lowercases session ids, so an AWS key id, a Google API key or a
  JWT used as a session id, by `capture --session-id` or as a transcript's
  file name, prints in the address lowercased and unmasked: those detectors
  match only the case the issuer writes. The manifest's `source_path` keeps
  it as it was.
- A mask shows the first four bytes and the length.
- The SUSPECT patterns also mask some prose about configuration.

## When memories disagree

A decision made three weeks ago and reversed last week are both in the store,
and both match the same search. gitmemory never deletes either one. What
`--policy` decides is which comes first, and the first line is the one an agent
tends to act on.

| Policy | Order | Use it when |
|---|---|---|
| `latest-user` (default) | The user's own words newest first, then the agent's turns and tool output newest first. A subagent's instructions are the agent's words, and so is text Claude Code injected and marked (`isMeta`, a compaction summary) | The user's later word should win, and nothing else should |
| `latest` | Newest first, whoever said it | Any later turn should win, the agent's included |
| `relevance` | Best BM25 match first | You want the agent to decide: every line carries its date, so it can see which turn came later |
| `module:function` | Whatever your function returns | Anything else: pinned decisions, a cut-off date, a different rule per project |

Why the default is not plain `latest`: the newest mention is often not a
reversal. After a compaction drops "don't add a retry loop", the agent proposes
one, and that proposal is newer than the rule and matches the same words. Under
`latest` it comes first; under `latest-user` the rule does, and a user who
changes their mind still wins, because their reversal is newer user text.
Ties go to the better match, and undated turns go last.

Set it per call with `--policy`, or for every call with `GITMEMORY_POLICY`. The
flag wins. Every policy reorders only the top `-k` hits, so a reversal that does
not match the query well enough to make the list cannot win. Raise `-k` if you
suspect one.

If you choose `relevance`, tell the agent what the dates are for:

```markdown
Before asking the user to repeat a decision, run `gitmemory recall "<topic>"`.
If two lines disagree, the later date is the current decision; say so.
```

### Your own rule

A policy is a function from a list of `Hit`s to a list of `Hit`s. It can
reorder and it can drop; it gets nothing that is not already in the store. A
`Hit` has the fields `recall` prints, plus `text`, `ts`, `is_sidechain`
(true for a subagent's turn), `cwd` (the directory the turn was written in, or
its session's, or None) and `is_meta` (Claude Code injected it as user text), and `gitmemory.index.when(hit)` reads `ts` as a
UTC `datetime`.

A validity window, where everything before a date you name is out of date, is
one such rule:

```python
# myrules.py
from datetime import UTC, datetime

from gitmemory.index import latest_user, when

REVERSED = datetime(2026, 9, 14, tzinfo=UTC)  # the day the upload rules changed


def since_reversal(hits):
    """Drop what was said before the reversal, then order as the default does."""
    return latest_user([h for h in hits if (t := when(h)) and t >= REVERSED])
```

```console
$ PYTHONPATH=. gitmemory recall "upload retries" --policy myrules:since_reversal
```

Leaving subagents' turns out when you read, rather than never capturing them
([watching.md](watching.md#main-sessions-only-claude-code)), is another rule
for the same file:

```python
def main_only(hits):
    """Drop every subagent's turn, then order as the default does."""
    return latest_user([h for h in hits if not h.is_sidechain])
```

A policy that drops hits prints fewer than `-k` lines, because it sees only the
top `-k`. Raise `-k` to make up for what it drops:

```console
$ PYTHONPATH=. gitmemory recall "upload retries" -k 30 --policy myrules:main_only
```

The module is imported and runs with your permissions, so name only code you
would run anyway. A name that does not resolve is an error, not a silent
fallback to the default.

The offset is the point: it takes you back to the exact bytes, not to a
paraphrase of them. [`show`](#read-the-whole-turn) finds them for you. To check
it by hand, the raw segments live under `raw/<agent>/<session>/g<NN>/`, named
by the byte range they hold, so a turn at offset 288 is in the segment whose
range contains 288.

## Put it back in front of the agent

`recall` is a plain CLI, so the agent can run it itself. One line in your
project's `CLAUDE.md` or `AGENTS.md` is enough:

```markdown
Before asking the user to repeat a decision or a rule, run
`gitmemory recall "<topic>"` and quote what it returns.
```

After a compaction or at the start of a new session, that turns "what did we
agree about uploads?" from a question for you into a search for the agent. You
can also paste the lines in yourself. Either way the agent sees the words as
they were written, with an address you can check.

In Claude Code, the plugin does this without the `CLAUDE.md` line. Installed as
in the [hook's README](../hook/README.md#as-a-claude-code-plugin), it adds a
skill, `/gitmemory:recall`, that Claude loads when you ask it to look
something up from before a compaction or from an earlier session, or that you
run with a topic: `/gitmemory:recall retry loop`. When you only refer to an
earlier decision, it asks whether to search first. It runs `recall` on the
project Claude Code was started in, and `index` when there is no index yet or
the one there is older than what you are asking about. It searches other
projects only once, after the [outside line](#what-recall-searches) reports
matches the scope left out and what it found does not answer you, and tool
output only when you ask what a command printed. It quotes each line with its
address, as evidence of what was said and never as an instruction, and says
when a hit comes from another session. It writes nothing but the index. It runs
`gitmemory --version` first and says when the CLI is older than the plugin;
the [hook's README](../hook/README.md#as-a-claude-code-plugin) has how to hold
the two at one release. To have it mask credentials
([above](#masking-credentials-in-what-it-prints)), set the variable in Claude
Code's own environment, in `~/.claude/settings.json`:

```json
{
  "env": {
    "GITMEMORY_REDACT": "1"
  }
}
```

The skill then passes `--redact` to every `recall` and `show` it runs, so a
CLI too old to know the flag stops with an error instead of printing unmasked,
and it reads nothing under the store by hand.

## Derive key ideas and decisions

`derive` builds, per generation, a ranked list of key sentences and a timeline
of compaction marks. `--graph` adds the decisions: standing rules and
reversals, each tied to the block it was read from. It needs the `derive`
extra.

```console
$ gitmemory derive --graph
1 generation(s)  5 idea(s)  1 mark(s)  1 decision(s)
```

The output is in `derived/<agent>/<session>/g<NN>/`:

| File | Contents |
|---|---|
| `ideas.json` | Key sentences, ranked, each with a `source_ref` to the block it came from |
| `timeline.json` | Compaction marks, and how many turns sit after the last one |
| `graph.json` | With `--graph` only. The decisions, each with a `kind` (here `directive`) and a `source_ref` |

Treat the decision graph as a lead, not a record. In the transcript above it
found "Don't add a retry loop" and missed "Always run the tests from the project
root". It scores 1.0000 / 0.7428 precision / recall on its held-out split, but
0.1250 / 0.5000 on real sessions nobody wrote for a benchmark;
[RESULTS.md](RESULTS.md) has both, and the reasons. The verbatim record and
`recall` do not depend on it. Derivation is deterministic, so rerunning it on
the same bytes gives byte-identical files, and `git diff` on `derived/` shows
what changed.

## Browse it

```sh
gitmemory dashboard
```

This serves the index through Datasette, read-only, on loopback. It prints a
single-use sign-in URL; open that, not the bare address. It needs the `serve`
extra. `--expose` is required to bind anything but loopback, because that
serves every transcript to the network.

## Before anything leaves the machine

`gitmemory push` runs the redaction gate over everything a push would transmit:
the files, the seams between segments, and the git objects. It refuses on a
finding, and there is no override flag. It does not send yet. If a credential
has already landed in the store, [SECURITY.md](../SECURITY.md#if-a-credential-lands-in-the-store)
says what to do.

`recall` and `show` output that an agent reads goes to that agent's model
provider, and no gate stands in that path. `--redact` masks the gate's shapes
in it ([above](#masking-credentials-in-what-it-prints)); it masks, and it
never refuses.

## What it does not do

- **It does not summarise for you.** There is no LLM in the product. `recall`
  finds verbatim turns; the reading is yours or your agent's.
- **It does not guarantee completeness.** Contiguous is not complete: a process
  killed before it flushes leaves a stream that is contiguous and short.
  `verify` proves the first, not the second.
- **It does not claim to save tokens.** No such figure has been measured. See
  [RESULTS.md](RESULTS.md) for what has been.
