"""Two fixtures: one keeps tests out of the developer's home, one budgets I/O.

A test suite for a program that reads transcripts and writes a store has two
ways to escape `tmp_path`, and both are a default rather than a bug in any one
test. `$HOME` is read by `expanduser`, which is how `~/.claude/projects` and
`~/.gitmemory` are resolved; `$GITMEMORY_HOME` is read by `resolve_home`. A
test that forgets to name a root gets the real one, silently, and passes.

This is not hypothetical here. A mutation run against the hook shim wrote 74
files into the developer's actual home before anyone noticed, because a fixture
copied `os.environ` and the shim's fallback is `$HOME/.gitmemory`; that fixture
was fixed in place, and this is the same fix applied once rather than per file.
It is also what makes `find_session`'s required `projects_root` enforceable:
the signature stops a caller from omitting the root, and this stops a caller
from reaching the real one through the environment instead. [E7 S11]

Deliberately not a `monkeypatch.delenv`: unset `HOME` makes `expanduser` fall
through to the password database, which finds the real home anyway. It has to
point somewhere else, and somewhere else has to exist.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GITMEMORY_HOME", raising=False)
    return home


@pytest.fixture
def scandir_budget(monkeypatch):
    """Fail on the `limit + 1`-th directory read, and count them.

    A cost bug needs a cost assertion, and wall-clock is not one: it is flaky
    on a loaded machine, and it reports "slow" for a tree that is merely large.
    Directory reads are what a traversal actually spends and what a symlink
    loop multiplies, so budget those instead. The failure lands on the read
    that busts the budget rather than after a timeout, which is what makes the
    symlink-bomb tests finish in milliseconds against the bombed code.

    Here rather than in one test module because two adapters share the walk
    being budgeted, and a test importing another test module to borrow a helper
    is the shape that makes `-k` selections mysterious.

    Returns a callable: `calls = scandir_budget(16)`, then `calls[0]` after.
    """

    def budget(limit: int) -> list[int]:
        calls = [0]
        real = os.scandir

        def counted(path="."):
            calls[0] += 1
            if calls[0] > limit:
                raise AssertionError(f"more than {limit} directory reads; {path!r} was last")
            return real(path)

        monkeypatch.setattr(os, "scandir", counted)
        return calls

    return budget
