"""Append-only segment store, generations, and the contiguity proof.

The store never writes to the source transcript. It copies out the bytes that
appeared since the last capture, records where they sat, and leaves a manifest
that a stranger can check with `cat` and `shasum` — no Python, no LLM, no
access to this machine. DESIGN.md §2.4/§2.5/§2.5a.

Layout under `$GITMEMORY_HOME` (default `~/.gitmemory`):

    raw/<agent>/<session_id>/g00/000000000000-000000131072.jsonl
    sessions/<agent>/<session_id>/g00.json

One manifest **per generation**, flat and never rewritten once the next
generation opens. DESIGN.md §2.5a puts generations in a subdirectory and the
manifest under `sessions/<agent>/<date>/…`; both are changed here [E2] and the
reasons are recorded in that section. The short version: a `<date>` tier keys a
committed path on a value we may not have (a transcript whose first line has no
timestamp), and one manifest per session would leave a sealed generation
verifiable only by digging through git history — a stranger with a checkout
could not check g00 at all.

Two invariants this module exists to hold:

1. **Segments tile `[0, size)` exactly** — no hole, no overlap, no gap at the
   start. Contiguity is arithmetic, not a heuristic.
2. **A sealed generation is immutable.** When the source is rewritten under us
   we do not stop capturing and we do not overwrite: we seal what we have and
   open `g01`. The pre-rewrite bytes stay.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from glob import glob
from typing import IO

from .records import canonical_json

__all__ = [
    "Capture",
    "EscapingSegment",
    "Stored",
    "UnreadableManifest",
    "capture",
    "resolve_home",
    "segment_groups",
    "session_id_for",
    "sessions",
    "span",
    "verify",
]

SCHEMA = 1
CHUNK = 1 << 20
EMPTY_SHA256 = hashlib.sha256().hexdigest()

# `\Z` and not `$` in every anchored pattern in this module, because `$` also
# matches immediately before a trailing newline and a newline is a legal
# character in a POSIX filename. Measured: `_GEN_RE.match("g00.json\n")` was
# True while `fnmatch("g00.json\n", "g*.json")` was False — the same check-says-
# yes/enumeration-says-no split as `_listed`, reached through a byte instead of
# through a case. The consequence was a file in the proof tree that `verify`
# never named: planting `sessions/<a>/<s>/g00.json\n` left `verify` reporting
# `[]`, because the `sessions/` walk skipped it as a manifest and the
# manifest-driven `glob` never yielded it to be read. Renaming the same bytes to
# `g99x.json` got it reported. `_SAFE_RE` had it worse, since it is the trust
# boundary for path components: `_SAFE_RE.match("sess\n")` was True, so a
# newline could reach a directory name, a manifest field and a commit.
# [E7b L2-F4]
_GEN_RE = re.compile(r"^g(\d+)\.json\Z")
# What `_write_atomic` leaves behind when it is killed between the create and
# the rename. See `_sweep_temps`.
_TMP_RE = re.compile(r"^g\d+\.json\.tmp\.")
# Leading alnum bans `..`, `-rf`, and dotfiles in one rule. `agent` and
# `session_id` become path components, so they are a trust boundary even when
# today's only caller is our own adapter.
_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# A segment file, and nothing else, in a generation directory.
_SEG_RE = re.compile(r"^(\d{12})-(\d{12})\.jsonl\Z")
# How long a reader waits for a writer's session lock before going without it.
# A capture holds that lock for one tail copy, one fsync and one manifest
# write — milliseconds — so this is ~50x headroom for the case it is meant to
# wait out, and a bound at all for the case it is not. See
# `_locked_if_writable`. [E7 fs-F6]
LOCK_WAIT = 5.0
# The errnos that mean "this store cannot be written to", which is the one case
# where not holding the session lock is sound rather than degraded. See
# `_locked_if_writable`. [E7b L2-F5]
_UNWRITABLE = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


def _safe(name: str, what: str) -> str:
    if not isinstance(name, str) or not _SAFE_RE.match(name):
        raise ValueError(f"unsafe {what} for a path component: {name!r}")
    # Case-fold. APFS and NTFS are case-insensitive but case-preserving, so
    # `CLAUDE-code` and `claude-code` are one directory on disk and two strings
    # in two manifests — and `verify` compares strings, so it would fail from
    # then on, permanently, for a difference the filesystem does not have. [E2]
    return name.lower()


def identity(agent: str, session_id: str) -> tuple[str, str]:
    """The `(agent, session_id)` a capture will actually be **filed under**.

    `capture` normalises both before touching the disk, so the names a caller
    passes in and the names that come back out of `sessions()` are not
    necessarily the same strings. Anything that has to match one against the
    other has to normalise too, and the only safe way to do that is to call the
    store rather than to reimplement `_safe`'s rule.

    That is not hypothetical. The watcher kept its "have I captured this
    already?" bookkeeping keyed on the raw `watch.agent` and the raw
    `session_id_for(source)`, while `_recorded` read the lowercased names back
    out of the manifests. One capital letter anywhere — `Claude-Code` in
    `config.toml`, `Session.jsonl` on disk — and the lookup missed for ever:
    every session looked brand new on every pass, so the once-an-hour
    coalescing gate was bypassed and the watcher cut a segment and re-hashed
    the whole prefix every five seconds instead. Measured at 454 ms per tick on
    a 20 MB transcript. Silent, and it got worse as the transcript grew.
    [E4, review: store-contract 3]

    Raises `ValueError` on a name that is not a legal path component, which is
    the same refusal `capture` would make a moment later — better here, where
    the caller still has somewhere to put the message.
    """
    return _safe(agent, "agent"), _safe(session_id, "session_id")


def _gen_file(n: int) -> str:
    return f"g{n:02d}.json"


def _gen_dir(n: int) -> str:
    return f"g{n:02d}"


def resolve_home(home: str | None = None) -> str:
    """Absolute `$GITMEMORY_HOME`, refusing to sit inside another work tree.

    A store nested in a checkout gets its history rewritten by whoever owns the
    outer repo — a `git clean`, a branch switch, a rebase. Refusing is cheaper
    than explaining the loss afterwards (DESIGN.md §2.4 [R1]).

    `realpath`, not `abspath`: a home that is a symlink into a work tree has
    parents that are not the real ones, so a lexical walk sails straight past
    the `.git` it exists to find. `~/.gitmemory -> ~/dotfiles/gitmemory` is an
    ordinary layout, not a contrived one. [E2]
    """
    path = os.path.realpath(
        os.path.expanduser(home or os.environ.get("GITMEMORY_HOME") or "~/.gitmemory")
    )
    parent = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(parent, ".git")):
            raise RuntimeError(
                f"GITMEMORY_HOME {path} is inside the work tree at {parent}; "
                "the store must own its own repository"
            )
        up = os.path.dirname(parent)
        if up == parent:
            return path
        parent = up


def is_store(home: str | None = None) -> bool:
    """Whether `home` has ever been a store, as distinct from being an empty one.

    `verify` is glob-driven, and an empty glob is indistinguishable from a clean
    store — so `gitmemory verify --home /Volumes/backup/store` with the volume
    unmounted, or with a typo in the path, printed `0 problem(s)` and exited 0.
    The README says of `verify` that "there is an empty list or a list of
    problems; there is no third answer", and that was the third answer wearing
    the first one's clothes.

    The predicate is deliberately not "is this store healthy" — that is what
    `verify` is for, and keeping the two apart is what lets `verify` keep its
    two-answer contract. A store that `watch` has initialised but never captured
    into has `.git` and nothing else, and it is a store; a path with none of the
    three has never been one. [E4, review: CLI 3]
    """
    home = resolve_home(home)
    return any(os.path.exists(os.path.join(home, n)) for n in (".git", "sessions", "raw"))


def session_id_for(source_path: str) -> str:
    """The store's name for the session a transcript file holds.

    Basename plus a digest of the real path. The bare basename made
    `~/projA/session.jsonl` and `~/projB/session.jsonl` one session, and every
    alternating capture "diverged" past the other and re-copied it whole —
    unbounded duplication that `verify` calls clean. [E2]

    It lives here rather than in the CLI because the watcher has to arrive at
    the same name from the same path without asking the store, and two
    implementations of one naming rule is one implementation and one bug. [E4]

    The stem is squeezed into what `_SAFE_RE` accepts, lossily and on purpose:
    the digest is what makes the name unique, so the stem is only there to make
    a directory listing readable. A CLI user picks the path they pass; the
    watcher is handed whatever is in a directory it was pointed at, and
    `capture` raising on a transcript whose name has a space in it would be the
    watcher stopping at the first file it did not choose. [E4]

    Two ceilings, both deliberate, both noted here because the name is the only
    place either is visible:

    The tag was 32 bits, which is a 50% chance of a collision at about 77,000
    transcripts sharing a squeezed stem. That is the [E2] bug above, and it is
    not silent — `_capture` compares the incoming path against the manifest's
    `source_path` and refuses rather than duplicating. But the refusal is a
    stuck session, and under the watcher it is stuck for good: the CLI can be
    handed a different `--session-id` and the watcher cannot, so one transcript
    simply stops being captured. Gemini called that a defect wearing a comment
    and was right. 64 bits costs eight characters in a directory name and moves
    the same 50% out past a hundred billion, and the digest can be widened now
    only because nothing has been released to migrate. The refusal stays: it is
    the floor, and this is just making it unreachable. [E4, review: Gemini r3 §5]

    ponytail: identity is the path, so a transcript that moves is a new session
    and is copied again whole. Bounded — one extra copy, fully attested, and
    the old session stays valid. The upgrade is `st_dev`/`st_ino` in the
    manifest plus a prefix-hash match before adopting the new path; unbuilt
    because inodes are reused, so it needs the hash anyway, for a rename. [E4]
    """
    stem = os.path.splitext(os.path.basename(source_path))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:96].lstrip("._-")
    tag = hashlib.sha256(os.path.realpath(source_path).encode()).hexdigest()[:16]
    return f"{stem}-{tag}" if stem else f"s-{tag}"


@dataclass(slots=True, frozen=True)
class Stored:
    """One generation as a reader sees it: identity, plus its bytes in order."""

    agent: str
    session_id: str
    generation: int
    manifest: str  # absolute path
    segments: tuple[str, ...]  # absolute paths, ascending by start offset
    size: int
    boundaries: tuple[int, ...]  # compaction byte offsets carried by the manifest

    @property
    def key(self) -> str:
        """Stable identity for a generation. Not a path — `agent` is untrusted."""
        return f"{self.agent}/{self.session_id}/g{self.generation:02d}"


@dataclass(slots=True)
class Capture:
    manifest_path: str
    generation: int
    segment: str | None  # repo-relative path written, None when nothing was new
    appended: int
    size: int
    diverged: str | None  # why this capture forked a generation, if it did
    # Did this capture reclaim a killed one's orphaned segment? Separate from
    # `appended`, because adoption writes a manifest without copying a byte out
    # of the source: the store changed, `appended` is 0, and the watcher's
    # "should I commit?" test read `appended` alone. So crash recovery repaired
    # the store on disk and the repair was never committed — and if the session
    # had ended, never would be. Reported by two reviewers independently.
    # [E4, review: store-contract 4 / concurrency 4]
    #
    # Names rather than a flag since E7: a caller that is told *something* was
    # reclaimed cannot say which bytes, and neither could the manifest. Empty
    # is falsey, so every truthiness test on it still reads the same. [E7 fs-F4]
    adopted: tuple[str, ...] = ()
    # Boundaries the caller offered that do not address a byte of this
    # generation, and so were not recorded. See the filter in `_capture`.
    dropped_boundaries: int = 0


def _manifests(session_dir: str) -> list[tuple[int, str]]:
    found = []
    for path in glob(os.path.join(session_dir, "g*.json")):
        m = _GEN_RE.match(os.path.basename(path))
        if m:
            found.append((int(m.group(1)), path))
    return sorted(found)


def _prefix_hash(fh, n: int):
    """sha256 state over the first `n` bytes, or None if the file is shorter.

    Returns the live hashlib object, not a digest — the caller keeps feeding it
    the newly appended bytes so the whole-file hash costs one pass, not two.
    On return the handle sits at byte `n`, ready for that append.
    """
    fh.seek(0)
    h = hashlib.sha256()
    left = n
    while left:
        chunk = fh.read(min(CHUNK, left))
        if not chunk:
            return None
        h.update(chunk)
        left -= len(chunk)
    return h


def _tmp_name(prefix: str) -> str:
    # pid alone collides between threads of one process, and the loser's bytes
    # end up inside the winner's segment. [E2]
    return f"{prefix}.{os.getpid()}.{threading.get_ident():x}"


# The store holds whatever the transcript held, which on a shared box is the
# same argument as `~/.ssh`: 0600 and 0700, never the 0644 a default umask of
# 022 gives you. An explicit mode rather than `os.umask(0o077)` around the
# write, because umask is process-global and not thread-safe — and since umask
# can only *clear* permission bits, an explicit 0600 is already the floor under
# every umask a caller might have. [E3]
def _mkdir(path: str) -> None:
    # One level at a time, because `os.makedirs(mode=...)` applies the mode to
    # the leaf only and leaves every directory it had to create on the way at
    # 0777 & ~umask — so `sessions/` stayed world-readable while the manifest
    # inside it was 0600.
    #
    # `lstat`, not `os.path.isdir`, and the difference is the whole of [E7]
    # index-F1: `isdir` follows symlinks, so a symlink anywhere in the chain was
    # already "a directory" and everything below it was written to the link's
    # target — outside `$GITMEMORY_HOME` entirely, with no error anywhere.
    # Measured on this tree: a symlink at `raw/<agent>` put a whole session's
    # segments in `/tmp`, and one at `derived/<agent>/<session>/g00` put all
    # three artifacts there while `derive.build` reported nothing skipped. Every
    # one of those names is predictable from the store's own contents and does
    # not exist yet before the first write, so any process running as the user
    # can plant one and wait.
    #
    # ponytail: check-then-create, so a fast enough swap between the `lstat` and
    # the `mkdir` still wins. `O_NOFOLLOW` on every final open is the version
    # that closes that, and it is worth doing when something can show the race
    # is reachable.
    missing = []
    while path:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            missing.append(path)
            path = os.path.dirname(path)
            continue
        if stat.S_ISDIR(mode):
            break
        kind = "a symbolic link" if stat.S_ISLNK(mode) else "not a directory"
        raise NotADirectoryError(
            f"{path} is {kind}, so it is not somewhere gitmemory will write; "
            "the store writes only into directories it made itself"
        )
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(d, 0o700)


def _create(path: str):
    """Open `path` for writing, owner-only, refusing to reuse an existing file."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


