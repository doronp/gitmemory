# The hook shim

`gitmemory-hook.sh` is a POSIX `sh` script with no dependencies. It writes its
standard input to the spool and exits 0. That is the whole program.

It is **not** how gitmemory captures your sessions. The watcher is. The hook
only makes a capture happen *sooner* — right before a compaction, instead of at
the next sweep. **If you never install this hook, gitmemory is still correct**;
you lose the guarantee that a segment is cut exactly at the compaction boundary,
and nothing else. That property is what makes a shell script in your agent's
critical path an acceptable thing to ship.

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
| p50 | 7.43 ms | 1.86 ms |
| p95 | 9.06 ms | 2.32 ms |
| p99 | 10.48 ms | 2.68 ms |
| max | 37.18 ms | 3.41 ms |

400 iterations on Darwin 25.6.0, arm64 (Apple M4 Pro), under `/bin/sh` — here
bash 3.2.57 in posix mode — against a 58.9 KB `PreCompact` payload.

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
