"""The watcher. [E4]

The property under test throughout: **if the hook is never installed the system
is still correct.** The hook is latency; the watcher is the guarantee. Anything
that makes the watcher depend on a hook record for correctness is a bug, and
several tests here exist only to pin that down.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from gitmemory import daemon, gitrepo, store

TURN = '{"type":"user","message":{"role":"user","content":"hello"}}\n'


def _write(path, text: str, *, append: bool = False) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


def _config(home: str, roots, agent: str = "claude-code", pattern: str | None = None) -> None:
    listed = ", ".join(json.dumps(str(r)) for r in roots)
    body = f'[[watch]]\nagent = "{agent}"\nroots = [{listed}]\n'
    if pattern:
        body += f"pattern = {json.dumps(pattern)}\n"
    _write(os.path.join(home, "config.toml"), body)
    gitrepo.init(home)


def _spool(home: str, name: str, payload) -> str:
    path = os.path.join(home, daemon.SPOOL, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload if isinstance(payload, str) else json.dumps(payload))
    return path


# --- config -----------------------------------------------------------------


def test_no_config_means_watch_nothing(tmp_path):
    """The one thing this product promises not to do is scan a home directory."""
    assert daemon.load_watches(str(tmp_path)) == []


def test_a_malformed_config_means_watch_nothing_not_watch_everything(tmp_path):
    _write(os.path.join(str(tmp_path), "config.toml"), "[[watch]\nthis is not toml")
    assert daemon.load_watches(str(tmp_path)) == []


def test_a_watch_missing_its_roots_is_skipped_and_the_rest_survive(tmp_path):
    _write(
        os.path.join(str(tmp_path), "config.toml"),
        '[[watch]]\nagent = "a"\n\n[[watch]]\nagent = "b"\nroots = ["/tmp/x"]\n',
    )
    got = daemon.load_watches(str(tmp_path))
    assert [w.agent for w in got] == ["b"]


def test_a_root_that_names_a_file_is_dropped_rather_than_silently_watching_nothing(tmp_path):
    """Pointing at one transcript instead of the directory holding it. [E4, Gemini 10]

    The natural way to get this wrong, and before the fix it was silent *and*
    total: `discover()` finds no `**/*.jsonl` under a file, and `_covers`
    excludes the root itself, so the interval path found nothing and every
    doorbell naming that file was dropped as out-of-watch. Two capture paths,
    both dead, no error anywhere.

    Dropping the root at load time does not make it work — nothing can — but it
    makes `gitmemory watch` report the watch as gone, which is a signal.
    """
    transcript = _write(str(tmp_path / "proj" / "s.jsonl"), TURN)
    _write(
        os.path.join(str(tmp_path), "config.toml"),
        f'[[watch]]\nagent = "a"\nroots = [{json.dumps(transcript)}]\n'
        f'\n[[watch]]\nagent = "b"\nroots = [{json.dumps(str(tmp_path / "proj"))}]\n',
    )
    assert [w.agent for w in daemon.load_watches(str(tmp_path))] == ["b"]


def test_a_root_that_does_not_exist_yet_is_kept(tmp_path):
    """The narrower half of the rule above, and the reason it is narrow.

    An agent that has never run has no transcript directory. Config is re-read
    every pass (`run()`), so the watch starts working the moment the directory
    appears — but only if it survived loading. Dropping it would report
    "not started yet" as "misconfigured", and would break the far more common
    case to fix the rarer one.
    """
    absent = str(tmp_path / "not-yet")
    _write(
        os.path.join(str(tmp_path), "config.toml"),
        f'[[watch]]\nagent = "a"\nroots = [{json.dumps(absent)}]\n',
    )
    assert daemon.load_watches(str(tmp_path))[0].roots == (os.path.realpath(absent),)


def test_roots_are_expanded_and_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write(os.path.join(str(tmp_path), "config.toml"), '[[watch]]\nagent="a"\nroots=["~/logs"]\n')
    (tmp_path / "logs").mkdir()
    assert daemon.load_watches(str(tmp_path))[0].roots == (os.path.realpath(tmp_path / "logs"),)


# --- containment ------------------------------------------------------------


def test_a_sibling_directory_with_a_shared_prefix_is_not_covered(tmp_path):
    """`/a/b` is not inside `/a/bc`, and a `startswith` test says it is.

    The store's redaction gate had exactly this bug and it made the gate scan
    nothing at all. [E2]
    """
    (tmp_path / "proj").mkdir()
    (tmp_path / "projects-elsewhere").mkdir()
    watch = daemon.Watch(agent="a", roots=(str(tmp_path / "proj"),))
    assert daemon._covers([watch], str(tmp_path / "proj" / "s.jsonl")) is watch
    assert daemon._covers([watch], str(tmp_path / "projects-elsewhere" / "s.jsonl")) is None


def test_a_path_that_climbs_out_of_the_root_is_not_covered(tmp_path):
    (tmp_path / "proj").mkdir()
    watch = daemon.Watch(agent="a", roots=(str(tmp_path / "proj"),))
    assert daemon._covers([watch], str(tmp_path / "proj" / ".." / "secret.jsonl")) is None


def test_a_symlink_out_of_the_root_is_not_covered(tmp_path):
    """Resolved before comparison, so a link inside the root cannot point outside it."""
    (tmp_path / "proj").mkdir()
    outside = _write(str(tmp_path / "outside" / "s.jsonl"), TURN)
    link = tmp_path / "proj" / "link.jsonl"
    link.symlink_to(outside)
    watch = daemon.Watch(agent="a", roots=(str(tmp_path / "proj"),))
    assert daemon._covers([watch], str(link)) is None


def test_the_root_itself_is_not_a_transcript(tmp_path):
    (tmp_path / "proj").mkdir()
    watch = daemon.Watch(agent="a", roots=(str(tmp_path / "proj"),))
    assert daemon._covers([watch], str(tmp_path / "proj")) is None


# --- the spool --------------------------------------------------------------


def test_a_spool_record_naming_an_unwatched_path_is_dropped(tmp_path):
    """The record is a doorbell. A hook cannot make the watcher read any file it likes."""
    home = str(tmp_path / "home")
    secret = _write(str(tmp_path / "elsewhere" / "private.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-2-PreCompact.json", {"transcript_path": secret})
    wanted, tick = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {}
    assert (tick.spool_consumed, tick.spool_dropped) == (1, 1)


def test_an_unusable_spool_record_is_consumed_anyway(tmp_path):
    """Otherwise it is re-read on every tick for the life of the store."""
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-2-Stop.json", "not json at all")
    _spool(home, "1-3-Stop.json", {"no_path_here": True})
    _spool(home, "1-4-Stop.json", ["a", "list"])
    _, tick = daemon.drain_spool(home, daemon.load_watches(home))
    assert (tick.spool_consumed, tick.spool_dropped) == (3, 3)
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == []


def test_a_forcing_event_is_distinguished_from_a_liveness_one(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    other = _write(str(root / "b.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "1-2-PreCompact.json", {"transcript_path": src})
    _spool(home, "1-3-Stop.json", {"transcript_path": other})
    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {os.path.realpath(src): True, os.path.realpath(other): False}


def test_forcing_wins_when_a_session_has_both_kinds_of_record(tmp_path):
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-9-Stop.json", {"transcript_path": src})
    _spool(home, "1-2-PreCompact.json", {"transcript_path": src})
    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {os.path.realpath(src): True}


def test_stop_rings_the_doorbell_without_forcing_a_cut(tmp_path):
    """`Stop` fires after every assistant turn; forcing on it is one segment per reply.

    Asserted through `drain_spool` rather than as `"Stop" not in FORCING`. The
    constant is what the module intends; only the drain says what it does, and a
    second hard-coded event test somewhere in the path would satisfy the intent
    while ignoring it. The session still shows up — the record is a doorbell, so
    the sweep looks at the transcript — it just arrives with forcing off. [E4]
    """
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-9-Stop.json", {"transcript_path": src})
    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {os.path.realpath(src): False}


def test_a_partial_hook_write_is_ignored_until_it_is_old(tmp_path):
    """A dot-named file is a hook mid-write. Reading it would capture a truncated record."""
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    partial = _spool(home, ".tmp-99-0", '{"transcript_path": "' + src)
    wanted, tick = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {} and tick.spool_consumed == 0
    assert os.path.exists(partial)


def test_a_temp_file_from_a_killed_hook_is_eventually_swept(tmp_path):
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    partial = _spool(home, ".tmp-99-0", "{")
    later = time.time() + daemon.STALE_TMP + 1
    daemon.drain_spool(home, daemon.load_watches(home), now=later)
    assert not os.path.exists(partial)


def test_the_sweep_leaves_other_dot_files_alone(tmp_path):
    """Only `.tmp-` is ours. `.DS_Store` and friends are not the watcher's to delete."""
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    other = _spool(home, ".DS_Store", "x")
    daemon.drain_spool(home, daemon.load_watches(home), now=time.time() + daemon.STALE_TMP + 1)
    assert os.path.exists(other)


