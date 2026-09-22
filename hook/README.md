# The hook shim

`gitmemory-hook.sh` is a POSIX `sh` script with no dependencies. It writes its
standard input to the spool and exits 0. That is the whole program.

It is **not** how gitmemory captures your sessions. The watcher is. The hook
only makes a capture happen *sooner* — right before a compaction, instead of at
the next sweep. **If you never install this hook, gitmemory is still correct**;
you lose the guarantee that a segment is cut exactly at the compaction boundary.
That property is what makes a shell script in your agent's critical path an
acceptable thing to ship. What a broken run costs beyond that one capture is
[below](#what-a-broken-run-can-cost), and is not nothing.

## Install

gitmemory never edits another program's configuration. Installing the hook is
yours to do, and to undo.

`$GITMEMORY_HOME` **must be an absolute path.** Your agent sets the hook's
working directory to whatever project you are in, so a relative value would
create a separate spool under every directory you visit and the watcher would
read none of them. The shim refuses one and says so on stderr. It also refuses
when `$HOME` is unset and `$GITMEMORY_HOME` is not absolute, because the
default `$HOME/.gitmemory` would then expand to `/.gitmemory` while the watcher
resolving the same default through the password database finds your real home —
the two ends of the seam pointing at different directories, in silence.

Both refusals exit 0 and write to stderr, and **whether you ever see them is
your agent's decision**, not this shim's: some agents discard hook stderr
entirely. If you have installed the hook and no records are appearing, run it
by hand — `echo '{}' | ./gitmemory-hook.sh PreCompact` — where you can see what
it says. Either way the watcher's sweep still captures the session; a refused
hook costs latency, not bytes.

**A failed write says nothing at all**, and running it by hand is the only way
to find out why. An unwritable spool, a full disk and a refused `rename` all
exit 0 in silence. That is the opposite of the rule the two refusals above
follow, and it is on purpose: a refusal is a configuration you fix once, while
a full disk would put a line in your agent's transcript at every compaction
until you cleared it. The value of the trade is exactly this paragraph — if
you installed the hook and the spool is empty, the shim will not tell you, so
ask it directly.

### What a broken run can cost

Not only the one capture. Two things escape that description and both are
measured rather than argued:

- **A line of the shell's own noise, once.** Not the shim's — the shell's.
  With `ulimit -f` set in the environment, a payload over the limit killed
  `cat` with SIGXFSZ and `/bin/sh` announced it with this script's path and a
  line number. The failure itself was always handled correctly; the noise was
  the symptom, and it is now inside the redirect that the earlier `set -C`
  message taught us to want. [E7 S8]
- **The record is as large as the payload.** There is no size cap, and one is
  not coming. `head -c` would cost the same single fork, but it closes the pipe
  early and an agent that does not handle `EPIPE` on its own hook would then
  die — trading a large file for a broken session, which is the wrong way round
  for a program whose first rule is never to disturb the caller. Measured, a 20
  MB payload lands in the spool as a 20 MB record; the watcher reads it, drops
  it if it will not parse, and the sweep captures the session regardless.

For Claude Code, in `~/.claude/settings.json` — each event key takes an array of
matcher groups, and the event name is passed in `args`, because Claude Code
sends the payload on stdin and no arguments of its own:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/gitmemory/hook/gitmemory-hook.sh",
            "args": ["PreCompact"]
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/gitmemory/hook/gitmemory-hook.sh",
            "args": ["SessionEnd"]
          }
        ]
      }
    ]
  }
}
```

Replace `/absolute/path/to/gitmemory` with wherever you cloned this.

`Stop` is accepted by the shim but deliberately left out of the example: it
fires after every assistant turn, and the watcher does not force a capture on
it, so installing it buys nothing and costs one process per reply.

## The seam

```
$GITMEMORY_HOME/spool/
    .tmp-<pid>-<n>                        being written; a reader ignores any
                                          name starting with a dot
    <pid>-<event>[-<n>].json              complete; the content is the hook's
                                          stdin, byte for byte
