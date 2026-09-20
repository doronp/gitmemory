#!/bin/sh
# Fetch the third-party conformance corpus (claude-code-log, MIT).
# Not vendored: 21 MB, and it belongs to its author. Pinned so the suite is
# reproducible. Never point this at your own ~/.claude/projects.
set -eu
DEST="${1:-${TMPDIR:-/tmp}/gitmemory-fixtures}"
REV="6ad029e"
if [ ! -d "$DEST/.git" ]; then
  git clone --filter=blob:none --no-checkout \
    https://github.com/daaain/claude-code-log "$DEST"
fi
git -C "$DEST" sparse-checkout set test/test_data >/dev/null 2>&1 || true
git -C "$DEST" checkout -q "$REV"
echo "export GITMEMORY_CC_FIXTURES=$DEST/test/test_data"
