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
read none of them. The shim refuses one and says so on stderr.

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
    <epoch_s>-<pid>-<event>[-<n>].json    complete; the content is the hook's
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
| p50 | 6.82 ms | 1.63 ms |
| p95 | 8.40 ms | 2.02 ms |
| p99 | 10.84 ms | 2.45 ms |
| max | 12.83 ms | 5.26 ms |

400 iterations on Darwin 25.6.0, arm64 (Apple M4 Pro), under `/bin/dash`,
against a 58.9 KB `PreCompact` payload.

So the shim's own share of p50 is **5.2 ms**, and the other 1.6 ms is what this
machine charges to start any process at all. Of that 5.2 ms, the shell is nearly
free — `dash` running a script that does nothing but `exit 0` costs 1.59 ms
against `true`'s 1.42 ms. What it buys is the external commands the script
forks, at roughly 1.5 ms each. There are two left: `cat`, to move stdin to disk,
and `date`, to name the record. Both are load-bearing and neither has a POSIX
`sh` builtin equivalent, so this is close to the floor for a shell hook.

A third, `mkdir -p`, used to run on every fire; it is now behind a `[ -d ]` test
and only runs once. That one-line change is the difference between the 8.3 ms
this table used to report and the 6.8 ms it reports now — which is the argument
for keeping the control column. Without it the number looks like irreducible
overhead instead of three specific forks, one of which was removable.
