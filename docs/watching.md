# Running the watcher

The watcher is how gitmemory captures. Everything else is an optimisation.

It polls the transcript roots you configure, copies out whatever bytes appended
since it last looked, and commits. The [hook shim](../hook/README.md) makes a
capture happen *sooner* — at the compaction boundary rather than at the next
sweep — but it is not the mechanism. **If you never install a hook the system is
still correct.** If the watcher never runs, nothing is captured at all.

## Configure what to watch

Watch roots have no default, deliberately. A memory tool whose first run scans
your home directory is a tool you have to audit before you can trust it, so
gitmemory watches exactly what you name and nothing else.

`$GITMEMORY_HOME/config.toml`:

```toml
[[watch]]
agent = "claude-code"
roots = ["~/.claude/projects"]
```

| Key | Required | Meaning |
|---|---|---|
| `agent` | yes | Which adapter finds compaction boundaries. See below. |
| `roots` | yes | Directories to tail. `~` is expanded; symlinks are resolved. |
| `pattern` | no | Glob under each root. Defaults to `**/*.jsonl`. |

Repeat the table for each agent you run. Absent, unreadable, or malformed
config means the watcher watches nothing — and says so on every pass, naming
the file and the reason, because "nothing configured" and "misconfigured" must
not look alike.

### What gets refused, and why

A root is skipped, loudly, when it:

- **is a file rather than a directory.** Pointing at one transcript instead of
  the directory holding it is the common way to write this wrong, and it would
  otherwise capture nothing while looking configured.
- **contains `$GITMEMORY_HOME`, or sits inside it.** The store's own segments
  are `*.jsonl`, so a store under a watch root reads its own output back as new
  sessions, forever, at full poll rate — with `verify` reporting clean the whole
  time.
- **is `/`.** Walking the filesystem every five seconds is never the intent.

A *transcript* — as opposed to a root — is refused when it is a symlink, and
that applies to `gitmemory capture <path>` too: pass the path the link resolves
to. The sweep resolves every file it finds and drops the ones that resolve
outside their root, so under ordinary operation this refusal never fires. It is
there for the link that appears *after* that check and before the read, which
would otherwise copy a file from outside the watch root into the store as an
attested segment. [E7 fs-F10]

An `agent` with no adapter is *not* refused. You get a warning naming the
adapters that exist, and the watch runs anyway: an adapter supplies compaction
boundaries, while discovery and byte-copying need none. Capturing the bytes
without boundaries beats capturing nothing, and it means `agent = "hermes"`
works the day before the hermes adapter lands.

`$GITMEMORY_HOME` itself must not be inside a git work tree. The store owns its
own repository, and a store nested in a checkout gets its history rewritten by
whoever owns the outer one — a `git clean`, a branch switch, a rebase. It is
refused at start-up with the offending work tree named.

## Run it

```
gitmemory watch                 # foreground, until Ctrl-C
gitmemory watch --once          # one pass, then exit
```

| Flag | Default | Meaning |
|---|---|---|
| `--poll` | 5s | Seconds between passes. Minimum 0.01. |
| `--interval` | 3600s | Seconds before an *idle* session is captured again. A hook doorbell and a brand-new session both bypass it. |
| `--once` | off | One pass, then exit — 0 if the pass was clean, 1 if anything errored. The form to put in a supervisor that expects to be re-run. |
| `--no-parse` | off | Skip the adapter; record bytes with no boundaries. |

`--interval` is the coalescing knob. Without it an actively-appending transcript
would cut a segment every `--poll` seconds; with it, an ordinary session
produces a segment at each compaction and roughly one an hour otherwise.

Each pass prints what it did, or nothing when nothing happened. Errors are
repeated only when they change or once an interval, so a permanently broken
root does not drown the log it belongs in.

Ctrl-C exits 130. There is no daemonisation, no pidfile, and no installer:
`gitmemory watch` is a foreground process, and how you keep it alive — a user
LaunchAgent, a systemd user unit, a terminal you leave open — is yours to
choose. Nothing here needs root.

## Check it

```
gitmemory verify
```

Empty output and `0 problem(s)` or a list naming each hole. There is no third
answer, and a clean `verify` is a statement about contiguity — that the bytes
on disk tile the byte range they claim — not about whether your config points
anywhere useful. The two failures worth watching for are different shapes:

| Symptom | Likely cause |
|---|---|
| `no .../config.toml; watching nothing` | The file is not where the watcher is looking. Check `$GITMEMORY_HOME`. |
| Watcher quiet, store empty, no complaint | A root that **does not exist** is kept, not refused — before an agent has run for the first time that is the normal state, and `run` re-reads the config every pass, so it starts working the moment the directory appears. The cost is that a typo'd root and a not-yet-used one look identical. Check the path by hand. |
| `compact_boundaries: []` everywhere | Adapter missing or `--no-parse`. The bytes are intact; only the boundaries are absent. |
| `source changed while parsing` | The transcript was rewritten between the parse and the copy — a compaction landing mid-pass. The bytes are captured; that one generation has no boundaries. Nothing to do. |
| `was captured from <path>, not <path>; pass a distinct --session-id` | Two transcripts collided on one session name, and the store refuses to mix them rather than duplicating both for ever. The advice in the message is for the CLI and there is no `--session-id` to pass here: rename or move either file — the name is derived from the path — and the next pass captures both. Until then the second one is **not being captured**, so this is not a warning to leave running. |

There is no discovery counter: a pass prints only when it captured something,
so "found 12 transcripts, all unchanged" and "found nothing" are the same
silence. Listing the roots yourself is currently the way to tell them apart.
