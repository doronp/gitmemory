# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""E4: conformance and durability tests for the POSIX sh hook shim.

The shim is the entry point for all capture triggers. It must never block the
user session, never crash, write data atomically, and consume minimal resources.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
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


def _sh_reads_shellopts() -> bool:
    """Whether `/bin/sh` imports `SHELLOPTS` from the environment.

    bash does, even in posix mode, which is what `/bin/sh` is on macOS. dash,
    `/bin/sh` on Debian and Ubuntu, ignores it, so there is no verbose door to
    measure there and the line count below has nothing to count.
    """
    env = {"PATH": os.environ.get("PATH", ""), "SHELLOPTS": "verbose"}
    res = subprocess.run(["/bin/sh", "-c", ":"], env=env, capture_output=True)
    return bool(res.stderr)


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

    # `finally`, because the restore used to sit after the assertion and so ran
    # only when the assertion passed. A run that failed here — any `-x` mutation
    # pass, which is how this was found — left a mode-000 directory under
    # pytest's shared basetemp that `rm_rf` cannot enter, and pytest warns about
    # it on every session afterwards. Cleanup that only happens on the happy
    # path is not cleanup. [E4, review: vacuity audit]
    try:
        res = subprocess.run(
            run_shim_cmd() + ["PreCompact"],
            env=env_unwritable,
            input=b"test payload",
            capture_output=True,
        )
        assert res.returncode == 0, "shim must exit 0 even if spool is unwritable"
    finally:
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
    # fallback that escaped it would have to land outside this tree. Stated as
    # "one store, and it is that one" rather than as the directory listing it
    # was: `conftest`'s autouse isolation also lives under `tmp_path` now, and a
    # listing would have to be edited every time something else does. [E7 S11]
    assert [p.parent for p in tmp_path.rglob(".gitmemory")] == [fallback]


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
    though they were is how that came out. On the published record there is no
    write-through to prevent — `mv` is `rename(2)`, which replaces the symlink
    rather than following it — so `[ -h "$F" ]` buys something smaller and worth
    naming honestly: the shim does not silently destroy a name it did not
    create. Both are asserted below, each for what it actually does.

    The `.tmp-` name has two guards, not one, and the version of this docstring
    written first credited the wrong one. Measured 2×2, dangling link planted at
    both names:

        [ -h ] and set -C   victim untouched
        set -C alone        victim untouched   <- so `[ -h ]` is not what holds it
        [ -h ] alone        victim untouched
        neither             victim written

    `set -C` makes the redirect `O_CREAT|O_EXCL`, which fails on a dangling
    symlink outright, so it closes the same case the `[ -h ]` loop does — and it
    additionally closes the window *between* that test and the write, which is a
    race no state test can reach. The old docstring called it "left in and left
    untested on purpose"; it was in fact the load-bearing one, and untested only
    because this test cannot reach it while `[ -h ]` is present. The sibling
    below runs a stripped shim so that it can. [E4, review: F2 / CLI 5]
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


