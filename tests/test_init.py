# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""`gitmemory init <agent>`: the `[[watch]]` table, written because it was asked for.

Not `gitrepo.init`, which makes the store's repository and is tested in
`test_gitrepo.py`. This one writes `config.toml`, and the properties under test
are the ones that make writing it safe: the file also holds the `[remote.*]`
push opt-ins, so it is appended to and never rewritten; a second run writes
nothing; and a table the watcher would skip is refused before it is written,
with the watcher's own sentence, rather than written and left to fail quietly
on every pass.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess

import pytest

from gitmemory import daemon, gitrepo, redact
from gitmemory.__main__ import main


def _cfg(home: str) -> str:
    return os.path.join(home, "config.toml")


def test_cli_init_writes_the_table_the_watcher_reads(tmp_path, capsys, _no_real_home):
    """No config, one command, and the watcher watches the documented root."""
    home = str(tmp_path / "store")
    assert main(["--home", home, "init", "claude-code"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {_cfg(home)}: watch ~/.claude/projects for claude-code\n")
    assert "next: run `gitmemory watch`" in out
    assert "~/.claude/projects does not exist yet" in err
    with open(_cfg(home), encoding="utf-8") as fh:
        assert fh.read().startswith("# added by `gitmemory init claude-code`\n[[watch]]\n")
    want = os.path.realpath(_no_real_home / ".claude" / "projects")
    assert daemon.load_watches(home) == [daemon.Watch("claude-code", (want,))]
    assert stat.S_IMODE(os.stat(_cfg(home)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700


def test_cli_init_twice_is_one_table(tmp_path, capsys):
    home = str(tmp_path / "store")
    assert main(["--home", home, "init", "pi"]) == 0
    with open(_cfg(home), "rb") as fh:
        first = fh.read()
    capsys.readouterr()
    assert main(["--home", home, "init", "pi"]) == 0
    with open(_cfg(home), "rb") as fh:
        assert fh.read() == first
    assert "already watches ~/.pi/agent/sessions for pi; nothing to do" in capsys.readouterr().out


def test_cli_init_appends_and_keeps_every_byte_already_there(tmp_path):
    """`config.toml` is also where a push is opted into. Rewriting it would
    revoke the remote; appending has to leave it, comments and all."""
    home = tmp_path / "store"
    home.mkdir()
    old = b'# mine\n[remote.origin]\nurl = "git@host:me/p.git"\nallow_push = true'  # no final \n
    (home / "config.toml").write_bytes(old)
    assert main(["--home", str(home), "init", "claude-code"]) == 0
    assert main(["--home", str(home), "init", "pi"]) == 0
    assert (home / "config.toml").read_bytes().startswith(old + b"\n")
    assert redact.push_allowed(str(home), "origin")[0]
    assert [w.agent for w in daemon.load_watches(str(home))] == ["claude-code", "pi"]


@pytest.mark.parametrize("case", ["file-root", "store-under-root", "root-under-store"])
def test_cli_init_refuses_a_table_the_watcher_would_skip_and_writes_nothing(
    tmp_path, capsys, _no_real_home, case
):
    """Each one would be skipped by the watcher on every pass. The refusal is
    the watcher's own sentence, so the two cannot disagree about why, and the
    two overlaps are told apart: a root inside the store does not contain it."""
    projects = _no_real_home / ".claude" / "projects"
    if case == "file-root":
        projects.parent.mkdir()
        projects.write_text("one transcript, not a directory")
        home, why = str(tmp_path / "store"), "is a file, not a directory"
    elif case == "store-under-root":
        projects.mkdir(parents=True)
        home, why = str(projects / "store"), "contains the store itself"
    else:
        home, why = str(_no_real_home / ".claude"), "is inside the store"
    assert main(["--home", home, "init", "claude-code"]) == 1
    err = capsys.readouterr().err
    assert re.search(rf"^refusing: .*\[\[watch\]\] #1: root ~/.claude/projects {why}", err, re.M)
    assert not os.path.lexists(home), "a refusal writes nothing, not even the store"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"[[watch]\nnot toml", "is not valid TOML"),
        (b"\xff\xfe", "is not valid TOML"),
        (b'watch = [{agent = "pi", roots = ["/nowhere"]}]\n', "cannot join"),
        (b'[watch]\nagent = "pi"\n', "cannot join"),
        (b"watch = 5\n", "cannot join"),
    ],
    ids=["syntax", "not-utf8", "inline-array", "plain-table", "not-a-list"],
)
def test_cli_init_refuses_a_config_it_cannot_append_to_and_leaves_it(
    tmp_path, capsys, body, why
):
    """The last three parse, and appending `[[watch]]` to them does not: the
    reason is said rather than left to whichever check happens to trip next."""
    home = tmp_path / "store"
    home.mkdir()
    (home / "config.toml").write_bytes(body)
    assert main(["--home", str(home), "init", "claude-code"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: ") and why in err, err
    assert (home / "config.toml").read_bytes() == body


@pytest.mark.parametrize("live", [True, False], ids=["live", "dangling"])
def test_cli_init_does_not_replace_a_symlinked_config(tmp_path, capsys, live):
    """`os.replace` over a link swaps the link for a file, so the user's dotfile
    would silently stop being the config. Refused instead."""
    home = tmp_path / "store"
    home.mkdir()
    target = tmp_path / "dotfiles.toml"
    if live:
        target.write_text("")
    (home / "config.toml").symlink_to(target)
    assert main(["--home", str(home), "init", "claude-code"]) == 1
    assert "is not a regular file" in capsys.readouterr().err
    assert os.path.islink(home / "config.toml")
    assert (target.read_text() == "") if live else not target.exists()


def test_cli_init_does_not_split_a_hard_linked_config(tmp_path, capsys):
    """A regular file, and still a link: `os.replace` gives `config.toml` a new
    inode, so the dotfile hard-linked to it would keep the old bytes, push
    opt-in and all, and silently stop being the config. Refused, like a symlink."""
    home = tmp_path / "store"
    home.mkdir()
    dotfile = tmp_path / "dotfiles.toml"
    body = '[remote.origin]\nurl = "git@host:me/p.git"\nallow_push = true\n'
    dotfile.write_text(body)
    os.link(dotfile, home / "config.toml")
    assert main(["--home", str(home), "init", "claude-code"]) == 1
    assert "has another hard link" in capsys.readouterr().err
    assert os.path.samefile(dotfile, home / "config.toml")
    assert dotfile.read_text() == body


@pytest.mark.parametrize("case", ["home-is-a-file", "unreadable"])
def test_cli_init_says_error_and_exits_2_when_it_could_not_look(tmp_path, capsys, case):
    """Exit 1 is an answer: init looked, and refused. A config it could not
    read is not one, and taking it for absent would replace it with the table
    alone, push opt-ins gone. So it is main's `error:` and exit 2, and the
    file is left as it was."""
    home = tmp_path / "store"
    body = '[remote.origin]\nurl = "git@host:me/p.git"\nallow_push = true\n'
    if case == "home-is-a-file":
        home.write_text(body)
        kept = home
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        home.mkdir()
        kept = home / "config.toml"
        kept.write_text(body)
        kept.chmod(0)
    try:
        assert main(["--home", str(home), "init", "pi"]) == 2
    finally:
        kept.chmod(0o600)
    assert capsys.readouterr().err.startswith("error: ")
    assert kept.read_text() == body


def test_cli_init_says_so_when_another_init_wrote_first(tmp_path, capsys, monkeypatch):
    """Two inits at once read the same bytes, and the later rename dropped the
    other's table while both said "added". The other one is run here between
    this one's read and its write: this one now sees the file move, and says so."""
    home = str(tmp_path / "store")
    watches_from = daemon.watches_from

    def racer(*args):
        monkeypatch.setattr(daemon, "watches_from", watches_from)
        assert main(["--home", home, "init", "pi"]) == 0
        return watches_from(*args)

    monkeypatch.setattr(daemon, "watches_from", racer)
    assert main(["--home", home, "init", "claude-code"]) == 1
    assert "changed while init was reading it; run it again" in capsys.readouterr().err
    assert [w.agent for w in daemon.load_watches(home)] == ["pi"]


def test_cli_init_refuses_a_root_another_agent_already_watches(tmp_path, capsys, _no_real_home):
    """Two watches on one root at the same depth: `discover` keeps the first, so
    the table init would add is dead on arrival. Spelled absolute and with a
    trailing slash, because the comparison is the watcher's resolved path."""
    home = tmp_path / "store"
    home.mkdir()
    body = f'[[watch]]\nagent = "pi"\nroots = ["{_no_real_home}/.claude/projects/"]\n'
    (home / "config.toml").write_text(body)
    assert main(["--home", str(home), "init", "claude-code"]) == 1
    assert "already watches ~/.claude/projects as 'pi'" in capsys.readouterr().err
    assert (home / "config.toml").read_text() == body


def test_cli_init_knows_only_adapters_that_ship_and_roots_the_docs_name():
    """`ROOTS` is a third copy of a path the adapter's docstring and agents.md
    already state, so it is checked against both. An agent with no documented
    root is not offered: argparse refuses it as a usage error."""
    from gitmemory.adapters import ADAPTERS

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "docs", "agents.md"), encoding="utf-8") as fh:
        agents = fh.read()
    for agent, root in daemon.ROOTS.items():
        assert agent in ADAPTERS
        assert f"`{root}/**/*.jsonl`" in agents, root
        assert ADAPTERS[agent].__doc__.split(":")[1].strip().startswith(root), agent
    with pytest.raises(SystemExit) as done:
        main(["init", "kimi-code"])
    assert done.value.code == 2


def test_cli_init_leaves_no_temp_the_watcher_would_commit(tmp_path, monkeypatch):
    """`init` publishes `config.toml` by rename, so an init killed between the
    write and the `os.replace` leaves a full copy of the file — push credential
    included — beside it, and the watcher's `git add --all` would commit it.
    Asked of the temp the writer really made, while it exists: a name typed into
    the test passed against a writer that spelled its temp any other way."""
    home = str(tmp_path / "store")
    gitrepo.init(home)
    asked = []
    rename = os.replace

    def spy(src, dst):
        if os.path.basename(dst) == "config.toml":
            rel = os.path.relpath(src, home)
            ask = subprocess.run(["git", "-C", home, "check-ignore", "-q", rel], check=False)
            asked.append((rel, ask.returncode))
        return rename(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    assert main(["--home", home, "init", "pi"]) == 0
    assert [code for _, code in asked] == [0], asked
