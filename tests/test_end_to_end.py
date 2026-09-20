"""The whole path, with nothing stubbed: agent -> shim -> spool -> watcher -> git. [E4]

Every other test in E4 exercises one half. `test_hook.py` runs the real shim and
stops at the spool; `test_daemon.py` starts at a spool file it wrote itself and
runs the real watcher. Both pass with a seam between them that does not line up
— the shim's filename grammar and the watcher's parse of it are agreed on only
by two tests that never meet.

So these run the actual `hook/gitmemory-hook.sh` binary, with the payload shape
Claude Code actually sends, and then the actual watcher over the actual spool,
and then ask git what landed. No fixtures, no monkeypatching, no fakes.

The three properties the ship gate asks for, in order:

1. a compaction captured through the hook is in the store and in a commit;
2. a hook killed mid-write loses nothing — not the segment, not the store;
3. concurrent hooks do not clobber each other and all of them arrive.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from gitmemory import daemon, gitrepo, store

SHIM = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook", "gitmemory-hook.sh"
)


def _shim_cmd() -> list[str]:
    """The shebang's interpreter — see `tests/test_hook.py`. [E4, review: shell MAJOR]"""
    return [SHIM]


def _transcript(path: str, turns: int, *, start: int = 0) -> str:
    """A Claude Code transcript: one JSON object per line, appended to."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for i in range(start, start + turns):
            fh.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": f"u{i}",
                        "sessionId": "s-e2e",
                        "message": {"role": "user", "content": f"turn {i}"},
                    }
                )
                + "\n"
            )
    return path


def _fire(home: str, event: str, transcript: str, **extra):
    """Run the real shim exactly as an agent would: payload on stdin, event in argv."""
    payload = {"session_id": "s-e2e", "transcript_path": transcript, "hook_event_name": event}
    payload.update(extra)
    env = os.environ.copy()
    env["GITMEMORY_HOME"] = home
    res = subprocess.run(
        _shim_cmd() + [event],
        env=env,
        input=json.dumps(payload).encode(),
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return res


def _store(home: str, root) -> None:
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write(f'[[watch]]\nagent = "claude-code"\nroots = [{json.dumps(str(root))}]\n')
    gitrepo.init(home)


def _sweep(home: str) -> daemon.Tick:
    return daemon.tick(home, daemon.load_watches(home), interval=0)


@pytest.fixture
def world(tmp_path):
    """A store and a project directory, wired to each other and to nothing else."""
    home = str(tmp_path / "store")
    proj = tmp_path / "proj"
    _store(home, proj)
    return home, proj


# --- 1. the happy path, end to end -------------------------------------------


def test_a_compaction_fired_through_the_real_shim_lands_in_a_real_commit(world):
    home, proj = world
    src = _transcript(str(proj / "s-e2e.jsonl"), 5)

    _fire(home, "PreCompact", src)
    spooled = os.listdir(os.path.join(home, daemon.SPOOL))
    assert len(spooled) == 1 and spooled[0].endswith(".json"), spooled

    result = _sweep(home)

    assert result.errors == []
    assert result.appended == os.path.getsize(src)
    assert result.commit, "a capture with bytes in it must produce a commit"
    assert result.spool_consumed == 1
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == [], "the record was not consumed"

    # The bytes are addressable and they are the bytes that were on disk.
    (stored,) = store.sessions(home)
    with open(src, "rb") as fh:
        assert store.span(stored, 0, result.appended) == fh.read()
    assert store.verify(home) == []

    # And git agrees. Asked of git directly rather than read back off the Tick.
    log = subprocess.run(
        ["git", "-C", home, "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": home, "GIT_CONFIG_GLOBAL": "/dev/null"},
    ).stdout
    assert result.commit[:7] in log


def test_the_watcher_alone_captures_the_same_bytes_with_no_hook_at_all(world):
    """The load-bearing claim of the whole design, stated as a test.

    If this passes, the shim is a latency optimisation and a user who never
    installs it has a correct system. If it ever fails, the hook has quietly
    become the capture path and the README is lying.
    """
    home, proj = world
    src = _transcript(str(proj / "s-e2e.jsonl"), 5)

    result = _sweep(home)  # no `_fire` — nothing ever rang the doorbell

    assert result.appended == os.path.getsize(src)
    assert result.commit
    (stored,) = store.sessions(home)
    with open(src, "rb") as fh:
        assert store.span(stored, 0, result.appended) == fh.read()


def test_a_second_compaction_appends_rather_than_rewriting(world):
    home, proj = world
    src = _transcript(str(proj / "s-e2e.jsonl"), 3)
    _fire(home, "PreCompact", src)
    first = _sweep(home)

    _transcript(src, 4, start=3)
    _fire(home, "PreCompact", src)
    second = _sweep(home)

    assert second.appended == os.path.getsize(src) - first.appended
    (stored,) = store.sessions(home)
    total = first.appended + second.appended
    with open(src, "rb") as fh:
        assert store.span(stored, 0, total) == fh.read()
    assert store.verify(home) == []
    assert first.commit != second.commit


# --- 2. a hook killed mid-write ------------------------------------------------


def test_a_hook_killed_mid_write_loses_nothing(world):
    """SIGKILL during `cat`, then the full pass. The store must not notice.

    The shim publishes by `rename(2)`, so a killed run leaves a dot-named temp
    and no record. The point of the test is not that the temp is orphaned — that
    is obvious — but that the *transcript* is captured in full anyway, because
    the watcher never needed the record to find it.
    """
    home, proj = world
    src = _transcript(str(proj / "s-e2e.jsonl"), 5)
    spool = os.path.join(home, daemon.SPOOL)
    os.makedirs(spool, exist_ok=True)

    env = os.environ.copy()
    env["GITMEMORY_HOME"] = home
    p = subprocess.Popen(
        _shim_cmd() + ["PreCompact"], env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    p.stdin.write(b'{"transcript_path": "')  # a truncated payload, stdin left open
    p.stdin.flush()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(f.startswith(".tmp") for f in os.listdir(spool)):
            break
        time.sleep(0.01)
    else:
        pytest.fail("the shim never began writing its temp file")

    p.send_signal(signal.SIGKILL)
    p.wait()
    p.stdin.close()

    assert [f for f in os.listdir(spool) if not f.startswith(".")] == [], "a partial was published"

    result = _sweep(home)

    assert result.errors == []
    assert result.appended == os.path.getsize(src), "the kill cost us the transcript"
    assert store.verify(home) == []


def test_the_orphaned_temp_is_swept_once_it_is_too_old_to_be_live(world):
    home, proj = world
    _transcript(str(proj / "s-e2e.jsonl"), 2)
    spool = os.path.join(home, daemon.SPOOL)
    os.makedirs(spool, exist_ok=True)
    orphan = os.path.join(spool, ".tmp-99999-0")
    with open(orphan, "w", encoding="utf-8") as fh:
        fh.write("half a payload")
    old = time.time() - daemon.STALE_TMP - 60
    os.utime(orphan, (old, old))

    _sweep(home)

    assert not os.path.exists(orphan)


# --- 3. concurrency -------------------------------------------------------------


def test_eight_hooks_firing_at_once_all_arrive_and_none_clobbers_another(world):
    """Same second, eight processes, one spool directory.

    Note what this does *not* exercise: the collision suffix. Eight processes
    have eight pids and the record name carries `$$`, so the eight names differ
    in the pid field and no `-N` is ever appended. Mutating `_event_of` back to
    its old last-field parse leaves this test green — checked. The suffix path
    needs the same pid twice in one second, which is
    `test_a_forced_name_collision_still_parses_as_a_compaction` below. [E4]
    """
    home, proj = world
    sources = [_transcript(str(proj / f"s-{i}.jsonl"), 3) for i in range(8)]
    env = os.environ.copy()
    env["GITMEMORY_HOME"] = home
    os.makedirs(os.path.join(home, daemon.SPOOL), exist_ok=True)

    procs = []
    for src in sources:
        payload = json.dumps({"session_id": os.path.basename(src), "transcript_path": src})
        procs.append(
            subprocess.Popen(
                _shim_cmd() + ["PreCompact"],
                env=env,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        procs[-1].stdin.write(payload.encode())
    for p in procs:
        p.stdin.close()
    assert [p.wait() for p in procs] == [0] * 8

    records = sorted(f for f in os.listdir(os.path.join(home, daemon.SPOOL)) if f.endswith(".json"))
    assert len(records) == 8, f"a hook clobbered another: {records}"

    # Every record still parses as PreCompact, suffix or no suffix.
    assert {daemon._event_of(r) for r in records} == {"PreCompact"}

    result = _sweep(home)

    assert result.errors == []
    assert result.spool_consumed == 8
    assert len(store.sessions(home)) == 8
    assert result.appended == sum(os.path.getsize(s) for s in sources)
    assert store.verify(home) == []


def test_a_forced_name_collision_still_parses_as_a_compaction(world):
    """The `-N` suffix path, end to end, because concurrency alone never hits it.

    This is the bug that shipped and was caught by running it: `_event_of` read
    the *last* `-`-separated field, so `<sec>-<pid>-PreCompact-1.json` parsed its
    event as `1`, fell out of `FORCING`, and a compaction quietly stopped forcing
    a cut. Reaching it needs one pid publishing twice in one second, so the
    collision is planted rather than raced: the shim blocks in `cat` until stdin
    closes and `dash <script>` execs in the process we started, so its pid is
    `p.pid` and the name it will choose is predictable. Both candidate seconds
    are planted because the shim calls `date` after the write, not before.
    """
    home, proj = world
    src = _transcript(str(proj / "s-e2e.jsonl"), 4)
    spool = os.path.join(home, daemon.SPOOL)
    os.makedirs(spool, exist_ok=True)

    env = os.environ.copy()
    env["GITMEMORY_HOME"] = home
    p = subprocess.Popen(
        _shim_cmd() + ["PreCompact"], env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # Written while the shim is blocked in `cat`, which is before it picks a
    # name, so the collision is real rather than raced. Two of them, so the
    # suffix loop is exercised past its first step.
    taken = [
        os.path.join(spool, n) for n in (f"{p.pid}-PreCompact.json", f"{p.pid}-PreCompact-1.json")
    ]
    for path in taken:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"transcript_path": "/nowhere/at/all.jsonl"}')
    p.stdin.write(json.dumps({"transcript_path": src}).encode())
    p.stdin.close()
    assert p.wait() == 0

    (suffixed,) = [
        f for f in os.listdir(spool) if f.endswith(".json") and os.path.join(spool, f) not in taken
    ]
    assert suffixed.removesuffix(".json").split("-")[-1] == "2", suffixed
    assert daemon._event_of(suffixed) == "PreCompact"

    # And it still *forces*: the transcript is younger than the interval, so a
    # non-forcing record would leave it uncaptured until the interval elapsed.
    result = daemon.tick(home, daemon.load_watches(home), interval=3600)

    assert result.appended == os.path.getsize(src), "the suffixed record stopped forcing"
    assert store.verify(home) == []


def test_a_spooled_record_naming_a_transcript_outside_the_watch_is_dropped(world):
    """The record is a doorbell, and a doorbell cannot nominate its own target.

    Without this the hook would be an arbitrary-file-read primitive: anything
    that can write to the spool could name any path on the machine and have the
    watcher commit it into the store.

    Two layers, and the test pins both, because it passed with the first one
    deleted. `drain_spool` drops the record — that is `spool_dropped`. And
    `tick` iterates `discover(watches)` and only ever *looks up* the spool's
    paths, so even a record that survived the drop could not add a capture
    target. The structural layer is the one that makes this safe; the drop is
    what stops a doomed record being re-read forever. [E4]
    """
    home, proj = world
    _transcript(str(proj / "s-e2e.jsonl"), 2)
    outsider = _transcript(str(proj.parent / "elsewhere" / "secret.jsonl"), 2)

    _fire(home, "PreCompact", outsider)
    result = _sweep(home)

    assert result.spool_consumed == 1 and result.spool_dropped == 1
    ids = {s.session_id for s in store.sessions(home)}
    assert not any("secret" in i for i in ids), f"captured an unwatched path: {ids}"
    assert result.errors == []