def test_set_c_alone_stops_the_write_through(clean_env, tmp_path):
    """The other half of the guard above, reached by removing the half in front of it.

    While `[ -h ]` is in the shim the loop has already moved off any planted
    name by the time `cat` runs, so no test of the shipped file can tell whether
    `set -C` does anything. That is how it came to be described as untested on
    purpose, which was a guess, and the wrong one. Run a copy with the two
    `|| [ -h ... ]` clauses deleted and `set -C` is the only thing left between
    a dangling link and the victim — measured writing through the moment both
    are gone, and not writing through here.

    A stripped copy rather than a mutation of the real file: this is a test of
    what a guard is worth, and the shipped shim keeps both. [E4, review: CLI 5]
    """
    env, home = clean_env
    stripped, n = re.subn(r' \|\| \[ -h "[^"]+" \]', "", SHIM_PATH.read_text())
    assert n == 2, f"the shim's `[ -h ]` guards moved; found {n}"
    variant = tmp_path / "no-h-guard.sh"
    variant.write_text(stripped)
    variant.chmod(0o755)

    spool = home / "spool"
    spool.mkdir(parents=True)
    target = home / "victim"

    p = subprocess.Popen(
        [str(variant), "PreCompact"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert p.stdin is not None
    (spool / f".tmp-{p.pid}-0").symlink_to(target)
    p.stdin.write(b"the payload")
    p.stdin.close()
    assert p.wait() == 0, "a planted link is still not a reason to fail the session"

    assert not target.exists(), "`set -C` did not stop the write through the temp symlink"
    # And the record is still lost rather than mis-filed: nothing on the way
    # out, because the one name the shim would have used was taken by a link.
    assert [f for f in spool.glob("*.json") if not f.is_symlink()] == []


# Each of these is a way to edit the agent's terminal from inside the one value
# the shim quotes back. The first was the only one the original `tr -d
# '\000-\037'` removed; the other four are what an allowlist is for, and the
# last three are multi-byte, which is why a `tr` denylist could never have been
# extended to cover them. [E7 S7]
TERMINAL_EDITS = {
    "ESC": "rel\033[2K\rgitmemory: everything is fine",
    "DEL": "rel\177x",
    "C1 CSI": "rel2K",
    "bidi override": "rel\u202etxt.exe",
    "line separator": "rel gitmemory: everything is fine",
}


@pytest.mark.parametrize("what", sorted(TERMINAL_EDITS))
def test_a_refusal_cannot_rewrite_the_agents_terminal(clean_env, what):
    """The shim's one message that quotes a value, with an edit in the value.

    `GITMEMORY_HOME` is the user's own variable, so this is not a privilege
    boundary — but the shim's stderr lands in somebody else's agent transcript,
    and `\\033[2K\\r` there does not print, it erases the warning line and
    substitutes whatever follows it. The Python side holds itself to exactly
    this standard (`records.safe_text`); the shim is the half that runs inside
    another program. [E4, review: CLI 6]

    Parametrised because the fix was a denylist of C0 and the standard it
    claimed parity with is much wider. Measured against the shipped version,
    DEL, U+009B, U+202E and U+2028 all reached stderr intact. [E7 S7]
    """
    env, _ = clean_env
    env["GITMEMORY_HOME"] = TERMINAL_EDITS[what]
    out = subprocess.run(
        run_shim_cmd() + ["PreCompact"],
        env=env,
        input=b"",
        capture_output=True,
    )
    assert out.returncode == 0
    assert out.stdout == b""
    assert out.stderr.startswith(b"gitmemory: GITMEMORY_HOME must be an absolute path")
    # Printable ASCII and the trailing newline, and nothing else. Stated as the
    # allowlist rather than as a list of the five sequences above, so a sixth
    # way in is a failure here without anyone having thought of it first.
    body = out.stderr.rstrip(b"\n")
    assert all(0x20 <= b <= 0x7E for b in body), out.stderr
    # And it still says what was refused: a message that drops the value is
    # safe and useless, and this is the one place a user learns what was set.
    assert b"'rel" in out.stderr, out.stderr


def test_a_file_size_limit_does_not_put_the_shells_own_noise_in_the_transcript(clean_env):
    """`ulimit -f` in the environment, and a payload over the limit. [E7 S8]

    The shim's redirects are around the write, and this message is not: the
    shell prints it when it reaps the child, after `cat` has died on SIGXFSZ.
    Measured before the fix, `gitmemory-hook.sh: line 93: 20074 Filesize limit
    exceeded: 25   cat 2> /dev/null > "$T"` — script path, line number and all
    — went straight into the agent's stderr.

    Set through `sh -c` rather than `preexec_fn`, because `ulimit` is how a user
    or a CI image actually sets this and the point is the shell's behaviour.
    """
    env, home = clean_env
    res = subprocess.run(
        ["/bin/sh", "-c", 'ulimit -f 1; exec "$0" PreCompact', str(SHIM_PATH)],
        env=env,
        input=b"x" * 4_000_000,
        capture_output=True,
    )
    assert res.returncode == 0
    assert res.stderr == b"", res.stderr.decode()
    assert res.stdout == b""
    # The doorbell is lost, which is the cost the header owns up to, and the
    # partial file is not left behind pretending to be one.
    assert list((home / "spool").iterdir()) == []


def test_the_shim_survives_being_run_under_nounset(clean_env):
    """`SHELLOPTS=nounset` in the environment, and no `argv[1]`.

    The shim does not set `-u` itself, so this looked like someone else's
    problem. It is not: `SHELLOPTS` is exported by some setups, `/bin/sh` here
    is bash, and bash honours it in a script it starts. Measured before the fix,
    the shim died on `[ "$1" = "PreCompact" ]` with `$1: unbound variable`,
    which is both halves of the failure — the doorbell is lost *and* the noise
    lands in the agent's stderr, from a shim whose entire contract is to do
    neither. Every expansion now carries a `:-` default. [E4, review]
    """
    env, home = clean_env
    env["SHELLOPTS"] = "nounset"

    res = subprocess.run(run_shim_cmd(), env=env, input=b"no argv", capture_output=True)

    assert res.returncode == 0, res.stderr.decode()
    assert res.stderr == b"", res.stderr.decode()
    (record,) = (home / "spool").glob("*.json")
    assert record.read_bytes() == b"no argv"
    # Unnamed events are `unknown`, not a crash and not a guess.
    assert daemon._event_of(record.name) == "unknown"


def test_the_shim_is_silent_under_xtrace_and_does_not_echo_the_home_it_was_given(clean_env):
    """`SHELLOPTS=xtrace` is the nounset door one option over, and it is worse.

    Trace goes to fd 2, which is the agent's stderr, and it prints the value of
    every assignment — including `H`, which is `$GITMEMORY_HOME`. S7 built an
    ASCII allowlist for the one message that echoes that variable; xtrace prints
    it three lines earlier, raw. Measured before the fix: 700 bytes of trace per
    compaction, with `ESC [ 2 K \\r` reaching the terminal byte for byte.

    The value here is the refusal case on purpose — a relative path is when a
    user's `GITMEMORY_HOME` is most likely to be something odd, and it is the
    path that ends in the sanitised `echo`, so the assertion below is that the
    sanitised message is the *only* thing written. [E7 pair review]
    """
    env, home = clean_env
    env["SHELLOPTS"] = "xtrace"
    env["GITMEMORY_HOME"] = "relative\x1b[2K\rHIJACKED"

    res = subprocess.run(run_shim_cmd(), env=env, input=b"{}", capture_output=True)

    assert res.returncode == 0
    assert res.stderr.count(b"\n") == 1, res.stderr
    assert b"\x1b" not in res.stderr, res.stderr
    assert res.stderr.startswith(b"gitmemory: GITMEMORY_HOME must be an absolute path"), res.stderr

    # And the accepted path says nothing at all, which is where 700 bytes of it
    # would otherwise land: the refusal above is one fire, this is every fire.
    env["GITMEMORY_HOME"] = str(home)
    quiet = subprocess.run(run_shim_cmd(), env=env, input=b"{}", capture_output=True)
    assert quiet.stderr == b"", quiet.stderr
    assert len(list((home / "spool").glob("*.json"))) == 1


@pytest.mark.skipif(not _sh_reads_shellopts(), reason="/bin/sh ignores SHELLOPTS (dash)")
def test_under_verbose_the_shim_echoes_two_lines_because_the_guard_is_the_second(clean_env):
    r"""`set +v` narrows the verbose door; *where* it sits is what closes it.

    `xtrace` prints commands as they run, so a guard anywhere before the first
    command covers everything. `verbose` prints input as the shell *reads* it,
    so every line above the guard is already on stderr — including the comment
    explaining the guard. Measured with the guard under its own comment block:
    1,723 bytes at every compaction, all of it this file's prose. On line 2 it
    is 90, which is the shebang and the guard line and nothing else.

    Nothing is disclosed either way — verbose echoes source text, so
    `H="${GITMEMORY_HOME:-$HOME/.gitmemory}"` prints unexpanded, which is why
    this is Low and not the xtrace finding above. It is the noise the shim's
    opening paragraph promises not to make, growing by a line every time
    somebody explains something above the guard, so the count is what is pinned
    here rather than a byte budget: one more comment line above it is one more
    line on the agent's stderr. [E7b L2-F6]
    """
    env, home = clean_env
    env["SHELLOPTS"] = "verbose"

    res = subprocess.run(run_shim_cmd(), env=env, input=b"{}", capture_output=True)

    assert res.returncode == 0
    assert len(list((home / "spool").glob("*.json"))) == 1, "the record is still written"
    assert res.stderr.count(b"\n") == 2, res.stderr
    assert res.stderr.startswith(b"#!/bin/sh\n{ set +xv; }"), res.stderr
    assert b"umask" not in res.stderr, "the guard let the body through"


def test_with_no_home_and_no_absolute_override_the_shim_refuses_out_loud(tmp_path):
    """`HOME` unset is a disagreement between the two ends of the seam.

    The default is `$HOME/.gitmemory`, so with `HOME` unset it expands to
    `/.gitmemory` — absolute, so the existing refusal waves it through — and
    then fails to `mkdir` and exits 0 saying nothing. The watcher resolving the
    same default goes through `expanduser`, which falls back to the password
    database and finds the real home. So the hook writes nowhere, the watcher
    reads somewhere else, and neither says a word about it.

    Refused with a message instead, on this project's own rule that loud and
    degraded beats silent and degraded. An absolute `GITMEMORY_HOME` does not
    need `HOME` at all and is unaffected — asserted below, because a refusal
    that fires too widely would break the one configuration that is fine.
    [E4, review]
    """
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "GITMEMORY_HOME")}

    res = subprocess.run(run_shim_cmd() + ["PreCompact"], env=env, input=b"x", capture_output=True)

    assert res.returncode == 0
    assert b"HOME unset" in res.stderr
    assert not Path("/.gitmemory").exists(), "the shim wrote to the filesystem root"

    env["GITMEMORY_HOME"] = str(tmp_path / "store")
    ok = subprocess.run(run_shim_cmd() + ["PreCompact"], env=env, input=b"y", capture_output=True)
    assert ok.returncode == 0, ok.stderr.decode()
    assert ok.stderr == b"", "an absolute home needs no HOME and must not be refused"
    (record,) = (tmp_path / "store" / "spool").glob("*.json")
    assert record.read_bytes() == b"y"


