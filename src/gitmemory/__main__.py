"""`python -m gitmemory` — capture, verify, push.

Deliberately thin. `push` exists at E2 only so the refusal is a real, runnable
surface rather than a paragraph in a design doc: DESIGN.md §2.4 promises push
is opt-in per remote and gated on redaction, and a promise you cannot execute
is not a gate. The transport itself lands with the git daemon in E4.
"""

from __future__ import annotations

import argparse
import builtins
import os
import re
import sqlite3
import sys

from . import daemon, derive, index, redact, store
from .adapters import get as get_adapter

# Everything this module prints is bytes an attacker may have chosen: a recall
# hit is transcript content verbatim. `\x1b[2K\r` erases the line it was found
# on, which is how one hit hides the hit above it, and U+202E reverses the
# apparent order of what is left. Neither is ever meaningful in a transcript.
_UNSAFE = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f"  # C0 except tab and newline, DEL, C1
    "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"  # bidi marks and overrides
    "\u2028\u2029]"  # line and paragraph separators
)


def _safe_str(text: str) -> str:
    r"""`text` with every control, bidi, and separator character spelled out.

    Tab and newline survive because the CLI's own layout uses them; `\r` does
    not, because carriage return is the erase.
    """
    return _UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def print(*args, sep=" ", end="\n", file=None, flush=False):  # noqa: A001 - see below
    """`builtins.print`, but escaping what it is handed.

    Shadowing the builtin for the whole module is deliberate: the alternative
    is remembering to wrap every call site, and the one that gets forgotten is
    the vulnerability. `end` and `sep` are the caller's own, never transcript
    content, so they pass through.
    """
    builtins.print(*(_safe_str(str(a)) for a in args), sep=sep, end=end, file=file, flush=flush)


def _capture(args) -> int:
    home = store.resolve_home(args.home)
    if args.session_id:
        # An explicit id skips `capture_one`'s naming, not its parse handling.
        boundaries = None
        if not args.no_parse:
            try:
                session = get_adapter(args.agent).parse(args.source)
                boundaries = [e.byte_offset for e in session.events if e.kind == "compaction"]
            except (OSError, RecursionError, ValueError) as exc:
                print(f"parse failed ({exc}); capturing bytes without boundaries", file=sys.stderr)
        cap = store.capture(
            args.source, args.agent, args.session_id, home=home, boundaries=boundaries
        )
    else:
        cap = daemon.capture_one(
            home,
            args.source,
            args.agent,
            parse=not args.no_parse,
            log=lambda m: print(m, file=sys.stderr),
        )
    if cap.diverged:
        print(f"diverged: {cap.diverged}", file=sys.stderr)
        print(f"sealed generation {cap.generation - 1}; now writing g{cap.generation:02d}")
    print(f"{cap.manifest_path}  +{cap.appended}B  size={cap.size}  g{cap.generation:02d}")
    return 0


def _seconds(text: str) -> float:
    """A finite, non-negative number of seconds.

    `type=float` accepted anything `float()` did. `--poll 0` spun a core at 99%
    with nothing in the log; `--poll inf` reached `time.sleep` and raised
    `OverflowError`, which is an `ArithmeticError` and so is not in `main`'s
    except clause — a traceback, from a typo. `--poll -1` was caught, but only
    by `time.sleep` at the *end* of the first pass, so the diagnosis arrived
    underneath a line saying the capture had succeeded. Validating in the parser
    puts all three before `gitrepo.init` instead of after a commit.
    [E4, review: CLI 6]
    """
    value = float(text)  # argparse turns a ValueError here into its own usage error
    if value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError(f"{text} is not a finite number of seconds")
    if value < 0:
        raise argparse.ArgumentTypeError(f"cannot be negative: {text}")
    return value


def _poll_seconds(text: str) -> float:
    """`_seconds` with a floor. Only `--poll` has one — `--interval 0` is
    meaningful (capture every pass) whereas `--poll 0` is a busy loop."""
    value = _seconds(text)
    if value < _MIN_POLL:
        raise argparse.ArgumentTypeError(f"must be at least {_MIN_POLL}s, not {text}")
    return value


# Low enough to be a deliberate choice for a test, high enough that the busy
# loop at 0 is unreachable.
_MIN_POLL = 0.01


def _watch(args) -> int:
    home = store.resolve_home(args.home)
    # The emptiness check used to live here, once, before the loop — so it
    # described the config as it was at start-up and never again. `run` re-reads
    # every pass and now reports what it finds, which is the only version of
    # this message that stays true. [E4, review: CLI 2]
    return daemon.run(
        home,
        poll=args.poll,
        interval=args.interval,
        once=args.once,
        parse=not args.no_parse,
        log=lambda m: print(m, file=sys.stderr),
    )


def _not_a_store(home: str | None) -> int | None:
    """Exit 2 and say so, rather than letting an empty glob read as a clean store.

    Shared by the two commands that are otherwise vacuous on a path that is not
    a store: `verify` reported `0 problem(s)` and exited 0, and `index` reported
    zero of everything and *created* the directory on the way. `recall` already
    had the floor (`no index at ...`, exit 2) and this is its wording.
    [E4, review: CLI 3]
    """
    if store.is_store(home):
        return None
    print(f"not a store: {store.resolve_home(home)}", file=sys.stderr)
    return 2