def test_an_absent_spool_is_not_an_error(tmp_path):
    home = str(tmp_path / "home")
    os.makedirs(home)
    wanted, tick = daemon.drain_spool(home, [])
    assert wanted == {} and tick.spool_consumed == 0


# --- discovery --------------------------------------------------------------


def test_discover_finds_transcripts_at_any_depth(tmp_path):
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _write(str(root / "x" / "y" / "b.jsonl"), TURN)
    _write(str(root / "x" / "notes.md"), "not a transcript")
    watch = daemon.Watch(agent="a", roots=(str(root),))
    assert [os.path.basename(p) for _, p in daemon.discover([watch])] == ["a.jsonl", "b.jsonl"]


def test_a_transcript_under_two_roots_is_captured_once(tmp_path):
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    watch = daemon.Watch(agent="a", roots=(str(root), str(root / "..")))
    assert len(daemon.discover([watch])) == 1


def test_a_symlinked_leaf_pointing_outside_the_root_is_not_discovered(tmp_path):
    """`glob` will not descend a symlinked directory but it will match a symlinked file."""
    root = tmp_path / "proj"
    root.mkdir()
    outside = _write(str(tmp_path / "outside" / "private.jsonl"), TURN)
    (root / "innocent.jsonl").symlink_to(outside)
    assert daemon.discover([daemon.Watch(agent="a", roots=(str(root),))]) == []


