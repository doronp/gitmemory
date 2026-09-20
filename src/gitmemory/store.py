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
import hashlib
import json
import os
import re
from dataclasses import dataclass
from glob import glob

from .records import canonical_json

__all__ = ["Capture", "capture", "resolve_home", "verify"]

SCHEMA = 1
CHUNK = 1 << 20
EMPTY_SHA256 = hashlib.sha256().hexdigest()

_GEN_RE = re.compile(r"^g(\d+)\.json$")
# Leading alnum bans `..`, `-rf`, and dotfiles in one rule. `agent` and
# `session_id` become path components, so they are a trust boundary even when
# today's only caller is our own adapter.
_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe(name: str, what: str) -> str:
    if not isinstance(name, str) or not _SAFE_RE.match(name):
        raise ValueError(f"unsafe {what} for a path component: {name!r}")
    return name


def _gen_file(n: int) -> str:
    return f"g{n:02d}.json"


def _gen_dir(n: int) -> str:
    return f"g{n:02d}"


def resolve_home(home: str | None = None) -> str:
    """Absolute `$GITMEMORY_HOME`, refusing to sit inside another work tree.

    A store nested in a checkout gets its history rewritten by whoever owns the
    outer repo — a `git clean`, a branch switch, a rebase. Refusing is cheaper
    than explaining the loss afterwards (DESIGN.md §2.4 [R1]).
    """
    path = os.path.abspath(
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


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _fsync_dir(path: str) -> None:
    """Durability of the rename itself, not just the bytes.

    Without this a crash can leave the segment on disk and the manifest naming
    it absent, or the reverse. Cheap here; the alternative is a store that only
    looks append-only.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # some filesystems refuse fsync on a directory
        pass
    finally:
        os.close(fd)


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

    session_dir = os.path.join(home, "sessions", agent, session_id)
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
        os.makedirs(seg_dir, exist_ok=True)
        tmp = os.path.join(seg_dir, f".incoming.{os.getpid()}")
        seg_hash = hashlib.sha256()
        appended = 0
        try:
            with open(tmp, "wb") as out:
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
        os.replace(tmp, os.path.join(seg_dir, name))
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
        "source_path": os.path.abspath(source_path),
        "generation": gen,
        "diverged_from": diverged_from,
        "size": end,
        "file_sha256": whole.hexdigest(),
        "prev_manifest_sha256": prev_manifest_sha,
        "segments": segments,
        "compact_boundaries": sorted({*carried, *(boundaries or [])}),
    }
    manifest_path = os.path.join(session_dir, _gen_file(gen))
    _write_atomic(manifest_path, canonical_json(manifest))
    return Capture(manifest_path, gen, seg_rel, appended, end, diverged)


def _unlink(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def verify(home: str | None = None) -> list[str]:
    """Check every manifest in the store. Empty list means the proof holds.

    Same arithmetic as the documented one-liner in DESIGN.md §2.5, plus the
    checks a shell one-liner cannot express: that no unrecorded file is sitting
    in a generation directory, and that the generation chain links up.
    """
    home = resolve_home(home)
    problems: list[str] = []
    for path in sorted(glob(os.path.join(home, "sessions", "*", "*", "g*.json"))):
        if _GEN_RE.match(os.path.basename(path)):
            problems += _verify_manifest(home, path)
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

    gen = man.get("generation")
    filed_as = os.path.basename(path)
    if not isinstance(gen, int) or _gen_file(gen) != filed_as:
        out.append(f"{rel}: declares generation {gen!r} but is filed as {filed_as}")

    segments = sorted(man.get("segments", []), key=lambda s: s["start"])
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
    for stray in sorted(glob(os.path.join(seg_dir, "*"))):
        if os.path.realpath(stray) not in listed:
            name = os.path.basename(stray)
            out.append(f"{rel}: unrecorded file in the generation directory: {name}")

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
