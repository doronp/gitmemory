# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""Which project a directory is in, for `recall`'s default scope.

A project is a repository: the nearest `.git` at or above the directory, read
from the live filesystem and never by running git, so recall executes nothing
a repository configures. A linked worktree joins its repository; a submodule or
a clone vendored inside one is a repository of its own. `$HOME` and `/` are
looked at like any level and are the last: a dotfiles repository in `~` is a
project, and nothing above `~` gathers it with every other. Outside any
repository a directory is only itself, matched by equality.

Identity is the inode of the `.git` (or of the directory), not its spelling:
macOS's `/tmp` is `/private/tmp`, and its default volume reads `/W/A` as `/w/a`.
A deleted directory has no inode, so it is its nearest existing ancestor's
inode plus the missing tail, folded on a case-insensitive volume. The stored
cwd is resolved here, where recall runs, never at index time, so the index's
digest does not depend on the machine that built it.

A `.git` file is trusted local configuration: one that names another
repository's gitdir joins that repository, as git itself would.
"""

from __future__ import annotations

import os
import stat
import sys

__all__ = ["admitted", "project_id"]

# Git writes one short line into each of these; anything longer is not one.
_READ = 4096


def _ident(path: str):
    """The file `path` names, however it is spelled; the path when unreadable."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return path
    return (st.st_dev, st.st_ino)


def _first_line(path: str) -> str | None:
    """The first line of a small regular file, or None. Never opens a FIFO or a
    device: stored cwds come from transcripts, and one would block recall."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as fh:
            line = fh.read(_READ).decode("utf-8").splitlines()
    except (OSError, ValueError):
        return None
    if not line or "\0" in line[0]:
        return None
    return line[0]


def _gitdir(d: str) -> str:
    """The repository a `.git` file points at: its gitdir's `commondir` when it
    has one (a linked worktree's), else `<common>` for a gitdir under
    `<common>/worktrees/`, else the gitdir. A file that does not point at a
    directory holding `HEAD` is a repository of its own."""
    dotgit = os.path.join(d, ".git")
    line = _first_line(dotgit)
    if line is None or not line.startswith("gitdir: "):
        return dotgit
    g = os.path.realpath(os.path.join(d, line[len("gitdir: ") :]))
    if not os.path.isfile(os.path.join(g, "HEAD")):
        return dotgit
    common = _first_line(os.path.join(g, "commondir"))
    if common:
        return os.path.realpath(os.path.join(g, common))
    if os.path.basename(os.path.dirname(g)) == "worktrees":
        return os.path.dirname(os.path.dirname(g))
    return g


def _dotgit(d: str):
    """The project `d` roots, if it holds a `.git`."""
    dotgit = os.path.join(d, ".git")
    if os.path.isdir(dotgit):
        return ("git", _ident(dotgit))
    if os.path.lexists(dotgit):
        return ("git", _ident(_gitdir(d)))
    return None


def _case_blind(p: str) -> bool:
    """Whether the volume holding the existing directory `p` ignores case."""
    other = p.swapcase()
    if other == p:
        return sys.platform == "darwin"
    try:
        return os.path.samefile(p, other)
    except (OSError, ValueError):
        return False


def project_id(path: str):
    """A hashable identity for the project holding `path`. Equal ids are one
    project; nothing else about the value means anything."""
    orig = os.path.normpath(os.path.realpath(os.path.expanduser(path)))
    # A deleted worktree is still in its repository: look from where it was.
    p = orig
    while not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    home = _ident(os.path.realpath(os.path.expanduser("~")))
    d = p
    while True:
        found = _dotgit(d)
        if found is not None:
            return found
        if d == os.path.dirname(d) or _ident(d) == home:
            break
        d = os.path.dirname(d)
    if p == orig:
        return ("dir", _ident(orig))
    # The ancestor was only where to look; the directory is the one that was.
    tail = os.path.relpath(orig, p)
    return ("dir", (_ident(p), tail.casefold() if _case_blind(p) else tail))


def admitted(cwds, project) -> list[str]:
    """The recorded directories that are in `project`. Each distinct one is
    resolved once, never per row."""
    return [c for c in cwds if project_id(c) == project]
