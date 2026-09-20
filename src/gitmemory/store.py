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
import fcntl
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from glob import glob

from .records import canonical_json

__all__ = [
    "Capture",
    "Stored",
    "capture",
    "resolve_home",
    "segment_groups",
    "sessions",
    "span",
    "verify",
]

SCHEMA = 1
CHUNK = 1 << 20
EMPTY_SHA256 = hashlib.sha256().hexdigest()

_GEN_RE = re.compile(r"^g(\d+)\.json$")
# Leading alnum bans `..`, `-rf`, and dotfiles in one rule. `agent` and
# `session_id` become path components, so they are a trust boundary even when
# today's only caller is our own adapter.
_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A segment file, and nothing else, in a generation directory.
_SEG_RE = re.compile(r"^(\d{12})-(\d{12})\.jsonl$")


def _safe(name: str, what: str) -> str:
    if not isinstance(name, str) or not _SAFE_RE.match(name):
        raise ValueError(f"unsafe {what} for a path component: {name!r}")
    # Case-fold. APFS and NTFS are case-insensitive but case-preserving, so
    # `CLAUDE-code` and `claude-code` are one directory on disk and two strings
    # in two manifests — and `verify` compares strings, so it would fail from
    # then on, permanently, for a difference the filesystem does not have. [E2]
    return name.lower()


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
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
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
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # some filesystems refuse fsync on a directory
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(home: str, agent: str, session_id: str):
    """One capture at a time per session. Other sessions still run in parallel.

    Without it, two captures that read the same prior manifest each write a
    segment and the loser's is left named by nothing: `verify` reports an
    unrecorded file from then on, and no later capture clears it. A hook fire
    racing a watcher is the ordinary case, not the exotic one. [E2]

    Locks live under `<home>/.locks/`, not beside the manifests — the sessions
    tree is what gets committed, and a lock file is not part of the proof.
    """
    path = os.path.join(home, ".locks", agent, f"{session_id}.lock")
    _mkdir(os.path.dirname(path))
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _adopt_orphans(home: str, agent: str, session_id: str, session_dir: str) -> None:
    """Finish a capture that was killed after its segment landed.

    Write ordering makes "segment on disk, manifest not written" the only crash
    state. Nothing used to reclaim it: the next capture resumes from the
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
    prior = _manifests(session_dir)
    gen = prior[-1][0] if prior else 0
    seg_dir = os.path.join(home, "raw", agent, session_id, _gen_dir(gen))
    if not os.path.isdir(seg_dir):
        return

    man: dict | None = None
    if prior:
        with open(prior[-1][1], "rb") as fh:
            man = json.loads(fh.read())
        # Before anything is rewritten, and here rather than only in `_capture`:
        # adoption is the *first* reader of the last manifest, and it feeds
        # `size` straight to `while size in pending`. [E3]
        _check_manifest(man)

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
    adopted = False
    while size in pending:
        end, name = pending.pop(size)
        full = os.path.join(seg_dir, name)
        if os.path.getsize(full) != end - size:
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
        size, adopted = end, True

    if not adopted:
        return

    whole = hashlib.sha256()
    for seg in segments:
        p = seg.get("path")
        full = _inside(home, p) if isinstance(p, str) else None
        if full is None:
            raise EscapingSegment(f"segment path escapes the store: {p!r}")
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
        "compact_boundaries": (man or {}).get("compact_boundaries", []),
    }
    _write_atomic(os.path.join(session_dir, _gen_file(gen)), canonical_json(manifest))


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
    with _locked(home, agent, session_id):
        return _capture(home, source_path, agent, session_id, boundaries)


def _capture(  # noqa: PLR0912, PLR0915 - one branch per failure mode; splitting hides the ordering
    home: str,
    source_path: str,
    agent: str,
    session_id: str,
    boundaries: list[int] | None,
) -> Capture:
    session_dir = os.path.join(home, "sessions", agent, session_id)
    _adopt_orphans(home, agent, session_id, session_dir)
    prior = _manifests(session_dir)

    gen, base, segments, diverged_from, prev_manifest_sha = 0, 0, [], None, None
    diverged: str | None = None
    carried: list[int] = []

    with open(source_path, "rb") as fh:
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
        if prior and not diverged:
            # Nothing new and nothing wrong: leave the manifest byte-identical
            # so a no-op capture produces no commit.
            return Capture(prior[-1][1], gen, None, 0, end, None)

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
        "compact_boundaries": sorted({*carried, *(boundaries or [])}),
    }
    manifest_path = os.path.join(session_dir, _gen_file(gen))
    # Invariant 2 (a sealed generation is immutable) was breakable by a slow
    # capture writing g00 after a neighbour had already forked g01. The session
    # lock is the fix; a redundant "refuse if g<n+1> exists" guard was written
    # here and then deleted, because `gen` always comes from the newest manifest
    # and no reachable state makes it fire. An untestable branch is not insurance. [E2]
    _write_atomic(manifest_path, canonical_json(manifest))
    return Capture(manifest_path, gen, seg_rel, appended, end, diverged)


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


def sessions(home: str) -> list[Stored]:
    """Every readable generation in the store, in a stable order.

    A generation, not a session, is the unit: a fork seals one byte history and
    starts another, and both are real. Callers that want only the live history
    take the highest `generation` per `(agent, session_id)`.

    Unreadable manifests are skipped — a reader that raised on them would make
    one bad manifest un-indexable for the whole store, which is the E2 sweep bug.
    An *escaping* segment path is the exception and fails the call: see
    `EscapingSegment`.
    """
    out = []
    for path in sorted(glob(os.path.join(home, "sessions", "*", "*", "g*.json"))):
        if not _GEN_RE.match(os.path.basename(path)):
            continue
        try:
            with open(path, "rb") as fh:
                man = json.loads(fh.read())
            # Ordered by start offset, which is the key `verify` already sorts
            # by. Reading `man["segments"]` verbatim while `verify` sorted was a
            # contradiction a reordered manifest passed straight through: the
            # proof reported clean and every offset addressed a concatenation
            # that was never the transcript. [E3]
            segs = sorted(_seq(man.get("segments")), key=_seg_start)
            run = [_segment_path(home, s) for s in segs]
        except EscapingSegment:
            raise
        except Exception:  # noqa: BLE001 - a manifest is untrusted data
            continue
        if not run or not all(run):
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
    """
    return [list(s.segments) for s in sessions(home)]


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
        try:
            problems += _verify_manifest(home, path)
        except Exception as exc:  # noqa: BLE001 - a manifest is untrusted data
            # One malformed manifest used to abort the sweep, so real tampering
            # in every later session went unreported and the exit code blamed
            # the crash instead. A bad manifest is one problem, not a stop. [E2]
            problems.append(f"{os.path.relpath(path, home)}: unverifiable manifest ({exc!r})")
    return problems


