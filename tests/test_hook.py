"""E4: conformance and durability tests for the POSIX sh hook shim.

The shim is the entry point for all capture triggers. It must never block the
user session, never crash, write data atomically, and consume minimal resources.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from gitmemory import daemon

SHIM_PATH = Path(__file__).parent.parent / "hook" / "gitmemory-hook.sh"


def run_shim_cmd() -> list[str]:
    """The shebang's interpreter, which is the one production uses.

    This preferred `dash` when installed, and that is the wrong shell: the shim
    says `#!/bin/sh`, and an agent firing a hook gets whatever `/bin/sh` is on
    that machine — here, bash 3.2.57 in posix mode. So every behaviour test in
    this file, and every number in `hook/README.md`, was measured under an
    interpreter that never runs the shim. The reason it was there — catching a
    bashism — is already covered, and covered better, by the parse test below,
    which sweeps every shell on the box instead of silently picking one.
    [E4, review: shell MAJOR]
    """
    return [str(SHIM_PATH)]


@pytest.fixture
def clean_env(tmp_path):
    """An isolated environment, including `HOME`.

    `os.environ.copy()` alone carries the real `HOME` in, and the shim falls
    back to `$HOME/.gitmemory` on any path where `GITMEMORY_HOME` is refused or
    unset. A mutation run that broke the refusal wrote 74 files into the
    developer's actual home directory before anyone noticed. A test for a shim
    whose whole job is writing files should not be able to write outside
    `tmp_path`, so the fallback now lands inside it too. [E4, review: F1]
    """
    env = os.environ.copy()
    home = tmp_path / "gitmemory_home"
    env["GITMEMORY_HOME"] = str(home)
    env["HOME"] = str(tmp_path / "fallback_home")
    return env, home


def test_exits_0_unconditionally(clean_env):
    """The user session must never be blocked or crashed by the shim failing.

    We test failure modes including unwritable directories and closed stdin,
    guaranteeing that the shim shields the calling agent from filesystem errors.
    """
    env, home = clean_env
    # 1. Unwritable spool directory
    unwritable = home / "unwritable"
    unwritable.mkdir(parents=True, exist_ok=True)
    os.chmod(unwritable, 0o000)

    env_unwritable = env.copy()
    env_unwritable["GITMEMORY_HOME"] = str(unwritable)

    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env_unwritable,
        input=b"test payload",
        capture_output=True,
    )
    assert res.returncode == 0, "shim must exit 0 even if spool is unwritable"

    # Restore permission so tmp_path cleanup works
    os.chmod(unwritable, 0o700)

    # 2. Closed stdin
    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    assert res.returncode == 0, "shim must exit 0 even if stdin is empty/DEVNULL"


@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "ksh", "zsh"])
def test_the_shim_parses_under_every_posix_shell_on_this_machine(shell):
    """`-n`: parse, do not run. The shim runs under whatever `/bin/sh` is there.

    This replaces a test that scanned the shim's text for the substring `"git"`.
    That test did real damage: to get past it the shim had been written as
    `_g="gi"; _t="t"` and `.${_g}${_t}memory`, splitting its own default path
    across two variables so a grep would not see it. A reader could no longer
    tell where the store lived. The property that test was reaching for — the
    shim spawns no interpreter — is not a property of its spelling, and it is
    measured for real in `tools/hook_latency.py`. [E4]
    """
    if not (found := shutil.which(shell)):
        pytest.skip(f"{shell} is not installed")
    res = subprocess.run([found, "-n", str(SHIM_PATH)], capture_output=True)
    assert res.returncode == 0, res.stderr.decode()


def test_a_relative_home_is_refused_rather_than_resolved(tmp_path):
    """Two processes, two working directories, two different spools.

    The agent sets the hook's cwd to the project the user is in; the watcher
    resolves `GITMEMORY_HOME` against its own. A relative value therefore makes
    a spool under every directory the user visits and the watcher drains none of
    them — while littering their checkouts. Reproduced, then closed. [E4]
    """
    (tmp_path / "projX").mkdir()
    env = os.environ.copy()
    env["GITMEMORY_HOME"] = "relstore"
    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        cwd=str(tmp_path / "projX"),
        input=b"payload",
        capture_output=True,
    )
    assert res.returncode == 0, "refusing is still not a reason to fail the session"
    assert b"absolute" in res.stderr
    assert not (tmp_path / "projX" / "relstore").exists()


def test_writes_payload_verbatim(clean_env):
    """The shim must preserve binary integrity, accepting non-UTF-8 and non-JSON data.

    Because the shim acts as a blind pass-through, it must write exactly the bytes
    it receives on stdin, avoiding any encoding or parsing errors.
    """
    env, home = clean_env
    payload = b"\xff\x00\xde\xad\xbe\xef\nnon-json-line\x00"

    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        input=payload,
        capture_output=True,
    )
    assert res.returncode == 0

    spool_dir = home / "spool"
    files = list(spool_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == payload, "spooled payload must match input verbatim"


def test_never_writes_outside_the_spool(clean_env):
    """The shim must restrict all side-effects strictly to the spool directory.

    We assert that only the 'spool' folder is created under GITMEMORY_HOME,
    preventing any pollution of other directories in the workspace.
    """
    env, home = clean_env
    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        input=b"data",
        capture_output=True,
    )
    assert res.returncode == 0

    items = list(home.iterdir())
    assert len(items) == 1
    assert items[0].name == "spool"


def test_concurrent_fires_do_not_collide(clean_env):
    """Multiple concurrent hooks must never clobber each other's data.

    Uniqueness is guaranteed by using the process ID and a collision counter. We
    launch 50 shims at once and verify all 50 payloads are captured perfectly
    with zero leftover temp files.
    """
    env, home = clean_env
    count = 50
    processes = []

    for i in range(count):
        payload = f"payload-{i}".encode()
        p = subprocess.Popen(
            run_shim_cmd() + ["PreCompact"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append((p, payload))

    for p, payload in processes:
        assert p.stdin is not None
        p.stdin.write(payload)
        p.stdin.close()

    for p, _ in processes:
        p.wait()
        assert p.returncode == 0

    spool_dir = home / "spool"
    files = list(spool_dir.glob("*.json"))
    assert len(files) == count, f"expected {count} spooled files, found {len(files)}"

    temps = list(spool_dir.glob(".tmp*"))
    assert len(temps) == 0, f"expected 0 leftover temp files, found {len(temps)}"

    payloads_found = {f.read_bytes() for f in files}
    expected_payloads = {f"payload-{i}".encode() for i in range(count)}
    assert payloads_found == expected_payloads


def test_kill_9_mid_write_leaves_no_complete_file(clean_env):
    """An aborted or killed shim must never publish a partial transcript.

    By keeping the stdin pipe open, we force the shim's `cat` to wait. We then
    kill it with SIGKILL and verify that only a `.tmp-` file is left, but no
    complete `.json` file is visible.
    """
    env, home = clean_env
    p = subprocess.Popen(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Write a partial payload but keep the pipe open so `cat` remains blocked
    assert p.stdin is not None
    p.stdin.write(b"partial data")
    p.stdin.flush()

    # Wait for the temp file to appear rather than sleeping a guessed interval.
    # A fixed 0.05s was a coin flip on a loaded machine: too early and there is
    # no temp file to assert on, and the durability gate fails for a reason that
    # has nothing to do with durability. [E4]
    spool_dir = home / "spool"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(spool_dir.glob(".tmp*")) and any(f.stat().st_size for f in spool_dir.glob(".tmp*")):
            break
        time.sleep(0.005)
    else:
        pytest.fail("the shim never began writing its temp file")

    # Simulate kill -9 (SIGKILL)
    p.kill()
    p.wait()

    complete_files = list(spool_dir.glob("*.json"))
    assert len(complete_files) == 0, "partial payload must never be renamed to a complete file"

    temps = list(spool_dir.glob(".tmp*"))
    assert len(temps) == 1, "abandoned temp file should remain for the sweeping daemon"
    assert temps[0].read_bytes() == b"partial data", "temp file contains partial data"


def test_unknown_event_becomes_unknown(clean_env):
    """Unrecognized hook argv[1] events must fallback to 'unknown' rather than fail.

    Even if an agent sends a telemetry event we do not recognize, we should still
    spool it to capture all active conversation signals.
    """
    env, home = clean_env
    res = subprocess.run(
        run_shim_cmd() + ["WeirdEvent"],
        env=env,
        input=b"weird payload",
        capture_output=True,
    )
    assert res.returncode == 0

    spool_dir = home / "spool"
    files = list(spool_dir.glob("*.json"))
    assert len(files) == 1
    assert "-unknown.json" in files[0].name, (
        f"filename {files[0].name} should end with unknown.json"
    )


def test_a_home_with_spaces_in_it_works(tmp_path):
    """Every path variable is quoted, or word-splitting picks the store apart."""
    home = tmp_path / "space dir with spaces"
    home.mkdir()
    env = os.environ.copy()
    env["GITMEMORY_HOME"] = str(home)

    res = subprocess.run(
        run_shim_cmd() + ["PreCompact"], env=env, input=b"spaced payload", capture_output=True
    )
    assert res.returncode == 0
    files = list((home / "spool").glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"spaced payload"


def test_with_no_variable_set_the_spool_lands_under_the_documented_default(clean_env, tmp_path):
    """`$HOME/.gitmemory`, which is the path the README tells people to expect.

    Untested until now, and it is the branch every first-time user takes. It is
    also what makes `clean_env`'s `HOME` override load-bearing rather than
    decorative: a test that exercises the fallback while pointing at a real home
    directory writes into it. One did, 74 files' worth, during a mutation run
    that broke the refusal of a relative `GITMEMORY_HOME`. [E4, review: F1]
    """
    env, _ = clean_env
    del env["GITMEMORY_HOME"]
    fallback = tmp_path / "fallback_home"

    res = subprocess.run(run_shim_cmd() + ["PreCompact"], env=env, input=b"x", capture_output=True)

    assert res.returncode == 0, res.stderr.decode()
    (record,) = (fallback / ".gitmemory" / "spool").glob("*.json")
    assert record.read_bytes() == b"x"
    # And nowhere else. The fixture's tmp_path is the containment boundary, so a
    # fallback that escaped it would have to land outside this tree.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fallback_home"]


def test_a_tilde_in_the_variable_is_expanded(tmp_path):
    """`GITMEMORY_HOME=~/store` in a config file arrives as a literal tilde.

    The shell that set it expanded nothing, because nothing was unquoted at the
    point it was set. Without this branch the shim makes a directory literally
    named `~`. The branch existed; nothing tested it. [E4]
    """
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["GITMEMORY_HOME"] = "~/store"

    res = subprocess.run(
        run_shim_cmd() + ["SessionEnd"], env=env, input=b"tilde payload", capture_output=True
    )
    assert res.returncode == 0, res.stderr.decode()
    assert not (tmp_path / "~").exists()
    files = list((tmp_path / "store" / "spool").glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"tilde payload"


@pytest.mark.parametrize("event", ["PreCompact", "SessionEnd", "Stop"])
def test_each_accepted_event_lands_in_the_filename(clean_env, event):
    """The watcher reads the event out of the name; a wrong name is a lost boundary.

    Asked through `_event_of`, the reader that actually runs, rather than by
    re-splitting the name here. The copy said `split("-")[2]`, which was the
    grammar of the day and broke the moment the `date` fork came off the hot
    path — while the real reader, which finds the event rather than counting to
    it, carried on fine. A conformance test that reimplements the thing it is
    confirming tests the reimplementation. [E4, review: shell MAJOR]
    """
    env, home = clean_env
    res = subprocess.run(run_shim_cmd() + [event], env=env, input=b"x", capture_output=True)
    assert res.returncode == 0
    (name,) = [f.name for f in (home / "spool").glob("*.json")]
    assert daemon._event_of(name) == event


def test_a_name_that_is_already_taken_does_not_clobber_it(clean_env):
    """The collision counter, which 50 concurrent shims never reach.

    50 processes have 50 pids, so their names differ on the pid and the `-<n>`
    suffix is never taken — the concurrency test above proves uniqueness it
    never has to work for. The suffix is taken when the name a shim picks
    already exists, and the daemon had a real bug on exactly that path: it read
    the event out of the *last* field, so a suffixed `PreCompact` record parsed
    as event `1` and silently stopped forcing a capture. Untested code on both
    sides of one seam. [E4]

    Forced rather than raced: the shim blocks in `cat` until stdin closes and
    picks its name only afterwards, and the shebang execs in the process we
    started, so its pid is `p.pid` and the name it is about to choose is
    predictable. Two are planted so the suffix loop has to step past its first
    try rather than landing on `-1` by default.

    The planted names are the shim's current grammar, and for one commit they
    were not. Dropping the `date` prefix left this test planting
    `<epoch>-<pid>-PreCompact.json`, which the shim no longer picks — so nothing
    collided, the suffix loop never ran, and the test went on passing while
    testing nothing. Its end-to-end twin failed loudly on the same change and
    got fixed; this one did not, because the assertion it happened to break on
    had just been rewritten to ask `_event_of` instead of splitting the name.
    A collision test has to assert that the collision happened.
    [E4, review: shell MAJOR]
    """
    env, home = clean_env
    spool = home / "spool"
    p = subprocess.Popen(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert p.stdin is not None
    spool.mkdir(parents=True, exist_ok=True)
    taken = [spool / f"{p.pid}-PreCompact.json", spool / f"{p.pid}-PreCompact-1.json"]
    for path in taken:
        path.write_bytes(b"ALREADY HERE")

    p.stdin.write(b"the new payload")
    p.stdin.close()
    assert p.wait() == 0

    for path in taken:
        assert path.read_bytes() == b"ALREADY HERE", "an existing record was overwritten"
    (suffixed,) = [f for f in spool.glob("*.json") if f not in taken]
    assert suffixed.read_bytes() == b"the new payload"
    assert suffixed.name == f"{p.pid}-PreCompact-2.json", "the collision was never forced"
    # ...and the watcher can still tell what event it was, collision suffix and all.
    assert daemon._event_of(suffixed.name) == "PreCompact"


def test_a_symlink_planted_at_the_record_name_is_not_written_through(clean_env):
    """`set -C` and the `[ -h ]` tests, which nothing exercised.

    The spool sits under a home directory the shim creates with `umask 077`, so
    this is a defence in depth rather than the front door — but the shim is the
    one part of gitmemory that runs inside someone else's agent, with their
    environment, and a spool directory that is group- or world-writable for any
    local reason turns both candidate paths into symlink targets. A plain
    `cat > "$T"` follows a symlink and writes through it; `[ -e ]` alone does
    not see a dangling one, which is the case that matters, because a link to a
    file that does not exist yet is exactly how you get the shim to create it.

    The two names are not covered by the same thing, and writing this test as
    though they were is how that came out. On the `.tmp-` the guard is real and
    the negative control confirms it: strip `[ -h ]` and the payload goes
    through the link into the victim file. On the published record there is no
    write-through to prevent — `mv` is `rename(2)`, which replaces the symlink
    rather than following it — so `[ -h "$F" ]` buys something smaller and worth
    naming honestly: the shim does not silently destroy a name it did not
    create. Both are asserted below, each for what it actually does.

    `set -C` is a third guard and this test cannot reach it, because the `[ -h ]`
    loop has already moved off any planted name by the time `cat` runs. It
    covers the window between that test and the write, which is a race, not a
    state. Left in and left untested on purpose. [E4, review: F2]
    """
    env, home = clean_env
    spool = home / "spool"
    spool.mkdir(parents=True)
    target = home / "victim"

    p = subprocess.Popen(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert p.stdin is not None
    (spool / f".tmp-{p.pid}-0").symlink_to(target)
    (spool / f"{p.pid}-PreCompact.json").symlink_to(target)

    p.stdin.write(b"the payload")
    p.stdin.close()
    assert p.wait() == 0, "a planted link is still not a reason to fail the session"

    assert not target.exists(), "the shim wrote through the temp symlink"
    assert (spool / f"{p.pid}-PreCompact.json").is_symlink(), "the shim clobbered a link"
    written = [f for f in spool.glob("*.json") if not f.is_symlink()]
    assert [f.read_bytes() for f in written] == [b"the payload"]