def test_a_refused_write_is_silent(clean_env):
    """A failed `cat >` must not put the shell's own error in the agent's output.

    Redirections apply left to right, so `cat > "$T" 2>/dev/null` silences a
    stderr the shell has already used: it reports the refused open first, on the
    original stderr, and only then swaps it for `/dev/null`. The failure branch
    always handled the failure correctly — `rm -f`, exit 0 — so the sole symptom
    was a line of noise, which is the one thing this shim promises not to emit.

    Forced with a read-only spool rather than with `set -C`: noclobber only
    fires in the window between the `[ -e ]` test and the write, which is a race
    no test can hold open. `EACCES` takes the same branch through the same
    redirect and is deterministic. [E4, review]
    """
    env, home = clean_env
    spool = home / "spool"
    spool.mkdir(parents=True)
    os.chmod(spool, 0o500)
    try:
        res = subprocess.run(
            run_shim_cmd() + ["PreCompact"], env=env, input=b"x", capture_output=True
        )
    finally:
        os.chmod(spool, 0o700)

    assert res.returncode == 0
    assert res.stderr == b"", res.stderr.decode()
    assert list(spool.iterdir()) == [], "a refused write left something behind"


def test_the_claude_code_plugin_runs_this_shim_with_the_event_it_registers(clean_env):
    """`hooks/hooks.json` is a second copy of the install example, and copies drift.

    Each registered command is run here the way Claude Code runs it: the plugin
    root substituted for `${CLAUDE_PLUGIN_ROOT}`, then `command` spawned with
    `args` and no shell. Run, not compared, because what the watcher needs is the
    event arriving in the record name; one that does not arrive is filed as
    `unknown` and forces nothing. The events are exactly the two that force a
    capture, so `Stop` stays out for the reason `hook/README.md` gives, and no
    group has a matcher that could skip one. The git mode is checked because a
    plugin installed from GitHub comes from a clone: the executable bit it gets
    is git's, not this working tree's. `plugin.json` pins installed copies to
    its version until that changes, so it moves with `pyproject.toml` or a
    release reaches nobody who installed the plugin. And the marketplace entry
    is pinned twice over: its `source` of `./` is what makes the repository
    root the plugin root substituted below, and its name and the marketplace's
    make the install id both READMEs tell you to type.
    """
    env, home = clean_env
    root = SHIM_PATH.parent.parent
    hooks = json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    with open(root / "pyproject.toml", "rb") as fh:
        assert manifest["version"] == tomllib.load(fh)["project"]["version"]
    assert market["plugins"] == [{"name": manifest["name"], "source": "./"}]
    install_id = f"{manifest['name']}@{market['name']}"
    assert install_id == "gitmemory@gitmemory"
    for readme in (root / "README.md", root / "hook" / "README.md"):
        assert install_id in readme.read_text(), readme
    staged = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "--", "hook/gitmemory-hook.sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged.startswith("100755 "), staged

    assert set(hooks) == daemon.FORCING
    for event, groups in hooks.items():
        ((handler,),) = [group["hooks"] for group in groups]
        assert groups == [{"hooks": [handler]}], "a matcher would skip some events"
        assert handler["type"] == "command"
        command = handler["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(root))
        assert Path(command) == SHIM_PATH
        res = subprocess.run([command, *handler["args"]], env=env, input=b"{}")
        assert res.returncode == 0
        (record,) = (home / "spool").glob("*.json")
        assert daemon._event_of(record.name) == event
        record.unlink()
