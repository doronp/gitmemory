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
import pathlib
import stat
import subprocess
import time
import tomllib
from dataclasses import dataclass, field

from . import gitrepo, store
from .adapters import ADAPTERS
from .adapters import get as get_adapter

SPOOL = "spool"
INTERVAL = 3600.0  # seconds between idle captures of one session (DESIGN.md §2.4)
# How often a standing error is repeated to the log. Its own constant, not
# `interval`: those two defaulted to the same number and were therefore the same
# knob, so `--interval 0` — which is a documented, parser-blessed setting meaning
# "capture every pass" — also set the error log's repeat rate to zero and put
# back the 2.06 M lines a day the limit exists to stop. [E4, review: daemon 9]
ERROR_REPEAT = 3600.0
POLL = 5.0  # seconds between ticks
STALE_TMP = 3600.0  # a `.tmp-` this old is a killed hook, not a live one

# A module constant, not `Watch.pattern`: on a `slots=True` dataclass the class
# attribute is the slot's `member_descriptor`, so reading the default off the
# class hands the matcher a descriptor and every discovery raises. [E4]
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
    # Records that were not a usable file at all — a FIFO, a device, a symlink,
    # a directory, bytes that are not JSON. A separate field because
    # `spool_dropped` means one specific thing, says so above, and is reported
    # as a *configuration* fact: sending someone to check their config because
    # a FIFO appeared in the spool is worse than saying nothing. [E7 fs-F3]
    spool_unreadable: int = 0
    # Compaction offsets the store refused because they fall outside the bytes
    # it holds. Zero in a healthy pass, which is why it is worth printing when
    # it is not: an adapter or a race handed us an offset into nothing.
    boundaries_dropped: int = 0
    # Segments this pass attested to without copying a byte — reclaimed from the
    # generation directory rather than read out of the source. The manifest
    # records them permanently; this is so the operator hears about it at the
    # time, since a repair that shows up only as "+0B" reads like a quiet pass.
    # Not an error: after a killed capture it is the correct outcome. [E7 fs-F4]
    adopted: list[str] = field(default_factory=list)


