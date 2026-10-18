# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""`gitmemory service install`: the file that keeps `watch` running.

A systemd user unit on Linux, a LaunchAgent on macOS. Written, never enabled:
the command prints the line that starts it and runs no `systemctl`,
`launchctl` or `loginctl` itself. Installing it is the user's action, which is
the hook's rule too (hook/README.md), and a service manager's failures read
better in the user's own terminal than translated here.

What it is for is the facts only the running process has. A user service gets
none of the shell's state — not its working directory, not an exported
`$GITMEMORY_HOME`, not its `PATH` — so a hand-written unit can start a
different Python, watch a different store, or not find git. Each of those was
reproduced; each is written into the file instead of inherited.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import sys

LABEL = "io.github.doronp.gitmemory"
UNIT = "gitmemory.service"
DOCS = "https://github.com/doronp/gitmemory/blob/main/docs/watching.md#keep-it-running"


def argv(home: str) -> list[str]:
    """The watcher's command line, for a manager that inherits nothing.

    `sys.executable` as given: its `realpath` is the base interpreter outside
    the venv, which cannot import gitmemory. `-P`, because a systemd user unit
    starts in `$HOME` and `python -m` puts that first on `sys.path`, so a stray
    `~/gitmemory.py` ran in place of the package. `--home` always, resolved
    now: the manager never sees the shell's `$GITMEMORY_HOME`, and the watcher
    would otherwise read a store the hook does not write to.
    """
    if not sys.executable:
        # Empty or None when Python cannot say where it is; a unit naming ""
        # cannot start.
        raise RuntimeError(
            "cannot tell which Python is running; run this from an installed gitmemory"
        )
    return [sys.executable, "-P", "-m", "gitmemory", "--home", home, "watch"]


def _word(text: str) -> str:
    """One double-quoted word in a unit file (systemd.syntax(7)): `\\` and `"`
    escaped, `%` doubled so a store path spelling `%h` is not a specifier."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render(platform: str, home: str) -> tuple[str, bytes, list[tuple[str, str]]]:
    """`(path, contents, steps)`: where the file goes, its bytes, and the
    `(what, command)` lines the user runs, the removal last."""
    if platform not in ("linux", "darwin"):
        raise ValueError(
            f"no user service manager known for {platform}; "
            "run `gitmemory watch` under your own supervisor"
        )
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH; the watcher needs it to commit")
    # gitrepo finds git through PATH, and a manager's PATH is not the shell's:
    # a git from Homebrew or nix is on one and not the other. Absolute, since a
    # relative PATH entry would resolve against the service's working
    # directory; not realpath, so a symlinked git keeps the shell's spelling.
    path = f"{os.path.abspath(os.path.dirname(git))}:/usr/bin:/bin"
    words = argv(home)
    # Checked once for both: a unit file would end the line there, and plistlib
    # turns `\r` into `\n`, so a plist would name a path that does not exist.
    for word in (*words, path):
        if "\n" in word or "\r" in word:
            raise ValueError(f"a service file cannot hold a line break: {word!r}")
    if platform == "linux":
        # The XDG rule: a relative value is invalid and is ignored, and a unit
        # written relative to wherever this ran is one no user manager reads.
        base = os.environ.get("XDG_CONFIG_HOME", "")
        if not os.path.isabs(base):
            base = os.path.expanduser("~/.config")
        target = os.path.join(base, "systemd", "user", UNIT)
        # `$` is expanded on a command line and has no meaning in Environment=,
        # so it is doubled in one and not the other. No WorkingDirectory: a
        # user unit's default is `$HOME`, which is what the plist below sets.
        body = (
            "[Unit]\n"
            "Description=gitmemory watcher\n"
            f"Documentation={DOCS}\n"
            "\n"
            "[Service]\n"
            f"ExecStart={' '.join(_word(w.replace('$', '$$')) for w in words)}\n"
            f"Environment={_word('PATH=' + path)}\n"
            "Restart=on-failure\n"
            "RestartSec=30\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode()
        ctl = "systemctl --user"
        steps = [
            ("Nothing is running yet. Start it now and at every login",
             f"{ctl} daemon-reload && {ctl} enable --now {UNIT}"),
            ("Keep it running after you log out, on a machine you reach over SSH",
             "loginctl enable-linger"),
            ("Check it", f"{ctl} status {UNIT}"),
            ("Its log", f"journalctl --user -u {UNIT} -f"),
            ("After upgrading gitmemory", f"{ctl} restart {UNIT}"),
            # `;`: the file goes even when the manager cannot be reached.
            ("Remove it", f"{ctl} disable --now {UNIT}; rm {shlex.quote(target)}"),
        ]
        return target, body, steps
    target = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    # Not in the store: the watcher commits it with `git add --all`.
    log = os.path.expanduser("~/Library/Logs/gitmemory.log")
    body = plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": words,
            "EnvironmentVariables": {"PATH": path},
            # launchd starts a job in `/`; a relative watch root should resolve
            # where it does under systemd.
            "WorkingDirectory": os.path.expanduser("~"),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )
    job = f"gui/$(id -u)/{LABEL}"
    # `;`, not `&&`, in the removal: `bootout` of a job that was written and
    # never bootstrapped exits 3, and the file has to go all the same.
    steps = [
        ("Nothing is running yet. Start it now and at every login",
         f"launchctl bootstrap gui/$(id -u) {shlex.quote(target)}"),
        ("Check it", f"launchctl print {job}"),
        ("Its log", f"tail -f {shlex.quote(log)}"),
        ("After upgrading gitmemory", f"launchctl kickstart -k {job}"),
        ("Remove it", f"launchctl bootout {job}; rm {shlex.quote(target)}"),
    ]
    return target, body, steps


def write(path: str, body: bytes) -> bool:
    """Create `path` with `body`, or return False and touch nothing if it exists.

    `O_EXCL`, not a check then a write: a file already there may be one the
    user edited, and a second install is not a reason to lose it.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
    return True