# What the next capture reads out of the last manifest, and what it must be.
# The flag marks a field that may be absent or null — `diverged_from` and
# `prev_manifest_sha256` are both null in a first generation. [E3]
_FIELDS = {
    "size": (int, False),
    "file_sha256": (str, False),
    "segments": (list, False),
    "compact_boundaries": (list, False),
    "diverged_from": (dict, True),
    "prev_manifest_sha256": (str, True),
    "source_path": (str, True),
    "adopted": (list, True),
}


def _check_manifest(man: object) -> None:
    """Refuse a previous manifest that cannot be used, before anything is written.

    Every field here reaches arithmetic, a path join, or a hash update further
    down. Untyped, a hand-edited or truncated-then-repaired manifest surfaced
    as a TypeError from the middle of a capture — after the segment directory
    existed — rather than as a refusal. [E3]
    """
    if not isinstance(man, dict):
        raise ValueError(f"previous manifest is {type(man).__name__}, not an object")
    for field, (want, optional) in _FIELDS.items():
        val = man.get(field)
        if optional and val is None:
            continue
        if field not in man:
            raise ValueError(f"previous manifest is missing {field!r}")
        # `isinstance(True, int)` is True, and a bool size would tile from 1.
        if not isinstance(val, want) or (want is int and isinstance(val, bool)):
            raise ValueError(
                f"previous manifest has {field!r} as {type(val).__name__}, not {want.__name__}"
            )


def _own_manifest(home: str, path: str) -> bool:
    """Whether `path` is a manifest in this store's own `sessions/` tree.

    Every reader reaches manifests through `glob(home/sessions/*/*/g*.json)`,
    and `glob` follows symlinks — `_mkdir` refuses to *write* through one
    (index-F1) but nothing re-checked on the way back in. A link at `sessions/`
    therefore puts the whole proof outside the store while every check passes.
    Measured: `verify` reads the manifests through the link and reports
    `0 problem(s)`, and `git add --all` commits the *link* — one `120000` blob —
    so the versioned copy holds two raw segments, zero manifests, and `verify`
    in a clone is red for both. Local and remote disagreeing about whether the
    proof exists is the one disagreement this command cannot have.

    Realpath rather than `islink`, so a link at the agent or session level is
    the same answer as one at `sessions/`. [E7 fs-F5]
    """
    return _inside(home, os.path.relpath(path, home)) is not None


def _adopted_names(carried: object, added: tuple[str, ...] = ()) -> list[str]:
    """This generation's adopted-segment names: what the manifest already said, plus new.

    Adoption writes a manifest attesting to bytes it found on disk rather than
    bytes it copied out of the source, and the two used to be indistinguishable
    once written. Filtered against `_SEG_RE` rather than copied, for the reason
    `_clean_boundaries` gives about the previous manifest: it is a file on disk,
    it can be hand-edited, and everything read out of it here is written
    straight back out. Zero-padded names sort chronologically, and a manifest
    that lists one twice is a manifest, not two adoptions. [E7 fs-F4]
    """
    names = {n for n in _seq(carried) if isinstance(n, str) and _SEG_RE.match(n)}
    return sorted(names | set(added))


def _clean_boundaries(values: object, size: int) -> tuple[list[int], int]:
    """The offered boundaries that address a byte of `[0, size]`, and the count dropped.

    Total over any input, which is the whole point of it. `_check_manifest`
    types the *container*, and the elements went straight to `0 <= b <= end` —
    where `0 <= "x"` is a TypeError raised **after** `os.replace` had published
    the segment. So the capture left an orphan behind, the next capture's
    adoption copied the same list into the manifest it wrote, and every capture
    after that died in the same place. `verify` called the store clean
    throughout. [E4, review: store 3]

    Dropped rather than raised, for the reason `_capture` already gives: bytes
    outrank boundaries, and refusing a capture would lose a transcript over an
    annotation of it. Filtering the *union* rather than only the new argument is
    what lets a store that already holds a bad offset heal on its next capture.

    `repr` for the drop count because a boundary can be anything a JSON document
    can hold, including a dict, and a set of those is a TypeError of its own.
    """
    kept: set[int] = set()
    dropped: set[str] = set()
    for b in _seq(values):
        if isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= size:
            kept.add(b)
        else:
            dropped.add(repr(b))
    return sorted(kept), len(dropped)


