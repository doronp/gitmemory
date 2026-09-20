#!/bin/sh
# gitmemory hook shim. Writes stdin to the spool and exits 0, always.
#
# It constructs no JSON, parses nothing, and spawns nothing. The watcher
# re-derives every fact from the transcript itself, so this file is a doorbell:
# the worst a broken run can do is cost the session one immediate capture, and
# the watcher's sweep picks it up anyway.
umask 077
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
        echo "gitmemory: GITMEMORY_HOME must be an absolute path, got '$H'" >&2
        exit 0
        ;;
esac
E="unknown"
if [ "$1" = "PreCompact" ] || [ "$1" = "SessionEnd" ] || [ "$1" = "Stop" ]; then
    E="$1"
fi
# `[ -d ]` first: `mkdir` is an external command and costs about as much as the
# whole shell does (~1.5 ms of the shim's ~8 ms, measured). The directory exists
# on every fire but the first, so the test pays for itself immediately. [E4]
if [ ! -d "$H/spool" ] && ! mkdir -p "$H/spool" 2>/dev/null; then
    exit 0
fi
P=$$
N=0
while [ -e "$H/spool/.tmp-$P-$N" ] || [ -h "$H/spool/.tmp-$P-$N" ]; do
    N=$((N + 1))
done
T="$H/spool/.tmp-$P-$N"
set -C
if ! cat > "$T" 2>/dev/null; then
    set +C
    rm -f "$T" 2>/dev/null
    exit 0
fi
set +C
S=$(date +%s 2>/dev/null || echo "0")
B="$H/spool/${S}-${P}-${E}"
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