# --- capture ----------------------------------------------------------------


def test_capture_one_records_the_compaction_boundary(tmp_path):
    home = str(tmp_path / "home")
    boundary = '{"type":"system","subtype":"compact_boundary","uuid":"c1","sessionId":"s"}\n'
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN + boundary + TURN)
    cap = daemon.capture_one(home, src, "claude-code")
    assert cap.appended == os.path.getsize(src)
    assert store.sessions(home)[0].boundaries == (len(TURN),)


def test_a_transcript_the_adapter_cannot_parse_is_still_captured(tmp_path):
    """The parse is only ever for boundaries. Losing the bytes over it is the real failure."""
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), "}{ not jsonl at all\n")
    cap = daemon.capture_one(home, src, "claude-code")
    assert cap.appended == os.path.getsize(src)
    assert store.sessions(home)[0].boundaries == ()


def test_two_sessions_with_the_same_basename_stay_separate(tmp_path):
    home = str(tmp_path / "home")
    a = _write(str(tmp_path / "projA" / "session.jsonl"), TURN)
    b = _write(str(tmp_path / "projB" / "session.jsonl"), TURN + TURN)
    daemon.capture_one(home, a, "claude-code")
    daemon.capture_one(home, b, "claude-code")
    assert len({s.session_id for s in store.sessions(home)}) == 2


# --- the tick ---------------------------------------------------------------


def test_a_first_sighting_is_captured_without_waiting_for_the_interval(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    result = daemon.tick(home, daemon.load_watches(home), interval=1e9)
    assert result.appended == len(TURN)
    assert result.errors == []


def test_growth_inside_the_interval_waits(tmp_path):
    """Coalescing: a session is cut at most once per interval unless something forces it."""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=1e9)
    _write(src, TURN, append=True)
    assert daemon.tick(home, watches, interval=1e9).appended == 0


def test_a_forcing_record_cuts_a_segment_inside_the_interval(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=1e9)
    _write(src, TURN, append=True)
    _spool(home, "1-2-PreCompact.json", {"transcript_path": src})
    assert daemon.tick(home, watches, interval=1e9).appended == len(TURN)


def test_the_interval_elapsing_captures_without_any_hook_at_all(tmp_path):
    """This is the guarantee. No spool record is involved anywhere in this test."""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=1e9)
    _write(src, TURN, append=True)
    later = time.time() + 7200
    assert daemon.tick(home, watches, interval=3600, now=later).appended == len(TURN)
    assert store.verify(home) == []


