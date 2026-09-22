#!/bin/sh
# Fetch the third-party conformance corpora. Not vendored: they belong to their
# authors, and every pi/oh-my-pi fixture carries its author's home directory in
# a `cwd` field, so a vendored copy would put somebody else's absolute paths in
# this tree. Pinned so the suite is reproducible.
#
# Never point this at your own ~/.claude/projects or ~/.pi/agent/sessions.
set -eu
DEST="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/.conformance}"
mkdir -p "$DEST"

# name|repo|pinned rev|sparse subtree|fixtures subdir (relative to the clone)
CORPORA="claude-code-log|https://github.com/daaain/claude-code-log|6ad029e|test/test_data|test/test_data
pi|https://github.com/earendil-works/pi|a8ed497|packages/coding-agent/test/fixtures|packages/coding-agent/test/fixtures
oh-my-pi|https://github.com/can1357/oh-my-pi|b52e1f5|packages/coding-agent/test/fixtures|packages/coding-agent/test/fixtures"

echo "$CORPORA" | while IFS='|' read -r NAME REPO REV SUBTREE FIXTURES; do
  DIR="$DEST/$NAME"
  if [ ! -d "$DIR/.git" ]; then
    git clone --filter=blob:none --no-checkout "$REPO" "$DIR"
  fi
  git -C "$DIR" sparse-checkout set "$SUBTREE" >/dev/null 2>&1 || true
  git -C "$DIR" checkout -q "$REV"
  echo "# $NAME @ $REV -> $DIR/$FIXTURES" >&2
done

# The two env vars the suites read. Both default to the paths above, so this is
# only needed when the corpora live somewhere else.
echo "export GITMEMORY_CC_FIXTURES=$DEST/claude-code-log/test/test_data"
echo "export GITMEMORY_PI_FIXTURES=$DEST/pi/packages/coding-agent/test/fixtures:$DEST/oh-my-pi/packages/coding-agent/test/fixtures"
