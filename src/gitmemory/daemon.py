"""The watcher: the thing that makes capture a guarantee rather than a hope.

DESIGN.md §2.3 splits the trigger in two. The hook is for latency and is
best-effort — it writes one file to the spool and exits 0 whatever happens. The
watcher is the guarantee: it tails the configured transcript roots and captures
what grew, so a session that crashed, never compacted, or ran before the hook was
installed is still captured. **If the hook is never installed the system is still
correct.** That property is what makes the hook safe to ship, and it is this
module's job to hold it.

Three rules that are easy to get wrong and are therefore stated here:

- **Watch roots have no default.** The watcher watches exactly what
  `config.toml` names. A product whose first run scans `~/.claude/projects`
  ingests its owner's entire history on install, which is the one thing this
  project promises not to do. The adapter documents the conventional path; the
  config has to name it.
- **A spool record is a doorbell, not data.** It says something happened to a
  transcript. Everything else — how many bytes, where the boundary is, whether
  the file was rewritten — is re-derived from the source file. A record naming a
  path outside every configured root is dropped, so a hook cannot make the
  watcher capture an arbitrary file.
- **Coalescing changes where the cuts fall, never whether they tile.** A forcing
  event (`PreCompact`, `SessionEnd`) captures immediately; otherwise a session is
  captured at most once per `interval`. That bounds a session to roughly ten
  segments without any effect on the contiguity proof.

[E4]
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from glob import glob

from . import gitrepo, store
from .adapters import get as get_adapter

SPOOL = "spool"
INTERVAL = 3600.0  # seconds between idle captures of one session (DESIGN.md §2.4)
POLL = 5.0  # seconds between ticks
STALE_TMP = 3600.0  # a `.tmp-` this old is a killed hook, not a live one

# A module constant, not `Watch.pattern`: on a `slots=True` dataclass the class
# attribute is the slot's `member_descriptor`, so reading the default off the
# class hands `glob` a descriptor and every discovery raises. [E4]
PATTERN = "**/*.jsonl"

# Hook events that mean "capture now" rather than "something is alive". Only
# these two exist as reasons to cut a segment: a compaction boundary is about to
# be crossed, or the session is over. `Stop` fires after every assistant turn, so
# forcing on it would put one segment per response in the tree and defeat the
# coalescing it is supposed to feed.
FORCING = frozenset({"PreCompact", "SessionEnd"})

# Every event the shim will write into a record name. `FORCING` is a subset:
# `Stop` is a legitimate event that simply does not force. Kept separate so
# `_event_of` can reject a field that is not an event at all rather than trust
# its position in the filename — see the note there.
EVENTS = FORCING | {"Stop", "unknown"}

# Where a hook payload keeps the transcript path, most specific first. Claude
# Code says `transcript_path`; the others are what the next adapter is likely to
# call it. Unknown shapes are dropped, not guessed at.
_PATH_KEYS = ("transcript_path", "transcriptPath", "transcript", "file")


@dataclass(frozen=True, slots=True)
class Watch:
    agent: str
    roots: tuple[str, ...]
    pattern: str = PATTERN


@dataclass(slots=True)
class Tick:
    """What one pass did. Every field is something the dashboard will want."""

    captured: list[str] = field(default_factory=list)  # store keys
    appended: int = 0
    commit: str | None = None
    errors: list[str] = field(default_factory=list)
    spool_consumed: int = 0
    spool_dropped: int = 0  # records naming a path no watch covers


def load_watches(home: str) -> list[Watch]:
    """`[[watch]]` tables from `config.toml`. No config means watch nothing.

        [[watch]]
        agent = "claude-code"
        roots = ["~/.claude/projects"]

    Absent, unreadable, and malformed all mean the empty list. The watcher then
    does nothing at all, loudly, which is the correct behaviour for a tool whose
    failure mode is reading files nobody asked it to read.
    """
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for entry in cfg.get("watch") or []:
        if not isinstance(entry, dict):
            continue
        agent = entry.get("agent")
        roots = entry.get("roots")
        if not isinstance(agent, str) or not isinstance(roots, list):
            continue
        # A root that exists and is not a directory is dropped. Review found
        # that such a root does nothing at all and says nothing about it:
        # `discover()` globs `<root>/**/*.jsonl` and finds nothing under a file,
        # and `_covers` excludes the root itself, so every doorbell naming a
        # file under it is dropped too. Silent and total, and the obvious way to
        # write the config wrong — pointing at one transcript instead of the
        # directory holding it. [E4, Gemini 10]
        #
        # Only *existing* non-directories, which is the narrower rule and the
        # right one. A root that does not exist yet is the normal state before
        # an agent has run for the first time, and `run()` re-reads the config
        # every pass, so it starts being watched the moment it appears. Dropping
        # those too would turn "not started yet" into "misconfigured".
        real = tuple(
            resolved
            for r in roots
            if isinstance(r, str) and r
            for resolved in (os.path.realpath(os.path.expanduser(r)),)
            if not (os.path.exists(resolved) and not os.path.isdir(resolved))
        )
        if real:
            pattern = entry.get("pattern")
            out.append(
                Watch(
                    agent=agent,
                    roots=real,
                    pattern=pattern if isinstance(pattern, str) else PATTERN,
                )
            )
    return out


def _covers(watches: list[Watch], path: str) -> Watch | None:
    """The watch whose root contains `path`, or None.

    Compared by path components, not by prefix: `/a/b` is not inside `/a/bc`,
    and a `startswith` test says it is. The store's own redaction gate had this
    bug and it made the gate scan nothing at all. [E2]
    """
    real = os.path.realpath(path)
    for watch in watches:
        for root in watch.roots:
            if real == root:
                continue
            rel = os.path.relpath(real, root)
            if not rel.startswith(os.pardir + os.sep) and rel != os.pardir:
                return watch
            if _same_dir(os.path.dirname(real), root):
                return watch
    return None


def _same_dir(a: str, b: str) -> bool:
    """Do two paths name the same directory, whatever the strings say?

    The string test above is exact on a case-sensitive filesystem and wrong on
    the one most of this project's users are running. APFS is case-insensitive
    and case-preserving, and `realpath` does not correct case, so a watch root
    written `~/Projects/MyApp` and an agent reporting
    `~/projects/myapp/s.jsonl` are the same directory that `relpath` reports as
    `../myapp/s.jsonl` — outside the root. The doorbell was dropped in silence
    and the compaction waited for the next interval. [E4]

    Compared by device and inode rather than by casefolding the strings, which
    would be wrong in the other direction: on a case-sensitive filesystem `/a/B`
    and `/a/b` really are different directories, and a watch on one must not
    cover the other. The kernel already knows the answer; ask it.

    Only the immediate parent is checked, not the whole chain. A record whose
    path differs in case *above* the parent still falls through to the interval,
    which is the same outcome as no hook at all — degraded, never wrong.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def drain_spool(home: str, watches: list[Watch], now: float | None = None) -> tuple[dict, Tick]:
    """Consume the spool. Returns `{realpath: forcing}` and a partial `Tick`.

    Consumed records are unlinked whether or not they were usable: a record the
    watcher cannot act on is a record that would otherwise be re-read on every
    tick forever. What survives a delete is the transcript itself, which is the
    only thing the watcher trusts anyway.
    """
    now = time.time() if now is None else now
    spool = os.path.join(home, SPOOL)
    tick = Tick()
    wanted: dict[str, bool] = {}
    try:
        names = sorted(os.listdir(spool))
    except OSError:
        return wanted, tick
    for name in names:
        path = os.path.join(spool, name)
        if name.startswith("."):
            # A killed hook's temp file. Swept only once it is old enough that it
            # cannot be a live hook still writing.
            with contextlib.suppress(OSError):
                if name.startswith(".tmp-") and now - os.path.getmtime(path) > STALE_TMP:
                    os.unlink(path)
            continue
        event = _event_of(name)
        try:
            with open(path, "rb") as fh:
                payload = json.loads(fh.read())
        except (OSError, ValueError):
            payload = None
        _unlink(path)
        tick.spool_consumed += 1
        source = _payload_path(payload)
        if source is None:
            tick.spool_dropped += 1
            continue
        if _covers(watches, source) is None:
            # Not a watched path. Dropping it is the whole reason the watcher
            # re-derives from config instead of obeying the record.
            tick.spool_dropped += 1
            continue
        real = os.path.realpath(source)
        wanted[real] = wanted.get(real, False) or event in FORCING
    return wanted, tick


