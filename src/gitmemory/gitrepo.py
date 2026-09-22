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
import stat
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .store import _mkdir, resolve_home

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
# The third one: `derive._write` publishes by rename too, and its temp is a
# dotfile in the artifact directory. Swept by `derive._sweep_temps`; anchored
# here for the same reason as the two above. [E5:8]
derived/*/*/*/.deriving-*
# The push credential's only home. `redact.safe_url` goes to trouble never to
# *print* the token in `[remote.X] url`, and `git add --all` was committing the
# file it lives in — so the store authenticated to a remote with a secret it was
# about to push to that same remote. `push_allowed` now refuses an inline
# credential outright; this line is the belt to that brace, and it also keeps a
# store's remote list out of a repository other people can read. [E7]
/config.toml
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
    #
    # What is **not** scrubbed, said here because the argument above reads as
    # though the subprocess were sealed: `PATH`. `subprocess.run(["git", …])`
    # resolves `argv[0]` through it, so a `git` planted earlier in `PATH` runs
    # — measured, and left as it is. Anyone who can do that owns every tool the
    # user runs, and resolving to an absolute path at import would only move
    # the same lookup earlier in the same process. [E7 fs-F8]
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


def _assert_own_git_dir(home: str) -> None:
    """Refuse a `.git` that is not a real directory, before anything is written.

    `_assert_no_foreign_config` proves that no *setting* reaches this repository
    from outside. It cannot prove that the repository is this store's, and two
    shapes of `.git` make it somebody else's:

    - **A gitfile.** `.git` as a regular file holding `gitdir: /elsewhere/.git`
      is git's own worktree mechanism. `is_repo` says no, `git init` is run, git
      sees the pointer and adopts the foreign repository — and the four config
      settings are written into it *before* the assertion below gets a chance to
      refuse. The refusal was real but it came second.
    - **A symlink.** This one never refused at all. `os.path.isdir` follows the
      link, so `is_repo` says yes and `git init` is skipped entirely; the config
      writes land in the foreign repository; and `_assert_no_foreign_config`
      **passes**, because it compares `realpath(home/.git/config)` against
      itself and a symlinked `.git` resolves to the foreign config on both
      sides. `init` succeeds, `.gitignore` is written, `commit` succeeds, and
      the whole transcript store lands in a repository gitmemory does not own,
      with no error and no warning anywhere.

    One `lstat` covers both, and it runs before the first `git` call rather than
    after it. `lexists` so a dangling symlink is caught too — the foreign repo it
    points at may not exist yet, and creating it later is the attacker's half of
    the job. A store's `.git` is a directory; if it is anything else, gitmemory
    did not make it. [E7]
    """
    dot = os.path.join(home, ".git")
    if not os.path.lexists(dot):
        return
    mode = os.lstat(dot).st_mode
    if stat.S_ISDIR(mode):
        return
    kind = "a symbolic link" if stat.S_ISLNK(mode) else "not a directory"
    raise GitError(
        f"{home}/.git is {kind}, so it is not a repository this store made. "
        "gitmemory will not write a transcript store into a repository it does "
        "not own. Move it aside, or point GITMEMORY_HOME somewhere else."
    )