def test_an_unchanged_transcript_is_not_recaptured(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)
    result = daemon.tick(home, watches, interval=0, now=time.time() + 7200)
    assert result.captured == [] and result.commit is None


def test_a_transcript_rewritten_to_the_same_length_is_still_noticed(tmp_path):
    """Size alone misses an in-place edit. The store has to see that as a divergence."""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)
    _write(src, TURN.replace("hello", "world"))
    os.utime(src, (time.time() + 10, time.time() + 10))
    result = daemon.tick(home, watches, interval=0, now=time.time() + 7200)
    assert result.captured != []
    assert [s.generation for s in store.sessions(home)] == [0, 1]


def test_an_unreadable_transcript_is_reported_and_the_pass_continues(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    bad = _write(str(root / "bad.jsonl"), TURN)
    _write(str(root / "good.jsonl"), TURN)
    os.chmod(bad, 0o000)
    _config(home, [root])
    try:
        result = daemon.tick(home, daemon.load_watches(home), interval=0)
    finally:
        os.chmod(bad, 0o600)
    assert len(result.errors) == 1
    assert result.appended == len(TURN)


def test_one_pass_makes_one_commit(tmp_path):
    """A hundred single-session commits for one wake-up is a history nobody reads."""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    for i in range(4):
        _write(str(root / f"s{i}.jsonl"), TURN)
    _config(home, [root])
    result = daemon.tick(home, daemon.load_watches(home), interval=0)
    assert len(result.captured) == 4
    assert result.commit is not None
    assert "(+1 more)" in _subject(home)


def test_a_pass_that_captured_nothing_makes_no_commit(tmp_path):
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    assert daemon.tick(home, daemon.load_watches(home)).commit is None


def test_a_broken_git_is_reported_but_does_not_lose_the_capture(tmp_path):
    """The bytes are the product; the commit is the layer on top.

    Found by running it: an un-initialised (or corrupted, or full) repository
    raised out of `tick` *after* the segment was already safely on disk, so a
    recoverable problem — the next pass commits everything at once — read to the
    caller as a pass that failed entirely. [E4]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    os.rename(os.path.join(home, ".git"), os.path.join(home, ".git-moved"))

    result = daemon.tick(home, daemon.load_watches(home), interval=0)

    assert result.appended == len(TURN)  # the bytes landed
    assert result.commit is None
    assert any(e.startswith("commit:") for e in result.errors)
    assert store.verify(home) == []  # ...and the store is sound without git


def _subject(home: str) -> str:
    import subprocess

    out = subprocess.run(
        ["git", "-C", home, "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": home},
    )
    return out.stdout.strip()


# --- run --------------------------------------------------------------------


def test_run_once_initialises_the_repository_and_commits(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    lines: list[str] = []
    assert daemon.run(home, once=True, interval=0, log=lines.append) == 0
    assert os.path.isdir(os.path.join(home, ".git"))
    assert any("captured 1" in line for line in lines)
    assert store.verify(home) == []


def test_run_with_no_config_captures_nothing_and_succeeds(tmp_path):
    """Installed but unconfigured is a no-op, not a crash and not a home-directory scan."""
    home = str(tmp_path / "home")
    _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    assert daemon.run(home, once=True, log=lambda _: None) == 0
    assert store.sessions(home) == []


def test_run_reports_a_failing_pass_in_its_exit_code(tmp_path):
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    bad = _write(str(root / "bad.jsonl"), TURN)
    os.chmod(bad, 0o000)
    _config(home, [root])
    try:
        assert daemon.run(home, once=True, interval=0, log=lambda _: None) == 1
    finally:
        os.chmod(bad, 0o600)


# --- the seam the hook writes to -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["1758300000-4242-PreCompact.json", "1758300000-4242-PreCompact-1.json"],
)
def test_the_event_is_read_from_the_filename_the_shim_writes(tmp_path, name):
    """Including the collision suffix: two hooks in the same second must both force."""
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    _spool(home, name, {"transcript_path": src})
    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {os.path.realpath(src): True}


# --- findings from Gemini's E4 cross-review, each verified before being fixed ---


def test_an_unreadable_store_is_reported_and_the_watcher_stays_up(tmp_path, monkeypatch):
    """One bad manifest used to kill the daemon, and keep killing it. [E4, Gemini 06]

    `tick` guarded the commit but not `_recorded`, which calls
    `store.sessions()` — and that raises `EscapingSegment` on a manifest naming
    a path outside the store. The exception went straight out of `tick`, so the
    watcher process died and died again on every restart. A tripwire nobody is
    alive to read is not a tripwire.
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])

    def boom(_home):
        raise store.EscapingSegment("manifest names /etc/passwd")

    monkeypatch.setattr(store, "sessions", boom)

    result = daemon.tick(home, daemon.load_watches(home), interval=0)

    assert result.appended == 0, "wrote into a store it could not read"
    assert any("store unreadable" in e for e in result.errors), result.errors