def _write_atomic(path: str, data: bytes) -> None:
    _mkdir(os.path.dirname(path))
    tmp = _tmp_name(f"{path}.tmp")
    try:
        with _create(tmp) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # keeps tmp's 0600
    except BaseException:
        _unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def _fsync_dir(path: str) -> None:
    """Durability of the rename itself, not just the bytes.

    This *orders* the two publications; it does not make them one. A crash
    between the segment rename and the manifest write still leaves a segment
    no manifest names — `_adopt_orphans` is what makes that state recoverable
    rather than permanent. The earlier version of this docstring claimed the
    fsync closed that window. It does not. [E2]

    And on macOS `os.fsync` does not reach the platter. It flushes the page
    cache to the device and returns; only `fcntl(F_FULLFSYNC)` asks the drive to
    empty its own write cache, and the difference is not subtle: tens of
    microseconds against several milliseconds. `tools/fsync_cost.py` is that
    measurement, so it can be repeated instead of quoted — four runs here gave
    16.8–28.2 µs against 2996–3941 µs. This docstring used to state the quotient,
    "a factor of 106", as if it were a constant; it is the least stable thing in
    the measurement, because the denominator is small and moves with cache state
    while the numerator sits on the drive. It ranged 106–219× across those runs.
    [E4, review: docs — the 106× ratio]

    So the ordering this function establishes is real against a *process* dying,
    which is the failure the store is built for and the one the `kill -9` tests
    exercise, and is not guaranteed against power loss, where the drive may
    commit the two renames in either order.

    Not fixed with `F_FULLFSYNC`, deliberately. It is macOS-only, so it buys a
    platform branch on the capture path, and what it would buy back is already
    covered from the other end: `verify` reads the segments and checks they tile
    the range the manifest claims, so a reordered power-loss state is detected
    rather than trusted, and `_adopt_orphans` recovers the one it can. A
    guarantee that is checked afterwards is worth more than one asserted at
    write time. [E4, review: concurrency 8]
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # some filesystems refuse fsync on a directory
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _lockfile(path: str):
    """Hold an exclusive `flock` on `path`, making it and its parent if needed.

    The mechanism, with no opinion about what is being serialised — `_locked`
    below names sessions, `index.build` names a directory of derived databases,
    and neither wants its own copy of five lines of `os.open`. [E7]
    """
    _mkdir(os.path.dirname(path))
    # `_mkdir` refuses a symlinked *directory*; the lock file itself is the one
    # name below `$GITMEMORY_HOME` that nothing checked. Measured: a symlink at
    # `.locks/<agent>/<sid>.lock` pointing anywhere the user can write had an
    # empty 0600 file created there by the next capture, which reported success
    # — a file-creation primitive with no content and no truncation, and then
    # `flock` taken on whatever is at the far end rather than on this session.
    # `O_NOFOLLOW` turns both into an error the caller sees. `O_CREAT` is
    # unaffected when the name is free, which is every ordinary run. [E7 fs-F5]
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _locked(home: str, agent: str, session_id: str):
    """One capture at a time per session. Other sessions still run in parallel.

    Without it, two captures that read the same prior manifest each write a
    segment and the loser's is left named by nothing: `verify` reports an
    unrecorded file from then on, and no later capture clears it. A hook fire
    racing a watcher is the ordinary case, not the exotic one. [E2]

    Locks live under `<home>/.locks/`, not beside the manifests — the sessions
    tree is what gets committed, and a lock file is not part of the proof.
    """
    return _lockfile(os.path.join(home, ".locks", agent, f"{session_id}.lock"))


def _sweep_temps(session_dir: str) -> None:
    """Remove the manifest temps a killed `_write_atomic` left behind.

    The segment half of that crash — `raw/.../.incoming.<pid>.<tid>` — has been
    swept since E2, a few lines below. This half was not:
    `sessions/<a>/<s>/g00.json.tmp.<pid>.<tid>` stayed on disk for ever, and
    `gitrepo.GITIGNORE` documented that as a standing condition rather than
    fixing it. A temp is an abandoned copy that never became a manifest;
    removing it is what the interrupted process would have done itself.

    Safe because the caller holds the session lock, and the lock is what serialises
    the only thing that writes one. [E4, review: store 8]
    """
    with contextlib.suppress(OSError), os.scandir(session_dir) as it:
        for entry in it:
            if _TMP_RE.match(entry.name):
                _unlink(entry.path)


def _adopt_orphans(home: str, agent: str, session_id: str, session_dir: str) -> tuple[str, ...]:
    """Finish a capture that was killed after its segment landed. The names it took.

    The return value is what tells the watcher a pass did something worth
    committing. [E4, review]

    **Adoption is the one path that attests to bytes nobody here copied.** The
    only tests an orphan passes are its name, that its start offset continues
    the manifest, and that its length matches the name — none of which tie it
    to a capture this machine performed, and adoption runs at the head of every
    `_capture`, not only after a crash. So a file dropped into the generation
    directory is adopted, hashed into `file_sha256`, and served as transcript
    content, and `verify` — which reported it as an unrecorded file one command
    earlier — goes quiet. Turning a red proof green is the worst direction for
    a proof to fail in. The names are therefore written into the manifest
    (`adopted`) and carried forward, so the proof states which bytes were
    reclaimed from disk rather than copied from the source, and a reader can
    tell "we found this here" from "we wrote this". [E7 fs-F4]

    Write ordering makes "segment on disk, manifest not written" the only crash
    state — for a process crash. Power loss can reorder the two publications,
    because `os.fsync` on macOS does not flush the drive's own cache; see
    `_fsync_dir`. That state is detected by `verify` rather than repaired here.
    Nothing used to reclaim the ordinary one: the next capture resumes from the
    recorded offset, publishes a differently-named overlapping segment, and
    `verify` reports an unrecorded file on every run thereafter with no command
    that fixes it.

    Adopting beats deleting. The orphan can hold bytes that are no longer in the
    source — the fable-pruner case, where the transcript is rewritten in place
    while the manifest still points before the rewrite. Deleting would destroy
    the only surviving copy, which is the loss the generations design exists to
    prevent. So we write the manifest the killed capture would have written and
    let the next divergence check decide whether the source still continues it.

    Re-hashing the whole generation is the cost, and it is paid only after a
    crash. Callers hold the session lock. [E2]
    """
    _sweep_temps(session_dir)
    prior = _manifests(session_dir)
    gen = prior[-1][0] if prior else 0
    raw_dir = os.path.join(home, "raw", agent, session_id)
    # A fork is killed in the same publish-then-manifest window as any other
    # capture, but its segment lands one generation *above* anything a manifest
    # names. Computing `gen` from `prior[-1]` therefore looked in `g<N>/`, found
    # nothing, and returned — while the next capture forked again, built the
    # same `g<N+1>/000000000000-NNN.jsonl` name, and hit `refusing to overwrite
    # an existing segment`. Permanently: every capture after it repeats that,
    # or `verify` stays red for ever if the source went away.
    # [E4, review: store 1]
    forked = bool(prior) and os.path.isdir(os.path.join(raw_dir, _gen_dir(gen + 1)))
    seg_dir = os.path.join(raw_dir, _gen_dir(gen + 1 if forked else gen))
    if not os.path.isdir(seg_dir):
        return ()

    man: dict | None = None
    if prior:
        with open(prior[-1][1], "rb") as fh:
            prev_bytes = fh.read()
        man = json.loads(prev_bytes)
        # Before anything is rewritten, and here rather than only in `_capture`:
        # adoption is the *first* reader of the last manifest, and it feeds
        # `size` straight to `while size in pending`. [E3]
        _check_manifest(man)
        if forked:
            # The killed capture's ancestry is entirely reconstructible: it is a
            # function of the manifest it forked away from, which is on disk and
            # has just been checked. Everything else about a fresh generation is
            # a constant — it starts at byte zero, carries no segments, and
            # carries no boundaries, exactly as `_capture` writes one.
            man = {
                "source_path": man.get("source_path", ""),
                "diverged_from": {
                    "generation": gen,
                    "at_byte": man["size"],
                    "prev_file_sha256": man["file_sha256"],
                },
                "prev_manifest_sha256": hashlib.sha256(prev_bytes).hexdigest(),
                "segments": [],
                "size": 0,
                "compact_boundaries": [],
            }
            gen += 1

    with os.scandir(seg_dir) as it:
        names = sorted(e.name for e in it if e.is_file())
    # A temp file is an abandoned copy that never became a segment. Removing it
    # is what the interrupted process would have done itself; it is also the one
    # thing `verify`'s stray check cannot see, because it starts with a dot.
    for name in names:
        if name.startswith(".incoming."):
            _unlink(os.path.join(seg_dir, name))

    listed = {os.path.basename(s["path"]) for s in (man or {}).get("segments", [])}
    pending: dict[int, tuple[int, str]] = {}
    for name in names:
        m = _SEG_RE.match(name)
        if m and name not in listed:
            pending[int(m.group(1))] = (int(m.group(2)), name)

    segments = list((man or {}).get("segments", []))
    size = (man or {}).get("size", 0)
    taken: list[str] = []
    while size in pending:
        end, name = pending.pop(size)
        full = os.path.join(seg_dir, name)
        if not _regular(full) or os.path.getsize(full) != end - size:
            break  # a torn temp, not a published segment; leave it for verify
        with open(full, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        segments.append(
            {
                "path": os.path.join("raw", agent, session_id, _gen_dir(gen), name),
                "start": size,
                "end": end,
                "sha256": digest,
            }
        )
        size = end
        taken.append(name)

    if not taken:
        return ()

    whole = hashlib.sha256()
    for seg in segments:
        p = seg.get("path")
        full = _inside(home, p) if isinstance(p, str) else None
        if full is None:
            raise EscapingSegment(f"segment path escapes the store: {p!r}")
        if not _regular(full):
            # Same lock, same block as `_verify_one`'s, but this hash is the
            # proof — a proof computed over a run with a hole in it is worse
            # than no adoption at all, so this one raises. Not
            # `EscapingSegment`: the path does not escape, it is the wrong kind
            # of thing, and `sessions` re-raises `EscapingSegment` specifically
            # because an escape is an attack rather than a mess. [E7]
            raise ValueError(f"segment is not a regular file: {p!r}")
        with open(full, "rb") as fh:
            while chunk := fh.read(CHUNK):
                whole.update(chunk)
    manifest = {
        "schema": SCHEMA,
        "agent": agent,
        "session_id": session_id,
        "source_path": (man or {}).get("source_path", ""),
        "generation": gen,
        "diverged_from": (man or {}).get("diverged_from"),
        "size": size,
        "file_sha256": whole.hexdigest(),
        "prev_manifest_sha256": (man or {}).get("prev_manifest_sha256"),
        "segments": segments,
        # Filtered, not copied. Adoption is the one writer that used to pass a
        # previous manifest's boundaries straight into `canonical_json`, which
        # is how one unusable element became permanent. [E4, review: store 3]
        "compact_boundaries": _clean_boundaries((man or {}).get("compact_boundaries"), size)[0],
        # A fork rebuilt `man` above without this key, which is right: the names
        # address files in *this* generation's directory, so a new generation
        # starts with none. [E7 fs-F4]
        "adopted": _adopted_names((man or {}).get("adopted"), tuple(taken)),
    }
    _write_atomic(os.path.join(session_dir, _gen_file(gen)), canonical_json(manifest))
    return tuple(taken)


def capture(
    source_path: str,
    agent: str,
    session_id: str,
    home: str | None = None,
    boundaries: list[int] | None = None,
) -> Capture:
    """Copy out the bytes appended since the last capture. Never writes to source.

    `boundaries` are byte offsets of compaction boundaries, accumulated across
    captures within a generation. DESIGN.md §2.5 lists them as turn `seq`
    values; byte offsets are used instead [E2] because this module holds no
    record model and must not import an adapter to get one — and because the
    coalescer (E4) cuts segments on byte positions.
    """
    home = resolve_home(home)
    agent = _safe(agent, "agent")
    session_id = _safe(session_id, "session_id")
    # No `realpath` here, deliberately: the first version of the fs-F10 fix put
    # one on this line, and it re-resolved the attacker's link a moment before
    # the read that was supposed to refuse it — the repro still leaked, 3 of 400
    # ticks. Resolution belongs where the path is *validated* (`daemon.discover`)
    # and must not happen again afterwards. [E7 fs-F10]
    with _locked(home, agent, session_id):
        return _capture(home, source_path, agent, session_id, boundaries)


def _open_source(source_path: str) -> IO[bytes]:
    """Open the transcript for reading. A transcript is a file, not a name for one.

    `daemon.discover` resolves every path it hands down and drops anything that
    resolves outside its watch root, so a symlink here is a link that appeared
    *after* that check — the TOCTOU window between `discover` and this read.
    Measured before fixing: a thread flipping the transcript to a symlink landed
    a private key from outside the watch root into the store as an attested
    segment on 7 of 400 ordinary `tick` calls, and deterministically when
    swapped between the two steps `tick` already performs in that order. The
    segment verified clean, because it was: the store faithfully recorded bytes
    it should never have been shown. [E7 fs-F10]

    The refusal is unconditional rather than a flag the daemon sets, because
    the first attempt at this *was* conditional — a `realpath` in `capture` so
    that naming a link on the command line kept working — and it re-resolved
    the attacker's link a moment before this open, leaking on 3 of 400 ticks.
    A path resolved twice is a path resolved at the wrong time. So
    `gitmemory capture <link>` now refuses too, and says to name the target;
    it was always a half-truth anyway, since the manifest records the realpath.

    `O_NOFOLLOW` guards the final component only. Swapping an intermediate
    *directory* needs write access to a parent of the watch root, and there the
    glob's own `_covers` check — realpath against the resolved root — is what
    refuses the climb.

    ponytail: no `openat` walk. The demonstrated attack is the leaf, the leaf
    is closed, and a full fd-relative descent is the upgrade if a watch root
    ever lives somewhere a stranger can rename directories.
    """
    try:
        fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        if os.path.islink(source_path):
            raise RuntimeError(
                # The target is deliberately not named. For the command-line
                # case the user can see it; for the attack case, "pass
                # /home/…/id_rsa instead" is the wrong advice to print.
                f"{source_path} is a symlink; capture reads a file, not a name "
                f"for one — pass the path it resolves to"
            ) from None
        raise
    return os.fdopen(fd, "rb")


def _capture(  # noqa: PLR0912, PLR0915 - one branch per failure mode; splitting hides the ordering
    home: str,
    source_path: str,
    agent: str,
    session_id: str,
    boundaries: list[int] | None,
) -> Capture:
    session_dir = os.path.join(home, "sessions", agent, session_id)
    adopted = _adopt_orphans(home, agent, session_id, session_dir)
    prior = _manifests(session_dir)

    gen, base, segments, diverged_from, prev_manifest_sha = 0, 0, [], None, None
    diverged: str | None = None
    carried: list[int] = []
    carried_adopted: list[str] = []

    with _open_source(source_path) as fh:
        if prior:
            prev_gen, prev_path = prior[-1]
            with open(prev_path, "rb") as pf:
                prev_bytes = pf.read()
            prev = json.loads(prev_bytes)
            _check_manifest(prev)

            # A different file is not a rewrite of this one. Two transcripts
            # with the same basename used to land in one session directory and
            # "diverge" past each other forever, re-copying both in full on
            # every capture — zero duplication (DESIGN.md §2.4) inverted. [E2]
            was = prev.get("source_path")
            if was and was != os.path.realpath(source_path):
                raise ValueError(
                    f"session {session_id!r} was captured from {was}, not "
                    f"{os.path.realpath(source_path)}; pass a distinct --session-id"
                )
            whole = _prefix_hash(fh, prev["size"])
            if whole is None:
                diverged = f"source is shorter than the {prev['size']} bytes already captured"
            elif whole.hexdigest() != prev["file_sha256"]:
                diverged = f"bytes [0,{prev['size']}) changed after capture"

            if diverged:
                # Seal, do not overwrite. fable's pruner rewrites live
                # transcripts in place (DESIGN.md §2.5a); a store that halts on
                # first divergence would stop capturing the moment a neighbour
                # like that runs, and one that overwrites would lose exactly
                # the bytes nobody else keeps.
                gen = prev_gen + 1
                fh.seek(0)
                whole = hashlib.sha256()
                diverged_from = {
                    "generation": prev_gen,
                    "at_byte": prev["size"],
                    "prev_file_sha256": prev["file_sha256"],
                }
                prev_manifest_sha = hashlib.sha256(prev_bytes).hexdigest()
            else:
                gen, base = prev_gen, prev["size"]
                segments = list(prev["segments"])
                diverged_from = prev["diverged_from"]
                prev_manifest_sha = prev["prev_manifest_sha256"]
                carried = list(prev["compact_boundaries"])
                # Carried for the same reason the boundaries are: this manifest
                # replaces the previous one outright, so a field the previous
                # one stated and this one omits is a fact deleted by the next
                # ordinary capture. Adoption would be recorded for exactly as
                # long as nothing else happened. The fork branch above does not
                # carry it — a new generation has adopted nothing. [E7 fs-F4]
                carried_adopted = _adopted_names(prev.get("adopted"))
        else:
            whole = hashlib.sha256()

        seg_dir = os.path.join(home, "raw", agent, session_id, _gen_dir(gen))
        _mkdir(seg_dir)
        tmp = _tmp_name(os.path.join(seg_dir, ".incoming"))
        seg_hash = hashlib.sha256()
        appended = 0
        try:
            with _create(tmp) as out:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    whole.update(chunk)
                    seg_hash.update(chunk)
                    appended += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            # The pruner race: the source can shrink between our read of byte 0
            # and our read of EOF, in which case `whole` hashes a state that
            # never existed on disk. Growth is normal and benign (the next
            # capture picks it up); shrinkage is not.
            live = os.fstat(fh.fileno()).st_size
            if live < base + appended:
                raise RuntimeError(
                    f"{source_path} shrank to {live} bytes while being read "
                    f"(expected at least {base + appended}); capture abandoned"
                )
        except BaseException:
            _unlink(tmp)
            raise

    end = base + appended
    # A boundary is an offset into this generation, so one outside `[0, end]`
    # addresses bytes the store does not have. The daemon tries not to produce
    # those — it re-stats the source around the parse — but that is a mitigation
    # with a window in it, and a guarantee needs a floor. Without this the store
    # wrote whatever it was handed, `verify` called it clean, and the next
    # capture carried it into the following manifest for ever; a *negative* one
    # got through too, which sends `span` reading from the wrong place. The
    # union is filtered rather than only the argument, so a store that already
    # holds a bad offset heals on its next capture. [E4, review: Gemini r3 §4]
    #
    # Computed here rather than after the segment is published, because the
    # no-op return below has to know whether these changed. [E4, review: store 7]
    kept_boundaries, dropped = _clean_boundaries([*carried, *(boundaries or [])], end)
    seg_rel: str | None = None
    if appended:
        name = f"{base:012d}-{end:012d}.jsonl"
        seg_path = os.path.join(seg_dir, name)
        # Never clobber. A file already at this name holds bytes no manifest
        # claims, and an equal-length in-place rewrite of the source produces
        # exactly this collision — `os.replace` would destroy the only copy of
        # the pre-rewrite bytes. Adoption above should have taken it; if one is
        # still here, stop rather than overwrite. [E2]
        # ponytail: TOCTOU-free only because the session lock is held.
        if os.path.exists(seg_path):
            _unlink(tmp)
            raise RuntimeError(f"refusing to overwrite an existing segment: {name}")
        os.replace(tmp, seg_path)
        _fsync_dir(seg_dir)
        seg_rel = os.path.join("raw", agent, session_id, _gen_dir(gen), name)
        segments.append(
            {"path": seg_rel, "start": base, "end": end, "sha256": seg_hash.hexdigest()}
        )
    else:
        _unlink(tmp)
        if prior and not diverged and kept_boundaries == carried:
            # Nothing new and nothing wrong: leave the manifest byte-identical
            # so a no-op capture produces no commit. `adopted` still travels —
            # this is the exact path a recovery pass takes, and reporting 0
            # appended with nothing else set is what made the repair invisible.
            #
            # Gated on the boundaries as well as the bytes. A compaction that
            # appends nothing still happened, and taking this return before the
            # merge below threw its offset away in silence — `verify` clean,
            # capture reporting success, the annotation simply gone.
            # [E4, review: store 7]
            return Capture(
                prior[-1][1], gen, None, 0, end, None, adopted=adopted, dropped_boundaries=dropped
            )

    manifest = {
        "schema": SCHEMA,
        "agent": agent,
        "session_id": session_id,
        "source_path": os.path.realpath(source_path),
        "generation": gen,
        "diverged_from": diverged_from,
        "size": end,
        "file_sha256": whole.hexdigest(),
        "prev_manifest_sha256": prev_manifest_sha,
        "segments": segments,
        "compact_boundaries": kept_boundaries,
        "adopted": carried_adopted,
    }
    manifest_path = os.path.join(session_dir, _gen_file(gen))
    # Invariant 2 (a sealed generation is immutable) was breakable by a slow
    # capture writing g00 after a neighbour had already forked g01. The session
    # lock is the fix; a redundant "refuse if g<n+1> exists" guard was written
    # here and then deleted, because `gen` always comes from the newest manifest
    # and no reachable state makes it fire. An untestable branch is not insurance. [E2]
    _write_atomic(manifest_path, canonical_json(manifest))
    return Capture(
        manifest_path,
        gen,
        seg_rel,
        appended,
        end,
        diverged,
        adopted=adopted,
        dropped_boundaries=dropped,
    )


def _unlink(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


class EscapingSegment(RuntimeError):
    """A manifest named a segment path outside the store.

    Its own class, and deliberately not a `ValueError`: `sessions` skips
    unreadable manifests, and `json.JSONDecodeError` *is* a `ValueError`, so
    `except ValueError: raise` there turned the ordinary half-written manifest
    a full disk leaves behind into a store that cannot be read at all. [E3]

    `RuntimeError` because the CLI already reports that as `error:` and exits
    2, which is the handling this wants.
    """


class UnreadableManifest(RuntimeError):
    """A manifest `segment_groups` could not turn into a segment run. [E7]

    `EscapingSegment`'s sibling, and raised for the same reason one field
    further out. `sessions` skips a manifest it cannot read, because a reader
    that raised would make one bad manifest un-indexable for the whole store.
    But the *gate* reads through `segment_groups`, and a skip there is not a
    skip: it removes that generation's seams from the scan, and the seam join is
    the only thing that sees a credential cut in half by a segment boundary.

    So one byte — `"start": 0` to `"start": "0"`, or a `segments` dict instead
    of a list, or a truncated write leaving unparseable JSON — used to take a
    generation out of the gate's feed while `push` went on printing "gate
    passed". The segments were still scanned individually; exactly the
    straddling secret was lost, which is the one the group scan exists for.
    """


class _Skip(Exception):
    """A manifest shape `sessions` declines, raised so it joins the same path a
    `json.JSONDecodeError` takes. Two ways out of one loop body is how the
    `strict` check gets added to one of them and forgotten on the other."""


def _seg_start(seg: object) -> int:
    """The start offset a segment entry claims, or -1 if it claims nothing usable.

    Total over any input so the sort is well-defined whatever the manifest
    holds. What then *rejects* a garbage entry is `_segment_path`, not this.
    """
    return _int(seg.get("start")) if isinstance(seg, dict) else -1


def _segment_path(home: str, seg: object) -> str | None:
    """The absolute path a segment entry names, or None if it names none.

    None costs the whole generation rather than one entry. Dropping just the
    entry leaves a run that is silently one segment short, which concatenates
    to bytes that were never the transcript and moves every offset past the
    gap — the failure the store exists to make impossible. [E3]

    "Names one" is `verify`'s definition of a well-formed entry, deliberately:
    a `start` this reader ordered by and `verify` called malformed is the same
    sessions/verify divergence in a smaller place.
    """
    if not isinstance(seg, dict) or not isinstance(seg.get("path"), str):
        return None
    if not isinstance(seg.get("start"), int) or isinstance(seg.get("start"), bool):
        return None
    full = _inside(home, seg["path"])
    if full is None:
        # Not a skip. `segment_groups` feeds the egress gate, so a manifest that
        # points outside the store would opt its own run out of the seam scan;
        # dropping the row quietly is the bypass, so the whole read fails. [E3]
        raise EscapingSegment(f"segment path escapes the store: {seg['path']!r}")
    return full


def sessions(home: str, *, strict: bool = False) -> list[Stored]:
    """Every readable generation in the store, in a stable order.

    A generation, not a session, is the unit: a fork seals one byte history and
    starts another, and both are real. Callers that want only the live history
    take the highest `generation` per `(agent, session_id)`.

    Unreadable manifests are skipped — a reader that raised on them would make
    one bad manifest un-indexable for the whole store, which is the E2 sweep bug.
    An *escaping* segment path is the exception and fails the call: see
    `EscapingSegment`.

    `strict=True` turns every one of those skips into `UnreadableManifest`. It
    is what `segment_groups` passes, and nothing else should: a reader wants as
    much of the store as it can get, and the gate wants all of it or nothing.
    A file matching `g*.json` that is not a generation name at all is still
    skipped under `strict` — it is a stray file, which `verify` reports and the
    file walk scans, not a generation claiming segments nobody looked at. [E7]
    """
    out = []
    for path in sorted(glob(os.path.join(home, "sessions", "*", "*", "g*.json"))):
        if not _GEN_RE.match(os.path.basename(path)):
            continue
        if not _own_manifest(home, path):
            # `EscapingSegment`, and not a skip, for the reason the docstring
            # gives about segments: an escape is an attack rather than a mess,
            # and a reader that quietly returned "no sessions" would have the
            # index, the dashboard and the egress gate all agree the store is
            # empty. [E7 fs-F5]
            raise EscapingSegment(
                f"{os.path.relpath(path, home)} leaves the store, so it is not its manifest"
            )
        try:
            with open(path, "rb") as fh:
                man = json.loads(fh.read())
            # Ordered by start offset, which is the key `verify` already sorts
            # by. Reading `man["segments"]` verbatim while `verify` sorted was a
            # contradiction a reordered manifest passed straight through: the
            # proof reported clean and every offset addressed a concatenation
            # that was never the transcript. [E3]
            # A run of *zero* segments is a real generation, not a broken one: a
            # source truncated to nothing forks, and the fork holds no bytes
            # yet. `not run` dropped it, so every reader took the sealed
            # previous generation as the live one — stale history presented as
            # current, with nothing saying so. A missing or non-list `segments`
            # is still a skip, because that manifest speaks for nothing and
            # `segment_groups` feeds the egress gate. [E4, review: store 6]
            raw_segments = man.get("segments")
            if not isinstance(raw_segments, list):
                raise _Skip(f"'segments' is {type(raw_segments).__name__}, not a list")
            segs = sorted(raw_segments, key=_seg_start)
            run = [_segment_path(home, s) for s in segs]
            if not all(run):
                raise _Skip("a segment entry names no usable path")
        except EscapingSegment:
            raise
        except Exception as exc:  # noqa: BLE001 - a manifest is untrusted data
            if strict:
                # The path, always: a `json.JSONDecodeError` says "line 1 column
                # 2" and nothing about which of a thousand manifests it read.
                raise UnreadableManifest(f"{path}: {exc}") from exc
            continue
        # Everything below is best-effort. A row is never dropped for a bad
        # *metadata* field, only for an unreadable or escaping segment list:
        # `segment_groups` feeds the egress gate, and a manifest that opts out
        # of the scan by naming itself oddly would be a bypass, not a skip.
        out.append(
            Stored(
                agent=_text(man.get("agent")),
                session_id=_text(man.get("session_id")),
                generation=_int(man.get("generation")),
                manifest=path,
                segments=tuple(p for p in run if p),
                size=_int(man.get("size")),
                boundaries=tuple(sorted(_int(b) for b in _seq(man.get("compact_boundaries")))),
            )
        )
    return out


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _seq(value: object) -> tuple:
    return tuple(value) if isinstance(value, list) else ()


def segment_groups(home: str) -> list[list[str]]:
    """One ordered, absolute segment run per generation manifest.

    The egress gate needs these because a credential can straddle a cut: what
    leaves the machine is the concatenation, not any one file. [E2]

    `strict=True`, because this is the gate's feed and not a reader's: raises
    `UnreadableManifest` rather than returning a store with a generation's seams
    quietly missing from it. [E7]
    """
    return [list(s.segments) for s in sessions(home, strict=True)]


def span(stored: Stored, offset: int, length: int) -> bytes:
    """`length` bytes at a generation-relative `offset`, across segment cuts.

    Every offset in this system — a `Turn`, a compaction boundary, a `Hit` —
    addresses the *concatenation* of a generation, because that is what the
    parser was handed. A segment is a copy window, not a unit of meaning.

    So the resolver is this and not `(path, offset)`: no such pair exists for a
    turn the store happened to cut in half, and a caller who seeks with one
    reads whatever the following segment begins with — plausible bytes from the
    wrong place, which is the failure this whole layer is built to prevent. [E3]

    Sizes come from the files rather than the manifest on purpose: a manifest is
    untrusted data, and whether its arithmetic matches the bytes is `verify`'s
    question, not this reader's. Segments are immutable once published — a
    capture writes a new one — so the sizes cannot move underneath the walk.

    A short generation returns short rather than raising: the caller asked what
    is there.
    """
    out = bytearray()
    pos = 0  # where the current segment starts within the generation
    for seg in stored.segments:
        size = os.path.getsize(seg)
        if pos + size > offset and len(out) < length:
            with open(seg, "rb") as fh:
                fh.seek(max(0, offset - pos))
                out += fh.read(length - len(out))
        pos += size
    return bytes(out)


def verify(home: str | None = None) -> list[str]:
    """Check every manifest in the store. Empty list means the proof holds.

    Same arithmetic as the documented one-liner in DESIGN.md §2.5, plus the
    checks a shell one-liner cannot express: that no unrecorded file is sitting
    in a generation directory, and that the generation chain links up.
    """
    home = resolve_home(home)
    problems: list[str] = []
    for path in sorted(glob(os.path.join(home, "sessions", "*", "*", "g*.json"))):
        if not _GEN_RE.match(os.path.basename(path)):
            continue
        rel = os.path.relpath(path, home)
        if not _own_manifest(home, path):
            # Reported rather than raised, because `verify` reports: it is the
            # one command whose contract is a list of problems. Continuing also
            # leaves the segments this manifest would have vouched for to the
            # walk below, which says so in its own words — which is exactly
            # what a stranger's clone of this store sees. [E7 fs-F5]
            problems.append(f"{rel}: leaves the store, so it is not this store's manifest")
            continue
        try:
            problems += _verify_manifest(home, path)
        except Exception as exc:  # noqa: BLE001 - a manifest is untrusted data
            # One malformed manifest used to abort the sweep, so real tampering
            # in every later session went unreported and the exit code blamed
            # the crash instead. A bad manifest is one problem, not a stop. [E2]
            problems.append(f"{rel}: unverifiable manifest ({exc!r})")
    # Guarded for the same reason the loop above is, and it was not: `verify` is
    # the proof command, so every part of it has to survive a store that has
    # been corrupted in a way nobody predicted. An unguarded second sweep put
    # the abort back in — one unreadable directory and the whole report,
    # including the manifest findings already collected, was replaced by a
    # traceback. Found by the E2 regression test written for the first version
    # of this mistake. [E4]
    try:
        problems += _verify_unattested(home)
    except Exception as exc:  # noqa: BLE001 - the store is untrusted data
        problems.append(f"raw/: unverifiable ({exc!r})")
    return problems


def _listed(path: str) -> bool:
    """Is `path` a name the directory actually holds, spelled the way it asks?

    `os.path.exists` asks the filesystem to resolve a name, and a
    case-insensitive one resolves `g00.json` onto `G00.json` and says yes. Every
    enumeration in this module is a `glob`, which matches its pattern
    case-sensitively against the listing and says no. A check and a sweep that
    disagree about which files are there is how bytes end up attested by a
    manifest nothing verifies. This asks the enumeration's question.
    [E7b L2-F1]
    """
    try:
        return os.path.basename(path) in os.listdir(os.path.dirname(path))
    except OSError:
        return False


def _verify_unattested(home: str) -> list[str]:
    """Every file in the store that no manifest speaks for.

    The sweep above is manifest-driven: it starts from `sessions/*/*/g*.json`
    and checks that the bytes each manifest names are the bytes on disk. That
    direction alone has a blind spot in the shape of a whole session. A
    generation directory holding **zero** manifests is never visited, so the
    "unrecorded file in the generation directory" check never runs for it, and
    its segments are invisible to the proof entirely.

    Reachable, and permanently: crash on a session's *first* capture, before
    its g00 manifest is written, and then let the transcript go away — the
    session ended, the agent cleaned up. `discover()` never yields it again, so
    `_adopt_orphans` never runs, so the manifest is never written. Meanwhile
    another session keeps growing, that pass commits, and `git add --all`
    stages the orphaned segment. Real transcript bytes, in git for ever, that
    `verify` declared clean. For a store whose pitch is that a stranger can
    check it with `cat` and `shasum`, unattested bytes the proof command calls
    clean is a hole in the proof rather than a missing nicety.
    [E4, review: concurrency 5]

    The first version of this was pinned to `raw/*/*/g*` — the one depth the
    incident of the day had used. That is a denylist with a single entry: a file
    at `raw/<agent>/<session>/leftover.jsonl`, or at `raw/loose.jsonl`, was
    invisible to it and `git add --all` committed it anyway. And there was no
    check on `sessions/` at all, which is the tree that *is* the proof.

    So the question is asked once, in the shape the guarantee needs: which files
    in this store does no manifest speak for? Under `raw/`, that is everything
    outside an attested generation directory — inside one, the manifest-driven
    sweep names the stray against the manifest it contradicts, which is the more
    useful message and does not race. Under `sessions/`, the store writes
    exactly one kind of file at exactly one depth, so anything else is a
    finding, including the `g<NN>.json.tmp.<pid>.<tid>` a killed `_write_atomic`
    leaves. [E4, review: store 2]

    One extra glob and two walks, all in the other direction.
    """
    found: list[tuple[str, str]] = []
    raw = os.path.join(home, "raw")
    attested = set()
    for seg_dir in sorted(glob(os.path.join(raw, "*", "*", "g*"))):
        if not os.path.isdir(seg_dir):
            continue
        gen_dir = os.path.basename(seg_dir)
        session_dir = os.path.dirname(seg_dir)
        manifest = os.path.join(
            home,
            "sessions",
            os.path.basename(os.path.dirname(session_dir)),
            os.path.basename(session_dir),
            gen_dir + ".json",
        )
        # `_own_manifest` as well as `exists`: without it a manifest reached
        # through a symlinked `sessions/` marks the generation directory
        # attested, so the loop above skips it *and* the walk below skips it,
        # and the bytes nothing in the store speaks for are reported by
        # nobody. [E7 fs-F5]
        #
        # And `_GEN_RE`, because "a manifest exists" was a laxer rule here than
        # in the loop this defers to. `verify` skips `g-1.json` — `-` is not a
        # digit — while this marked `raw/…/g-1` attested on the same file, so
        # the bytes in it were reported by neither. The store is not silent
        # about it: the `sessions/` walk below calls `g-1.json` "not a
        # manifest", which uses this same regex and is why the two rules have
        # to be the same one. But naming the odd manifest is not naming the
        # transcript bytes beside it. One definition of "a generation
        # directory", used by everything that skips one. [E7 pair review]
        #
        # `_listed` rather than `os.path.exists`, because on a case-insensitive
        # filesystem — which is the macOS default, so it is the common case and
        # not the exotic one — those are different questions. A manifest on disk
        # as `G00.json` answers `exists(".../g00.json")` with True, while the
        # `glob("g*.json")` the manifest-driven sweep enumerates with matches its
        # pattern case-sensitively and never yields it. Measured: `exists` True,
        # `glob` empty, on the same directory. So the generation directory was
        # marked attested by a manifest that nothing then verified, and a file
        # planted beside the segments went unnamed — the `g-1.json` asymmetry
        # above, reappearing through a spelling instead of through a regex. The
        # store still says *something* (`G00.json: not a manifest`), and saying
        # something about the manifest is not naming the transcript bytes, which
        # is the distinction the paragraph above exists to make. [E7b L2-F1]
        if (
            _GEN_RE.match(os.path.basename(manifest))
            and _listed(manifest)
            and _own_manifest(home, manifest)
        ):
            attested.add(os.path.realpath(seg_dir))
    for dirpath, dirnames, filenames in os.walk(raw):
        if os.path.realpath(dirpath) in attested:
            # `_verify_manifest` owns this directory, holding the session lock
            # while it lists it. A subdirectory of it is not a generation
            # directory of anything and shows up there as an unrecorded entry.
            dirnames[:] = []
            continue
        found += _linked(home, dirpath, dirnames)
        # An empty directory attests to nothing and is not evidence of loss.
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), home)
            found.append((rel, f"{rel}: no manifest speaks for these bytes"))
    sess = os.path.join(home, "sessions")
    for dirpath, dirnames, filenames in os.walk(sess):
        rel_dir = os.path.relpath(dirpath, sess)
        at_depth = rel_dir != os.curdir and rel_dir.count(os.sep) == 1
        found += _linked(home, dirpath, dirnames)
        for name in sorted(filenames):
            if at_depth and _GEN_RE.match(name):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), home)
            found.append((rel, f"{rel}: not a manifest"))
    return _settled(home, found)


def _linked(home: str, dirpath: str, dirnames: list[str]) -> list[tuple[str, str]]:
    """Symlinked subdirectories of `dirpath`, as findings.

    `os.walk` does not follow a symlinked directory and does not list it among
    `filenames` either, so it was the one entry the sweep above could not see —
    the last member of the single-entry denylist that docstring rejects.
    Measured: `raw/<agent>/linked -> /outside` holding `unattested.jsonl`, and
    `verify` returned `[]`.

    A link is a finding on its own terms rather than a door to walk through.
    What it names is not in this store: `git add --all` commits the `120000`
    blob, so a clone has the name and not the bytes, and following it would
    have `verify` report files that a stranger checking the same commit cannot
    see. The store never creates one, at any depth. [E7 fs-F7]
    """
    out = []
    for name in sorted(dirnames):
        full = os.path.join(dirpath, name)
        if os.path.islink(full):
            rel = os.path.relpath(full, home)
            out.append((rel, f"{rel}: a symlink, so what it names is not in this store"))
    return out


def _session_of(rel: str) -> tuple[str, str] | None:
    """The session a candidate finding belongs to, or None above that level."""
    parts = rel.split(os.sep)
    return (parts[1], parts[2]) if len(parts) >= 4 else None  # <tree>/<agent>/<session>/…


def _settled(home: str, found: list[tuple[str, str]]) -> list[str]:
    """The candidate findings that are litter rather than files being written.

    The walk above is lockless by nature: it has no session in hand until it has
    already found something. So it sees the in-flight artefacts too — the
    `g<NN>.json.tmp.<pid>.<tid>` a live `_write_atomic` is holding open, the
    `.incoming.<pid>.<tid>` a live capture is filling. Both are real unattested
    bytes once a crash leaves them behind, and neither is a problem while the
    process that made them is still there. Left unchecked, this sweep reported
    the sound store it was added to protect — the same false positive the lock
    in `_locked_if_writable` exists to prevent, arriving from the other side.

    Taking the session's lock settles which one it is with no heuristic about
    pids or mtimes: the writer holds that lock for as long as either artefact
    exists, so by the time we have it the file is either gone (it was in flight)
    or still on disk (it was abandoned). Paths above the session level have no
    writer to wait for. [E4, review: store 2]

    `lexists`, not `exists`: a dangling symlink is a file the walk found and
    this check then threw away, because the question `exists` answers is about
    the target. Measured: a link to a nonexistent path at
    `raw/<agent>/<session>/dangling` was reported by the walk and dropped here,
    so `verify` returned `[]`. The same link one level up was reported, because
    that depth never reaches this function — a finding that appeared or
    vanished with how deep the attacker put it. [E7 fs-F7]

    **Once per session, and the wait's verdict is used.** This was once per
    candidate *file*, and it threw away the `timed_out` the lock yields — so
    both halves of fs-F6 were undone here, in the sweep, after being fixed in
    `_verify_one`. Two measurements, both on a sound store with one live writer:

    - The same in-flight `.incoming` file was reported as litter or not
      depending only on how long the writer held the lock — `0 problem(s)` at a
      2 s hold, `no manifest speaks for these bytes` at an 8 s hold. Giving up
      on the lock is exactly the moment the answer stops being trustworthy, and
      it was the moment the old code started answering from the filesystem.
    - Every candidate paid the full `LOCK_WAIT` on the *same* lock, so the cost
      was linear in the number of findings: 2 files took 15.01 s, 4 took 25.05 s.
      A hundred stray files in one session is over eight minutes of waiting to
      produce a hundred wrong answers.

    So the lock is taken once per session, held across every candidate of that
    session, and a session whose lock never came says so in one line instead of
    guessing for each of its files. Output order follows the walk, because the
    walk's order is `verify`'s. [E7b L2-F2, L2-F3]
    """
    order: list[tuple[str, str]] = []
    rows: dict[tuple[str, str], list[str]] = {}
    keep: set[str] = set()
    for rel, _ in found:
        key = _session_of(rel)
        if key is None:
            keep.add(rel)
            continue
        if key not in rows:
            rows[key] = []
            order.append(key)
        rows[key].append(rel)

    declined: list[str] = []
    for key in order:
        with _locked_if_writable(home, *key) as unswept:
            if unswept:
                declined.append(f"{key[0]}/{key[1]}: not swept for unattested files — {unswept}")
                continue
            keep |= {r for r in rows[key] if os.path.lexists(os.path.join(home, r))}
    return [msg for rel, msg in found if rel in keep] + declined


def _flock_within(fd: int, seconds: float) -> bool:
    """Take an exclusive lock, or give up after `seconds`. True if we hold it.

    Polled rather than alarm-based: `signal.setitimer` is main-thread-only and
    this runs under the daemon's threads too, and `SIGALRM` would race any
    other timer in the process. The sleep is short enough that an ordinary
    capture is waited out in one or two passes, and the whole point is the
    deadline, not the latency. [E7 fs-F6]
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


