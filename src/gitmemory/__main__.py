# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""`python -m gitmemory` — capture, verify, push.

Deliberately thin. `push` exists at E2 only so the refusal is a real, runnable
surface rather than a paragraph in a design doc: DESIGN.md §2.4 promises push
is opt-in per remote and gated on redaction, and a promise you cannot execute
is not a gate. The transport itself lands with the git daemon in E4.
"""

from __future__ import annotations

import argparse
import builtins
import ipaddress
import os
import shlex
import socket
import sqlite3
import stat
import sys
import tomllib

from . import __version__, daemon, dashboard, derive, gitrepo, index, redact, service, store
from .records import safe_text


def print(*args, sep=" ", end="\n", file=None, flush=False):  # noqa: A001 - see below
    """`builtins.print`, but escaping what it is handed.

    Shadowing the builtin for the whole module is deliberate: the alternative
    is remembering to wrap every call site, and the one that gets forgotten is
    the vulnerability. `end` and `sep` are the caller's own, never transcript
    content, so they pass through.
    """
    builtins.print(*(safe_text(str(a)) for a in args), sep=sep, end=end, file=file, flush=flush)


def _safe_exc(exc: BaseException) -> str:
    """An exception's message with any credential in it masked. [E7]

    An `OSError` carries the path it failed on, and a session directory is named
    after a session id, which whoever called `capture` chose and may well be the
    token they were holding. F3's rule — the gate does not publish what it
    caught — is really about diagnostics: nobody *asked* to see this string, so
    masking costs the reader nothing and the path stays readable either side.

    Not in `print` itself, deliberately. `recall` prints transcript content
    because that is what it is for, and a store on the owner's own disk is not
    an egress; masking there would make the tool withhold the user's own bytes
    from them. Egress is `push` and `serve`, and an exception is neither — it is
    the third case, and it is the one that leaks by accident.
    """
    return redact.safe_path(str(exc))


def _refuse(why: str) -> int:
    print(f"refusing: {why}", file=sys.stderr)
    return 1


def _init(args) -> int:
    """Add the standard `[[watch]]` table for one agent to `config.toml`.

    Watch roots still have no default: this writes one only because the user
    named the agent. It appends and never changes a byte already there, because
    the same file holds the `[remote.*]` push opt-ins; it does nothing when the
    table is already there; and it refuses, writing nothing, a table the watcher
    would skip.
    """
    home = store.resolve_home(args.home)
    path = os.path.join(home, "config.toml")
    root = daemon.ROOTS[args.agent]
    real = os.path.realpath(os.path.expanduser(root))
    try:
        # `lstat`: `os.replace` over a symlink swaps the link for a file, so the
        # user's dotfile would silently stop being the config. And a FIFO hangs.
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return _refuse(f"{path} is not a regular file; add the table by hand")
        # A hard link is the same trap without the `l`: the rename gives the
        # name a new inode, and the other name keeps the old bytes.
        if st.st_nlink > 1:
            return _refuse(f"{path} has another hard link; add the table by hand")
        with open(path, "rb") as fh:
            old = fh.read()
    except FileNotFoundError:
        old = b""
    try:
        before = tomllib.loads(old.decode("utf-8"))
    except ValueError as exc:  # `TOMLDecodeError`, and `UnicodeDecodeError` on a bad byte
        return _refuse(f"{path} is not valid TOML ({exc}); fix it first")
    have = before.get("watch", [])
    table = (
        f"# added by `gitmemory init {args.agent}`\n"
        f'[[watch]]\nagent = "{args.agent}"\nroots = ["{root}"]\n'
    )
    sep = b"" if not old else b"\n" if old.endswith(b"\n") else b"\n\n"
    new = old + sep + table.encode()
    # Re-parsed whole, because appending is not always joining: after a plain
    # `[watch]` or an inline `watch = [...]` the result does not parse at all.
    try:
        after = tomllib.loads(new.decode("utf-8"))
    except ValueError:
        after = {}
    if not isinstance(have, list) or len(after.get("watch") or []) != len(have) + 1:
        return _refuse(f"{path} has a `watch` a [[watch]] table cannot join; add it by hand")
    for w in daemon.watches_from(before, path, home, lambda _m: None):
        if real in w.roots:
            if w.agent == args.agent:
                print(f"{path} already watches {root} for {args.agent}; nothing to do")
                return 0
            # `discover` gives each file under a shared root to the first watch
            # in the file, so a table appended after it gets nothing it takes.
            return _refuse(f"{path} already watches {root} as {w.agent!r}")
    # The watcher's own rules and its own sentence, so the two cannot disagree
    # about why: a table it would skip on every pass is not written at all.
    notes: list[str] = []
    if not any(
        w.agent == args.agent and real in w.roots
        for w in daemon.watches_from(after, path, home, notes.append)
    ):
        mine = f"{path} [[watch]] #{len(have) + 1}:"
        return _refuse("; ".join(n for n in notes if n.startswith(mine)))
    store._write_atomic(path, new)  # 0600, and a running watcher reads old or new
    print(f"{'added to' if old else 'wrote'} {path}: watch {root} for {args.agent}")
    if not os.path.isdir(real):
        print(f"note: {root} does not exist yet; it is watched once it does", file=sys.stderr)
    print("next: run `gitmemory watch`; a watcher already running reads this on its next pass")
    return 0


def _capture(args) -> int:
    home = store.resolve_home(args.home)
    cap = daemon.capture_one(
        home,
        args.source,
        args.agent,
        parse=not args.no_parse,
        log=lambda m: print(m, file=sys.stderr),
        session_id=args.session_id,
    )
    # Said before the result line and on stderr, beside `diverged`, because it
    # is the same kind of fact: something about this store is not the ordinary
    # case. A capture that reclaims an orphan reports `+0B`, so without this the
    # loudest thing about the pass is how quiet it was. [E7 fs-F4]
    if cap.adopted:
        print(
            f"adopted {len(cap.adopted)} segment(s) found on disk, not copied from "
            f"the source: {', '.join(cap.adopted)}",
            file=sys.stderr,
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


def _positive_int(text: str) -> int:
    """A strictly positive integer.

    [E9, review: 10]
    """
    try:
        value = int(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from err
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text}")
    return value


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


def _service(args) -> int:
    home = store.resolve_home(args.home)
    target, body, steps = service.render(sys.platform, home)
    if not service.write(target, body):
        # One line, with the way out last on it so it pastes whole: the
        # removal step, which does not wait on stopping a job never started.
        way_out = steps[-1][1]
        return _refuse(f"{target} exists; to replace it, run this and install again: {way_out}")
    print(f"wrote {target}\nit runs: {shlex.join(service.argv(home))}")
    for what, command in steps:
        print(f"{what}:\n  {command}")
    return 0


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
    if not gitrepo.is_repo(home):
        # There is no push without a repository, and the gate's file set is now
        # git's own. Saying so beats printing "gate passed" over a store that
        # has no remote, no history and no way to send anything. `watch` runs
        # `gitrepo.init`; a `capture`-only store has never needed one.
        print(f"refusing to push: {home} is not a git repository", file=sys.stderr)
        return 1
    # Two scans, because a push has two halves and one walk cannot see both.
    #
    # The working tree, from `git ls-files` rather than `os.walk`: that is the
    # set git would add, so `spool/` and `index/` — gitignored, and full of the
    # raw bytes a detector fires on — stop refusing pushes that would never have
    # sent them. It also drops the hand-rolled `{".git", ".locks"}` skip that
    # was E2's full-bypass bug. Segment runs still go through `scan_group`,
    # which is the only scan that sees a credential cut in half by a seam.
    #
    # And the object graph, which is what `git push` actually transmits. A
    # credential committed and then deleted is absent from every walk of the
    # checkout and present in the push. [E7]
    try:
        groups = store.segment_groups(home)
    except (store.EscapingSegment, store.UnreadableManifest) as exc:
        # Not an `error:` traceback out of the top-level handler. A manifest the
        # gate cannot read is a store the gate cannot attest to, and the reason
        # belongs on the same line as the refusal — `verify` is what says which
        # manifest and how, and this is the sentence that sends you to it. [E7]
        #
        # Masked on the way out, because the message carries the manifest path
        # and a generation is named after a session id, which can itself be a
        # token. F3's rule — the gate does not publish what it caught — reaches
        # every channel that prints a path, and this is a new one.
        print(f"refusing to push: {_safe_exc(exc)}", file=sys.stderr)
        print("run `gitmemory verify` — the gate cannot scan seams it cannot find", file=sys.stderr)
        return 1
    grouped = {p for g in groups for p in g}
    files = [p for p in gitrepo.tracked(home) if os.path.realpath(p) not in grouped]
    clean, findings = redact.gate(files, groups, objects=gitrepo.pushable_objects(home))
    for f in findings:
        print(f, file=sys.stderr)
    if not clean:
        print("refusing to push: the redaction gate found credentials", file=sys.stderr)
        return 1
    print(
        f"gate passed for {why}; push runs the redaction gate only and does not send yet",
        file=sys.stderr,
    )
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


def _is_loopback(host: str) -> bool:
    """Does every address `host` resolves to stay on this machine?

    Resolution, not a string set, in both directions.

    The set said `127.0.0.2` and `127.1` and `[::1]` and `::ffff:127.0.0.1` were
    not loopback — six spellings, all of them this machine, all of them warned
    about. E6's own reason for adding `localhost` to the set was that a warning
    which cries wolf on the safe spelling is one people learn to click past
    before they meet `0.0.0.0`; that reasoning stopped at one spelling.

    And it can be wrong the *unsafe* way, which matters more now that the answer
    is a refusal rather than a warning: `localhost` is a name resolved through
    `/etc/hosts` and NSS, so on a machine where it has been pointed somewhere
    routable, a string set says loopback and the socket says otherwise. This
    asks the resolver the same question the bind will ask. [E7 dashboard-F3]

    False for anything that does not resolve, including `""` — which binds
    every interface — so the refusal is the default for everything unrecognised.
    """
    try:
        infos = socket.getaddrinfo(host.strip("[]"), None)
    except (OSError, ValueError, UnicodeError):
        return False
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0].split("%")[0]).is_loopback for info in infos
    )


def _dashboard(args) -> int:
    if not _is_loopback(args.host) and not args.expose:
        # A refusal, not a warning. The store is the most sensitive file on the
        # machine, and the control in front of publishing every transcript the
        # developer owns to the LAN was one line on *stderr* while the URL went
        # to stdout — so in a pipe or a log the warning was not even next to the
        # thing it warned about, and the server came up either way with exit 0.
        # [E7 dashboard-F3]
        print(
            f"refusing: --host {args.host} is not loopback. Pass --expose to serve the "
            "store to the network anyway.",
            file=sys.stderr,
        )
        return 2
    if args.expose and not _is_loopback(args.host):
        print(f"serving the store on {args.host}, which is not loopback", file=sys.stderr)
    return dashboard.serve(args.home, db=args.db, host=args.host, port=args.port)


def _derive(args) -> int:
    if (code := _not_a_store(args.home)) is not None:
        return code
    stats = derive.build(args.home, count=args.ideas, graph=args.graph)
    for line in stats.skipped:
        print(f"skipped {line}", file=sys.stderr)
    print(
        f"{stats.generations} generation(s)  {stats.ideas} idea(s)  "
        f"{stats.marks} mark(s)  {stats.decisions} decision(s)"
    )
    # Same rule as `index`: derived artifacts are rebuildable, so a generation
    # nothing can parse is reported and is not a failure of the store.
    return 0


def _recall(args) -> int:
    # First, so a mistyped policy is the error you see, with or without an index.
    resolve = index.policy(args.policy)
    path = args.db or index.db_path(args.home)
    if not os.path.exists(path):
        print(f"no index at {path}; run `gitmemory index` first", file=sys.stderr)
        return 2
    db = index.open_db(path)
    try:
        hits = index.search(db, args.query, k=args.k)
    finally:
        db.close()
    if hits.dropped:
        # Before the answer, not after it: "no matches" for a query whose
        # matching word was the one past the cap is a wrong answer, not an
        # empty one. [E7 index-F10]
        print(
            f"query truncated to {index.MAX_TERMS} terms; {hits.dropped} dropped",
            file=sys.stderr,
        )
    try:
        shown = list(resolve(hits))
    except Exception as exc:  # a custom policy is the user's code: report it, no traceback
        raise ValueError(f"policy {args.policy!r} failed: {exc!r}") from exc
    for h in shown:
        head = " ".join(h.text.split())[:160]
        t = index.when(h)
        # The date on every line, whatever the policy: it is what lets the
        # reader see that two disagreeing turns are three weeks apart.
        date = t.strftime("%Y-%m-%dT%H:%MZ") if t else "undated".ljust(17)
        # In the role's column, not a column of its own: whatever parses these
        # lines reads role/kind as the fourth field, and a subagent's user/text
        # is the parent agent's instructions, which must not be quoted as the
        # user's.
        who = f"subagent:{h.role}" if h.is_sidechain else h.role
        print(f"{h.score:8.3f}  {date}  {h.session_key}@{h.byte_offset}  {who}/{h.kind}  {head}")
    if not hits:
        print("no matches", file=sys.stderr)
    elif not shown:
        # Not a bare "no matches": the search found something and the policy
        # kept none of it, which is a different fact about the store.
        print(f"no matches kept: policy {args.policy!r} dropped all {len(hits)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gitmemory")
    # The plugin and the CLI are installed apart and can be releases apart, so
    # the recall skill asks this before it trusts `recall`'s columns. argparse
    # prints it past the escaping `print` above: fine for a source constant.
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--home", default=None, help="store root (default $GITMEMORY_HOME or ~/.gitmemory)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="add an agent's standard [[watch]] table to config.toml")
    ini.add_argument("agent", choices=sorted(daemon.ROOTS), help="whose transcript root to watch")
    ini.set_defaults(fn=_init)

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

    svc = sub.add_parser("service", help="keep `watch` running under your user service manager")
    act = svc.add_subparsers(dest="action", required=True)
    act.add_parser(
        "install",
        help="write a systemd user unit (Linux) or a LaunchAgent (macOS); starts nothing",
    ).set_defaults(fn=_service)

    ver = sub.add_parser("verify", help="check every manifest's contiguity proof")
    ver.set_defaults(fn=_verify)

    push = sub.add_parser("push", help="run the redaction gate only (does not send yet)")
    push.add_argument("remote", nargs="?", default="origin")
    push.set_defaults(fn=_push)

    idx = sub.add_parser("index", help="rebuild the retrieval index from the store")
    idx.add_argument("--db", default=None, help="database path (default $GITMEMORY_HOME/index/)")
    idx.set_defaults(fn=_index)

    der = sub.add_parser("derive", help="rebuild derived/ — key ideas and a timeline")
    der.add_argument(
        "--ideas",
        type=_positive_int,
        default=derive.DEFAULT_IDEAS,
        help="key sentences per generation",
    )
    der.add_argument(
        "--graph", action="store_true", help="extract and build decision graph"
    )
    der.set_defaults(fn=_derive)

    dash = sub.add_parser("dashboard", help="serve the index with Datasette on localhost")
    dash.add_argument("--db", default=None)
    dash.add_argument("--host", default=dashboard.HOST, help="default: %(default)s (loopback)")
    dash.add_argument("--port", type=int, default=dashboard.PORT)
    dash.add_argument(
        "--expose",
        action="store_true",
        help="allow a --host that is not loopback (serves the store to the network)",
    )
    dash.set_defaults(fn=_dashboard)

    rec = sub.add_parser("recall", help="search the index; one line per turn, best first")
    rec.add_argument("query")
    rec.add_argument("-k", type=_positive_int, default=10)
    rec.add_argument("--db", default=None)
    rec.add_argument(
        "--policy",
        default=os.environ.get("GITMEMORY_POLICY") or index.DEFAULT_POLICY,
        help="which hit goes first when they disagree: latest-user, latest, relevance, or "
        "module:function (default $GITMEMORY_POLICY or %(default)s)",
    )
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
    except (
        OSError,
        OverflowError,  # `recall -k 99999999999999999999`: SQLite's int is 64-bit
        RecursionError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {_safe_exc(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
