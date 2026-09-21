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

from gitmemory import gitrepo, store

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


def _global_config(tmp_path, monkeypatch, body: str) -> str:
    """Plant a hostile `$HOME/.gitconfig`. It does **not** reach the child.

    This docstring used to end "`$HOME/.gitconfig` survives", and that was the
    second wrong answer to the same question. `GIT_CONFIG_GLOBAL` was the first:
    `_env` strips every `GIT_CONFIG*` name, so planting the setting there tests
    the scrub rather than the flag. Moving to `$HOME` did not fix it, because
    `_env` also sets `HOME` to `/dev/null` — three locks on the global file, and
    the tests below were bouncing off the outermost one every time.

    So a test that calls only this is a test of the isolation, which is a real
    property and is what these are now documented as proving. To reach the inner
    lock, pair it with `_without_the_global_isolation`.
    """
    fake = tmp_path / "fakehome"
    fake.mkdir(exist_ok=True)
    (fake / ".gitconfig").write_text(body)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return str(fake)


def _without_the_global_isolation(monkeypatch, fake_home: str) -> None:
    """Let the planted global config through, so the inner lock is on its own.

    Three settings in this module exist only for the case where `_env` fails:
    `--template=`, `commit.gpgSign = false`, and `--no-verify`. While `_env`
    holds, none of them can be observed — a vacuity audit deleted each in turn
    and the whole suite stayed green. Defence in depth that nothing distinguishes
    is defence in depth nobody will notice losing.

    Narrow on purpose: `HOME` is redirected and `GIT_CONFIG_GLOBAL` dropped, and
    that is all. `GIT_CONFIG_SYSTEM` and `GIT_CONFIG_NOSYSTEM` stay, so the
    machine's own `/etc/gitconfig` cannot make this pass or fail. The
    `init`-time self-check has to go too: it exists to refuse exactly the state
    being constructed here. [E4, review: vacuity audit]
    """
    real = gitrepo._env

    def permissive() -> dict[str, str]:
        env = real()
        env["HOME"] = fake_home
        env.pop("GIT_CONFIG_GLOBAL", None)
        return env

    monkeypatch.setattr(gitrepo, "_env", permissive)
    monkeypatch.setattr(gitrepo, "_assert_no_foreign_config", lambda home: None)