@contextlib.contextmanager
def _locked_if_writable(home: str, agent: str, session_id: str):
    """The session lock when the store can be written to, and nothing when it cannot.

    `verify` read a manifest, hashed its segments, and only then listed the
    generation directory — taking no lock at any point. A capture that published
    a segment inside that window was reported as an unrecorded file, on a store
    that was perfectly sound. A watcher in one terminal and a `verify` in
    another is the ordinary way to run this, not a contrived race; the reviewer
    measured 79 of 82 concurrent runs calling a healthy store corrupt.
    [E4, review: store 5]

    Degrading rather than failing is what keeps the central claim true: a
    stranger with a read-only checkout has to be able to run `verify`, and a
    read-only store is exactly the one where no capture can be in flight — so a
    lock we cannot take is a lock we do not need. The names are checked because
    they come off the filesystem rather than through `_safe`, and they are about
    to be joined into a path.

    Bounded since E7, because `LOCK_EX` had no bound and `verify`'s contract is
    that there is an empty list or a list of problems and no third answer. One
    process holding this lock and not letting go was a third answer: measured,
    `verify` did not return at all while the lock was held.

    Yields **why the sweep would be unsound**, or None, which is *not* the same
    question as whether the lock is held. A read-only store yields None with no
    lock, because a store nobody can write to is one where no capture can be in
    flight — the sweep is sound without the lock. A timeout yields a reason,
    because a holder exists and is mid-publication.

    Which of those a failed `os.open` is depends on *why* it failed, and it used
    to be read as the first one unconditionally. `EROFS`, `EACCES` and `EPERM`
    are the read-only checkout this function is named for. Every other errno is
    a lock we did not take on a store that can still be written: `ELOOP` from the
    `O_NOFOLLOW` that fs-F5 added, which an unprivileged local process causes at
    will by replacing `.locks/<agent>/<sid>.lock` with a symlink *after* a
    capture has taken it; `EMFILE` under fd pressure, which needs no attacker at
    all; `ENOSPC` from `_mkdir`. Measured: with a symlink at the lock path this
    yielded the same thing an ordinary writable store did, so the sweep ran
    unlocked beside a live capture and the E4 store-5 false positive came back —
    deterministically, and silently. [E7b L2-F5]

    A reason rather than a flag, because the two ways of not having the lock are
    not the same sentence, and the caller prints what it is given: "another
    process held it" is a lie about a lock that was never opened. [E7b L2-F5]

    Bounding the wait without telling the caller
    put the E4 store-5 false positive back, gated behind five seconds: under a
    saturated machine `verify` gave up on the lock and reported 71 of a healthy
    store's live segments as litter, silently, in the command whose whole job is
    to be believed. `_verify_one` now declines the sweep and says which session
    it declined, so a degraded answer is legible as one.
    `O_NOFOLLOW` for the reason `_lockfile` has it: this is the same file, and
    it was the second open of it. [E7 fs-F6, fs-F5, E7 pair review]
    """
    fd = None
    unswept: str | None = None
    if _SAFE_RE.match(agent) and _SAFE_RE.match(session_id):
        try:
            lock = os.path.join(home, ".locks", agent, f"{session_id}.lock")
            _mkdir(os.path.dirname(lock))
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if not _flock_within(fd, LOCK_WAIT):
                unswept = (
                    f"another process held the session lock for more than {LOCK_WAIT:g}s"
                )
                os.close(fd)
                fd = None
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            fd = None
            if exc.errno not in _UNWRITABLE:
                # The errno and not the message: `strerror` is the same phrase
                # for everyone and the path is already the caller's subject.
                code = errno.errorcode.get(exc.errno or 0, exc.errno)
                unswept = f"the session lock could not be opened ({code})"
    try:
        yield unswept
    finally:
        if fd is not None:
            os.close(fd)


