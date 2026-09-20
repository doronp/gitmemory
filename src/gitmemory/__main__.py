"""`python -m gitmemory` — capture, verify, push.

Deliberately thin. `push` exists at E2 only so the refusal is a real, runnable
surface rather than a paragraph in a design doc: DESIGN.md §2.4 promises push
is opt-in per remote and gated on redaction, and a promise you cannot execute
is not a gate. The transport itself lands with the git daemon in E4.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

from . import redact, store
from .adapters import get as get_adapter


def _default_session_id(source: str) -> str:
    """Basename plus a digest of the full path.

    The bare basename made `~/projA/session.jsonl` and `~/projB/session.jsonl`
    one session, and every alternating capture "diverged" past the other and
    re-copied it whole — unbounded duplication that `verify` calls clean. [E2]
    """
    stem = os.path.splitext(os.path.basename(source))[0]
    tag = hashlib.sha256(os.path.realpath(source).encode()).hexdigest()[:8]
    return f"{stem}-{tag}"


def _capture(args) -> int:
    session_id = args.session_id or _default_session_id(args.source)
    boundaries = None
    if not args.no_parse:
        # Parse first only to collect boundaries; a parse failure must not cost
        # us the bytes, so it degrades to a capture without them. [E2]
        try:
            session = get_adapter(args.agent).parse(args.source)
            boundaries = [e.byte_offset for e in session.events if e.kind == "compaction"]
        except (OSError, RecursionError, ValueError) as exc:
            print(f"parse failed ({exc}); capturing bytes without boundaries", file=sys.stderr)
    cap = store.capture(args.source, args.agent, session_id, home=args.home, boundaries=boundaries)
    if cap.diverged:
        print(f"diverged: {cap.diverged}", file=sys.stderr)
        print(f"sealed generation {cap.generation - 1}; now writing g{cap.generation:02d}")
    print(f"{cap.manifest_path}  +{cap.appended}B  size={cap.size}  g{cap.generation:02d}")
    return 0


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

    ver = sub.add_parser("verify", help="check every manifest's contiguity proof")
    ver.set_defaults(fn=_verify)

    push = sub.add_parser("push", help="send the store to an opted-in remote")
    push.add_argument("remote", nargs="?", default="origin")
    push.set_defaults(fn=_push)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, RecursionError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
