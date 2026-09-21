#!/usr/bin/env python3
"""What `os.fsync` costs against what `F_FULLFSYNC` costs, on this machine.

`store._fsync_dir` chooses the cheap one deliberately and its docstring says by
how much. The docstring used to say "a factor of 106" from a measurement nobody
could repeat; this is the measurement. Run it before quoting a number.

    python3 tools/fsync_cost.py

macOS only for the comparison — `F_FULLFSYNC` is an Apple fcntl. Elsewhere the
script reports the `fsync` side and says the other one does not exist, which is
itself the answer to "should the store branch on platform".
"""

from __future__ import annotations

import os
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable

N = 200


def _median_us(fd: int, flush, n: int = N) -> float:
    times = []
    for _ in range(n):
        os.pwrite(fd, b"x" * 64, 0)  # dirty the page, or the flush is a no-op
        start = time.perf_counter()
        flush(fd)
        times.append((time.perf_counter() - start) * 1e6)
    return statistics.median(times)


def main() -> int:
    full_fsync: Callable[[int], None] | None = None
    if sys.platform == "darwin":
        import fcntl

        def _apple_full_fsync(fd: int) -> None:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)

        full_fsync = _apple_full_fsync

    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(os.path.join(tmp, "probe"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            cheap = _median_us(fd, os.fsync)
            expensive = _median_us(fd, full_fsync) if full_fsync else None
        finally:
            os.close(fd)

    print(f"{platform.platform()} · {N} iterations · medians")
    print(f"  os.fsync        {cheap:8.1f} µs")
    if expensive is None:
        print("  F_FULLFSYNC          n/a  (macOS only)")
        return 0
    print(f"  F_FULLFSYNC     {expensive:8.1f} µs   ({expensive / cheap:.0f}× the cost)")
    # The ratio is the unstable half of this: the denominator is tens of
    # microseconds and moves with cache state, while F_FULLFSYNC sits on the
    # drive and barely moves. Quote the two medians, not their quotient.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