def _verify_manifest(home: str, path: str) -> list[str]:
    # Built from the directory this manifest is **filed under**, not from the
    # identity it declares about itself. Those are normally the same and a
    # tampered manifest is exactly the case where they are not: editing
    # `session_id` to name a directory that does not exist made `os.scandir`
    # raise, `contextlib.suppress(OSError)` swallow it, and the stray-file scan
    # examine nothing — while every other check below still passed, because they
    # all follow the `path` recorded inside each segment entry. The one check
    # that exists to find unattested bytes was the one an attacker could switch
    # off, from inside the file being checked.
    #
    # `_safe` is not applied to the components: they come off the filesystem,
    # they are already whatever `capture` wrote, and re-normalising them here
    # would reintroduce the same "trust the string over the location" move.
    # [E4, review: store-contract 2]
    session_dir = os.path.dirname(path)
    filed_agent = os.path.basename(os.path.dirname(session_dir))
    filed_session = os.path.basename(session_dir)
    with _locked_if_writable(home, filed_agent, filed_session) as unswept:
        return _verify_one(home, path, filed_agent, filed_session, unswept)


def _verify_one(
    home: str, path: str, filed_agent: str, filed_session: str, unswept: str | None = None
) -> list[str]:
    """The manifest's own checks, and then — always — the directory it speaks for.

    The stray-file check used to be the last statement of one long function with
    ten early `return`s in front of it, so any manifest that failed an earlier
    check took it down. That is the one check whose job is to find bytes no
    manifest attests, and the attacker chooses which check fails: plant a
    transcript in the generation directory, break the manifest, and `verify`
    reports "unreadable manifest" and nothing else. Measured, all eight
    tamperings tried hid the plant.

    `_verify_unattested` does not cover for it, which is what makes this a hole
    rather than a duplicated message: that sweep marks a generation directory
    attested when a manifest for it *exists*, not when it verifies, so it skips
    the directory too. Nobody reports the bytes.

    The sweep runs even if the checks raise, for the reason `verify` has its own
    blanket guard: a manifest is untrusted data, and "the directory is always
    swept" is only worth having if nothing in the file being checked can switch
    it off. [E7 S6]

    It does not run when the session lock could not be had, and that is the one
    thing the manifest cannot switch off either — `unswept` comes from the lock,
    not from the file. A capture mid-publication has segments on disk that its
    manifest does not name yet, so the sweep would report a sound store as
    littered; the declared checks above are safe either way, because segments
    are immutable and the manifest is replaced atomically. Saying which session
    went unswept is the difference between a degraded answer and a wrong one.
    [E7 pair review]
    """
    rel = os.path.relpath(path, home)
    listed: set[str] = set()
    try:
        out, reconciled = _verify_declared(home, path, rel, filed_agent, filed_session, listed)
    except Exception as exc:  # noqa: BLE001 - a manifest is untrusted data
        out, reconciled = [f"{rel}: unverifiable manifest ({exc!r})"], False
    if unswept:
        return out + [f"{rel}: not swept for unrecorded files — {unswept}"]
    out += _verify_generation_dir(home, path, rel, filed_agent, filed_session, listed, reconciled)
    return out