def _verify_manifest(home: str, path: str) -> list[str]:  # noqa: PLR0912 - one branch per failure mode
    rel = os.path.relpath(path, home)
    out: list[str] = []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        man = json.loads(raw)
    except (OSError, ValueError) as exc:
        return [f"{rel}: unreadable manifest ({exc})"]

    if not isinstance(man, dict):
        return [f"{rel}: manifest is {type(man).__name__}, not an object"]

    agent = man.get("agent")
    if not isinstance(agent, str) or not _SAFE_RE.match(agent):
        return [f"{rel}: declares unsafe agent name {agent!r}"]

    session_id = man.get("session_id")
    if not isinstance(session_id, str) or not _SAFE_RE.match(session_id):
        return [f"{rel}: declares unsafe session_id {session_id!r}"]

    gen = man.get("generation")
    filed_as = os.path.basename(path)
    if not isinstance(gen, int) or _gen_file(gen) != filed_as:
        # Stop here: every check below feeds `gen` to a path or a format spec.
        return [f"{rel}: declares generation {gen!r} but is filed as {filed_as}"]

    raw_segments = man.get("segments")
    if not isinstance(raw_segments, list):
        return [f"{rel}: segments is {type(raw_segments).__name__}, not a list"]
    for s in raw_segments:
        if not (
            isinstance(s, dict)
            and isinstance(s.get("start"), int)
            and isinstance(s.get("end"), int)
            and isinstance(s.get("path"), str)
            and isinstance(s.get("sha256"), str)
        ):
            return [f"{rel}: malformed segment entry {s!r}"]
    segments = sorted(raw_segments, key=lambda s: s["start"])
    whole = hashlib.sha256()
    cursor = 0
    listed: set[str] = set()
    for seg in segments:
        if seg["start"] != cursor:
            kind = "hole" if seg["start"] > cursor else "overlap"
            out.append(f"{rel}: {kind} at byte {cursor} (next segment starts at {seg['start']})")
        full = _inside(home, seg["path"])
        if full is None:
            out.append(f"{rel}: segment path escapes the store: {seg['path']!r}")
            return out
        listed.add(full)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            out.append(f"{rel}: segment {seg['path']} unreadable ({exc})")
            return out
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

    seg_dir = os.path.join(
        home, "raw", man.get("agent", ""), man.get("session_id", ""), _gen_dir(gen)
    )
    # `glob("*")` skips dotfiles, so the one artefact a killed capture leaves —
    # `.incoming.<pid>.<tid>`, real unattested transcript bytes — was the one
    # file this check could not see, and `.evil.jsonl` passed the proof. [E2]
    with contextlib.suppress(OSError), os.scandir(seg_dir) as it:
        for entry in sorted(it, key=lambda e: e.name):
            if os.path.realpath(entry.path) not in listed:
                out.append(f"{rel}: unrecorded file in the generation directory: {entry.name}")

    out += _verify_chain(path, rel, man, gen)
    return out


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
