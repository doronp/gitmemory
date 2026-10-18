# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""`gitmemory service install`: one inert file that keeps `watch` running.

The property under test: what a user service manager starts is the watcher the
user's shell would have run. systemd and launchd hand a job none of the shell's
state — not its working directory, not `$GITMEMORY_HOME`, not its `PATH` — so
everything the watcher needs has to be written into the file, and the only
proof that it was is to run the file's own command line under the manager's
conditions. That is the end-to-end test below; the others pin what each
manager reads and that the command writes, refuses and never starts.

Nothing here runs `systemctl`, `launchctl` or `loginctl`. HOME comes from
conftest and XDG_CONFIG_HOME is set or unset per test, so every file lands in
tmp_path, and both renderers run on either OS by setting `sys.platform`. The
one test that runs a printed command puts stand-ins for the managers first on
its PATH.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gitmemory.__main__ import main

LABEL = "io.github.doronp.gitmemory"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    path = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(path))
    return path


@pytest.fixture
def no_spawn(monkeypatch):
    """Fail on any child process: the command writes a file and starts nothing.

    `Popen` is what `run`, `call` and `check_output` all construct, so one patch
    covers every way the module could reach `systemctl` or `launchctl`.
    """

    def spawned(*args, **kwargs):
        raise AssertionError(f"service install spawned {args!r}")

    monkeypatch.setattr(subprocess, "Popen", spawned)


def _install(monkeypatch, platform: str, *argv: str) -> int:
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", platform)
        return main([*argv, "service", "install"])


