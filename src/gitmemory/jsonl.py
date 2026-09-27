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

import errno
import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterator
from typing import IO, Any, NamedTuple


class Record(NamedTuple):
    lineno: int
    offset: int  # byte offset of this object's span in the file
    length: int  # byte length of the span
    obj: Any


def open_untrusted(path: str) -> IO[bytes]:
    """Open a file someone else controls: no symlink, no FIFO, no device.

    `O_NONBLOCK` so opening a FIFO returns instead of waiting for a writer while
    the caller holds the store lock; cleared again once `fstat` says regular.
    [E9, review: 2]
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f"Not a regular file: {path}")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


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


class LineTruncated(ValueError):
    """A line that yielded objects, then stopped parsing with bytes to spare.

    A parse failure mid-line abandons the rest of that line: there is no
    resynchronisation point in concatenated JSON that isn't a guess, so the
    reader stops. That is the right call and it is not the finding. The
    finding is that it used to be indistinguishable from a line that simply
    failed to parse — same `json_decode_error`, count of one, and the
    accounting identity balances either way.

    Measured: a 844-byte transcript whose single line held five well-formed
    objects and one truncated fragment produced `records_seen=2`, one turn and
    one skip. 685 bytes — 81% of the file — left no trace, and every check in
    `conformance.py` passed.

    Raised only when the line already yielded at least one object, because
    that is the case where content demonstrably followed the failure. A line
    that fails at offset zero is an ordinary bad line and stays one.
    [E7 parsing-F2]
    """

    def __init__(self, lineno: int, residual: int, cause: Exception) -> None:
        super().__init__(f"line {lineno} stopped parsing with {residual} bytes left: {cause}")
        self.lineno = lineno
        # Failure point to end of physical line, terminator included — the
        # number as the reader has it, with no copy taken to trim it.
        self.residual = residual
        self.cause = cause


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
    with open_untrusted(path) as f:
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

            # A UTF-8 BOM is three bytes of file encoding, not three bytes of
            # JSON, and `raw_decode` refuses them — so a BOM used to cost the
            # whole first line, counted as a decode error. Skipped by moving
            # the span start past it rather than by slicing the line, so the
            # offsets stay true: the object still begins where it begins.
            # Claude Code does not write one; an adapter for an agent on
            # Windows will meet one. [E7 parsing-F15]
            if lineno == 1 and raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
                this_start += 3

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
            yielded = 0
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
                        # The residual is what the reader is choosing not to
                        # look at, and it knows the number exactly. Reporting
                        # it is the difference between "a line failed" and "a
                        # line failed and took 685 bytes with it". [E7
                        # parsing-F2]
                        on_error(
                            lineno,
                            LineTruncated(lineno, len(raw) - byte_pos, e) if yielded else e,
                        )
                    break
                byte_len = len(text[pos:end].encode("utf-8", "surrogateescape"))
                yield Record(lineno, this_start + byte_pos, byte_len, obj)
                pos, byte_pos = end, byte_pos + byte_len
                yielded += 1


def read_span(path: str, offset: int, length: int) -> bytes:
    """Read an exact byte span — the recall fast path."""
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)