def load_watches(home: str, log=None) -> list[Watch]:
    """`[[watch]]` tables from `config.toml`. No config means watch nothing.

        [[watch]]
        agent = "claude-code"
        roots = ["~/.claude/projects"]

    Absent, unreadable, and malformed all mean the empty list. The watcher then
    does nothing at all, loudly, which is the correct behaviour for a tool whose
    failure mode is reading files nobody asked it to read.

    "Loudly" is what `log` is for, and it was missing. Review walked ten
    distinct ways to get this file wrong — a TOML syntax error, `chmod 000`, a
    `roots` string where a list belongs, a watch with no `agent`, a root that
    names one transcript instead of the directory holding it — and found that
    every one of them produced the same single sentence, `no [[watch]] in
    <path>`, which is the right message for exactly one of them. Five more
    produced no output at all, the worst being the path typo: a misspelled root
    and a correctly-configured watcher that has not seen its first session were
    byte-identical experiences. This module's own docstring promises a
    misconfigured watcher does nothing *loudly*; measured, it mostly did it
    quietly. [E4, review: CLI 2]
    """
    say = log or (lambda _m: None)
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except FileNotFoundError:
        say(f"no {path}; watching nothing")
        return []
    except OSError as exc:
        say(f"{path} unreadable ({exc}); watching nothing")
        return []
    except tomllib.TOMLDecodeError as exc:
        # The one that matters most: this is the only fault reachable by editing
        # a *working* config, so it is the only one that can take a running
        # watcher silent. It used to read as "you never configured anything".
        say(f"{path} is not valid TOML ({exc}); watching nothing")
        return []
    except ValueError as exc:
        # `tomllib.load` decodes as UTF-8 and raises `UnicodeDecodeError` — a
        # `ValueError`, not a `TOMLDecodeError` — on the first bad byte. It
        # escaped the function whose docstring promises absent, unreadable and
        # malformed all mean the empty list, and `run`'s floor then logged it as
        # `{exc!r}`. `repr` of a `UnicodeDecodeError` carries the object it
        # failed on, so the entire config file went to stderr, once per pass,
        # for ever. Caught after `TOMLDecodeError` because that is a subclass of
        # this and the specific message is the better one. [E4, review: daemon 3]
        say(f"{path} is not readable as text ({exc.__class__.__name__}); watching nothing")
        return []
    out = []
    for n, entry in enumerate(cfg.get("watch") or []):
        where = f"{path} [[watch]] #{n + 1}"
        if not isinstance(entry, dict):
            say(f"{where}: not a table; skipped")
            continue
        agent = entry.get("agent")
        roots = entry.get("roots")
        if not isinstance(agent, str) or not isinstance(roots, list):
            say(f"{where}: needs a string `agent` and a list `roots`; skipped")
            continue
        if agent not in ADAPTERS:
            # Said, and then the watch is kept anyway.
            #
            # Review found this accepted in silence: a typo'd agent name
            # produced a full, committed, `verify`-clean store in which every
            # manifest carried `compact_boundaries: []` — indistinguishable from
            # a transcript that genuinely never compacted, because `capture_one`
            # catches the `ValueError` from `get_adapter` and degrades to no
            # boundaries. The silence is the bug and it is fixed here.
            #
            # Skipping the watch is not the fix, and the first version of this
            # did skip it. An adapter supplies *boundaries*; discovery globs
            # `pattern` and the store copies bytes, neither of which needs one.
            # So refusing the watch trades a store with no boundaries for no
            # store at all, against this module's own rule that bytes outrank
            # boundaries — and it would refuse `agent = "kimi-code"` written
            # the day before that adapter lands, which is a configuration we
            # have told people to expect to work. Loud and degraded beats silent
            # and degraded; it does not beat capturing nothing.
            # [E4, review: CLI 5]
            say(f"{where}: no adapter for {agent!r}; have {sorted(ADAPTERS)}; bytes only")
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
        kept = []
        for r in roots:
            if not (isinstance(r, str) and r):
                say(f"{where}: root {r!r} is not a path; skipped")
                continue
            try:
                resolved = os.path.realpath(os.path.expanduser(r))
            except ValueError as exc:
                # An embedded NUL. `realpath` reaches `os.lstat` and raises
                # `ValueError`, which is not an `OSError` and had nothing above
                # it: one bad root took the whole config with it — every *other*
                # watch in the file included — and did it again on every pass.
                # The entry it belongs to is the only thing that should suffer.
                # [E4, review: daemon 3]
                say(f"{where}: root is not a usable path ({exc.__class__.__name__}); skipped")
                continue
            if os.path.exists(resolved) and not os.path.isdir(resolved):
                say(f"{where}: root {r} is a file, not a directory; skipped")
                continue
            if resolved == os.sep:
                # Before the store check, not after it. `_inside(root, home)`
                # asks whether `home` is at or below `root`, and everything is
                # at or below `/` — so a root of `/` was refused for *containing
                # the store*, which is true and is not the reason, and this
                # branch was unreachable. `docs/watching.md` documents the
                # message that never printed. `discover` would walk the whole
                # filesystem every poll, which is the thing worth saying.
                # [E4, review: docs 8]
                say(f"{where}: root / is the whole filesystem; skipped")
                continue
            if _inside(resolved, home) or _inside(home, resolved):
                # The store's raw segments are `*.jsonl` and `discover` globs
                # `<root>/**/*.jsonl`, so a store under a watch root reads its
                # own output back as new sessions. New sessions bypass the
                # interval gate, so it runs at full poll rate: measured at six
                # phantom sessions and six commits in thirty seconds — roughly
                # 17,000 a day, growing in disk for as long as the watcher runs,
                # with `verify` reporting clean throughout. The default
                # `~/.gitmemory` is safe only because `_hits` skips dotted
                # components, which is why that skip is preserved rather than
                # inherited. [E4, review: CLI 3]
                say(f"{where}: root {r} contains the store itself; skipped")
                continue
            kept.append(resolved)
        if kept:
            out.append(
                Watch(agent=agent, roots=tuple(kept), pattern=_pattern(entry.get("pattern"), say))
            )
    return out


def _pattern(value: object, say) -> str:
    """The watch's glob, or the default if it is not one a root can contain.

    `discover` does `os.path.join(root, pattern)`, and `join` *discards* the
    root when the second argument is absolute. So `pattern = "/**/*.jsonl"`
    silently reconfigured the watch to glob the entire filesystem every poll —
    0.743 s of walking a pass, measured — and `_covers` then threw every result
    away, because the floor is the root check and the floor held. Nothing
    escaped; what leaked was the cost and the silence.

    `..` for the same reason one rung down: it climbs out of the root, and the
    only reason it is not an escape is that the same `_covers` catches it. A
    pattern that cannot match anything the watch will accept is a
    misconfiguration, and this module's rule for those is loud, not quiet.
    [E4, review: daemon 10]
    """
    if not isinstance(value, str) or not value:
        return PATTERN
    if os.path.isabs(value) or os.pardir in value.split(os.sep):
        say(f"pattern {value!r} leaves the watch root; using {PATTERN!r}")
        return PATTERN
    return value


def _inside(outer: str, inner: str) -> bool:
    """Is `inner` at or below `outer`? By components, never by prefix."""
    rel = os.path.relpath(os.path.realpath(inner), os.path.realpath(outer))
    return not rel.startswith(os.pardir + os.sep) and rel != os.pardir


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