def _verify_generation_dir(
    home: str,
    path: str,
    rel: str,
    filed_agent: str,
    filed_session: str,
    listed: set[str],
    reconciled: bool,
) -> list[str]:
    """Files in the generation directory that no verified segment entry names.

    The directory name comes off the manifest's own filename, not its declared
    `generation`, for the reason `_verify_manifest` builds the agent and session
    from the location: the declaration is the thing under suspicion. It is also
    the only way this can run at all when `generation` is what failed.

    `glob("*")` used to be the enumeration, and it skips dotfiles, so the one
    artefact a killed capture leaves — `.incoming.<pid>.<tid>`, real unattested
    transcript bytes — was the one file this check could not see, and
    `.evil.jsonl` passed the proof. [E2]
    """
    seg_dir = os.path.join(
        home, "raw", filed_agent, filed_session, os.path.splitext(os.path.basename(path))[0]
    )
    out = []
    with contextlib.suppress(OSError), os.scandir(seg_dir) as it:
        for entry in sorted(it, key=lambda e: e.name):
            if os.path.realpath(entry.path) in listed:
                continue
            if reconciled:
                out.append(f"{rel}: unrecorded file in the generation directory: {entry.name}")
            else:
                # Not the same sentence, because it is not the same claim. The
                # manifest was rejected, so `listed` is partial or empty and
                # every segment of a perfectly sound generation lands here too.
                # Saying "unrecorded" would point at the directory when the
                # finding is in the manifest. [E7 S6]
                out.append(f"{rel}: manifest rejected, so nothing attests {entry.name}")
    return out