def _assert_the_global_was_read(home: str, fake_home: str) -> None:
    """The neutralisation's own check, without which these tests re-rot.

    Asked through the patched `_env`, because the question is what *git*
    loaded. If `_without_the_global_isolation` ever stops working — a rename in
    `gitrepo`, another lock added — the tests it serves would go back to
    passing for the wrong reason, silently, which is how they got here.
    """
    seen = subprocess.run(
        ["git", "-C", home, "config", "--list", "--show-origin"],
        env=gitrepo._env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert os.path.join(fake_home, ".gitconfig") in seen, f"global not read:\n{seen}"


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


def test_the_empty_template_declines_a_template_the_isolation_let_through(tmp_path, monkeypatch):
    """The same attack with `_env`'s global-config lock switched off.

    The test above cannot fail when `--template=` is deleted, because the
    planted `init.templateDir` never reaches git — `HOME` is `/dev/null` and
    `GIT_CONFIG_GLOBAL` is too. So it proves the isolation, and the flag it is
    named for was unpinned until this one existed. [E4, review: vacuity audit]
    """
    template = tmp_path / "template" / "hooks"
    template.mkdir(parents=True)
    canary = tmp_path / "canary"
    hook = template / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\nexit 1\n")
    hook.chmod(0o755)
    fake = _global_config(tmp_path, monkeypatch, f"[init]\n\ttemplateDir = {template.parent}\n")
    _without_the_global_isolation(monkeypatch, fake)

    home = gitrepo.init(str(tmp_path / "store"))

    _assert_the_global_was_read(home, fake)
    assert not os.path.exists(os.path.join(home, ".git", "hooks", "pre-commit"))


def _plant_pre_commit(tmp_path, home: str):
    hooks = os.path.join(home, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    canary = tmp_path / "canary"
    path = os.path.join(hooks, "pre-commit")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!/bin/sh\ntouch {canary}\nexit 1\n")
    os.chmod(path, 0o755)
    return canary


def test_a_hook_planted_after_init_still_does_not_run(tmp_path):
    """`core.hooksPath` and `--no-verify` together. Neither is pinned here.

    Deleting either one leaves this passing, because the other still holds — so
    what it proves is the pair. The one below removes `core.hooksPath` first, so
    `--no-verify` has to do the work alone. [E4, review: vacuity audit]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    canary = _plant_pre_commit(tmp_path, home)
    assert gitrepo.commit(home, "first") is not None
    assert not canary.exists()


def test_no_verify_alone_stops_a_pre_commit_hook(tmp_path):
    """`core.hooksPath` unset, which the comment on it says anyone can do.

    Only `pre-commit` and `commit-msg`, which is why `core.hooksPath` is the
    lock that matters and this one is the belt — but a belt nothing tests is a
    belt that gets removed in a refactor. [E4, review: vacuity audit]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    # `check=False`: exit 5 means the key was not there, which is the state this
    # test wants anyway. Raising on it would turn "the other lock was removed"
    # into a setup error instead of the pass it should be.
    subprocess.run(["git", "-C", home, "config", "--unset", "core.hooksPath"], check=False)
    canary = _plant_pre_commit(tmp_path, home)

    assert gitrepo.commit(home, "first") is not None
    assert not canary.exists()


_HOSTILE_SIGNING = "[commit]\n\tgpgSign = true\n[user]\n\tsigningKey = DEADBEEF\n"


def test_a_global_gpgsign_does_not_wedge_the_commit(tmp_path, monkeypatch):
    """A daemon has no terminal, so a passphrase prompt is a hang, not an error.

    Proves the isolation: the planted config never reaches git, so the
    `commit.gpgSign = false` pin is not what makes this pass. The one below
    pins that. [E4, review: vacuity audit]
    """
    _global_config(tmp_path, monkeypatch, _HOSTILE_SIGNING)
    home = gitrepo.init(str(tmp_path / "store"))
    assert gitrepo.commit(home, "first") is not None


def test_the_pinned_gpgsign_survives_a_global_the_isolation_let_through(tmp_path, monkeypatch):
    """`commit.gpgSign = false` on its own, against a global that says true.

    A repository-local key outranks a global one, so the pin is what decides
    this — and with the pin deleted the commit is handed to a signing key that
    does not exist, which is the wedge the pin is for. [E4, review: vacuity audit]
    """
    fake = _global_config(tmp_path, monkeypatch, _HOSTILE_SIGNING)
    _without_the_global_isolation(monkeypatch, fake)

    home = gitrepo.init(str(tmp_path / "store"))

    _assert_the_global_was_read(home, fake)
    assert gitrepo.commit(home, "first") is not None


def test_a_global_ignore_file_cannot_drop_bytes_out_of_a_commit(tmp_path, monkeypatch):
    """The reproduction, not the mechanism: a global ignore that matches a segment.

    `git add --all` honours `core.excludesFile`, and a global ignore list is a
    normal thing to have. With `*.jsonl` in one, the manifest was staged and the
    segment it references was not — and every signal said fine. `commit`
    returned a sha, the working tree was untouched, so `verify` was clean, and
    the only place the loss existed was the history, which is the one copy the
    product claims is the reliable one.

    Found by instrumenting a git call for an unrelated flake and noticing
    `fsmonitor--daemon` in a store's `.git`, which is this machine's global
    `core.fsmonitor = true`. Same door, quieter symptom. [E4]
    """
    _global_config(tmp_path, monkeypatch, f"[core]\n\texcludesFile = {tmp_path / 'ignore'}\n")
    (tmp_path / "ignore").write_text("*.jsonl\n")

    home = gitrepo.init(str(tmp_path / "store"))
    session = os.path.join(home, "sessions", "claude-code", "2026-09-21")
    os.makedirs(session)
    for name in ("000000000000-000000000008.jsonl", "g0.json"):
        with open(os.path.join(session, name), "w", encoding="utf-8") as fh:
            fh.write('{"a":1}\n')

    assert gitrepo.commit(home, "capture: one") is not None
    out = subprocess.run(
        ["git", "-C", home, "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    committed = set(out.stdout.split())
    here = "sessions/claude-code/2026-09-21"
    assert f"{here}/000000000000-000000000008.jsonl" in committed, out.stdout
    assert f"{here}/g0.json" in committed, out.stdout


def test_the_ignore_and_attributes_git_reads_without_being_told_to_reach_nothing(
    tmp_path, monkeypatch
):
    """The same loss as above, through the door that needs no configuration at all.

    `core.excludesFile` is a *setting*, and the test above plants one. But git
    also reads `$XDG_CONFIG_HOME/git/ignore` and `$XDG_CONFIG_HOME/git/attributes`
    — defaulting to `~/.config/git/` — with no setting's help, so scrubbing
    `GIT_CONFIG_GLOBAL` closes one door and leaves its twin open. That file
    exists on the machine this was written on. What holds the door is `HOME`
    pointing at `/dev/null` and `XDG_CONFIG_HOME` being unset, which makes the
    default path unreadable rather than merely unset; the property was closed
    by that change and pinned by nothing until here. [E4, review: gitrepo 1]

    Both surfaces, because they fail in opposite directions. `ignore` is silent:
    the segment is skipped, the commit succeeds, `verify` reads disk and says
    clean. `attributes` is loud but worse — `working-tree-encoding` rewrites the
    bytes on the way into the blob, so history stops hashing to the digest the
    manifest attests, which is the one claim this product makes.
    """
    fake = tmp_path / "fakehome"
    (fake / ".config" / "git").mkdir(parents=True)
    (fake / ".config" / "git" / "ignore").write_text("*.jsonl\n")
    (fake / ".config" / "git" / "attributes").write_text("*.jsonl text eol=crlf\n")
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    home = gitrepo.init(str(tmp_path / "store"))
    src = str(tmp_path / "a.jsonl")
    with open(src, "w") as fh:
        fh.write('{"type":"user"}\n' * 20)
    sid = store.session_id_for(src)
    cap = store.capture(src, "claude-code", sid, home=home)
    assert gitrepo.commit(home, "capture: one") is not None

    tracked = _ask(home, "ls-files").split()
    seg = next((t for t in tracked if t.startswith("raw/")), None)
    assert seg is not None, tracked  # `ignore`: the bytes reached history at all

    # `attributes`: and they reached it unrewritten. `eol=crlf` rather than a
    # UTF-16 encoding because that one makes `git add` fail outright, which any
    # test would notice; a line-ending filter is the quiet version, and quiet is
    # the failure mode worth pinning.
    blob = subprocess.run(
        ["git", "-C", home, "cat-file", "blob", f"HEAD:{seg}"],
        capture_output=True,
        check=True,
    ).stdout
    with open(os.path.join(home, seg), "rb") as fh:
        assert blob == fh.read(), "the blob is not the bytes on disk"
    assert store.span(store.sessions(home)[0], 0, cap.size) == blob


def test_a_setting_that_gets_past_the_scrub_stops_the_store_at_init(tmp_path, monkeypatch):
    """The floor under the mechanism, for the git the mechanism does not reach.

    `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` need git 2.32, and bullseye is
    still running 2.30, where both names are ignored without a word. Rather
    than a version check — which would only cover the sources we know the names
    of — `init` asks git what it is actually going to apply and refuses if any
    of it came from outside the repository. That question also catches an
    `include.path` reaching out of the local config, an `XDG_CONFIG_HOME`
    spelling, and whatever source a later git adds.

    So the leak is simulated at the level the floor is written for: an `_env`
    with none of the isolation in it, which is what `_env` effectively *is* on
    an old git. [E4, review: Gemini r3 §3]
    """
    home = gitrepo.init(str(tmp_path / "store"))  # isolated, and must stay fine

    old = tmp_path / "oldgit-home"
    old.mkdir()
    (old / ".gitconfig").write_text("[core]\n\texcludesFile = /tmp/nothing\n")
    leaky = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    leaky["HOME"] = str(old)
    leaky.pop("XDG_CONFIG_HOME", None)
    monkeypatch.setattr(gitrepo, "_env", lambda: leaky)

    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.init(home)

    assert str(old / ".gitconfig") in str(exc.value), exc.value
    assert "drop bytes out of a commit" in str(exc.value), "the message does not say why"


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


def _foreign_repo(tmp_path) -> str:
    """A repository gitmemory does not own, of the shape an attacker supplies.

    It has to exist already. The first attempt at this reproduction pointed
    `.git` at a path with nothing behind it, and `git init` then failed
    `fatal: not a git repository` — so the write never happened and the finding
    looked unreproducible. `git init` does not create the far end; an
    attacker-owned repository with a remote does. [E7]
    """
    other = tmp_path / "foreign"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "--quiet"], check=True)
    return str(other)


def _leaked(foreign: str) -> list[str]:
    """gitmemory's own four settings, if they were written somewhere else."""
    listed = subprocess.run(
        ["git", "-C", foreign, "config", "--local", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    keys = {k.lower() for k in gitrepo._CONFIG}
    return sorted(line for line in listed.splitlines() if line.split("=")[0] in keys)


@pytest.mark.parametrize("shape", ["gitfile", "symlink"])
def test_a_git_dir_the_store_did_not_make_is_refused_before_anything_is_written(tmp_path, shape):
    """[E7] `.git` pointing at somebody else's repository, two ways.

    A **gitfile** — `.git` as a regular file holding `gitdir: /elsewhere/.git` —
    is git's own worktree mechanism. `_assert_no_foreign_config` did refuse it,
    but it refused *fourth*: `is_repo` said no, `git init` adopted the foreign
    repository, and four `git config` calls wrote gitmemory's settings into it
    before the assertion ran. Measured before the fix: 5 settings in the foreign
    repo, and `commit` then succeeding into its history.

    A **symlink** was worse and is in no report. `os.path.isdir` follows it, so
    `is_repo` said yes and `git init` was skipped; the config writes landed in
    the foreign repository; and `_assert_no_foreign_config` **passed**, because
    it compares `realpath(home/.git/config)` with itself and a symlinked `.git`
    resolves to the foreign config on both sides. `init` returned normally,
    `.gitignore` was written, `commit` succeeded, and the transcript store went
    into a repository gitmemory does not own — no error, no warning, nothing.

    So both halves are asserted: refused, **and** nothing written before the
    refusal. A guard that fires after the write is the gitfile case again.
    """
    foreign = _foreign_repo(tmp_path)
    home = tmp_path / "store"
    home.mkdir()
    dot = home / ".git"
    if shape == "gitfile":
        dot.write_text(f"gitdir: {foreign}/.git\n")
    else:
        dot.symlink_to(os.path.join(foreign, ".git"))

    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.init(str(home))
    assert "does not own" in str(exc.value), exc.value
    assert _leaked(foreign) == [], "settings were written before the refusal"
    assert not (home / ".gitignore").exists(), "the store wrote into someone else's tree"


@pytest.mark.parametrize("shape", ["gitfile", "symlink"])
def test_the_commit_refuses_the_same_git_dir_init_refused(tmp_path, shape):
    """[E7] Because `run` carries on after a failed `init`, deliberately.

    That decision is right and stays: two watchers racing `init`'s four `git
    config` calls is an ordinary lock collision, and the bytes are the part no
    later pass can recover. But "carry on" past an *ownership* refusal means the
    next thing that happens is `add --all` and `commit` into the foreign
    repository — which is exactly what was measured: `init` raised, and the
    transcript landed in the other repo's history one call later.

    The check therefore lives where the write does, not only where the refusal
    reads well.
    """
    foreign = _foreign_repo(tmp_path)
    home = tmp_path / "store"
    home.mkdir()
    dot = home / ".git"
    if shape == "gitfile":
        dot.write_text(f"gitdir: {foreign}/.git\n")
    else:
        dot.symlink_to(os.path.join(foreign, ".git"))
    (home / "transcript.jsonl").write_text('{"role":"user"}\n')

    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.commit(str(home), "capture")
    assert "does not own" in str(exc.value), exc.value
    # `rev-list --count` and not `log`: git exits 128 on a repository with no
    # commits, which is the state this test is asserting, so `log` cannot tell
    # "nothing was committed" from "the command failed".
    commits = subprocess.run(
        ["git", "-C", foreign, "rev-list", "--all", "--count"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert commits == "0", f"the store committed {commits} into a foreign repository"


def test_a_git_dir_symlinked_to_nothing_does_not_make_a_repository_somewhere_else(tmp_path):
    """[E7] Why the guard uses `lexists` and not `exists`.

    The two cases above need a repository already sitting at the far end. This
    one needs nothing: `.git` symlinked to a path that does not exist yet. With
    the guard removed, `git init` follows the link and **creates** a complete
    repository — `HEAD`, `config`, `objects`, `refs` — at whatever path the link
    named, outside the store entirely. Measured, not argued.

    `os.path.exists` follows the link and answers False for this, which would
    have walked the guard straight past the one shape that does not require the
    attacker to have set anything up first.
    """
    elsewhere = tmp_path / "attacker-chosen"
    elsewhere.mkdir()
    home = tmp_path / "store"
    home.mkdir()
    (home / ".git").symlink_to(elsewhere / ".git")

    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.init(str(home))
    assert "symbolic link" in str(exc.value), exc.value
    assert not (elsewhere / ".git").exists(), "a repository was created outside the store"


def test_a_store_the_daemon_really_made_is_not_caught_by_the_ownership_check(tmp_path):
    """[E7] The over-refusal side. `init` is idempotent and the watcher calls it
    on every start, so a guard that trips on an ordinary `.git` directory stops
    the product rather than an attacker."""
    home = gitrepo.init(str(tmp_path / "store"))
    with open(os.path.join(home, "payload"), "w", encoding="utf-8") as fh:
        fh.write("bytes")
    assert gitrepo.commit(home, "first") is not None
    assert gitrepo.init(home) == home


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

    Three names are set by `_env` itself, so those are checked by value and not
    excused by name: each is planted below with an attacker's value first, and
    the assertion is that what arrives is gitmemory's. An exemption by name
    alone would pass `GIT_CONFIG_SYSTEM=/tmp/attacker` through the hole written
    for `GIT_CONFIG_SYSTEM=/dev/null`. [E4]
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
        "GIT_CONFIG_SYSTEM",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_NOSYSTEM",
    ):
        monkeypatch.setenv(var, "/tmp/attacker")
    # Not `GIT_*` names, and so not covered by the scrub: the two other
    # spellings of "where the global config lives".
    monkeypatch.setenv("HOME", "/tmp/attacker")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/attacker")

    env = gitrepo._env()

    ours = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": os.devnull,
    }
    leaked = {k for k in env if k.startswith("GIT_")} - set(ours)
    assert not leaked, f"reached the git subprocess: {sorted(leaked)}"
    for name, value in ours.items():
        assert env[name] == value, f"{name} kept the planted value"
    assert "XDG_CONFIG_HOME" not in env, "the third spelling of the global config got through"
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

    Planted at the depths the store actually writes them, which the first
    version of this test did not: it put the manifest temp inside the *raw*
    generation directory, a place nothing writes one. That went unnoticed while
    the ignore patterns were unanchored, because an unanchored pattern matches
    at any depth — so the test passed against a layout the product does not
    have, and stopped passing the moment the patterns were pinned to the real
    one. [E4, review: gitrepo 2]
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    seg_dir = os.path.join(home, "raw", "claude-code", "s", "g00")
    man_dir = os.path.join(home, "sessions", "claude-code", "s")
    os.makedirs(seg_dir)
    os.makedirs(man_dir)
    partials = [
        os.path.join(seg_dir, ".incoming.4242.7"),
        os.path.join(man_dir, "g00.json.tmp.4242.7"),
    ]
    for path in partials:
        with open(path, "w") as fh:
            fh.write("half a file")
    with open(os.path.join(seg_dir, "000000000000-000000000006.jsonl"), "w") as fh:
        fh.write("whole\n")
    with open(os.path.join(man_dir, "g00.json"), "w") as fh:
        fh.write("{}\n")

    gitrepo.commit(home, "capture: one")
    tracked = _ask(home, "ls-files").split()
    assert any(t.endswith("000000000000-000000000006.jsonl") for t in tracked), tracked
    assert any(t.endswith("sessions/claude-code/s/g00.json") for t in tracked), tracked
    assert not [t for t in tracked if ".incoming." in t or ".tmp." in t], tracked


def test_a_session_named_like_the_stores_own_temp_files_still_reaches_history(tmp_path):
    """The ignore patterns hold a denylist against a path the *user* names. [E4, review: gitrepo 2]

    `*.tmp.*`, unanchored, matches a directory component — and one of the
    components under `raw/` and `sessions/` is `session_id_for`'s key, which is
    built from the transcript's filename. A transcript called
    `session.tmp.42.jsonl` therefore excluded its own entire session, raw and
    manifest both, from every commit git was asked to make. Capture returned
    normally, `verify` read the bytes off disk and reported clean, and the
    session was simply absent from the history that is the product.

    Nothing in the suite could see it, because nothing asserted that an
    ordinarily-named session *is* committed — only that partials are not. Both
    halves are asserted here, against a real capture rather than planted files,
    so the test cannot drift from the layout.
    """
    home = str(tmp_path / "store")
    gitrepo.init(home)
    src = str(tmp_path / "session.tmp.42.jsonl")
    with open(src, "w") as fh:
        fh.write('{"type":"user"}\n' * 20)
    sid = store.session_id_for(src)
    assert ".tmp." in sid, sid  # the collision this is about
    store.capture(src, "claude-code", sid, home=home)

    gitrepo.commit(home, "capture: one")
    tracked = _ask(home, "ls-files").split()
    assert [t for t in tracked if t.startswith(f"raw/claude-code/{sid}/")], tracked
    assert f"sessions/claude-code/{sid}/g00.json" in tracked, tracked


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


# --- E7 fs-F2: the mode that was set once and never again --------------------


def test_a_home_the_user_made_first_is_still_owner_only(tmp_path):
    """A mode set once is a mode set never.

    `os.makedirs(mode=...)` applies its mode only when it *creates* the
    directory, so `mkdir ~/.gitmemory` under the default 022 umask left the
    store at 0755 for ever — as did a restore by a `tar` or an `rsync` without
    `-p`. Measured before the fix: the chain from the filesystem root down to
    `.git/objects/xx/…` was other-traversable at every level and the blob was
    0444, so any local account read every captured credential. No foothold, no
    race, no write access; read is enough, which is why this outranked the
    symlink findings in the same round. [E7 fs-F2]
    """
    home = str(tmp_path / "store")
    os.mkdir(home)
    os.chmod(home, 0o755)  # explicit, so a 077 umask cannot make this vacuous
    gitrepo.init(home)

    with open(os.path.join(home, "t.jsonl"), "w", encoding="utf-8") as fh:
        fh.write('{"k":"AKIAZZZZQQQQWWWW1234"}\n')  # synthetic, right shape
    gitrepo.commit(home, "capture: one")

    objects = [
        os.path.join(root, name)
        for root, _dirs, files in os.walk(os.path.join(home, ".git", "objects"))
        for name in files
    ]
    assert objects, "no loose objects, so this test is not looking at the copy that leaks"

    assert os.stat(home).st_mode & 0o077 == 0, "the store itself is readable by someone else"
    gitdir = os.path.join(home, ".git")
    assert os.stat(gitdir).st_mode & 0o077 == 0, "the second copy is readable by someone else"

    # And the property those two modes exist for, checked against the objects
    # themselves rather than inferred: git leaves `objects/xx` at 0755 and the
    # blob at 0444, so "unreachable" has to mean *some* directory on the way
    # down refuses the traversal, not all of them.
    for obj in objects:
        chain, p = [], obj
        while p != home:
            p = os.path.dirname(p)
            chain.append(p)
        assert any(os.stat(d).st_mode & 0o001 == 0 for d in chain), (
            f"every directory from {home} down to {os.path.relpath(obj, home)} "
            "lets another account walk through it"
        )


def test_the_home_mode_is_re_applied_on_every_start_not_just_the_first(tmp_path):
    """`init` runs per start precisely so a store picks up what it is missing.

    A `chmod` aimed at the wrong path, a restore, a synced volume: the mode can
    be wrong at any time, not only at creation, so checking it only when the
    directory is new checks it at the one moment it is guaranteed right.
    [E7 fs-F2]
    """
    home = gitrepo.init(str(tmp_path / "store"))
    os.chmod(home, 0o755)
    os.chmod(os.path.join(home, ".git"), 0o755)

    gitrepo.init(home)

    assert os.stat(home).st_mode & 0o077 == 0
    assert os.stat(os.path.join(home, ".git")).st_mode & 0o077 == 0