def drain_spool(
    home: str, watches: list[Watch], now: float | None = None
) -> tuple[dict[tuple[int, int], bool], Tick]:
    """Consume the spool. Returns `{(dev, ino): forcing}` and a partial `Tick`.

    Consumed records are unlinked whether or not they were usable: a record the
    watcher cannot act on is a record that would otherwise be re-read on every
    tick forever. What survives a delete is the transcript itself, which is the
    only thing the watcher trusts anyway.

    **Keyed on device and inode, not on the path string.** The hook writes
    whatever spelling the agent handed it and `discover` writes whatever
    spelling the glob produced, and `realpath` does not reconcile them: it
    resolves symlinks but it does not correct case, so on APFS a record naming
    `~/projects/myapp/s.jsonl` survived `_covers` — which does ask the kernel,
    via `_same_dir` — and was then silently discarded by `tick`'s string
    lookup. The doorbell rang, the pass could not hear it, and the compaction
    waited for the interval. The fix that was shipped for `_covers` never
    reached the second half of the same journey. Device and inode is the
    identity the kernel itself uses, so both halves now ask the same question.
    [E4, review: concurrency 9]
    """
    now = time.time() if now is None else now
    spool = os.path.join(home, SPOOL)
    tick = Tick()
    wanted: dict[tuple[int, int], bool] = {}
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
                age = now - os.path.getmtime(path)
                # A temp whose mtime is in the *future* survives, because a
                # negative age fails `> STALE_TMP` on its own. That is the case
                # a store copied off a fast-clock machine lands in, and it is
                # handled by the arithmetic — an explicit `and age > 0` was
                # written here first and is dead code, since `STALE_TMP` is
                # positive and `age > STALE_TMP` already implies it. Deleted
                # rather than left as reassuring noise.
                #
                # The hazard the guard was written for is the opposite sign and
                # is *not* fixed: a forward realtime step of more than an hour
                # makes every in-flight hook's temp look like a corpse, and
                # nothing in an mtime can distinguish that from a hook that
                # really did die an hour ago. Left unfixed deliberately. The
                # cost is bounded to what this module is already built to
                # absorb: the hook's `mv` fails, it `rm -f`s and exits 0, that
                # compaction loses its doorbell, and the interval captures it
                # anyway — latency, not loss. Cheap detection would mean a pid
                # in the temp name and a liveness check, which buys a bounded
                # delay back and is not worth the second failure mode.
                # [E4, review: concurrency 6; corrected by its own negative control]
                if name.startswith(".tmp-") and age > STALE_TMP:
                    os.unlink(path)
            continue
        # Everything from here to the end of the loop body runs on bytes a hook
        # wrote, which is to say on untrusted input, and it used to run without
        # a floor under it. `json.loads` on a deeply nested payload raises
        # `RecursionError`, which is a `RuntimeError` and so slipped past an
        # `except (OSError, ValueError)`; a path containing a NUL makes
        # `os.path.realpath` raise `ValueError` from *outside* that guard. Both
        # escaped `drain_spool`, `tick`, and `run`'s `while True` — and because
        # the record is unlinked only after the parse, both did it again on
        # every restart, for ever, with no session anywhere being captured.
        #
        # The asymmetry was the tell: `capture_one` below names `RecursionError`
        # explicitly and `store.sessions` uses a bare `except Exception`. Every
        # other reader of untrusted data in this codebase is total. The one
        # reading hook-written bytes was not. [E4, review: concurrency 1, store-contract 6]
        try:
            event = _event_of(name)
            try:
                payload = json.loads(_read_record(path))
            except (OSError, ValueError, RecursionError):
                payload = None
            _unlink(path)
            tick.spool_consumed += 1
            if payload is None:
                tick.spool_unreadable += 1
                continue
            source = _payload_path(payload)
            if source is None or _covers(watches, source) is None:
                # Not a watched path. Dropping it is the whole reason the
                # watcher re-derives from config instead of obeying the record.
                tick.spool_dropped += 1
                continue
            key = _file_key(source)
            if key is None:
                tick.spool_dropped += 1
                continue
            wanted[key] = wanted.get(key, False) or event in FORCING
        except Exception:  # noqa: BLE001 - a spool record is untrusted data
            _unlink(path)
            tick.spool_dropped += 1
    return wanted, tick


