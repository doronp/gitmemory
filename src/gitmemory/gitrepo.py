"""The store's own git repository. Every call is `git -C <home>`, never cwd.

This module is the only place in gitmemory that runs git. It is small on
purpose: the versioned source is the product, so the thing that does the
versioning should be readable in one sitting.

Three properties it has to hold, none of which git gives you by default:

- **It commits the store and nothing else.** `git -C` sets the *directory*, but
  `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE` in the environment override it
  outright. A daemon started from inside another repository's hook inherits all
  three, and `git -C $GITMEMORY_HOME add -A` then stages the store's files into
  *that* repository. `_env` scrubs them.
- **It runs no code the store did not put there.** `init.templateDir` in the
  user's global config installs hooks into every new repository, including this
  one, and this repository's working tree is full of bytes a model wrote.
  `git init --template=` declines the template and `core.hooksPath` is pointed
  at a directory that does not exist.
- **It never blocks.** `GIT_TERMINAL_PROMPT=0` plus a timeout on every call: a
  credential prompt on a push would otherwise wedge the watcher forever, and
  the watcher is the capture guarantee (DESIGN.md §2.3).

[E4]
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .store import resolve_home

TIMEOUT = 120  # seconds; a `gc` on a large store is the slow case, not a hang

# Committed at init, so `index/` and `spool/` are ignored by the repository
# rather than by everyone remembering to. E3's `index.py` carried a comment
# claiming `index/` "is gitignored" while nothing anywhere ignored it. [E4]
GITIGNORE = """\
# Derived, rebuildable, and not diffable. DESIGN.md §2.4.
/index/
# Hook drop-box. A doorbell, not data.
/spool/
# Capture locks, one per session.
/.locks/
"""

# Repository-local, so nothing here reads or writes the user's global config.
_CONFIG = {
    "user.name": "gitmemory",
    "user.email": "gitmemory@localhost",
    # `derived/` is rewritten on every rebuild, which orphans blobs. [R2]
    "gc.auto": "256",
    # A global `commit.gpgSign = true` turns every capture into a passphrase
    # prompt against a daemon with no terminal.
    "commit.gpgSign": "false",
    # Belt to `--template=`'s braces. `/dev/null` rather than the
    # `.git/hooks-disabled` this used to name: a directory that does not exist
    # is a directory that can be created, and review showed why that matters —
    # `--no-verify` is not the second lock the comment here used to claim. It
    # suppresses the *verification* hooks (`pre-commit`, `commit-msg`) and
    # nothing else, so a `post-commit` dropped into the hooks path still runs on
    # every capture. Verified by running it. `/dev/null` is a file, so git
    # resolving `<hooksPath>/post-commit` gets ENOTDIR and there is no filename
    # anyone can create that changes that. [E4]
    #
    # ponytail: POSIX-only, which this project already is — the hook shim is
    # `/bin/sh`. A Windows port needs a different unusable path here.
    "core.hooksPath": "/dev/null",
}


class GitError(RuntimeError):
    """A git invocation that failed in a way the caller has to know about."""


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str


def _env() -> dict[str, str]:
    """The caller's environment with **every** `GIT_*` variable removed.

    This was a denylist of the four variables that move the repository, the work
    tree, the index, and the object store, plus anything starting `GIT_CONFIG`.
    A denylist was the wrong shape. Review found `GIT_AUTHOR_NAME` and
    `GIT_AUTHOR_EMAIL` still getting through, and they outrank the repository's
    own `user.name`/`user.email` — so anything that could set an environment
    variable could sign gitmemory's commits as somebody else. For a store whose
    entire pitch is "the history is the evidence", an attacker-chosen author on
    a real commit is worse than most things that could go wrong here.

    Chasing that with four more names invites the next gap. gitmemory runs git
    one way, against one repository, with every setting it cares about written
    into that repository's own config — it has never had a use for an inherited
    `GIT_*` variable. So: drop all of them, and set back the one we want.

    That also closes, without needing to rule on them individually,
    `GIT_EXTERNAL_DIFF`, `GIT_EXEC_PATH`, `GIT_COMMON_DIR`,
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_GRAFT_FILE`, and the rest of a list
    that is longer than it looks and grows with each git release. [E4]
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(home: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, scrubbed env
        ["git", "-C", home, *args],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=TIMEOUT,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def is_repo(home: str) -> bool:
    return os.path.isdir(os.path.join(home, ".git"))


def init(home: str | None = None) -> str:
    """Make `home` a git repository, or bring an existing one up to spec.

    Idempotent, and deliberately so: the watcher calls it on every start rather
    than remembering whether it has run. Re-applying the config is how a store
    created by an older version picks up a setting added by a newer one.
    """
    home = resolve_home(home)
    os.makedirs(home, mode=0o700, exist_ok=True)
    if not is_repo(home):
        # `--template=` empty, so a global `init.templateDir` cannot install
        # hooks into a repository whose work tree holds model-written bytes.
        _git(home, "init", "--quiet", "--template=", "--initial-branch=main")
    for key, value in _CONFIG.items():
        _git(home, "config", key, value)
    # Rewritten rather than merged: it is ours, it is three entries, and a merge
    # that half-applies is worse than a file that is simply current.
    path = os.path.join(home, ".gitignore")
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current != GITIGNORE:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(GITIGNORE)
    return home


def commit(home: str, message: str) -> Commit | None:
    """Stage everything and commit. `None` when there was nothing to commit.

    Nothing-to-commit is the common case — the watcher wakes, finds no growth,
    and has nothing to say — so it is a return value, not an exception.
    """
    _git(home, "add", "--all")
    if not _git(home, "diff", "--cached", "--quiet", check=False).returncode:
        return None
    # `--no-verify` as well as `core.hooksPath`: the config is ours to set and
    # someone else's to unset, and this flag costs nothing. It is *not* a second
    # lock of equal strength — it stops `pre-commit` and `commit-msg`, not
    # `post-commit` — so the unusable `core.hooksPath` above is the one actually
    # holding the door. This is the cheap redundancy, not the guarantee. [E4]
    _git(home, "commit", "--quiet", "--no-verify", "--message", message)
    sha = _git(home, "rev-parse", "HEAD").stdout.strip()
    return Commit(sha=sha, subject=message.splitlines()[0])


def gc(home: str) -> None:
    """`git gc --auto`. Honours `gc.auto`, so most calls do nothing."""
    _git(home, "gc", "--auto", "--quiet")
