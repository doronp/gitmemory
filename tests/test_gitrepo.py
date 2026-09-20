"""The git layer. [E4]

Every test here runs a real `git`, because every property this module claims is
a property of git's behaviour and not of our argv. A fake would pass while the
real thing staged the store into someone else's repository.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from gitmemory import gitrepo

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="git is not installed",
)


def _ask(home: str, *args: str) -> str:
    """Ask git a question about `home`, immune to whatever a test has planted.

    Deliberately not `gitrepo._env()`: a helper that scrubs the environment the
    same way the module does would agree with the module even when both are
    wrong. This one starts from nothing. Writing it the easy way is what made
    the first run of these tests report a failure the module did not have.
    """
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "GIT_CONFIG_GLOBAL": "/dev/null"}
    out = subprocess.run(
        ["git", "-C", home, *args], capture_output=True, text=True, check=True, env=env
    )
    return out.stdout


def _log(home: str) -> list[str]:
    return _ask(home, "log", "--format=%s").split("\n")[:-1]


def _tracked(home: str) -> set[str]:
    return set(_ask(home, "ls-files").split())


def test_init_makes_a_repository_and_is_idempotent(tmp_path):
    """The watcher calls `init` on every start rather than remembering it has."""
    home = str(tmp_path / "store")
    assert gitrepo.init(home) == home
    assert gitrepo.is_repo(home)
    gitrepo.commit(home, "first")
    gitrepo.init(home)  # must not raise, must not discard history
    assert _log(home) == ["first"]


def test_init_writes_the_gitignore_and_repairs_a_changed_one(tmp_path):
    home = gitrepo.init(str(tmp_path / "store"))
    path = os.path.join(home, ".gitignore")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == gitrepo.GITIGNORE
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# someone edited this\n")
    gitrepo.init(home)
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == gitrepo.GITIGNORE


def test_the_derived_index_and_the_spool_are_never_committed(tmp_path):
    """`index/` is rebuildable and `spool/` is a doorbell. Neither belongs in history."""
    home = gitrepo.init(str(tmp_path / "store"))
    for name in ("index", "spool", ".locks"):
        os.makedirs(os.path.join(home, name))
        with open(os.path.join(home, name, "x"), "w", encoding="utf-8") as fh:
            fh.write("derived")
    os.makedirs(os.path.join(home, "sessions"))
    with open(os.path.join(home, "sessions", "g00.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    gitrepo.commit(home, "first")
    assert _tracked(home) == {".gitignore", "sessions/g00.json"}


def test_commit_returns_none_when_there_is_nothing_to_commit(tmp_path):
    home = gitrepo.init(str(tmp_path / "store"))
    assert gitrepo.commit(home, "first") is not None  # the .gitignore itself
    assert gitrepo.commit(home, "second") is None
    assert _log(home) == ["first"]


def test_commit_reports_the_sha_it_wrote(tmp_path):
    home = gitrepo.init(str(tmp_path / "store"))
    got = gitrepo.commit(home, "subject\n\nbody")
    assert got is not None
    assert got.subject == "subject"
    head = subprocess.run(
        ["git", "-C", home, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    assert got.sha == head.stdout.strip()


def test_the_repository_is_created_on_main_whatever_the_user_configured(tmp_path):
    """A global `init.defaultBranch` must not decide what the store's branch is called."""
    home = gitrepo.init(str(tmp_path / "store"))
    gitrepo.commit(home, "first")
    out = subprocess.run(
        ["git", "-C", home, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "main"


def _global_config(tmp_path, monkeypatch, body: str) -> None:
    """Give git a global config the module cannot scrub away.

    `GIT_CONFIG_GLOBAL` is the obvious way and it is the wrong one here: `_env`
    strips every `GIT_CONFIG*` variable, so a test that plants the setting there
    is testing the scrub a second time and never reaches the thing it names.
    Both the template test and the gpgSign test below passed that way while the
    flag they were written for was absent. `$HOME/.gitconfig` survives.
    """
    fake = tmp_path / "fakehome"
    fake.mkdir(exist_ok=True)
    (fake / ".gitconfig").write_text(body)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_no_hook_from_a_global_template_is_installed(tmp_path, monkeypatch):
    """`init.templateDir` installs hooks into every new repo — including this one.

    The work tree here is full of bytes a model wrote. A template hook would run
    on the first capture of a session, at which point the store is executing
    code whose provenance it does not control.
    """
    template = tmp_path / "template" / "hooks"
    template.mkdir(parents=True)
    canary = tmp_path / "canary"
    hook = template / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\nexit 1\n")
    hook.chmod(0o755)
    _global_config(tmp_path, monkeypatch, f"[init]\n\ttemplateDir = {template.parent}\n")

    home = gitrepo.init(str(tmp_path / "store"))
    # The hook must not be on disk at all, not merely disabled: `core.hooksPath`
    # is a setting anyone can unset, and a hook that exists is one `git commit`
    # away from running. Declining the template is what keeps it off the disk.
    assert not os.path.exists(os.path.join(home, ".git", "hooks", "pre-commit"))
    assert gitrepo.commit(home, "first") is not None
    assert not canary.exists()


def test_a_hook_planted_after_init_still_does_not_run(tmp_path):
    """`core.hooksPath` plus `--no-verify`: two locks, because one can be unset."""
    home = gitrepo.init(str(tmp_path / "store"))
    hooks = os.path.join(home, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    canary = tmp_path / "canary"
    path = os.path.join(hooks, "pre-commit")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!/bin/sh\ntouch {canary}\nexit 1\n")
    os.chmod(path, 0o755)
    assert gitrepo.commit(home, "first") is not None
    assert not canary.exists()


def test_a_global_gpgsign_does_not_wedge_the_commit(tmp_path, monkeypatch):
    """A daemon has no terminal, so a passphrase prompt is a hang, not an error."""
    _global_config(
        tmp_path, monkeypatch, "[commit]\n\tgpgSign = true\n[user]\n\tsigningKey = DEADBEEF\n"
    )
    home = gitrepo.init(str(tmp_path / "store"))
    assert gitrepo.commit(home, "first") is not None


def test_an_inherited_git_dir_does_not_redirect_the_commit(tmp_path, monkeypatch):
    """The bug this guards: a watcher started from another repository's hook.

    `git -C <home>` sets the directory. It does not beat `GIT_DIR`, which is
    inherited by every process a hook spawns. Without the scrub, `add --all`
    stages the store's files into the *other* repository.
    """
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "--quiet"], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    home = gitrepo.init(str(tmp_path / "store"))
    with open(os.path.join(home, "payload"), "w", encoding="utf-8") as fh:
        fh.write("the store's bytes")
    gitrepo.commit(home, "first")

    assert "payload" in _tracked(home)
    assert _tracked(str(other)) == set()


def test_an_inherited_git_index_file_does_not_redirect_the_commit(tmp_path, monkeypatch):
    """`GIT_INDEX_FILE` survives `git -C` too, and a stale index stages the wrong tree."""
    stray = tmp_path / "stray-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(stray))
    home = gitrepo.init(str(tmp_path / "store"))
    with open(os.path.join(home, "payload"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert gitrepo.commit(home, "first") is not None
    assert "payload" in _tracked(home)
    assert not stray.exists()


def test_injected_config_cannot_outrank_the_repositorys_own(tmp_path, monkeypatch):
    """`GIT_CONFIG_COUNT` beats repository-local config, so it is scrubbed.

    Asserted as "git reports our value", not as "the hook did not fire":
    `--no-verify` would stop the hook either way, and a test that cannot tell
    the two locks apart passes with the scrub deleted.
    """
    home = gitrepo.init(str(tmp_path / "store"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "evil"))
    got = gitrepo._git(home, "config", "--get", "core.hooksPath").stdout.strip()
    assert got == gitrepo._CONFIG["core.hooksPath"]


def test_a_failing_git_raises_with_what_git_said(tmp_path):
    home = str(tmp_path / "not-a-repo")
    os.makedirs(home)
    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.commit(home, "first")
    assert "add --all" in str(exc.value)


def test_gc_is_safe_on_a_fresh_repository(tmp_path):
    home = gitrepo.init(str(tmp_path / "store"))
    gitrepo.gc(home)  # --auto on an empty repo does nothing, and must not raise


def test_every_call_is_bounded(tmp_path, monkeypatch):
    """A push that waits on credentials is a watcher that never ticks again."""
    seen = {}

    real = subprocess.run

    def spy(argv, **kw):
        seen.update(kw)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    gitrepo.init(str(tmp_path / "store"))
    assert seen["timeout"] == gitrepo.TIMEOUT
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


# --- findings from Gemini's E4 cross-review, each verified before being fixed ---


def test_the_environment_cannot_choose_who_signed_a_commit(tmp_path, monkeypatch):
    """`GIT_AUTHOR_NAME` outranks the repository's `user.name`. Verified, then fixed.

    The nastiest of the batch, because it produces a *valid* commit that says
    something false. gitmemory's whole claim is that the history is the
    evidence; an author line anything on the machine can set is not evidence.
    [E4, Gemini 04]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    for var, value in {
        "GIT_AUTHOR_NAME": "Somebody Else",
        "GIT_AUTHOR_EMAIL": "else@example.invalid",
        "GIT_COMMITTER_NAME": "Somebody Else",
        "GIT_COMMITTER_EMAIL": "else@example.invalid",
    }.items():
        monkeypatch.setenv(var, value)
    with open(os.path.join(home, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")

    gitrepo.commit(home, "one")

    who = _ask(home, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip()
    assert who == "gitmemory <gitmemory@localhost>|gitmemory <gitmemory@localhost>", who


def test_not_one_git_variable_survives_into_the_subprocess(tmp_path, monkeypatch):
    """The scrub is a denylist no longer. Asserted as "none", not as "not these four".

    A named-variable test is a test that passes until git adds a variable. This
    one fails the moment anything `GIT_*` reaches the child, which is the
    property the module actually promises. [E4, Gemini 01/02/03/14]
    """
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_EXTERNAL_DIFF",
        "GIT_EXEC_PATH",
        "GIT_GRAFT_FILE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.setenv(var, "/tmp/attacker")

    env = gitrepo._env()

    leaked = {k for k in env if k.startswith("GIT_")} - {"GIT_TERMINAL_PROMPT"}
    assert not leaked, f"reached the git subprocess: {sorted(leaked)}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PATH" in env, "the scrub took the whole environment with it"


def test_a_post_commit_hook_cannot_run(tmp_path, monkeypatch):
    """`--no-verify` does not stop `post-commit`. `core.hooksPath` has to. Verified.

    The module used to point `core.hooksPath` at `.git/hooks-disabled` and call
    the flag a second independent lock. It is not: running it showed a
    `post-commit` planted in that directory executing on every capture. The path
    is now `/dev/null`, which is a file, so there is no name anyone can create
    underneath it. [E4, Gemini 05]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    fired = tmp_path / "fired"
    for where in (".git/hooks", ".git/hooks-disabled"):
        hooks = os.path.join(home, where)
        os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "post-commit")
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\ntouch {fired}\n")
        os.chmod(hook, 0o755)

    with open(os.path.join(home, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert gitrepo.commit(home, "one") is not None

    assert not fired.exists(), "a hook ran inside the store repository"


def test_the_initial_branch_is_ours_and_not_the_users_default(tmp_path, monkeypatch):
    """Planted via `$HOME/.gitconfig`, because `_env` scrubs `GIT_CONFIG_GLOBAL`.

    The previous version of this test asserted `main` against a git that
    defaults to `main` anyway, so deleting `--initial-branch=main` left it
    green. [E4, Gemini 11]
    """
    fake = tmp_path / "fakehome"
    fake.mkdir()
    (fake / ".gitconfig").write_text("[init]\n\tdefaultBranch = theirs\n")
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    home = gitrepo.init(str(tmp_path / "store"))

    assert _ask(home, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_gc_actually_invokes_git(tmp_path, monkeypatch):
    """The old test called `gc()` and asserted it did not raise — so did `pass`.

    Not a bug, but a test that certified nothing. [E4, Gemini 12]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    calls = []
    real = subprocess.run

    def spy(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    gitrepo.gc(home)

    assert any(a[:2] == ["git", "-C"] and "gc" in a for a in calls), calls


# --- E4 review round ---------------------------------------------------------


def test_a_half_written_capture_is_not_committed(tmp_path):
    """`capture` publishes by rename, so a dotfile is a *partial* segment. [E4, review]

    `git add --all` stages dotfiles. A commit racing a live capture was therefore
    committing either a partial segment — unattested bytes — or a partial
    manifest, which `verify` reads as truncated JSON. The next capture sweeps
    both, which makes the working tree self-healing and the history not.
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    os.makedirs(os.path.join(home, "raw", "claude-code", "s", "g00"))
    for name in (".incoming.000000000000-000000000008.jsonl", "g00.json.tmp.4242"):
        with open(os.path.join(home, "raw", "claude-code", "s", "g00", name), "w") as fh:
            fh.write("half a file")
    with open(os.path.join(home, "raw", "claude-code", "s", "g00", "real.jsonl"), "w") as fh:
        fh.write("whole\n")

    gitrepo.commit(home, "capture: one")
    tracked = _ask(home, "ls-files").split()
    assert any(t.endswith("real.jsonl") for t in tracked), tracked
    assert not [t for t in tracked if ".incoming." in t or ".tmp." in t], tracked


def test_a_dead_index_lock_is_reclaimed_rather_than_wedging_the_store_for_ever(tmp_path):
    """A SIGKILL mid-`add` leaves `.git/index.lock` and nothing in git reclaims it.

    Measured before the fix: the watcher went on capturing correctly and
    committed nothing across three passes and 640 MB, reporting the same error
    each time, until a human ran `rm`. The bytes were safe throughout; the
    versioning layer, which is the product, was dead. [E4, review: concurrency 3]
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    with open(os.path.join(home, "a.txt"), "w") as fh:
        fh.write("x\n")
    lock = os.path.join(home, ".git", "index.lock")
    with open(lock, "w") as fh:
        fh.write("")
    old = time.time() - gitrepo.STALE_LOCK - 1
    os.utime(lock, (old, old))

    assert gitrepo.commit(home, "capture: one") is not None
    assert not os.path.exists(lock)
    assert _log(home) == ["capture: one"]


def test_a_live_index_lock_is_left_alone(tmp_path):
    """The other half of the rule, and the one that makes it safe to have.

    Two writers in one index is much worse than a wedged store, so a lock young
    enough to belong to a git that `_git` could still be waiting on is reported,
    never removed. `TIMEOUT` bounds the longest call this module will wait for,
    which is what makes "older than that" mean "nobody is holding it".
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    with open(os.path.join(home, "a.txt"), "w") as fh:
        fh.write("x\n")
    lock = os.path.join(home, ".git", "index.lock")
    with open(lock, "w") as fh:
        fh.write("")

    with pytest.raises(gitrepo.GitError, match="index.lock"):
        gitrepo.commit(home, "capture: one")
    assert os.path.exists(lock), "a lock that might be live must survive"


def test_an_add_that_fails_for_any_other_reason_is_not_retried(tmp_path):
    """Retrying a full disk just fails again, more slowly. One narrow retry only."""
    home = str(tmp_path / "store")
    gitrepo.init(home)
    calls = []
    real = gitrepo._git

    def counting(h, *args, **kw):
        calls.append(args[0])
        if args[0] == "add":
            raise gitrepo.GitError("git add --all failed (128): No space left on device")
        return real(h, *args, **kw)

    gitrepo._git = counting
    try:
        with pytest.raises(gitrepo.GitError, match="No space left"):
            gitrepo.commit(home, "capture: one")
    finally:
        gitrepo._git = real
    assert calls == ["add"], f"a non-lock failure must not be retried: {calls}"