def tracked(home: str) -> list[str]:
    """Paths git would ship from the working tree: tracked, plus unignored new.

    The gate used to build this list with `os.walk` and a two-name skip set,
    which scanned `spool/` and `index/` — both gitignored, both full of exactly
    the bytes a detector fires on. A push was refused over a file `git push`
    would never have sent, with no override flag anywhere in the CLI. Asking git
    which files are its own is shorter than reproducing `.gitignore`, and it is
    right by construction. [E7]
    """
    out = _git(home, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    return [os.path.join(home, p) for p in out.split("\0") if p]


def pushable_objects(home: str) -> Iterator[tuple[str, bytes]]:
    """Every object reachable from a ref, as `(label, bytes)`.

    `git push` does not send the working tree. It sends the object graph
    reachable from the refs, so a credential that was committed and later
    deleted is absent from any walk of the checkout and present in the push —
    and deleting the file is the first thing a user does on noticing a leak.
    A gate that answers "does the current checkout contain a known shape" is
    answering a different question from the one it is asked. [E7]

    Trees and commits come through too, not just blobs: a session id that is
    itself a credential is a *name* inside a tree object, and a commit message
    carries session keys derived from the transcript filename.

    Unreachable objects are deliberately excluded. `derived/` is rewritten on
    every rebuild and git prunes loose objects on a two-week delay, so a store
    carries garbage that will never be pushed; blocking egress on it is the
    over-refusal this function exists to stop doing.
    """
    listing = _git(home, "rev-list", "--objects", "--all").stdout
    names: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if sha and all(c in "0123456789abcdef" for c in sha):
            names.setdefault(sha, path)
    if not names:
        return
    # `--batch` over one pipe rather than one `cat-file` per object: a store with
    # a few thousand objects is a few thousand forks otherwise, and the gate runs
    # on a path a human is waiting on.
    with subprocess.Popen(  # noqa: S603 - fixed argv, no shell, scrubbed env
        ["git", "-C", home, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=_env(),
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None  # noqa: S101 - pipes above
        try:
            for sha, path in names.items():
                proc.stdin.write(f"{sha}\n".encode("ascii"))
                proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) != 3:  # `<sha> missing` — a ref raced a prune
                    continue
                data = proc.stdout.read(int(header[2]))
                proc.stdout.read(1)  # the trailing newline the protocol adds
                yield path or f"<{header[1].decode()} {sha[:12]}>", data
        finally:
            proc.stdin.close()


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
    # `store._mkdir`, not `os.makedirs`: the mode argument applies to the leaf
    # only, so every directory `makedirs` had to create on the way came out at
    # `0777 & ~umask`. The reviewer measured that at umask 022 and called it
    # harmless, which it is — the leaf is the barrier and those directories
    # hold nothing but the path to it. At umask 000 it is not: measured,
    # `GITMEMORY_HOME=<base>/a/b/store` left `<base>/a` and `<base>/a/b` at
    # 0777, and a world-writable parent of the store is a rename away from
    # standing the store up somewhere the attacker chose. `_mkdir` passes the
    # mode per level, where umask can only take bits off 0700 and never add
    # any. Its symlink refusal cannot fire on a legitimate layout: `home` came
    # back from `resolve_home`, so every component that exists is already the
    # real one, and a link at a component that does not exist yet is the race
    # `_mkdir` is there for. [E7 fs-F9]
    _mkdir(home)
    # `mode=` applies only when a directory is created. A home the
    # user made first — `mkdir ~/.gitmemory` under the default 022 umask —
    # stays 0755 for ever, as does one restored by a `tar` or an `rsync`
    # without `-p`. Measured: with home at 0755 the chain from `/` down to
    # `.git/objects/xx/…` is other-traversable at every level, and the blob
    # there decompresses to the transcript, credential and all. So the mode is
    # re-applied on every start rather than decided once, which is the same
    # reason this whole function is idempotent. [E7 fs-F2]
    os.chmod(home, 0o700)
    _assert_own_git_dir(home)
    if not is_repo(home):
        # `--template=` empty, so a global `init.templateDir` cannot install
        # hooks into a repository whose work tree holds model-written bytes.
        _git(home, "init", "--quiet", "--template=", "--initial-branch=main")
    # Every directory gitmemory makes is 0700 (`store._mkdir`). `.git` is the
    # one it does not make: git creates it at the ambient umask, and it holds a
    # second complete copy of every captured byte. Two mode slips to leak
    # rather than one. `core.sharedRepository` is not the answer — measured, it
    # takes 41 exposed entries to 11 and leaves this directory among them.
    # Neither chmod is suppressed: a store we cannot make private is one we
    # should refuse to write to. [E7 fs-F2]
    os.chmod(os.path.join(home, ".git"), 0o700)
    # The third directory under `home` that holds transcript bytes, and the only
    # one the hook shim creates. It creates it correctly — `umask 077`, so 0700
    # — but `mkdir -p` sets a mode only when it makes the directory, and the
    # shim's `[ -d ]` fast path means an existing spool is never looked at
    # again. Measured, a spool left at 0777 stayed 0777 across every fire.
    #
    # Not fixed in the shim: that branch runs on every fire but the first, and a
    # `chmod` there is a fork on the one path in this system a user waits for.
    # Here it is free and idempotent, which is the same argument `home` and
    # `.git` above are already settled by, and unsuppressed for the reason they
    # are: a directory the watcher will read records out of and cannot make
    # private is one it should refuse to run against. [E7 S9]
    spool = os.path.join(home, "spool")
    _mkdir(spool)
    os.chmod(spool, 0o700)
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
    # A `.gitignore` line does not untrack a file that is already tracked, and
    # `init` runs on every start precisely so an old store picks up a new rule.
    # Without this, every store created before `/config.toml` was ignored goes
    # on committing its own push credential and the fix reads as applied.
    # `--cached` leaves the file on disk; `--ignore-unmatch` makes it a no-op on
    # the normal path. The blob already in history stays there — that is F1, and
    # the answer to it is rotation, not this line. [E7]
    _git(home, "rm", "--cached", "--quiet", "--ignore-unmatch", "config.toml", check=False)
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

    The ownership check runs here and not only in `init`, because this is where
    the harm lands. `run` deliberately carries on after a failed `init` — a
    transient `.git/config` lock collision between two watchers must not cost a
    capture — and "carry on" past an *ownership* refusal means committing the
    transcript store into a repository gitmemory does not own. One `lstat` on the
    path about to be written is cheaper than threading a flag from `run` through
    `tick`, and it covers every caller rather than the one that was reported.
    [E7]
    """
    _assert_own_git_dir(home)
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