def _file_key(path: str) -> tuple[int, int] | None:
    """`(st_dev, st_ino)` — the identity two spellings of one file agree on."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _event_of(name: str) -> str:
    """The event out of `<pid>-<event>[-<n>].json`, or "".

    Found by position at first — the third field, not the last, because two
    hooks firing in the same second give the second one a `-1` collision suffix
    and taking the last field made that record's event `1`, so a `PreCompact`
    that raced another hook silently stopped forcing a capture. Then a clock set
    before 1970 turned up: `date +%s` goes negative, the name leads with `-`,
    every index shifts by one, and the third field comes back as the pid.

    Two positional bugs in one small grammar was the argument for not parsing
    positionally. The event is now *found*, wherever it sits. Every other field
    the shim writes is decimal and every event is alphabetic, so there is
    nothing for the scan to confuse — and it reads records from both the current
    shim and the one before it, which carried a leading `date +%s` that no
    reader ever looked at. That timestamp was one of three forks on the hook's
    hot path, so dropping it cost the record nothing and bought the session
    2.0 ms of its 7.6; this function is why it could go. [E4, review: shell MAJOR]
    """
    if not name.endswith(".json"):
        return ""
    return next((f for f in name.removesuffix(".json").split("-") if f in EVENTS), "")


def _payload_path(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _read_record(path: str) -> bytes:
    """One spool record's bytes, refusing anything that is not a plain file.

    The floor above catches `(OSError, ValueError, RecursionError)` and then
    everything, and it caught nothing here, because **blocking is not an
    exception**. `open(path, "rb")` on a FIFO waits for a writer that never
    comes. Measured: one `mkfifo $GITMEMORY_HOME/spool/1-Stop.json` and
    `gitmemory watch --once` was still running after eight seconds having
    written no log line, captured no session and made no commit — and since the
    record is unlinked only after the read, the FIFO is still there on the next
    start, for ever. That is the capture guarantee dying quietly, which is the
    one failure this module exists to prevent; the docstring above says so in
    those words about a different cause.

    `O_NONBLOCK` so the open cannot wait, `O_NOFOLLOW` so a symlink cannot
    redirect the read out of the spool, and an `fstat` because the flag only
    stops the open from blocking — it does not make a FIFO a record. Anything
    that is not a regular file raises, which routes it to the handling every
    other unusable record already gets: unlinked, counted dropped, pass
    continues. Both flags are no-ops on a regular file, so the ordinary path is
    unchanged. [E7 fs-F3]
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file, so it is not a spool record")
        return fh.read()


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _hits(root: str, pattern: str) -> list[str]:
    """`pattern` under `root`, sorted, without descending a symlinked directory.

    This was `glob.glob(os.path.join(root, pattern), recursive=True)`, and the
    docstring above it said symlinked directories are not followed. They are.
    Measured on 3.13.12: a `**` glob returned a file reachable only through a
    symlinked directory; the same pattern under `pathlib` returned nothing.

    Following them is not an escape — `_covers` is the floor and it holds — but
    it is a cost with no ceiling. Each self-link multiplies the candidate set
    per level and `**` descends until `ELOOP`, which the kernel only raises
    after 31 components: one directory holding one file and two `-> .` links
    billed 76,849 directory reads in five seconds and was still going, against
    six for a traversal that refuses the link. A watch root is a directory the
    owner named, not one they audited; a single stray self-link inside it turns
    every poll into that.

    `Path.glob` refuses since 3.13 (`recurse_symlinks=False` is the default)
    but it also returns dotted components, which `glob.glob` skips
    (`include_hidden=False`) — and `_roots` leans on that skip: the default
    store lives at `~/.gitmemory` and a watch root of `~` is allowed, so a
    daemon that saw dotted paths would read its own segments back as new
    sessions. Filtering them restores the old result set exactly; verified on a
    tree carrying a hidden directory, a hidden file, a symlinked directory and
    a plain one.

    A pattern that is absolute or climbs would raise here rather than silently
    widening; `_pattern` rejects both before a `Watch` is ever built.
    """
    return sorted(
        str(p)
        for p in pathlib.Path(root).glob(pattern)
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


def discover(watches: list[Watch]) -> list[tuple[Watch, str]]:
    """Every transcript under every watch root, deduplicated by device and inode.

    Matching is `_hits`, which is a walk that will not descend a symlinked
    directory: a symlink into somewhere the owner did not configure is exactly
    the escape this module is careful about elsewhere.

    Deduplicated by real path at first, which is the same mistake `_same_dir`
    was written for: `realpath` resolves symlinks but does not correct case, so
    on APFS two watch roots spelled `~/Projects` and `~/projects` are one
    directory that yields two different strings for every file under it. Both
    survive the set, both get a `session_id_for` of their own, and the same
    bytes are captured twice into two sessions that each look perfectly healthy
    to `verify`. The kernel already knows they are one file; `_file_key` asks
    it. A path that cannot be `stat`ed is dropped, which `isfile` did anyway.
    [E4, review: CLI minor]

    Inode identity buys the deduplication at the cost of a discontinuity, which
    a reviewer found and which is worth naming rather than fixing. Two *hard
    links* to one transcript are one inode, so exactly one of the two names is
    kept — whichever `_hits` reaches first, which is deterministic
    for a fixed set of names but changes if a name is added or removed. Break
    the link (`cp` over one of them) and the survivor's session carries on while
    the other becomes a new session that starts at offset zero and re-captures
    the whole file.

    Left as is on purpose. The pre-fix behaviour was strictly worse — *both*
    names captured, always, into two sessions — so this trades a rare
    discontinuity for a common duplication. The recommended alternative,
    case-normalising the path instead, does not work: `posixpath.normcase` is
    the identity function on macOS, and case sensitivity is a per-volume
    property no string transform can read. A caller who genuinely wants two
    names captured separately should give them two watch roots and two agents,
    which is the configuration that already says so. [E4, review: Gemini r3 §1]
    """
    seen: set[tuple[int, int]] = set()
    out = []
    # Deepest root first, so a transcript goes to the watch that names the most
    # of its path. `seen` is shared across watches — it has to be, or a nested
    # root captures the same bytes a second time under a second agent — and the
    # order it was filled in was the order of the config file. So
    #
    #     [[watch]] agent = "claude-code"  roots = ["~/agents"]
    #     [[watch]] agent = "kimi-code"    roots = ["~/agents/kimi"]
    #
    # filed every Kimi transcript under `claude-code`, which is the wrong
    # adapter, which is no compaction boundaries — and `capture_one` degrades to
    # bytes without them in silence, so the store looked healthy and `verify`
    # agreed. Swapping the two tables fixed it, which is not a property a config
    # format should have. Most-specific-wins is the rule every other nested
    # matcher uses and the only one that does not depend on file order.
    # [E4, review: daemon 6]
    pairs = [(watch, root) for watch in watches for root in watch.roots]
    pairs.sort(key=lambda wr: wr[1].count(os.sep), reverse=True)
    for watch, root in pairs:
        for path in _hits(root, watch.pattern):
            real = os.path.realpath(path)
            key = _file_key(real)
            if key is None or key in seen or not os.path.isfile(real):
                continue
            # `_hits` refuses to descend a symlinked directory, but a symlinked
            # *file* it matched inside the root is still a name for bytes that
            # can live anywhere, and the `realpath` above just followed it.
            if _covers([watch], real) is None:
                continue
            seen.add(key)
            out.append((watch, real))
    return out


def _recorded(home: str, now: float) -> dict[tuple[str, str], tuple[int, float]]:
    """`(agent, session_id) -> (captured size, manifest mtime)` for the live generation.

    Keyed on the highest generation number, not on "last in sorted order". Sort
    order is lexicographic, so it agrees with generation order only up to g99 —
    `"g100.json" < "g99.json"`, and a store that had forked a hundred times
    would start treating g99 as live for ever. Reachable only by a hundred
    divergences in one session, which is why it is small; but "the live
    generation" has an exact definition and taking the maximum is no more code
    than assuming the sort matches it. [E4, review: store-contract 7]

    A manifest mtime in the future is corrected on the way past — see the note
    at the gate in `tick`. [E4, review: concurrency 2]
    """
    out: dict[tuple[str, str], tuple[int, float]] = {}
    live: dict[tuple[str, str], int] = {}
    for s in store.sessions(home):
        key = (s.agent, s.session_id)
        if key in live and s.generation < live[key]:
            continue
        try:
            mtime = os.path.getmtime(s.manifest)
            if mtime > now:
                os.utime(s.manifest, (now, now))
                mtime = now
        except OSError:
            mtime = 0.0
        live[key] = s.generation
        out[key] = (s.size, mtime)
    return out


def capture_one(
    home: str, source: str, agent: str, *, parse: bool = True, log=None
) -> store.Capture:
    """Capture one transcript, collecting compaction boundaries if we can.

    The parse is only ever for boundaries, so a parse failure must not cost us
    the bytes — it degrades to a capture without them. Shared with the CLI's
    `capture` subcommand so that the command and the daemon cannot drift. [E4]

    `except Exception`, not a list of types. The list was
    `(OSError, RecursionError, ValueError, KeyError)` and it was already wrong:
    the Claude Code adapter raises `AttributeError` when a content block's
    `text` or `thinking` is not a string, and those blocks come from
    `toolUseResult` and `attachment` payloads that third-party MCP servers fill
    in and the adapter copies verbatim. One transcript line of
    `{"type":"text","text":{}}` and the exception escaped here, escaped `tick`,
    escaped `run`, and killed the watcher — which is *the capture guarantee* —
    permanently, because the transcript is still there on the next restart.
    Reproduced before fixing. [E4, review: store-contract 1]

    Naming types here makes the watcher's liveness depend on getting every
    future adapter's failure taxonomy exactly right, which is a bet this
    function's own docstring says we are not making: the parse is for
    boundaries, and boundaries are the thing we are allowed to lose.
    """
    boundaries = None
    if parse:
        try:
            # The parse and the copy are two reads of one file, and a rewrite
            # between them attaches the old content's offsets to the new
            # content's bytes. `store.capture` sees the rewrite and forks a
            # generation, correctly dropping the *carried* boundaries — and
            # then writes these ones into the fresh manifest, where every later
            # capture carries them forward for the life of the session. The
            # store now refuses an offset outside the bytes it holds, which is
            # the floor; this is the mitigation that keeps a *plausible* wrong
            # offset — one that still lands inside the new content — from being
            # recorded at all, because no floor can see that one.
            #
            # Same degrade as a parse failure, for the same reason: boundaries
            # are what we are allowed to lose. [E4, review]
            #
            # ponytail: a rewrite that lands on the same size and the same
            # nanosecond is invisible here. The next capture writes the right
            # boundaries for whatever it reads, so the ceiling is one wrong
            # generation, not a wrong session.
            before = os.stat(source)
            before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            session = get_adapter(agent).parse(source)
            boundaries = [e.byte_offset for e in session.events if e.kind == "compaction"]
            after = os.stat(source)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
                if log:
                    log("source changed while parsing; capturing bytes without boundaries")
                boundaries = None
        except Exception as exc:  # noqa: BLE001 - untrusted data; bytes outrank boundaries
            # `log` exists because the CLI's two capture paths disagreed about
            # this. `gitmemory capture <path> --session-id x` printed "parse
            # failed (…)"; `gitmemory capture <path>` — the form the README
            # documents — came through here and said nothing at all. E4 created
            # the split by routing the second form through this function.
            # [E4, review: CLI 5]
            if log:
                log(f"parse failed ({exc}); capturing bytes without boundaries")
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
    commit: bool = True,
) -> Tick:
    """One pass: drain the spool, capture what is due, commit once.

    One commit for the whole pass rather than one per session. A pass is the
    unit of work the watcher actually did, and a hundred single-session commits
    for one wake-up is a history nobody can read.

    `commit=False` captures without committing, and `run` passes it whenever
    `gitrepo.init` has not succeeded. Two comments used to cancel each other
    out: `init` refuses a repository whose config is not isolated — *"refusing
    beats warning"* — and `run` logged that refusal and ran the pass anyway,
    because *a git failure must not cost the bytes*. Both are right. Together
    they committed into a repository whose isolation was known to be broken.
    Measured: a `core.excludesFile` matching `*.jsonl`, reaching the repository
    through an `include.path`, left the manifest tracked and the segment it
    names untracked, with `verify` returning clean — verbatim the scenario
    `_assert_no_foreign_config`'s own docstring describes.

    The bytes still land, which is the rule this function keeps. Only the
    commit waits, and it is not lost: git commits whatever is in the tree, so
    the first pass after `init` succeeds carries the whole backlog. [E7 fs-F1]
    """
    now = time.time() if now is None else now
    forced, result = drain_spool(home, watches, now=now)
    try:
        recorded = _recorded(home, now)
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

    committable = False
    for watch, source in discover(watches):
        try:
            # The key the store will actually file this under, asked of the
            # store rather than assembled here. See `store.identity`: keying on
            # the raw names made every session look new on every pass the
            # moment a capital letter appeared anywhere.
            key = store.identity(watch.agent, store.session_id_for(source))
        except ValueError as exc:
            result.errors.append(f"{source}: {exc}")
            continue
        try:
            stat = os.stat(source)
        except OSError as exc:
            result.errors.append(f"{source}: {exc}")
            continue
        size, mtime = recorded.get(key, (-1, 0.0))
        # A manifest mtime in the future makes `now - mtime` negative, so the
        # interval gate can never fire and the session is skipped for the whole
        # duration of the skew — silently, with `verify` clean, and with the
        # hook left doing 100% of the capture. That inverts the property this
        # module exists for. It takes no clock manipulation to reach: a store
        # restored from a machine whose clock ran fast carries the future mtimes
        # with it, and `tar -p` and `rsync -a` both preserve them faithfully.
        # [E4, review: concurrency 2]
        #
        # Repaired rather than worked around. `min(mtime, now)` was the first
        # attempt and it fixes nothing: clamping a future mtime to `now` makes
        # the elapsed time exactly zero, which fails the same gate the negative
        # value did, for the same whole duration. The only way to stop reading a
        # corrupt timestamp is to stop having one — so correct it, once, and let
        # every pass after this one take the ordinary path. A manifest's mtime
        # is bookkeeping: git does not record it, `verify` does not read it, and
        # the bytes it describes are untouched. `_recorded` does the repair,
        # because the function that reads the timestamp is the one that can fix
        # it — it is the only place holding the manifest's path.
        # Size *differs*, not grows: a pruner that rewrites the transcript in
        # place can leave it shorter, and that is a divergence the store has to
        # see. mtime covers the rewrite that happens to land on the same length.
        changed = stat.st_size != size or stat.st_mtime > mtime
        if not changed:
            continue
        if not (forced.get((stat.st_dev, stat.st_ino)) or size < 0 or now - mtime >= interval):
            continue
        try:
            cap = capture_one(home, source, watch.agent, parse=parse)
        except Exception as exc:  # noqa: BLE001 - one session cannot suspend the rest
            # `except Exception`, for the reason `capture_one` gives one level
            # down: naming types makes the guarantee depend on getting every
            # future failure taxonomy exactly right. This list was
            # `(OSError, RuntimeError, ValueError)`, and a `KeyError` out of a
            # damaged manifest went straight past it — out of `tick`, into
            # `run`'s floor, which reports `pass failed` and abandons the
            # *whole* pass. One unreadable session therefore stopped capture for
            # every session on the machine, on every pass, for ever, with
            # `verify` clean throughout. Per-session here, so the blast radius
            # is the session the fault belongs to.
            #
            # The negative control is worth recording: the two extra types were
            # not load-bearing — the suite is green with `except OSError` alone.
            # They were guesses. [E4, review: daemon 1]
            result.errors.append(f"{source}: {exc}")
            continue
        if cap.appended or cap.diverged:
            result.captured.append(f"{key[0]}/{key[1]}/g{cap.generation:02d}")
            result.appended += cap.appended
        else:
            # A capture that appended nothing has just proved the manifest and
            # the source agree, so the manifest's mtime is told what it now
            # knows. Without this it is frozen at the last append, `changed`
            # above stays true for ever on the strength of a source mtime that
            # is merely *ahead* of it — a restore, a `touch`, a pruner — and
            # `now - mtime` only grows, so every pass re-read and re-hashed the
            # whole transcript to discover nothing again. Measured on a 21 MB
            # file: 2766 ms a pass, 55% of a core, and not one line of log.
            #
            # `utime` and not a write: mtime is bookkeeping here, git does not
            # record it and `verify` does not read it, so a no-op capture still
            # leaves the store byte-identical and still produces no commit.
            # [E4, review: daemon 4]
            with contextlib.suppress(OSError):
                os.utime(cap.manifest_path, (now, now))
        result.boundaries_dropped += cap.dropped_boundaries
        result.adopted.extend(
            f"{key[0]}/{key[1]}/g{cap.generation:02d}/{name}" for name in cap.adopted
        )
        # Adoption changes the store without appending a byte, so it has to be
        # asked about separately or the repair never reaches git. Kept out of
        # `result.captured` because nothing was captured; it only says the pass
        # has something to commit. [E4, review: store-contract 4, concurrency 4]
        committable = committable or bool(cap.appended or cap.diverged or cap.adopted)

    if committable and not commit:
        # Said through `result.errors` rather than logged here, so it reaches
        # the operator at `ERROR_REPEAT` and not once per poll, and so that a
        # store which is capturing but not versioning cannot look idle. [E7 fs-F1]
        result.errors.append(
            "captured, not committed: the repository is not initialised; "
            "the next pass after it is will commit the backlog"
        )
    if committable and commit:
        # "recovered" when the only work was adoption: the pass wrote a manifest
        # for a killed capture's orphaned segment and copied nothing out.
        head = ", ".join(result.captured[:3]) or "recovered an interrupted capture"
        more = f" (+{len(result.captured) - 3} more)" if len(result.captured) > 3 else ""
        # A git failure is reported, never raised. By this point the bytes are
        # already in the store and `verify` will vouch for them; the commit is
        # the versioning layer on top. Letting a full disk or a corrupted index
        # abort the pass here would turn a recoverable problem — the next pass
        # commits everything at once — into a watcher that stops capturing. [E4]
        try:
            made = gitrepo.commit(home, f"capture: {head}{more}  +{result.appended}B")
            result.commit = made.sha if made else None
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

    This loop is the floor under the whole product. "If the hook is never
    installed the system is still correct" is a claim about *this function
    continuing to run*, and until review it had no `try` in it anywhere: a
    `RecursionError` out of the spool, an `AttributeError` out of an adapter, a
    `ValueError` out of `realpath` on a NUL byte — each one killed the watcher
    and, because the input that caused it is still on disk, killed it again on
    every restart. Individually those are fixed where they happen. Collectively
    the lesson is that a guarantee needs a floor and not a list of the ways
    people have fallen through so far. [E4, review]
    """
    home = store.resolve_home(home)
    started = False
    last_errors: list[str] = []
    last_notes: list[str] | None = None
    last_dropped = 0
    last_unreadable = 0
    last_said = 0.0
    while True:
        # Inside the loop, because `init` is four `git config` calls and each
        # takes `.git/config`'s lock: two watchers starting within a few
        # milliseconds of each other collided and one died outright, six times
        # out of six measured. A transient lock loss is now a logged warning and
        # a retry on the next poll instead of process death. Still once per
        # *successful* start, so the steady state is the zero-subprocess idle
        # pass it was before. [E4, review: concurrency 7]
        # `or not exists(.git)`: the flag alone made the repository a start-up
        # fact, and it is not one. Delete `.git` under a running watcher — a
        # `rm -rf` aimed at the wrong path, a store restored from a backup that
        # skipped dotfiles — and every pass from then on captured bytes
        # correctly, failed to commit, logged the same sentence, and never tried
        # to init again, because `started` was still True. The bytes were never
        # at risk and `verify` stayed clean; what stopped for ever was the
        # versioning, which is half the product. One `stat` per pass buys it
        # back. Tested against the failure rather than the flag. [E4, review: CLI 9]
        init_error: str | None = None
        if not started or not os.path.exists(os.path.join(home, ".git")):
            try:
                gitrepo.init(home)
                started = True
            except (gitrepo.GitError, OSError, subprocess.SubprocessError) as exc:
                # Reported, and then the pass runs anyway. This used to
                # `continue`, which threw away the capture along with the
                # commit — against the rule `tick` states twenty lines below and
                # keeps: *a git failure is reported, never raised*, because the
                # bytes are the part no later pass can recover and the commit is
                # the part every later pass can. Two watchers racing `init`'s
                # four `git config` calls is the ordinary way to reach this, and
                # the loser was skipping transcripts over it.
                #
                # `started` stays False, so the next pass tries again — and now
                # also suppresses the commit, because the earlier claim on this
                # line ("`tick`'s own commit fails and says so") was not true:
                # the commit succeeded, into a repository `init` had just
                # refused. See `tick`'s `commit=`. [E4, review: daemon 7; E7 fs-F1]
                #
                # Carried into `result.errors` instead of logged here, for the
                # rate limit. Logged directly it was one line per poll for as
                # long as the fault lasted — ~17k lines a day at the default
                # interval — while the loop below goes to considerable trouble
                # to throttle every other repeating error. [E7 fs-F1]
                init_error = f"git init: {exc}"
        try:
            # Collected rather than logged directly, because `run` re-reads the
            # config every pass and a config fault that is still there is not
            # news. Said once when it appears, again when it changes, and never
            # in between — the same discipline as the error log below, for the
            # same reason. A config that *breaks* while the watcher runs is the
            # case this exists for: `_watch` checked for an empty watch list
            # once, before the loop, so a config edited into a syntax error took
            # the watcher silent for ever while still looking healthy.
            # [E4, review: CLI 2]
            notes: list[str] = []
            watches = load_watches(home, log=notes.append)
            if not watches:
                notes.append("watching nothing")
            if notes != last_notes:
                for note in notes:
                    log(f"config: {note}")
                last_notes = notes
            result = tick(home, watches, interval=interval, parse=parse, commit=started)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - the watcher is the guarantee; it does not die
            result = Tick(errors=[f"pass failed: {exc!r}"])
        if init_error:
            # First, because it is the cause of anything else this pass says.
            result.errors.insert(0, init_error)
        # Errors repeat for as long as their cause does, and there is one per
        # stuck session per pass. Measured at 100 unwritable sessions and the
        # default five-second poll: 2.06 M lines and 239 MB of stderr a day, all
        # of it the same hundred sentences. So: say it when it changes, then say
        # nothing until it changes again or `ERROR_REPEAT` has passed — which is
        # the rate the code comment above already claimed and the code did not
        # keep, by a factor of 720. [E4, review: CLI 4]
        now = time.time()
        if result.errors != last_errors or (result.errors and now - last_said >= ERROR_REPEAT):
            for err in result.errors:
                log(f"error: {err}")
            last_errors, last_said = list(result.errors), now
        # A doorbell naming a path no watch covers is the one signal that says
        # the hook and the config disagree about where transcripts live — a hook
        # installed for `~/.claude/projects` against a config that names
        # `~/agents`, say. It was counted into the `Tick` and then thrown away,
        # so that machine was indistinguishable from a quiet one: captures still
        # happened, at the interval instead of at the compaction, and nothing
        # anywhere said why. Change-detected rather than rate-limited, because
        # this is a configuration fact and not an event. [E4, review: daemon 8]
        if result.spool_dropped != last_dropped:
            if result.spool_dropped:
                log(
                    f"config: {result.spool_dropped} of {result.spool_consumed} hook record(s) "
                    "named a path no watch covers"
                )
            last_dropped = result.spool_dropped
        # Same change-detection, different fact, different word: nothing here
        # is the operator's config, and nothing here is fixed by editing it.
        # [E7 fs-F3]
        if result.spool_unreadable != last_unreadable:
            if result.spool_unreadable:
                log(
                    f"spool: {result.spool_unreadable} of {result.spool_consumed} "
                    "hook record(s) were not a readable file, and were discarded"
                )
            last_unreadable = result.spool_unreadable
        # Before `captured`, and not folded into it: an adoption-only pass never
        # reaches that branch, because nothing was captured. Unconditional
        # rather than change-detected — this is rare by construction, and a
        # repeat means it happened twice. [E7 fs-F4]
        if result.adopted:
            log(
                f"adopted {len(result.adopted)} segment(s) found on disk, not copied "
                f"from the source: {', '.join(result.adopted)}"
            )
        if result.captured:
            dropped = (
                f" ({result.boundaries_dropped} boundaries outside the bytes, dropped)"
                if result.boundaries_dropped
                else ""
            )
            log(
                f"captured {len(result.captured)} +{result.appended}B "
                f"commit={result.commit}{dropped}"
            )
        if once:
            return 1 if result.errors else 0
        time.sleep(poll)
