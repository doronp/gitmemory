#!/bin/sh
# gitmemory hook shim. Writes stdin to the spool and exits 0, always.
#
# It constructs no JSON, parses nothing, and spawns nothing. The watcher
# re-derives every fact from the transcript itself, so this file is a doorbell:
# the worst a broken run can do is cost the session one immediate capture, and
# the watcher's sweep picks it up anyway.
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
        # side holds itself to exactly this standard (`__main__._UNSAFE` and the
        # `print` shadow beside it); the shim is the half that runs inside
        # somebody else's agent, so it is the half that matters more. One fork,
        # on a path that is already exiting. [E4, review: CLI 6]
        echo "gitmemory: GITMEMORY_HOME must be an absolute path, got '$(printf '%s' "$H" | tr -d '\000-\037')'" >&2
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
if ! cat 2>/dev/null > "$T"; then
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