def _event_of(name: str) -> str:
    """The event out of `<epoch_s>-<pid>-<event>[-<n>].json`, or "".

    Read from the third field, not the last: two hooks firing in the same second
    give the second one a `-1` collision suffix, and taking the last field made
    that record's event `1`, so a `PreCompact` that raced another hook silently
    stopped forcing a capture. Found by running it. [E4]

    And then checked against the events the shim will actually write, rather
    than returned raw. Positional parsing assumes the first two fields are a
    positive integer each, and review found a case where they are not: a clock
    set before 1970 makes `date +%s` negative, so the name leads with `-` and
    every index shifts by one — `fields[2]` comes back as the pid. Validating
    the result closes that and any other grammar drift at once, because the
    only thing a caller does with this value is test it for membership in
    `FORCING`, and a field that is not a known event never belongs there. [E4]
    """
    if not name.endswith(".json"):
        return ""
    fields = name.removesuffix(".json").split("-")
    if len(fields) >= 3 and fields[2] in EVENTS:
        return fields[2]
    return ""


def _payload_path(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def discover(watches: list[Watch]) -> list[tuple[Watch, str]]:
    """Every transcript under every watch root, deduplicated by real path.

    `recursive=True` so `**` means what it looks like. Symlinked directories are
    not followed by `glob`, which is the conservative answer: a symlink into
    somewhere the owner did not configure is exactly the escape this module is
    careful about elsewhere.
    """
    seen: set[str] = set()
    out = []
    for watch in watches:
        for root in watch.roots:
            for path in sorted(glob(os.path.join(root, watch.pattern), recursive=True)):
                real = os.path.realpath(path)
                if real in seen or not os.path.isfile(real):
                    continue
                # A glob can climb out through a symlinked leaf even though it
                # will not descend one.
                if _covers([watch], real) is None:
                    continue
                seen.add(real)
                out.append((watch, real))
    return out


def _recorded(home: str) -> dict[tuple[str, str], tuple[int, float]]:
    """`(agent, session_id) -> (captured size, manifest mtime)` for the live generation.

    `sessions()` returns generations sorted by path and `g01.json` sorts after
    `g00.json`, so the last one seen per key is the live one — which is the only
    one that can still grow.
    """
    out: dict[tuple[str, str], tuple[int, float]] = {}
    for s in store.sessions(home):
        try:
            mtime = os.path.getmtime(s.manifest)
        except OSError:
            mtime = 0.0
        out[(s.agent, s.session_id)] = (s.size, mtime)
    return out


def capture_one(home: str, source: str, agent: str, *, parse: bool = True) -> store.Capture:
    """Capture one transcript, collecting compaction boundaries if we can.

    The parse is only ever for boundaries, so a parse failure must not cost us
    the bytes — it degrades to a capture without them. Shared with the CLI's
    `capture` subcommand so that the command and the daemon cannot drift. [E4]
    """
    boundaries = None
    if parse:
        try:
            session = get_adapter(agent).parse(source)
            boundaries = [e.byte_offset for e in session.events if e.kind == "compaction"]
        except (OSError, RecursionError, ValueError, KeyError):
            boundaries = None
    return store.capture(
        source,
        agent,
        store.session_id_for(source),
        home=home,
        boundaries=boundaries,
    )


def tick(
    home: str,
    watches: list[Watch],
    *,
    interval: float = INTERVAL,
    now: float | None = None,
    parse: bool = True,
) -> Tick:
    """One pass: drain the spool, capture what is due, commit once.

    One commit for the whole pass rather than one per session. A pass is the
    unit of work the watcher actually did, and a hundred single-session commits
    for one wake-up is a history nobody can read.
    """
    now = time.time() if now is None else now
    forced, result = drain_spool(home, watches, now=now)
    try:
        recorded = _recorded(home)
    except (OSError, RuntimeError) as exc:
        # `store.sessions()` raises `EscapingSegment` when a manifest names a
        # path outside the store — a deliberate tripwire for a store that has
        # been corrupted or edited. It was raising straight out of `tick`, so
        # one bad manifest killed the watcher process and kept killing it on
        # every restart: the loudest possible failure, delivered to nobody.
        #
        # Reported and the pass abandoned, rather than reported and continued.
        # Without `recorded` the watcher cannot tell what it has already
        # captured, so carrying on would re-capture every session on every tick.
        # And a store whose manifests are untrustworthy is one to stop writing
        # to, not one to append harder to. The watcher stays up and says the
        # same thing every interval until someone fixes it. [E4]
        result.errors.append(f"store unreadable, nothing captured this pass: {exc}")
        return result

    for watch, source in discover(watches):
        key = (watch.agent, store.session_id_for(source))
        try:
            stat = os.stat(source)
        except OSError as exc:
            result.errors.append(f"{source}: {exc}")
            continue
        size, mtime = recorded.get(key, (-1, 0.0))
        # Size *differs*, not grows: a pruner that rewrites the transcript in
        # place can leave it shorter, and that is a divergence the store has to
        # see. mtime covers the rewrite that happens to land on the same length.
        changed = stat.st_size != size or stat.st_mtime > mtime
        if not changed:
            continue
        if not (forced.get(source) or size < 0 or now - mtime >= interval):
            continue
        try:
            cap = capture_one(home, source, watch.agent, parse=parse)
        except (OSError, RuntimeError, ValueError) as exc:
            result.errors.append(f"{source}: {exc}")
            continue
        if cap.appended or cap.diverged:
            result.captured.append(f"{watch.agent}/{key[1]}/g{cap.generation:02d}")
            result.appended += cap.appended

    if result.captured:
        head = ", ".join(result.captured[:3])
        more = f" (+{len(result.captured) - 3} more)" if len(result.captured) > 3 else ""
        # A git failure is reported, never raised. By this point the bytes are
        # already in the store and `verify` will vouch for them; the commit is
        # the versioning layer on top. Letting a full disk or a corrupted index
        # abort the pass here would turn a recoverable problem — the next pass
        # commits everything at once — into a watcher that stops capturing. [E4]
        try:
            commit = gitrepo.commit(home, f"capture: {head}{more}  +{result.appended}B")
            result.commit = commit.sha if commit else None
            gitrepo.gc(home)
        except (gitrepo.GitError, OSError, subprocess.SubprocessError) as exc:
            result.errors.append(f"commit: {exc}")
    return result


def run(
    home: str | None = None,
    *,
    poll: float = POLL,
    interval: float = INTERVAL,
    once: bool = False,
    parse: bool = True,
    log=print,
) -> int:
    """Tick forever. `once` runs a single pass, which is what the tests drive.

    Config is re-read every pass, so adding a watch root does not need a
    restart. `gitrepo.init` is re-applied once per **start**, not per pass —
    this said "for the same reason" and it was describing code that does not
    exist [E4, Gemini 09]. Per-start is the right frequency and the sentence was
    the thing that was wrong: what `init` carries is gitmemory's own git config,
    and a store made by an older version picks up a newer version's setting when
    the newer version starts. That is the same event. Doing it per pass would
    buy nothing but four `git config` subprocesses every `poll` seconds, for
    ever.
    """
    home = gitrepo.init(home)
    while True:
        watches = load_watches(home)
        result = tick(home, watches, interval=interval, parse=parse)
        for err in result.errors:
            log(f"error: {err}")
        if result.captured:
            log(f"captured {len(result.captured)} +{result.appended}B commit={result.commit}")
        if once:
            return 1 if result.errors else 0
        time.sleep(poll)
