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
import time
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
# The store's own half-written files. `capture` publishes by rename, so both of
# these only exist between a write and its `os.replace` — or for ever, if the
# process was killed in between. `git add --all` stages dotfiles, so a commit
# racing a live capture was committing a *partial* segment or a *partial*
# manifest: unattested bytes for the first, and for the second a manifest that
# `verify` reads as truncated JSON. Both halves are swept by the next capture —
# the segment in `_adopt_orphans`, the manifest in `_sweep_temps`, which was
# added because this comment used to say the manifest half never was.
# [E4, review: store 8]
#
# Anchored to the exact depth each one lives at, because unanchored they match a
# *directory component* — and one of those components is the user's filename.
# A transcript called `session.tmp.42.jsonl` becomes the session key
# `session.tmp.42-<tag>`, and a bare `*.tmp.*` then excluded the whole session,
# raw and manifest both, from every commit. Capture succeeded, `verify` read the
# bytes off disk and said clean, and the session was simply not in history. The
# anchored forms cannot do that: the user controls one component and these pin
# all of them, and neither a segment (`<start>-<end>.jsonl`) nor a manifest
# (`g<NN>.json`) can spell the partial's name. [E4, review: gitrepo 2]
raw/*/*/*/.incoming.*
sessions/*/*/*.tmp.*
"""

# Repository-local. The reading half of that is enforced in `_env`, not here.
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
    # And the other half of the same hole, which the scrub above does not
    # touch: git reads `~/.gitconfig` and `/etc/gitconfig` without any
    # environment variable's help. The comment over `_CONFIG` said "nothing here
    # reads or writes the user's global config" and only the writing half was
    # true.
    #
    # Found by instrumenting a git call and seeing `fsmonitor--daemon` inside a
    # store's `.git`, which is this machine's global `core.fsmonitor = true`
    # arriving uninvited. A background daemon per store is the harmless end of
    # it. The other end, reproduced: a global `core.excludesFile` naming a file
    # that matches `*.jsonl` — and a global ignore list is a normal thing to
    # have — makes `git add --all` skip every segment while still staging the
    # manifest that references it. `commit` returns a sha, the working tree is
    # untouched, so `verify` stays clean, and the history quietly stops
    # containing the bytes it is the record of. `core.autocrlf` and a global
    # `core.attributesFile` carrying a filter are the same shape against the
    # bytes themselves.
    #
    # Naming those four keys in `_CONFIG` would fix the four we happened to
    # think of, which is the thing this file already refused to do once. Point
    # both config files at `/dev/null` instead: gitmemory's settings live in the
    # repository's own config, and it has never had a use for an inherited one.
    # git >= 2.32 for these two names; released 2021. [E4]
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    # Which was not enough, on two counts, and neither was visible until `init`
    # started asking git what it had actually loaded.
    #
    # First: `GIT_CONFIG_SYSTEM` does not reach every system config. Measured on
    # git 2.50.1 (Apple Git-155), `config --list --show-origin` with that name
    # set to `/dev/null` still returns `credential.helper` and
    # `init.defaultBranch` from
    # `/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig`; with
    # `GIT_CONFIG_NOSYSTEM=1` it returns neither. Four-way check, and only the
    # variable that predates the other two by a decade closes it. So the fix
    # written one commit ago was still leaking on the machine it was written on.
    #
    # Second, both 2.32 names are ignored in silence on an older git, and 2.32
    # is recent enough that Debian bullseye (2.30) is a live machine. A `HOME`
    # that cannot be a directory is how the global file is refused where no
    # variable for it exists: `/dev/null/.gitconfig` is ENOTDIR, which git reads
    # as absent. Nothing gitmemory runs needs a home directory — `init`, `add`
    # and `commit` were all measured working under it. `XDG_CONFIG_HOME` goes
    # too, being the third spelling of the global file and the only one that is
    # not a `GIT_*` name.
    #
    # None of this is trusted. `init` proves the isolation rather than assuming
    # it; see `_assert_no_foreign_config`. [E4, review: Gemini r3 §3]
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["HOME"] = os.devnull
    env.pop("XDG_CONFIG_HOME", None)
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


def _assert_no_foreign_config(home: str) -> None:
    """Prove the isolation in `_env` instead of trusting the mechanism.

    Every setting git will apply to this repository, with the file it came
    from. The only origin allowed is the repository's own config, so this
    catches a git too old for `GIT_CONFIG_GLOBAL`, an `XDG_CONFIG_HOME` spelling
    we did not think of, an `include.path` reaching out of the local file, and
    whatever config source a future git adds — none of which a list of variable
    names would have caught. A denylist grows one entry per incident; this
    asks the question the incidents are all instances of. [E4]

    Refusing beats warning. The failure it exists for is a global
    `core.excludesFile` that drops every segment out of `git add --all` while
    the commit still succeeds: nothing downstream looks wrong, `verify` reports
    clean, and the history stops holding the bytes it is the record of. A
    watcher that will not start is a problem you can see.
    """
    listing = _git(home, "config", "--list", "--show-origin").stdout
    foreign = []
    for line in listing.splitlines():
        origin = line.split("\t", 1)[0]
        path = origin[5:] if origin.startswith("file:") else None
        # Not a file at all — `command line:`, `blob:`, `standard input:` — is
        # foreign too, and reported as it was printed.
        if path is None or os.path.realpath(os.path.join(home, path)) != os.path.realpath(
            os.path.join(home, ".git", "config")
        ):
            foreign.append(origin)
    if foreign:
        names = ", ".join(sorted(set(foreign)))
        raise GitError(
            f"git config for {home} is not isolated: settings arriving from {names}. "
            "A global or system setting can drop bytes out of a commit that still "
            "succeeds, so the store refuses to run under one. git >= 2.32 isolates "
            "itself; on an older git, move the file aside."
        )


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
    _assert_no_foreign_config(home)
    # Rewritten rather than merged: it is ours, it is three entries, and a merge
    # that half-applies is worse than a file that is simply current.
    path = os.path.join(home, ".gitignore")
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except (OSError, ValueError):
        # `ValueError` for the `UnicodeDecodeError` a non-UTF-8 byte raises.
        # This read is only ever asking "is the file already what we write?", so
        # a file we cannot decode answers no — and the branch below rewrites it,
        # which is what the comment above already says we do with this file.
        # Unguarded, the exception escaped `init`, escaped `run`'s
        # `(GitError, OSError, SubprocessError)`, and exited 2 on every restart,
        # because the byte is still in the file. [E4, review: daemon 2]
        current = None
    if current != GITIGNORE:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(GITIGNORE)
    return home


STALE_LOCK = TIMEOUT  # an `index.lock` older than the longest call we allow is a corpse


def _clear_stale_index_lock(home: str) -> bool:
    """Remove `.git/index.lock` if it is too old to belong to a live git.

    git takes this lock for the duration of an `add` or a `commit` and removes
    it on the way out. A SIGKILL, an OOM kill, or power loss in between leaves
    it behind, and git then refuses every subsequent write to the index — for
    ever, because nothing in git reclaims it either. Measured: the watcher went
    on capturing correctly and committed nothing across three passes and 640 MB,
    reporting the same error each time, until a human ran `rm`. The bytes were
    safe the whole while; the versioning layer, which is the product, was dead.
    [E4, review: concurrency 3]

    Age-gated, and that is the whole safety argument. `TIMEOUT` already bounds
    the longest git call this module will wait for, so a lock older than that
    cannot belong to a git that `_git` is still waiting on. Unlinking a *live*
    lock would be much worse than the problem — two writers in one index — so
    the rule is deliberately conservative: when in doubt, leave it and report.
    """
    lock = os.path.join(home, ".git", "index.lock")
    try:
        if time.time() - os.path.getmtime(lock) <= STALE_LOCK:
            return False
        os.unlink(lock)
    except OSError:
        return False
    return True


def commit(home: str, message: str) -> Commit | None:
    """Stage everything and commit. `None` when there was nothing to commit.

    Nothing-to-commit is the common case — the watcher wakes, finds no growth,
    and has nothing to say — so it is a return value, not an exception.
    """
    try:
        _git(home, "add", "--all")
    except GitError as exc:
        # One retry, and only against a lock old enough to be a corpse. Any
        # other `add` failure — full disk, unreadable file — is raised as
        # before, because retrying it would just fail again more slowly.
        if "index.lock" not in str(exc) or not _clear_stale_index_lock(home):
            raise
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
