#!/bin/sh
{ set +xv; } 2>/dev/null  # line 2 on purpose — see "the tracing guard" below
# gitmemory hook shim. Writes stdin to the spool and exits 0, always.
#
# It constructs no JSON, parses nothing, and spawns nothing. The watcher
# re-derives every fact from the transcript itself, so this file is a doorbell:
# the worst a broken run can do is cost the session one immediate capture, and
# the watcher's sweep picks it up anyway.
#
# Two refusals are loud and every write failure is silent, and the asymmetry is
# deliberate. A refusal is a configuration the user can fix and it fires once;
# a write failure can be a full disk, and a message on that path fires at every
# compaction for as long as the disk stays full — which is the noise this shim
# exists not to make. The cost is real and is stated where a user meets it:
# `hook/README.md` says that an installed hook producing no records should be
# run by hand, because that is the only place the reason appears. [E7 S10]

# The tracing guard, which is line 2 and has to be.
#
# `SHELLOPTS` is exported by some setups and `/bin/sh` here is bash, which
# honours it — the same mechanism the `:-` defaults below exist for, one option
# over. Measured with `SHELLOPTS=xtrace`: 700 bytes of trace into the agent's
# stderr on every compaction, including `+ H=<value>` with `$GITMEMORY_HOME`
# **unfiltered**, so an ESC in it reached the terminal raw. That is the sequence
# S7 strips from the one message that echoes this variable — stripped there and
# printed here, three lines earlier, without the message. The brace group is
# S8's trick and for S8's reason: it takes the trace of its own contents and
# costs no fork. `+v` as well, because `SHELLOPTS=verbose` is the same door.
# [E7 pair review]
#
# `+v` *narrows* that door rather than closing it, and where the guard sits is
# what decides by how much: `verbose` echoes input as the shell reads it, so
# every line above the guard is already on stderr before the guard runs. The
# guard used to sit here, under this comment, and the comment is what it cost:
# 1,723 bytes per compaction, measured `sh … PreCompact </dev/null` with
# `SHELLOPTS=verbose`. On line 2 it is **90** and deleting it costs 9,491 —
# both re-measured against this file as it now stands, so they are larger than
# the report's 35 and 8,375 by exactly the paragraph you are reading. Nothing is
# disclosed either way (verbose echoes source text, so
# `H="${GITMEMORY_HOME:-$HOME/.gitmemory}"` prints unexpanded), but 1,723 bytes
# per compaction is exactly the noise this file's opening paragraph promises not
# to make, and it grows every time someone adds a line of explanation above it.
# Hence the guard on line 2 and the explanation down here. `SHELLOPTS=noexec` is
# a door no line in this file can close — `-n` is read before anything executes
# and POSIX gives `set +n` no effect in a non-interactive shell — so it is
# written down in `hook/README.md` instead, with the rest of the silent-failure
# ceiling. [E7b L2-F6, L2-F7]
umask 077
# Every parameter is expanded with a `:-` default, so the shim survives being
# run under `set -u`. It does not set `-u` itself, but `SHELLOPTS=nounset` is
# exported into the environment by some setups and `/bin/sh` here is bash, which
# honours it: measured, the shim died on line 27 with `$1: unbound variable`,
# printing that into the agent's stderr and losing the doorbell. [E4, review]
#
# `$HOME` is the one that cannot be defaulted, because a wrong guess is a spool
# the watcher does not read. With `HOME` unset the default expands to
# `/.gitmemory`, which is absolute — so it passes the check below — and then
# fails to `mkdir` and exits 0 in silence. The watcher meanwhile resolves the
# same default through `expanduser`, which falls back to the password database
# and finds the real home, so the two disagree about where the seam is. Refused
# out loud instead, on the project's own rule that loud and degraded beats
# silent and degraded. An absolute `GITMEMORY_HOME` needs no `HOME` at all and
# is not refused. [E4, review]
case "${GITMEMORY_HOME:-}" in
    /*) ;;
    *)
        if [ -z "${HOME:-}" ]; then
            echo "gitmemory: HOME unset and GITMEMORY_HOME is not absolute" >&2
            exit 0
        fi
        ;;
esac
H="${GITMEMORY_HOME:-$HOME/.gitmemory}"
case "$H" in
    \~/*) H="$HOME/${H#\~/}" ;;
    \~) H="$HOME" ;;
esac
# A relative GITMEMORY_HOME is refused, not resolved. The agent sets the hook's
# cwd to whatever directory the user is working in, so a relative home makes a
# separate spool under every project the user visits — none of which the watcher
# reads, since it resolves the same variable against its own cwd. It also litters
# the user's checkouts. Loud on stderr and exit 0: never block the session. [E4]
case "$H" in
    /*) ;;
    *)
        # Stripped before it is echoed. This is the one message that puts an
        # environment variable's value into the agent's own transcript, and a
        # value containing ESC or CR does not print, it *edits*: `\033[2K\r`
        # erases the warning line and substitutes whatever follows. The Python
        # side holds itself to exactly this standard (`records.safe_text` and
        # the `print` shadow in `__main__`); the shim is the half that runs
        # inside somebody else's agent, so it is the half that matters more.
        # One fork, on a path that is already exiting. [E4, review: CLI 6]
        #
        # An allowlist, `-cd`, not the denylist `-d '\000-\037'` this was. That
        # range is C0 and only C0, and `safe_text` also spells out DEL, the C1
        # block, the bidi overrides and U+2028/9 — so the comment above claimed
        # a parity the code did not have. Measured against the shipped version:
        # DEL, U+009B (a working CSI on a terminal that decodes C1), U+202E and
        # U+2028 all reached stderr intact. A denylist cannot be fixed here
        # anyway: in UTF-8 those are two- and three-byte sequences and `tr`
        # deletes bytes, so removing the lead byte of U+202E leaves the other
        # two. Everything outside printable ASCII goes instead, `LC_ALL=C` so
        # that is a byte range and no multi-byte sequence can half-survive. The
        # message says so, because a value written in a non-Latin script comes
        # out empty and an empty value must not read as "unset". [E7 S7]
        echo "gitmemory: GITMEMORY_HOME must be an absolute path, got (printable ASCII only) '$(printf '%s' "$H" | LC_ALL=C tr -cd '\040-\176')'" >&2
        exit 0
        ;;
esac
E="unknown"
if [ "${1:-}" = "PreCompact" ] || [ "${1:-}" = "SessionEnd" ] || [ "${1:-}" = "Stop" ]; then
    E="$1"
fi
# `[ -d ]` first: `mkdir` is an external command and costs about as much as the
# shell itself does — 1.3 ms of the shim's 5.6 ms, measured under `/bin/sh`, not
# under the `dash` the first measurement used by mistake. The directory exists on
# every fire but the first, so the test pays for itself immediately.
# [E4; re-measured, review: shell MAJOR]
if [ ! -d "$H/spool" ] && ! mkdir -p "$H/spool" 2>/dev/null; then
    exit 0
fi
P=$$
N=0
# `[ -h ]` as well as `[ -e ]`: a *dangling* symlink is not `-e`, and it is the
# case that matters, because a link to a file that does not exist yet is how you
# get `cat >` to create one. This guard is load-bearing on the temp name and only
# here — `cat` follows a link, `mv` below does not. [E4, review: F2]
while [ -e "$H/spool/.tmp-$P-$N" ] || [ -h "$H/spool/.tmp-$P-$N" ]; do
    N=$((N + 1))
done
T="$H/spool/.tmp-$P-$N"
# And `set -C` for the window between that test and this write, which is a race
# rather than a state and so is the one guard here with no test. [E4, review: F2]
set -C
# `2>/dev/null` *before* `> "$T"`, not after. Redirections are applied left to
# right, so with the old order the shell reported a refused open — which is what
# `set -C` exists to cause — on a stderr it had not yet silenced, and
# `cannot overwrite existing file` landed in the agent's output. The branch
# below then ran and cleaned up correctly, so the only symptom was the noise,
# which is the whole thing this shim promises not to make. [E4, review]
#
# The brace group takes the stderr the *shell itself* writes after the child is
# reaped, which the redirections inside the group are too early to cover. With
# `ulimit -f` set in the environment — some CI images set it — a payload over
# the limit killed `cat` with SIGXFSZ and the shell announced it: measured,
# `gitmemory-hook.sh: line 93: 20074 Filesize limit exceeded: 25   cat 2>
# /dev/null > "$T"` went into the agent's stderr, script path and all. The
# branch below already handled it correctly; as with the refused open, the only
# symptom was the noise. A group is not a subshell, so this costs no fork.
# [E7 S8]
if ! { cat 2>/dev/null > "$T"; } 2>/dev/null; then
    set +C
    rm -f "$T" 2>/dev/null
    exit 0
fi
set +C
# No timestamp in the name. It was the leading field for a long time and no
# reader ever read it: the watcher wants the event, the ordering is an
# order-independent fold, and the arrival time is the file's own mtime. What it
# cost was a third fork on the hot path — `cat`, `date`, `mv` — worth 1.9 ms of
# the shim's 7.6 ms, on the one path in this system a user waits for. It also
# took the grammar's only signed field with it; a clock set before 1970 made
# `date +%s` negative and shifted every index in the name. [E4, review: shell MAJOR]
B="$H/spool/${P}-${E}"
F="${B}.json"
N=0
while [ -e "$F" ] || [ -h "$F" ]; do
    N=$((N + 1))
    F="${B}-${N}.json"
done
if ! mv "$T" "$F" 2>/dev/null; then
    rm -f "$T" 2>/dev/null
fi
exit 0
