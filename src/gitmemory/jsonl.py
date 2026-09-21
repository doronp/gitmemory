"""Streaming JSONL reader with exact byte offsets.

Vendored from fable (https://github.com/grooverLab/fable), MIT, Copyright (c)
2026 Anoop Grover, with one local change marked [E3]. See THIRD_PARTY.md.

Kept as-is rather than rewritten: the two things it gets right are things a
reimplementation gets subtly wrong. `surrogateescape` keeps offsets byte-true
on invalid UTF-8 (`errors="replace"` inflates them — U+FFFD is 3 bytes), and
the `raw_decode` loop recovers multiple objects concatenated onto one physical
line, which occurs in real Claude Code transcripts.

Byte offsets from this module are gitmemory's only ordering authority.
Timestamps are never a sort key (six Claude Code line types have none).
"""

import json
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple


class Record(NamedTuple):
    lineno: int
    offset: int  # byte offset of this object's span in the file
    length: int  # byte length of the span
    obj: Any


_decoder = json.JSONDecoder()

# The largest physical line this reader will hold in memory. Everything an
# agent read lands verbatim in the transcript, so a line's length is chosen by
# whatever the agent fetched — a large HTTP body pasted into a tool result is
# one line. Measured over claude-code-log's 162 fixtures, 4,575 lines: median
# 1,067 bytes, p99 54 KB, largest 3.2 MB. 32 MiB is ten times that largest
# real line, and `iter_records` takes it as an argument for the caller who
# needs more. [E7]
MAX_LINE = 32 * 1024 * 1024


class LineTooLong(ValueError):
    """A physical line past the cap, refused before it is read into memory.

    A `ValueError` so that every caller already handling malformed input
    catches it, and its own class so the adapter can name the skip instead of
    folding it into `json_decode_error`.
    """


def iter_records(
    path: str,
    on_error: Callable[[int, Exception], None] | None = None,
    max_line: int = MAX_LINE,
) -> Iterator[Record]:
    """Yield Record for every JSON object in a JSONL file.

    Offsets are byte-accurate even for non-ASCII content; multiple
    concatenated objects on one line are yielded individually. A line longer
    than `max_line` is reported to `on_error` and skipped; its bytes are still
    in the store, which copies them without parsing.
    """
    with open(path, "rb") as f:
        line_start = 0
        lineno = 0
        while True:
            # `readline(max_line + 1)`, not `for raw in f`: iterating the file
            # materialises a whole physical line before anything can look at
            # its length, so a cap checked afterwards is a cap that has already
            # been paid. Measured before this bound, in a fresh interpreter per
            # run: a 33.6 MB line peaked at 266 MB RSS and a 134.2 MB line at
            # 1,002 MB — 7.5-7.9x the line, because the line is copied by
            # `strip`, decoded, parsed, re-sliced and re-encoded. [E7 parsing-F4]
            raw = f.readline(max_line + 1)
            if not raw:
                break
            lineno += 1
            this_start = line_start
            line_start += len(raw)

            if len(raw) > max_line:
                # Bounded drain to the end of the physical line, so the next
                # line's offset is still true and the rest of the file is still
                # read. Dropping out here instead would make one long line
                # truncate the transcript.
                while not raw.endswith(b"\n"):
                    raw = f.readline(max_line + 1)
                    if not raw:
                        break
                    line_start += len(raw)
                if on_error:
                    on_error(lineno, LineTooLong(f"line {lineno} exceeds {max_line} bytes"))
                continue

            # `raw.strip()` was a full copy of the line to answer a question
            # `isspace` answers in place; on a large line that copy was one
            # whole multiple of the blow-up above.
            if raw.isspace():
                continue

            # surrogateescape round-trips invalid UTF-8 byte-exactly, so the
            # offsets below are true byte offsets even for malformed input
            # (errors="replace" would inflate lengths: U+FFFD is 3 bytes).
            text = raw.decode("utf-8", errors="surrogateescape")
            pos = 0
            # Byte offset of `pos` within this line, carried instead of
            # recomputed. Upstream re-encoded `text[:pos]` per object, so a line
            # holding N concatenated objects cost O(N²) — the very case this
            # loop exists to handle. Measured: 6,400 objects on one line took
            # 165 ms, and the curve is quadratic, so a 175 MB line is minutes of
            # CPU on input a transcript chooses. Carrying the cursor is O(N) and
            # yields byte-identical offsets. [E3]
            byte_pos = 0
            while pos < len(text):
                ws = pos
                while pos < len(text) and text[pos] in " \t\r\n":
                    pos += 1
                byte_pos += pos - ws  # JSON's four whitespace characters are all 1 byte
                if pos >= len(text):
                    break
                try:
                    obj, end = _decoder.raw_decode(text, pos)
                # JSONDecodeError is a ValueError, so the ordinary path is
                # unchanged. `json` also raises bare RecursionError on deep
                # nesting and bare ValueError past the 4300-digit int limit —
                # both used to escape the whole capture, so one malformed line
                # made a session uncapturable. Malformed content is exactly what
                # an append-only store has to survive. [E2]
                except (ValueError, RecursionError) as e:
                    if on_error:
                        on_error(lineno, e)
                    break
                byte_len = len(text[pos:end].encode("utf-8", "surrogateescape"))
                yield Record(lineno, this_start + byte_pos, byte_len, obj)
                pos, byte_pos = end, byte_pos + byte_len


def read_span(path: str, offset: int, length: int) -> bytes:
    """Read an exact byte span — the recall fast path."""
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)