def test_the_watcher_recovers_once_the_store_is_readable_again(tmp_path, monkeypatch):
    """The other half: reporting must not be a latch. [E4]"""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    real = store.sessions
    monkeypatch.setattr(
        store, "sessions", lambda _h: (_ for _ in ()).throw(store.EscapingSegment("x"))
    )
    assert daemon.tick(home, daemon.load_watches(home), interval=0).errors

    monkeypatch.setattr(store, "sessions", real)
    result = daemon.tick(home, daemon.load_watches(home), interval=0)

    assert result.errors == []
    assert result.appended == os.path.getsize(src)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("1758300000-4242-PreCompact.json", "PreCompact"),
        ("1758300000-4242-PreCompact-1.json", "PreCompact"),
        ("1758300000-4242-SessionEnd.json", "SessionEnd"),
        ("1758300000-4242-Stop.json", "Stop"),
        ("1758300000-4242-unknown.json", "unknown"),
        # A clock set before 1970 makes `date +%s` negative, the name leads with
        # `-`, and every field shifts: `fields[2]` was the pid. [E4, Gemini 07]
        ("-100-4242-PreCompact.json", ""),
        # And the general case the validation buys: anything that is not an
        # event the shim writes is not an event, wherever it sits.
        ("1758300000-4242-4242.json", ""),
        ("nonsense.json", ""),
        ("1758300000-4242-PreCompact", ""),
    ],
)
def test_only_a_real_event_name_parses_as_an_event(name, expected):
    assert daemon._event_of(name) == expected


def test_a_negative_epoch_record_does_not_masquerade_as_forcing(tmp_path):
    """The parse bug mattered because the pid landed in `FORCING`'s lookup. [E4]"""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "-100-4242-PreCompact.json", {"transcript_path": src})

    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))

    # Still a doorbell — the record is consumed and the session noticed — but it
    # must not claim to be a compaction boundary it cannot be trusted to name.
    assert wanted == {os.path.realpath(src): False}


def _case_insensitive(where) -> bool:
    """Ask the filesystem under test, rather than guessing from `sys.platform`.

    A mac with a case-sensitive APFS volume exists, and so does a
    case-insensitive mount on Linux. The probe costs one file.
    """
    probe = os.path.join(str(where), "CaseProbe")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("")
    try:
        return os.path.exists(os.path.join(str(where), "caseprobe"))
    finally:
        os.unlink(probe)


def test_a_watch_root_written_in_the_wrong_case_still_covers_its_files(tmp_path):
    """APFS is case-insensitive; `realpath` does not correct case. [E4, Gemini 08]

    A user writing `roots = ["~/Projects/MyApp"]` while the agent reports
    `~/projects/myapp/s.jsonl` had every doorbell silently dropped, because
    `relpath` compares strings and said the file was outside the root. Data was
    never lost — `discover()` globs from the configured root and finds it on the
    next interval — but the hook stopped doing the one thing it is for.

    Skipped on a case-sensitive filesystem, where the two names really are two
    directories and dropping the record is the correct answer.
    """
    if not _case_insensitive(tmp_path):
        pytest.skip("this filesystem is case-sensitive; the drop is correct here")
    root = tmp_path / "Proj"
    src = _write(str(root / "a.jsonl"), TURN)
    watches = [
        daemon.Watch(agent="claude-code", roots=(str(tmp_path / "PROJ"),), pattern=daemon.PATTERN)
    ]

    assert daemon._covers(watches, src) is not None
