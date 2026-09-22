"""The property `conftest.py` exists for, asserted rather than assumed.

An autouse fixture is invisible at every call site it protects, so the one
thing that can go wrong with it — somebody deletes a line, or narrows its
scope, and every test still passes — is exactly the thing nothing would catch.
This is the negative control for the fixture itself. [E7 S11]
"""

from __future__ import annotations

import os
import pwd


def test_no_test_can_see_the_account_that_is_running_it():
    # `expanduser("~")` would be vacuous: it reads `$HOME` first, so it agrees
    # with whatever the fixture set. The password database is the copy the
    # fixture cannot reach, and it is also the copy `expanduser` falls back to
    # when `HOME` is unset — which is why the fixture sets it rather than
    # deleting it.
    assert os.environ["HOME"] != pwd.getpwuid(os.getuid()).pw_dir
    assert "GITMEMORY_HOME" not in os.environ
