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

from . import daemon, index, redact, store
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
        cap = daemon.capture_one(home, args.source, args.agent, parse=not args.no_parse)
    if cap.diverged:
        print(f"diverged: {cap.diverged}", file=sys.stderr)
        print(f"sealed generation {cap.generation - 1}; now writing g{cap.generation:02d}")
    print(f"{cap.manifest_path}  +{cap.appended}B  size={cap.size}  g{cap.generation:02d}")
    return 0


def _watch(args) -> int:
    home = store.resolve_home(args.home)
    watches = daemon.load_watches(home)
    if not watches:
        # Not an error: a store with no `[[watch]]` is a store nobody has told
        # what to watch, and guessing is the one thing the watcher must not do.
        print(
            f"no [[watch]] in {os.path.join(home, 'config.toml')}; watching nothing",
            file=sys.stderr,
        )
    return daemon.run(
        home,
        poll=args.poll,
        interval=args.interval,
        once=args.once,
        parse=not args.no_parse,
        log=lambda m: print(m, file=sys.stderr),
    )


def _verify(args) -> int:
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
    wat.add_argument("--poll", type=float, default=daemon.POLL)
    wat.add_argument("--interval", type=float, default=daemon.INTERVAL)
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

    rec = sub.add_parser("recall", help="search the index; one line per turn, best first")
    rec.add_argument("query")
    rec.add_argument("-k", type=int, default=10)
    rec.add_argument("--db", default=None)
    rec.set_defaults(fn=_recall)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, RecursionError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
