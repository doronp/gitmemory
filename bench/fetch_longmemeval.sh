#!/bin/sh
# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
# Fetch the third-party LongMemEval-Cleaned dataset (MIT licence).
# This script is a utility for the gitmemory benchmark harness.
# It must never be pointed at the owner's own data under ~/.claude/projects or similar.
# It fetches the Small version (longmemeval_s_cleaned.json, ~277 MB) of the cleaned benchmark.
set -eu

DEST_DIR="${1:-${TMPDIR:-/tmp}/gitmemory-longmemeval}"
mkdir -p "$DEST_DIR"

REV="98d7416c24c778c2fee6e6f3006e7a073259d48f"
FILE="longmemeval_s_cleaned.json"
URL="https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/$REV/$FILE"
DEST_FILE="$DEST_DIR/$FILE"
EXPECTED_SHA="d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    echo "Error: neither sha256sum nor shasum found." >&2
    exit 1
  fi
}

# Download to .part and only rename once the digest matches. Writing straight to
# the final name meant a Ctrl-C or a dropped connection left a short file that
# every later run treated as the cache: the digest check failed forever and the
# only repair was knowing to delete a file in TMPDIR by hand.
if [ ! -f "$DEST_FILE" ]; then
  echo "Downloading $URL to $DEST_FILE..." >&2
  curl -L -f -o "$DEST_FILE.part" "$URL"
  ACTUAL_SHA=$(sha_of "$DEST_FILE.part")
  if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    rm -f "$DEST_FILE.part"
    echo "Error: SHA256 mismatch on download!" >&2
    echo "Expected: $EXPECTED_SHA" >&2
    echo "Actual:   $ACTUAL_SHA" >&2
    exit 1
  fi
  mv "$DEST_FILE.part" "$DEST_FILE"
fi

ACTUAL_SHA=$(sha_of "$DEST_FILE")
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  echo "Error: SHA256 mismatch on cached file $DEST_FILE!" >&2
  echo "Expected: $EXPECTED_SHA" >&2
  echo "Actual:   $ACTUAL_SHA" >&2
  echo "Delete it and re-run." >&2
  exit 1
fi

echo "export GITMEMORY_LONGMEMEVAL=$DEST_FILE"
