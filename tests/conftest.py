"""One autouse fixture: no test can reach the developer's own home.

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

import pytest


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GITMEMORY_HOME", raising=False)
    return home
