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

`gitmemory init claude-code` (or `gitmemory init pi`) writes the table for
you, with the root [agents.md](agents.md) gives for that agent. That is still
not a default: the root is written because you named the agent, and the watcher
reads only the file. `init` appends to an existing `config.toml` and never
changes a byte already there, since the file also holds any `[remote.*]` push
opt-in. It writes the result to a new file and renames it into place, so a
running watcher reads the old file or the new one, and the new one is mode
0600. It does nothing if the table is already there. It refuses, writing
nothing and exiting 1, a root another agent already watches, a root the watcher
would skip (listed below; `init` prints the watcher's own message), a file that
is not valid TOML or already has a `watch` of another shape, and a
`config.toml` that is a symlink or has another hard link, because the rename
would leave the other name holding the old file. A `config.toml` it cannot read
is an error, exit 2. A running watcher picks the new table up on its next pass.

By hand, or for any other agent or root, `$GITMEMORY_HOME/config.toml`:

```toml
[[watch]]
agent = "claude-code"
roots = ["~/.claude/projects"]
```

| Key | Required | Meaning |
|---|---|---|
| `agent` | yes | Which adapter finds compaction boundaries. See below. |
| `roots` | yes | Directories to tail. `~` is expanded; symlinks are resolved. |
| `pattern` | no | Glob under each root, with `pathlib` semantics: `*` never crosses a `/`, and `**` spans any number of directories. Defaults to `**/*.jsonl`. |

Repeat the table for each agent you run. Absent, unreadable, or malformed
config means the watcher watches nothing — and says so on every pass, naming
the file and the reason, because "nothing configured" and "misconfigured" must
not look alike.

### Main sessions only (Claude Code)

The default pattern takes every transcript under the root, and under
`~/.claude/projects` most of them are not a conversation you had. Claude Code
writes each subagent to `<project>/<session>/subagents/agent-<id>.jsonl`, and
each workflow agent, beside the workflow's `journal.jsonl`, under
`<project>/<session>/subagents/workflows/<run>/`. A pattern one directory deep
takes only the main sessions, `<project>/<session>.jsonl`:

```toml
[[watch]]
agent = "claude-code"
roots = ["~/.claude/projects"]
pattern = "*/*.jsonl"
```

Put the `pattern` line in the `claude-code` table you already have, not in a
second table beside it. The watcher takes the files of every table, so another
table on the same root with the default pattern still finds every subagent.

A hook doorbell for a subagent's file then captures nothing: the spool only
hurries a file the pattern already found.

Prefer the default unless the store's size matters more than what this costs.
`recall` marks a subagent's turns `subagent:` (see
[USAGE.md](USAGE.md#find-something)), so they can be told apart without
leaving them out, and a policy can drop them from what it prints
([USAGE.md](USAGE.md#your-own-rule)). What leaving them out costs:

- **Those turns are never captured.** Once Claude Code cleans up old
  transcripts, they are gone. Until then this is reversible: put the default
  back, and the next pass captures each subagent file from its first byte.
- **The dashboard's spend undercounts.** It counts only what was captured, and
  subagents are where much of the spend goes: one measured session had 78% of
  its cache-read tokens in `subagents/**` ([DESIGN.md §2.2](DESIGN.md#22-claude-code-adapter--the-traps-each-a-test-case)).
- **What was sent survives, and usually what came back.** The parent session
  keeps the tool call that started a subagent or a workflow, with its
  instructions. A subagent's report comes back as that call's result. A
  background agent or workflow gets only a launch receipt there; its report
  comes later, as a task-notification turn in the parent, or not at all if the
  session ended first. What is lost is the subagents' own working turns, and a
  workflow's `journal.jsonl`, which holds each of its agents' results.
- Older Claude Code wrote a subagent's turns into the parent's own file, each
  line marked. A pattern cannot leave those out; the `subagent:` mark still
  labels them.

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
without boundaries beats capturing nothing, and it means `agent = "kimi-code"`
works the day before that adapter lands.

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

Ctrl-C exits 130. There is no daemonisation and no pidfile: `gitmemory watch`
is a foreground process, and how you keep it alive — a terminal you leave open,
or a user service, which `gitmemory service install` writes for you
([below](#keep-it-running)) — is yours to choose. Nothing here needs root.

## Keep it running

`gitmemory service install` writes one file for your user's service manager,
and starts nothing:

| Platform | File | Manager |
|---|---|---|
| Linux | `$XDG_CONFIG_HOME/systemd/user/gitmemory.service`, or under `~/.config` when that is unset or not an absolute path | systemd, as your user |
| macOS | `~/Library/LaunchAgents/io.github.doronp.gitmemory.plist` | launchd, in your login session |

It prints the file it wrote, the command line in it, and the commands below,
and runs none of them: no `systemctl`, no `launchctl`, no `loginctl`. Starting
it is yours to do, as installing the hook is. If the file is already there it
refuses, exit 1, and leaves it alone; the refusal ends with the line that
removes it, so paste that and install again. Any other platform exits 2, and so
does a store path with a line break in it.

A service starts with none of your shell's state, so the file carries it:

- **This Python, as it is,** with `-P -m gitmemory`. `-P` because a systemd
  user unit starts in `$HOME`, and `python -m` would run a `~/gitmemory.py` in
  place of the package. Not the interpreter's real path either, which is
  outside the virtual environment and cannot import gitmemory.
- **The store, as `--home <store>`.** A service does not read your shell's rc
  files, so an exported `$GITMEMORY_HOME` never reaches it, and the watcher
  would read `~/.gitmemory` while the hook writes somewhere else. If you move
  the store, remove the file and install again.
- **`PATH`**: the directory `git` was found in, then `/usr/bin:/bin`. No git on
  your `PATH` exits 2 and writes nothing.

On Linux:

```sh
systemctl --user daemon-reload && systemctl --user enable --now gitmemory.service   # start, now and at every login
loginctl enable-linger                        # keep it running after you log out
systemctl --user status gitmemory.service     # check it
journalctl --user -u gitmemory.service -f     # its log
systemctl --user restart gitmemory.service    # after upgrading gitmemory
systemctl --user disable --now gitmemory.service; rm ~/.config/systemd/user/gitmemory.service
```

On macOS:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.doronp.gitmemory.plist   # start, now and at every login
launchctl print gui/$(id -u)/io.github.doronp.gitmemory          # check it
tail -f ~/Library/Logs/gitmemory.log                             # its log
launchctl kickstart -k gui/$(id -u)/io.github.doronp.gitmemory   # after upgrading gitmemory
launchctl bootout gui/$(id -u)/io.github.doronp.gitmemory; rm ~/Library/LaunchAgents/io.github.doronp.gitmemory.plist
```

- **Linger** matters on a machine you reach only over SSH: without it, systemd
  stops your user services when your last session ends. `loginctl
  enable-linger` with no user name is for yourself, which stock systemd's
  polkit policy allows without a password; a distribution can change that.
- **It is restarted after a failure, 30 seconds later** (`Restart=on-failure`;
  `KeepAlive` on an unsuccessful exit). A watcher that cannot start — a store
  inside a git work tree, say — logs one line every 30 seconds until fixed.
- **Editing `config.toml` needs no restart;** the watcher re-reads it every
  pass. Upgrading gitmemory does, because the running process keeps the old
  code.
- **The macOS log is outside the store,** which commits everything in it.
  Nothing rotates it. On Linux the log is the journal.
- **Removing it stops it, then removes the file whether or not that worked.**
  A file written and never started has nothing to stop: `launchctl bootout`
  says `No such process` and exits 3, and the `rm` after the `;` runs anyway.

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
| `no .../config.toml; watching nothing` | The file is not where the watcher is looking. Check `$GITMEMORY_HOME`, or run `gitmemory init <agent>` to write it there. |
| Watcher quiet, store empty, no complaint | A root that **does not exist** is kept, not refused — before an agent has run for the first time that is the normal state, and `run` re-reads the config every pass, so it starts working the moment the directory appears. The cost is that a typo'd root and a not-yet-used one look identical. Check the path by hand; `gitmemory init` says so when the root it writes does not exist yet. |
| `compact_boundaries: []` everywhere | Adapter missing or `--no-parse`. The bytes are intact; only the boundaries are absent. |
| `source changed while parsing` | The transcript was rewritten between the parse and the copy — a compaction landing mid-pass. The bytes are captured; that one generation has no boundaries. Nothing to do. |
| `was captured from <path>, not <path>; pass a distinct --session-id` | Two transcripts collided on one session name, and the store refuses to mix them rather than duplicating both for ever. The advice in the message is for the CLI and there is no `--session-id` to pass here: rename or move either file — the name is derived from the path — and the next pass captures both. Until then the second one is **not being captured**, so this is not a warning to leave running. |

There is no discovery counter: a pass prints only when it captured something,
so "found 12 transcripts, all unchanged" and "found nothing" are the same
silence. Listing the roots yourself is currently the way to tell them apart.
