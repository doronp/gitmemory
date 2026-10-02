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

1. Install and configure, as in the [README quickstart](../README.md#quickstart).
   Watch roots have no default; `config.toml` names each agent and where its
   transcripts live. [agents.md](agents.md) lists what can be captured.
2. Run the watcher and leave it running:

   ```sh
   gitmemory watch
   ```

   How it stays running (a LaunchAgent, a systemd user unit, a terminal you
   leave open) is your choice; [watching.md](watching.md#run-it) covers the
   flags and how to tell a misconfigured watcher from an idle one.
3. Optional: install the [hook shim](../hook/README.md) so a capture happens at
   the compaction boundary itself rather than at the next sweep. The watcher
   alone is enough for correctness.

To check the loop end to end without waiting, run one pass:

```console
$ gitmemory watch --once
captured 1 +1167B commit=f355b10ace4b872ffe77794cbf4183afb1dcf478
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
| `assistant/text` | Role and block kind |

`-k` sets how many lines come back.

## When memories disagree

A decision made three weeks ago and reversed last week are both in the store,
and both match the same search. gitmemory never deletes either one. What
`--policy` decides is which comes first, and the first line is the one an agent
tends to act on.

| Policy | Order | Use it when |
|---|---|---|
| `latest-user` (default) | The user's own words newest first, then the agent's turns and tool output newest first | The user's later word should win, and nothing else should |
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
`Hit` has the fields `recall` prints, plus `text` and `ts`, and
`gitmemory.index.when(hit)` reads `ts` as a UTC `datetime`.

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

The module is imported and runs with your permissions, so name only code you
would run anyway. A name that does not resolve is an error, not a silent
fallback to the default.

The offset is the point: it takes you back to the exact bytes, not to a
paraphrase of them. The raw segments live under
`raw/<agent>/<session>/g<NN>/`, named by the byte range they hold, so a turn at
offset 288 is in the segment whose range contains 288.

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
skill, `/gitmemory:recall`, that Claude loads on its own when you refer to a
decision or a rule from before a compaction or from an earlier session, or that
you run with a topic: `/gitmemory:recall retry loop`. It runs `recall`, and
`index` when there is no index yet or the one there is older than what you are
asking about. It quotes each line with its address and says when a hit comes
from another session. It writes nothing but the index.

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

## What it does not do

- **It does not summarise for you.** There is no LLM in the product. `recall`
  finds verbatim turns; the reading is yours or your agent's.
- **It does not guarantee completeness.** Contiguous is not complete: a process
  killed before it flushes leaves a stream that is contiguous and short.
  `verify` proves the first, not the second.
- **It does not claim to save tokens.** No such figure has been measured. See
  [RESULTS.md](RESULTS.md) for what has been.