```

`<event>` is `argv[1]` when it is one of `PreCompact`, `SessionEnd`, `Stop`, and
`unknown` otherwise. Publication is `rename(2)`, so a reader sees the whole
record or no record. A shim killed mid-write leaves a `.tmp-` behind; the
watcher sweeps those once they are too old to be a live hook.

A record is a doorbell, not data. The watcher reads the transcript path out of
it and then re-derives everything — how many bytes are new, where the boundary
is, whether the file was rewritten — from the transcript itself. A record naming
a path outside the configured watch roots is dropped.

## Latency

The shim is in the agent's critical path, so it is measured rather than asserted.
`tools/hook_latency.py` runs it against a realistic payload, and — because most
of what a benchmark like this measures is the benchmark — times spawning `true`
the same way on every iteration as a control:

| Metric | Shim | Spawning `true` |
|---|---|---|
| p50 | 7.42 – 7.53 ms | 1.84 – 1.89 ms |
| p95 | 8.88 – 9.82 ms | 2.38 – 2.55 ms |
| p99 | 10.24 – 11.50 ms | 2.80 – 3.09 ms |
| worst seen | 14.14 ms | 3.78 ms |

Three runs of 400 iterations on Darwin 25.6.0, arm64 (Apple M4 Pro), under
`/bin/sh` — here bash 3.2.57 in posix mode — against a 58.9 KB `PreCompact`
payload, at **load average 10.5 – 11.8**.

**A range, and three runs, because the single-run table this replaces published
a `max` of 37.18 ms that nothing since has come near.** A maximum is one sample
of one scheduling accident; 1200 iterations put the worst at 14 ms. The
percentiles from that first table — 7.43 / 9.06 / 10.48 — sit inside the ranges
above, so they reproduced; the max did not, and a number that does not reproduce
does not belong in a row a reader compares against.

**The load sensitivity this section used to claim is not there.** It reported
p50 rising 7.43 → 8.00 ms at load average 7.87, "the worst this machine sees",
and read the 7% as contention the breakdown below survives. These runs are at
load 10.5 – 11.8 — half again as loaded — and p50 does not move: 7.42, 7.48,
7.53. The 8.00 was inside run-to-run spread and was read as a trend. What can be
said is narrower and is what the table now says: across a 4× spread of load
average this shim costs 7.4 – 7.5 ms at the median, and the spread that does
move is in the tail. Re-measure with `python3 tools/hook_latency.py 400`, three
times, and print `uptime` beside them. **[E4, review: docs — the latency table's
quiescence; re-measured at E7 close]**

**Every number in this table used to be measured under `/bin/dash`, which does
not run this shim.** The shebang says `#!/bin/sh` and an agent firing the hook
gets whatever that is; preferring `dash` when installed was a habit picked up
from the test harness, which had the same bug. Fixing it moved more than the
totals — it moved the explanation, because the two shells charge very
differently for the same script.

So the shim's own share of p50 is **5.6 ms**, and the other 1.9 ms is what this
machine charges to start any process at all. Of that 5.6 ms:

- **~1.4 ms is the shell itself.** `/bin/sh` running a script that does nothing
  but `exit 0` costs 2.93 ms against `true`'s 1.54 ms. Under `dash` the same gap
  is 0.17 ms, which is where the old claim that "the shell is nearly free" came
  from. It is free in the shell we were not using.
- **~3 ms is two forks**, at roughly 1.5 ms each: `cat`, to move stdin to disk,
  and `mv`, to publish it atomically. Neither has a POSIX `sh` builtin
  equivalent and the `rename(2)` is the seam's whole contract, so both stay.
- The rest is the script's own builtin work — the collision loops, the `case`
  tests, `umask`.

Two forks have come off this path, and both were found the same way: by
counting them. `mkdir -p` used to run on every fire and now sits behind a
`[ -d ]` test that costs nothing, worth 1.3 ms. `date`, which named the record
with an epoch prefix, was worth 2.0 ms and was **not load-bearing** — an earlier
version of this page called it that, and no reader of the spool has ever read
the timestamp. The watcher wants the event, the arrival time is the file's own
mtime, and the ordering it seemed to provide feeds an order-independent fold.
Dropping it also took the grammar's only signed field with it: a clock set
before 1970 made `date +%s` negative and shifted every field in the name.

That page also said there were "two forks left" while three were running. `mv`
was never counted, which is how `date` survived an audit that was looking for
exactly this. The control column is what makes the remainder legible — without
it, 7.4 ms reads as irreducible overhead rather than as a shell, two forks, and
a list of what is left to argue with.