def _verify(args) -> int:
    if (code := _not_a_store(args.home)) is not None:
        return code
    problems = store.verify(args.home)
    for p in problems:
        print(p, file=sys.stderr)
    print(f"{len(problems)} problem(s)", file=sys.stderr if problems else sys.stdout)
    return 1 if problems else 0


def _push(args) -> int:
    home = store.resolve_home(args.home)
    allowed, why = redact.push_allowed(home, args.remote)
    if not allowed:
        print(f"refusing to push: {why}", file=sys.stderr)
        return 1
    # `".git" not in d` was a substring test against the whole path, and the
    # default home is `~/.gitmemory` — so every file was filtered out, the gate
    # scanned nothing, and push was allowed. Compare path components, relative
    # to home, or the store's own name defeats its own gate. [E2]
    skip = {".git", ".locks"}
    groups = store.segment_groups(home)
    grouped = {p for g in groups for p in g}
    files = [
        p
        for d, _, fs in os.walk(home)
        if not skip & set(os.path.relpath(d, home).split(os.sep))
        for f in fs
        if (p := os.path.realpath(os.path.join(d, f))) not in grouped
    ]
    clean, findings = redact.gate(files, groups)
    for f in findings:
        print(f, file=sys.stderr)
    if not clean:
        print("refusing to push: the redaction gate found credentials", file=sys.stderr)
        return 1
    print(f"gate passed for {why}; transport lands in E4", file=sys.stderr)
    return 1


def _index(args) -> int:
    if (code := _not_a_store(args.home)) is not None:
        return code
    stats = index.build(args.home, path=args.db)
    for line in stats.skipped:
        print(f"skipped {line}", file=sys.stderr)
    print(
        f"{stats.generations} generation(s)  {stats.turns} turn(s)  "
        f"{stats.blocks} block(s)  content={stats.content_sha256[:12]}"
    )
    # Skipped generations are reported but not fatal: the index is derived, and
    # `verify` is the surface that decides whether the store itself is sound.
    return 0


def _derive(args) -> int:
    if (code := _not_a_store(args.home)) is not None:
        return code
    stats = derive.build(args.home, count=args.ideas)
    for line in stats.skipped:
        print(f"skipped {line}", file=sys.stderr)
    print(f"{stats.generations} generation(s)  {stats.ideas} idea(s)  {stats.marks} mark(s)")
    # Same rule as `index`: derived artifacts are rebuildable, so a generation
    # nothing can parse is reported and is not a failure of the store.
    return 0


def _recall(args) -> int:
    path = args.db or index.db_path(args.home)
    if not os.path.exists(path):
        print(f"no index at {path}; run `gitmemory index` first", file=sys.stderr)
        return 2
    db = index.open_db(path)
    try:
        hits = index.search(db, args.query, k=args.k)
    finally:
        db.close()
    for h in hits:
        head = " ".join(h.text.split())[:160]
        print(f"{h.score:8.3f}  {h.session_key}@{h.byte_offset}  {h.role}/{h.kind}  {head}")
    if not hits:
        print("no matches", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gitmemory")
    ap.add_argument(
        "--home", default=None, help="store root (default $GITMEMORY_HOME or ~/.gitmemory)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="copy out bytes appended since the last capture")
    cap.add_argument("source")
    cap.add_argument("--agent", default="claude-code")
    cap.add_argument("--session-id", default=None)
    cap.add_argument("--no-parse", action="store_true", help="skip adapter; record bytes only")
    cap.set_defaults(fn=_capture)

    wat = sub.add_parser("watch", help="tail the configured roots and capture what grew")
    wat.add_argument(
        "--poll",
        type=_poll_seconds,
        default=daemon.POLL,
        help="seconds between passes (default: %(default)s)",
    )
    wat.add_argument(
        "--interval",
        type=_seconds,
        default=daemon.INTERVAL,
        help="seconds between idle captures of one session (default: %(default)s)",
    )
    wat.add_argument("--once", action="store_true", help="one pass, then exit")
    wat.add_argument("--no-parse", action="store_true", help="skip adapter; record bytes only")
    wat.set_defaults(fn=_watch)

    ver = sub.add_parser("verify", help="check every manifest's contiguity proof")
    ver.set_defaults(fn=_verify)

    push = sub.add_parser("push", help="send the store to an opted-in remote")
    push.add_argument("remote", nargs="?", default="origin")
    push.set_defaults(fn=_push)

    idx = sub.add_parser("index", help="rebuild the retrieval index from the store")
    idx.add_argument("--db", default=None, help="database path (default $GITMEMORY_HOME/index/)")
    idx.set_defaults(fn=_index)

    der = sub.add_parser("derive", help="rebuild derived/ — key ideas and a timeline")
    der.add_argument(
        "--ideas", type=int, default=derive.DEFAULT_IDEAS, help="key sentences per generation"
    )
    der.set_defaults(fn=_derive)

    rec = sub.add_parser("recall", help="search the index; one line per turn, best first")
    rec.add_argument("query")
    rec.add_argument("-k", type=int, default=10)
    rec.add_argument("--db", default=None)
    rec.set_defaults(fn=_recall)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        # `watch` in the foreground is the documented way to run the watcher and
        # Ctrl-C is the documented way to stop it, so it ended in a fifteen-line
        # traceback every time. `KeyboardInterrupt` is a `BaseException`, so no
        # amount of widening the clause below would have caught it.
        # 130 is the shell's convention for SIGINT. [E4, review: CLI 7]
        return 130
    except (OSError, RecursionError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