def _verify_declared(  # noqa: PLR0912 - one branch per failure mode
    home: str, path: str, rel: str, filed_agent: str, filed_session: str, listed: set[str]
) -> tuple[list[str], bool]:
    """Everything the manifest says about itself, and whether it got to the end.

    The flag is what lets the caller tell "this directory disagrees with a
    manifest that parsed" from "this manifest never got far enough to say".
    """
    out: list[str] = []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        man = json.loads(raw)
    except (OSError, ValueError) as exc:
        return [f"{rel}: unreadable manifest ({exc})"], False

    if not isinstance(man, dict):
        return [f"{rel}: manifest is {type(man).__name__}, not an object"], False

    agent = man.get("agent")
    if not isinstance(agent, str) or not _SAFE_RE.match(agent):
        return [f"{rel}: declares unsafe agent name {agent!r}"], False

    session_id = man.get("session_id")
    if not isinstance(session_id, str) or not _SAFE_RE.match(session_id):
        return [f"{rel}: declares unsafe session_id {session_id!r}"], False

    gen = man.get("generation")
    filed_as = os.path.basename(path)
    if not isinstance(gen, int) or _gen_file(gen) != filed_as:
        # Stop here: every check below feeds `gen` to a path or a format spec.
        return [f"{rel}: declares generation {gen!r} but is filed as {filed_as}"], False

    raw_segments = man.get("segments")
    if not isinstance(raw_segments, list):
        return [f"{rel}: segments is {type(raw_segments).__name__}, not a list"], False
    for s in raw_segments:
        if not (
            isinstance(s, dict)
            and isinstance(s.get("start"), int)
            and isinstance(s.get("end"), int)
            and isinstance(s.get("path"), str)
            and isinstance(s.get("sha256"), str)
        ):
            return [f"{rel}: malformed segment entry {s!r}"], False
    segments = sorted(raw_segments, key=lambda s: s["start"])
    whole = hashlib.sha256()
    cursor = 0
    for seg in segments:
        if seg["start"] != cursor:
            kind = "hole" if seg["start"] > cursor else "overlap"
            out.append(f"{rel}: {kind} at byte {cursor} (next segment starts at {seg['start']})")
        full = _inside(home, seg["path"])
        if full is None:
            out.append(f"{rel}: segment path escapes the store: {seg['path']!r}")
            return out, False
        listed.add(full)
        if not _regular(full):
            # Before the open, not inside the `except`: a FIFO does not fail to
            # open, it never finishes opening, and this read holds the session
            # lock. [E7]
            out.append(f"{rel}: segment {seg['path']} is not a regular file")
            return out, False
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            out.append(f"{rel}: segment {seg['path']} unreadable ({exc})")
            return out, False
        want = seg["end"] - seg["start"]
        if len(data) != want:
            out.append(f"{rel}: segment {seg['path']} is {len(data)} bytes, manifest says {want}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != seg["sha256"]:
            out.append(f"{rel}: segment {seg['path']} content changed (sha256 mismatch)")
        whole.update(data)
        cursor = seg["end"]

    if cursor != man.get("size"):
        out.append(
            f"{rel}: segments cover {cursor} bytes, manifest declares size {man.get('size')}"
        )
    if whole.hexdigest() != man.get("file_sha256"):
        out.append(f"{rel}: concatenated segments do not hash to file_sha256")

    # Say so, rather than only declining to be misled by it. A manifest
    # whose declared identity disagrees with its location is a tampered or
    # hand-moved manifest either way, and `verify` exists to name that.
    if (agent, session_id) != (filed_agent, filed_session):
        out.append(
            f"{rel}: declares {agent}/{session_id} but is filed under {filed_agent}/{filed_session}"
        )
    # The writer's floor, applied by the reader. `verify` checked the fields it
    # needed for its own arithmetic and never `_FIELDS`, so a `size` of `370.0`
    # passed every one of them — `370 != 370.0` is False — while
    # `_check_manifest` raises on it at the top of the *next* capture. A clean
    # proof and a dead session, with no command that says which.
    #
    # Appended rather than returned, and last, so the specific checks above get
    # to say the more useful thing first. [E4, review: store 4]
    try:
        _check_manifest(man)
    except ValueError as exc:
        out.append(f"{rel}: {exc}")

    out += _verify_chain(path, rel, man, gen)
    return out, True


def _verify_chain(path: str, rel: str, man: dict, gen: object) -> list[str]:
    if not isinstance(gen, int) or gen == 0:
        if man.get("diverged_from") or man.get("prev_manifest_sha256"):
            return [f"{rel}: generation 0 claims to descend from something"]
        return []
    prev_path = os.path.join(os.path.dirname(path), _gen_file(gen - 1))
    try:
        with open(prev_path, "rb") as fh:
            prev_bytes = fh.read()
        prev = json.loads(prev_bytes)
    except (OSError, ValueError) as exc:
        return [f"{rel}: generation {gen} has no readable predecessor ({exc})"]
    out = []
    origin = man.get("diverged_from") or {}
    if origin.get("prev_file_sha256") != prev["file_sha256"]:
        out.append(f"{rel}: diverged_from does not match the sealed hash of generation {gen - 1}")
    if man.get("prev_manifest_sha256") != hashlib.sha256(prev_bytes).hexdigest():
        out.append(f"{rel}: prev_manifest_sha256 does not match generation {gen - 1}'s manifest")
    return out


def _regular(path: str) -> bool:
    """True if `path` is a regular file. [E7]

    `open` on a FIFO blocks until a writer arrives, and every segment read in
    this module happens under the session's exclusive lock — so one `mkfifo`
    inside a generation directory stops `verify` forever, and every `capture`
    for that session with it, from a process that looks idle. A store is a
    directory its owner can write to, a directory any process running as them
    can write to, and — once this is a two-way sync — a directory a stranger's
    checkout produced.

    `_inside` has already resolved symlinks and confirmed containment by the
    time this is asked, so this is the file type question and only that.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _inside(home: str, rel_path: str) -> str | None:
    """Realpath of `rel_path` under `home`, or None if it points outside.

    A manifest is data, and in a pushed repository it is data from elsewhere.
    `../../../etc/passwd` as a segment path must not be read, let alone hashed
    into a proof that then passes.
    """
    if not isinstance(rel_path, str) or os.path.isabs(rel_path):
        return None
    root = os.path.realpath(home)
    full = os.path.realpath(os.path.join(root, rel_path))
    return full if full == root or full.startswith(root + os.sep) else None