def _unit(path) -> dict[str, str]:
    """`key -> value` for a unit file's assignments; sections are not needed."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"(?: |$)', re.S)


def _words(value: str, *, dollars: bool) -> list[str]:
    """Read a unit's words back as systemd.syntax(7) says systemd does, and
    fail on anything it would read as something else.

    Strict, because a lenient reader is what made the quoting test decorative:
    turning `%%` into `%` also lets an undoubled `%h` through as itself, so the
    round trip held whether or not the unit escaped anything. To systemd a `%h`
    is a specifier, a lone `$` on a command line is a variable, and `\\z` is an
    escape it rejects, so each is a failure here. Order as systemd's: unquote,
    then specifiers, then (`ExecStart=` only) variables. The test's reading of
    the manual, not systemd's parser; there is no systemd to ask on a Mac.
    """

    def strict(pattern: str, keep: tuple[str, ...], word: str) -> str:
        def one(m: re.Match) -> str:
            assert m.group(1) in keep, f"systemd reads {m.group(0)!r} in {value!r}"
            return m.group(1)

        return re.sub(pattern, one, word, flags=re.S)

    words, pos = [], 0
    while pos < len(value):
        m = _QUOTED.match(value, pos)
        assert m, f"not a double-quoted word at {pos}: {value!r}"
        word = strict(r"%(.?)", ("%",), strict(r"\\(.?)", ("\\", '"'), m.group(1)))
        words.append(strict(r"\$(.?)", ("$",), word) if dollars else word)
        pos = m.end()
    return words


def _expected(home) -> list[str]:
    return [sys.executable, "-P", "-m", "gitmemory", "--home", os.path.realpath(home), "watch"]


def test_service_install_writes_a_systemd_user_unit_and_starts_nothing(
    tmp_path, monkeypatch, capsys, xdg, no_spawn
):
    home = tmp_path / "store"
    assert _install(monkeypatch, "linux", "--home", str(home)) == 0
    path = xdg / "systemd" / "user" / "gitmemory.service"
    unit = _unit(path)
    assert _words(unit["ExecStart"], dollars=True) == _expected(home)
    env = _words(unit["Environment"], dollars=False)
    assert env == [f"PATH={os.path.dirname(shutil.which('git'))}:/usr/bin:/bin"], env
    assert unit["Restart"] == "on-failure" and unit["RestartSec"] == "30"
    assert unit["WantedBy"] == "default.target"
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {path}\n") and not err
    ctl = "systemctl --user"
    assert f"{ctl} daemon-reload && {ctl} enable --now gitmemory.service" in out
    assert "loginctl enable-linger" in out
    removal = f"{ctl} disable --now gitmemory.service; rm {shlex.quote(str(path))}"
    assert f"  {removal}; {ctl} daemon-reload\n" in out


def test_service_install_writes_a_launch_agent_and_starts_nothing(
    tmp_path, monkeypatch, capsys, no_spawn
):
    """No `--home`: the store comes from `$GITMEMORY_HOME`, which a LaunchAgent
    started at login never sees, so the file has to name it outright."""
    home = tmp_path / "elsewhere"
    monkeypatch.setenv("GITMEMORY_HOME", str(home))
    assert _install(monkeypatch, "darwin") == 0
    user = os.environ["HOME"]
    path = os.path.join(user, "Library", "LaunchAgents", f"{LABEL}.plist")
    with open(path, "rb") as fh:
        job = plistlib.load(fh)
    assert job["Label"] == LABEL
    assert job["ProgramArguments"] == _expected(home)
    git = os.path.dirname(shutil.which("git"))
    assert job["EnvironmentVariables"] == {"PATH": f"{git}:/usr/bin:/bin"}
    assert job["WorkingDirectory"] == user
    assert job["RunAtLoad"] is True and job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["ThrottleInterval"] == 30
    # Outside the store, which commits everything in it with `git add --all`.
    log = os.path.join(user, "Library", "Logs", "gitmemory.log")
    assert job["StandardOutPath"] == job["StandardErrorPath"] == log
    out, _ = capsys.readouterr()
    assert f"launchctl bootstrap gui/$(id -u) {shlex.quote(path)}" in out
    assert f"launchctl bootout gui/$(id -u)/{LABEL}; rm {shlex.quote(path)}" in out


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_installed_service_runs_the_watcher_from_its_own_working_directory(
    tmp_path, monkeypatch, capsys, xdg, platform
):
    """Run the file's command line the way its manager would, plus `--once`.

    The manager's conditions are: the working directory is `$HOME` (systemd's
    default for a user unit, and what the plist sets), the environment is HOME
    and the file's PATH and nothing else, and `$HOME` holds a `gitmemory.py`.
    Each is a trap a hand-written unit fell into: `realpath(sys.executable)`
    leaves the venv and cannot import gitmemory; `python -m` without `-P` puts
    the working directory first on `sys.path` and runs the stray file instead;
    and without `--home` the watcher reads `~/.gitmemory`, not the store the
    user configured, and watches nothing.
    """
    home, roots = tmp_path / "store", tmp_path / "roots"
    os.makedirs(home)
    os.makedirs(roots / "proj")
    (roots / "proj" / "s1.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hello"}}\n'
    )
    (home / "config.toml").write_text(f'[[watch]]\nagent = "claude-code"\nroots = ["{roots}"]\n')
    user = os.environ["HOME"]
    with open(os.path.join(user, "gitmemory.py"), "w") as fh:
        fh.write("raise SystemExit('a gitmemory.py in the working directory ran instead')\n")
    assert _install(monkeypatch, platform, "--home", str(home)) == 0
    if platform == "linux":
        unit = _unit(xdg / "systemd" / "user" / "gitmemory.service")
        argv = _words(unit["ExecStart"], dollars=True)
        path = _words(unit["Environment"], dollars=False)[0].removeprefix("PATH=")
        cwd = user
    else:
        with open(os.path.join(user, "Library", "LaunchAgents", f"{LABEL}.plist"), "rb") as fh:
            job = plistlib.load(fh)
        argv, path = job["ProgramArguments"], job["EnvironmentVariables"]["PATH"]
        cwd = job["WorkingDirectory"]
    capsys.readouterr()
    done = subprocess.run(
        [*argv, "--once"],
        cwd=cwd,
        env={"HOME": user, "PATH": path},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "captured 1 " in done.stderr, done.stderr
    assert main(["--home", str(home), "verify"]) == 0


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_service_install_refuses_to_overwrite_an_existing_file(
    tmp_path, monkeypatch, capsys, xdg, no_spawn, platform
):
    """Exit 1, the file untouched, and one line that says how to replace it.

    A file already there may be one the user edited; a second install is not a
    reason to lose that."""
    if platform == "linux":
        path = xdg / "systemd" / "user" / "gitmemory.service"
    else:
        path = Path(os.environ["HOME"], "Library", "LaunchAgents", f"{LABEL}.plist")
    os.makedirs(path.parent)
    path.write_bytes(b"the user's own\n")
    assert _install(monkeypatch, platform, "--home", str(tmp_path / "store")) == 1
    assert path.read_bytes() == b"the user's own\n"
    out, err = capsys.readouterr()
    assert not out and err.count("\n") == 1, err
    assert err.startswith(f"refusing: {path} exists") and f"rm {shlex.quote(str(path))}" in err


def test_a_unit_survives_a_home_with_quotes_percent_and_dollar_in_it(
    tmp_path, monkeypatch, capsys, xdg, no_spawn
):
    """systemd expands `%h` and `$HOME` inside a quoted word and rejects `\\z`,
    so a store whose path spells any of them would be read as some other path,
    or the unit not read at all. `_words` fails on each unless it is escaped."""
    home = tmp_path / 'a b %h $HOME "q" \\z'
    assert _install(monkeypatch, "linux", "--home", str(home)) == 0
    path = xdg / "systemd" / "user" / "gitmemory.service"
    assert _words(_unit(path)["ExecStart"], dollars=True) == _expected(home)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("brk", ["\n", "\r"], ids=["lf", "cr"])
def test_a_line_break_in_the_store_path_is_refused_on_either_platform(
    tmp_path, monkeypatch, capsys, xdg, no_spawn, platform, brk
):
    """Exit 2 and no file. A unit file would end the line there; a plist holds
    `\\n`, but plistlib turns `\\r` into `\\n`, so that service would watch a
    path that does not exist rather than refuse."""
    assert _install(monkeypatch, platform, "--home", str(tmp_path / f"a{brk}b")) == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert not os.path.exists(xdg) and not os.listdir(os.environ["HOME"])


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_printed_removal_works_when_the_job_was_never_started(
    tmp_path, monkeypatch, capsys, xdg, platform
):
    """The refusal's way out, pasted, removes the file even when the stop fails.

    Written and never started is the state a second install meets: the user
    reads `it runs:`, sees the wrong store, and installs again. `launchctl
    bootout` of a job that was never bootstrapped exits 3 ("No such process"),
    and a user manager that cannot be reached fails `systemctl --user` too, so
    a removal that waits on the stop leaves the file, and the next install is
    refused with the same advice. The stand-ins below fail that way; the real
    managers are never on the PATH they run with.
    """
    if platform == "linux":
        path = xdg / "systemd" / "user" / "gitmemory.service"
    else:
        path = Path(os.environ["HOME"], "Library", "LaunchAgents", f"{LABEL}.plist")
    store = str(tmp_path / "store")
    assert _install(monkeypatch, platform, "--home", store) == 0
    out = capsys.readouterr().out
    printed = out.split("Remove it:\n  ", 1)[1].rstrip("\n")
    assert _install(monkeypatch, platform, "--home", store) == 1
    refusal = capsys.readouterr().err
    assert refusal.rstrip("\n").endswith(f": {printed}"), refusal
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    fails = "#!/bin/sh\necho 'Boot-out failed: 3: No such process' >&2\nexit 3\n"
    for manager in ("systemctl", "launchctl"):
        (stubs / manager).write_text(fails)
        (stubs / manager).chmod(0o755)
    done = subprocess.run(
        ["/bin/sh", "-c", printed],
        env={"HOME": os.environ["HOME"], "PATH": f"{stubs}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not path.exists(), (printed, done.stderr)
    assert _install(monkeypatch, platform, "--home", store) == 0


@pytest.mark.parametrize("value", [None, "", "rel"], ids=["unset", "empty", "relative"])
def test_a_unit_lands_under_dot_config_without_an_absolute_xdg_config_home(
    tmp_path, monkeypatch, capsys, no_spawn, value
):
    """The default most Linux users get, and the XDG rule that a relative
    `$XDG_CONFIG_HOME` is invalid and ignored: a unit written relative to
    wherever install ran is one no user manager looks for."""
    monkeypatch.chdir(tmp_path)
    if value is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", value)
    assert _install(monkeypatch, "linux", "--home", str(tmp_path / "store")) == 0
    path = os.path.join(os.environ["HOME"], ".config", "systemd", "user", "gitmemory.service")
    assert _words(_unit(path)["ExecStart"], dollars=True) == _expected(tmp_path / "store")
    assert not (tmp_path / "rel").exists()
    out = capsys.readouterr().out
    removal = f"; rm {shlex.quote(path)}; systemctl --user daemon-reload\n"
    assert out.startswith(f"wrote {path}\n") and removal in out


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_git_found_through_a_relative_path_entry_is_written_absolute(
    tmp_path, monkeypatch, capsys, xdg, no_spawn, platform
):
    """`PATH=relbin:...` makes `which` answer `relbin/git`. Written as it is,
    the service would resolve it against its own working directory, `$HOME`,
    not the directory install ran in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: os.path.join("relbin", name))
    assert _install(monkeypatch, platform, "--home", str(tmp_path / "store")) == 0
    if platform == "linux":
        unit = _unit(xdg / "systemd" / "user" / "gitmemory.service")
        path = _words(unit["Environment"], dollars=False)[0].removeprefix("PATH=")
    else:
        plist = Path(os.environ["HOME"], "Library", "LaunchAgents", f"{LABEL}.plist")
        path = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"]
    assert path == f"{os.path.join(os.getcwd(), 'relbin')}:/usr/bin:/bin"


@pytest.mark.parametrize("exe", ["", None], ids=["empty", "none"])
def test_service_install_refuses_when_it_cannot_tell_which_python_runs(
    tmp_path, monkeypatch, capsys, xdg, no_spawn, exe
):
    """`sys.executable` is empty or None when Python cannot say where it is.
    A unit naming `""` cannot start, and None was a traceback; exit 2 instead,
    with nothing written."""
    monkeypatch.setattr(sys, "executable", exe)
    assert _install(monkeypatch, "linux", "--home", str(tmp_path / "store")) == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert not os.path.exists(xdg)


def test_service_install_without_git_or_on_an_unknown_platform_writes_nothing(
    tmp_path, monkeypatch, capsys, xdg, no_spawn
):
    """Exit 2 either way: the watcher commits with git, and it would find none."""
    home = str(tmp_path / "store")
    assert _install(monkeypatch, "freebsd14", "--home", home) == 2
    assert "freebsd14" in capsys.readouterr().err
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert _install(monkeypatch, "linux", "--home", home) == 2
    assert "git" in capsys.readouterr().err
    assert not os.path.exists(xdg) and not os.listdir(os.environ["HOME"])
