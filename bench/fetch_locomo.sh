#!/bin/sh
# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
# Fetch the third-party LoCoMo dataset (snap-research/locomo, CC BY-NC 4.0).
# Never vendored: only scores computed from it are published. [E9]
# This script is a utility for the gitmemory benchmark harness.
# It must never be pointed at the owner's own data under ~/.claude/projects or similar.
set -eu

DEST_DIR="${1:-${TMPDIR:-/tmp}/gitmemory-locomo}"
# The default is a predictable name in a shared TMPDIR: refuse a directory
# somebody else created, or `curl -o` follows their symlink.
umask 077
mkdir -p "$DEST_DIR"
if [ ! -O "$DEST_DIR" ]; then
  echo "Error: $DEST_DIR is not owned by you; pass a directory as the first argument." >&2
  exit 1
fi

REV="3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
FILE="locomo10.json"
URL="https://raw.githubusercontent.com/snap-research/locomo/$REV/data/$FILE"
DEST_FILE="$DEST_DIR/$FILE"
EXPECTED_SHA="79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"

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

echo "export GITMEMORY_LOCOMO=$DEST_FILE"
