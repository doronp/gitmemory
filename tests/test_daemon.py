"""The watcher. [E4]

The property under test throughout: **if the hook is never installed the system
is still correct.** The hook is latency; the watcher is the guarantee. Anything
that makes the watcher depend on a hook record for correctness is a bug, and
several tests here exist only to pin that down.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from gitmemory import daemon, gitrepo, store

TURN = '{"type":"user","message":{"role":"user","content":"hello"}}\n'


def _write(path, text: str, *, append: bool = False) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


def _config(
    home: str,
    roots,
    agent: str = "claude-code",
    pattern: str | None = None,
    *,
    git: bool = True,
) -> None:
    """Write `config.toml`, and by default stand in for `run()`'s `gitrepo.init`.

    `tick` commits but does not initialise — `run` does, once per start — so
    every test that drives `tick` directly needs a repository from somewhere,
    and this is it. `git=False` is for the one test that is *about* that init:
    it asserted `.git` existed after `run`, having had this helper create it a
    moment earlier, so it passed with the init deleted. [E4, review: F3]
    """
    listed = ", ".join(json.dumps(str(r)) for r in roots)
    body = f'[[watch]]\nagent = "{agent}"\nroots = [{listed}]\n'
    if pattern:
        body += f"pattern = {json.dumps(pattern)}\n"
    _write(os.path.join(home, "config.toml"), body)
    if git:
        gitrepo.init(home)


def _key(path) -> tuple[int, int]:
    """What `drain_spool` keys its result on: device and inode, not the string.

    The hook writes whatever spelling the agent handed it and `discover` writes
    whatever spelling the glob produced. `realpath` resolves symlinks but does
    not correct case, so on APFS those are two keys for one file. [E4]
    """
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _on_pass(monkeypatch, action, *, poll: float = 0):
    """Run `action()` once per `run` pass, and only then.

    `run` is not the only caller of `time.sleep` in this process, and patching
    the module attribute cannot tell them apart: `daemon.time` *is* the `time`
    module, so `monkeypatch.setattr(daemon.time, "sleep", ...)` and
    `monkeypatch.setattr(time, "sleep", ...)` are the same patch, not two with
    different reach. What else arrives here is `subprocess.Popen._wait`, which
    busy-polls a child with `time.sleep(delay)` whenever a timeout is set, and
    `gitrepo._git` always sets one — so every `git` slow enough to need a second
    look was arriving as a pass boundary. That fired one test's deletion in the
    middle of `gitrepo.init`, between two of its `git config` calls, and the
    next one died with `fatal: not in a git directory`; it also burned a pass,
    so `run` hit its stop condition early. Roughly one run in twenty, and two
    wrong theories — an init lock collision, then the `interval` gate — before
    the probe printed the caller: `subprocess.py:2079 _wait delay=0.001`.

    So discriminate on the duration. `run` sleeps `poll` and nothing else, in
    both of its two places (the end of a pass and the retry after `init`
    fails); `_wait` sleeps a doubling sequence from 0.0005 and never zero.
    Anything that is not `poll` belongs to somebody else and is passed through
    to the real sleep, which is what that caller is owed.

    One helper because there were three call sites, two of them written without
    the discriminator and exposed to exactly the flake it was written for.
    [E4, review: CLI 9 / Gemini r3 §2]
    """
    real_sleep = time.sleep

    def sleep(seconds):
        if seconds != poll:
            return real_sleep(seconds)
        return action()

    monkeypatch.setattr(time, "sleep", sleep)


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
    home = str(tmp_path / "home")
    transcript = _write(str(tmp_path / "proj" / "s.jsonl"), TURN)
    _write(
        os.path.join(home, "config.toml"),
        f'[[watch]]\nagent = "a"\nroots = [{json.dumps(transcript)}]\n'
        f'\n[[watch]]\nagent = "b"\nroots = [{json.dumps(str(tmp_path / "proj"))}]\n',
    )
    assert [w.agent for w in daemon.load_watches(home)] == ["b"]


def test_a_root_that_does_not_exist_yet_is_kept(tmp_path):
    """The narrower half of the rule above, and the reason it is narrow.

    An agent that has never run has no transcript directory. Config is re-read
    every pass (`run()`), so the watch starts working the moment the directory
    appears — but only if it survived loading. Dropping it would report
    "not started yet" as "misconfigured", and would break the far more common
    case to fix the rarer one.
    """
    home = str(tmp_path / "home")
    absent = str(tmp_path / "not-yet")
    _write(
        os.path.join(home, "config.toml"),
        f'[[watch]]\nagent = "a"\nroots = [{json.dumps(absent)}]\n',
    )
    assert daemon.load_watches(home)[0].roots == (os.path.realpath(absent),)


def test_roots_are_expanded_and_resolved(tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write(os.path.join(home, "config.toml"), '[[watch]]\nagent="a"\nroots=["~/logs"]\n')
    (tmp_path / "logs").mkdir()
    assert daemon.load_watches(home)[0].roots == (os.path.realpath(tmp_path / "logs"),)


def test_a_root_that_would_make_the_store_watch_itself_is_dropped(tmp_path):
    """Both overlap directions, because both feed the store its own output.

    `discover()` globs `<root>/**/*.jsonl` and the store's own segments are
    `*.jsonl`, so a store under a watch root is read back as new sessions — and
    new sessions bypass the interval gate, so it runs at full poll rate. Review
    measured six phantom sessions and six commits in thirty seconds, roughly
    17,000 a day, with `verify` clean throughout. [E4, review: CLI 3]

    The reverse — a root *inside* the store — was not what review measured and
    is checked here because it was reproduced while fixing the first: a watch on
    `<home>/raw` globs the segment files directly. One symmetric test, because
    one symmetric rule covers both. The default `~/.gitmemory` escapes only
    because `_hits` skips dotted components — pinned separately by
    `test_a_dotted_component_is_not_discovered`.
    """
    home = str(tmp_path / "home")
    os.makedirs(os.path.join(home, "raw"))
    for root in (str(tmp_path), home, os.path.join(home, "raw")):
        _write(
            os.path.join(home, "config.toml"),
            f'[[watch]]\nagent = "claude-code"\nroots = [{json.dumps(root)}]\n',
        )
        assert daemon.load_watches(home) == [], root


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
    """Otherwise it is re-read on every tick for the life of the store.

    Three records, two facts. Bytes that are not JSON are `spool_unreadable`;
    JSON that carries no path is `spool_dropped`, which is reported as a
    *configuration* fact and must therefore stay meaning only that. [E7 fs-F3]
    """
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-2-Stop.json", "not json at all")
    _spool(home, "1-3-Stop.json", {"no_path_here": True})
    _spool(home, "1-4-Stop.json", ["a", "list"])
    _, tick = daemon.drain_spool(home, daemon.load_watches(home))
    assert (tick.spool_consumed, tick.spool_dropped, tick.spool_unreadable) == (3, 2, 1)
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
    assert wanted == {_key(src): True, _key(other): False}


def test_forcing_wins_when_a_session_has_both_kinds_of_record(tmp_path):
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)
    _config(home, [tmp_path / "proj"])
    _spool(home, "1-9-Stop.json", {"transcript_path": src})
    _spool(home, "1-2-PreCompact.json", {"transcript_path": src})
    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {_key(src): True}


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
    assert wanted == {_key(src): False}


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
    """`_hits` will not descend a symlinked directory but it will match a symlinked file."""
    root = tmp_path / "proj"
    root.mkdir()
    outside = _write(str(tmp_path / "outside" / "private.jsonl"), TURN)
    (root / "innocent.jsonl").symlink_to(outside)
    assert daemon.discover([daemon.Watch(agent="a", roots=(str(root),))]) == []


def test_a_symlinked_directory_under_the_root_is_not_descended(tmp_path):
    """The other half of the sentence above, which used to be false.

    `discover`'s docstring claimed `glob` does not follow symlinked directories.
    It does — measured on 3.13.12. Asserted against `_hits` rather than against
    `discover`, because `discover` cannot tell the difference: `_covers` throws
    the escaped file away either way, so a test on the returned list passes
    before the fix and after it. What the following costs is walking, and
    walking is what `_hits` decides. `test_a_self_linking_root_does_not_
    multiply_discovery` prices it. [review: paths F2]
    """
    root = tmp_path / "proj"
    root.mkdir()
    _write(str(root / "mine.jsonl"), TURN)
    _write(str(tmp_path / "outside" / "private.jsonl"), TURN)
    (root / "innocent").symlink_to(tmp_path / "outside")
    assert daemon._hits(str(root), daemon.PATTERN) == [str(root / "mine.jsonl")]


def test_a_self_linking_root_does_not_multiply_discovery(scandir_budget, tmp_path):
    """A watch root is a directory the owner named, not one they audited.

    `**` re-enters a self-linking directory once per link per level until the
    kernel's `ELOOP` at ~31 components, and materialises every candidate before
    matching: `k` links is `k ** 31` paths. One directory, one transcript, two
    `-> .` links billed 76,849 `scandir` calls in five seconds against the
    replaced implementation and had not finished; the walk costs six. Every
    poll paid it, so a single stray link inside a root the owner does not
    control is a hang plus a memory exhaust, five seconds apart.

    The budget is what makes this a regression test and not a smoke test — the
    old code blows it in the first millisecond rather than after a timeout.

    It was decoration for one commit, and the floor below is why it is not
    again. `scandir_budget` patched `os.scandir` only, which `pathlib`'s globber
    never reads — it binds `staticmethod(os.scandir)` at `glob` import — so this
    counted 0 reads and asserted `0 <= 16`, which nothing can fail. The fixture
    patches both now. [review: paths F2, tests 2]
    """
    root = tmp_path / "proj"
    root.mkdir()
    _write(str(root / "a.jsonl"), TURN)
    os.symlink(".", root / "x")
    os.symlink(".", root / "y")

    calls = scandir_budget(16)
    found = daemon.discover([daemon.Watch(agent="a", roots=(str(root),))])
    assert [os.path.basename(p) for _, p in found] == ["a.jsonl"]
    assert 0 < calls[0] <= 16, calls[0]


def test_a_dotted_component_is_not_discovered(tmp_path):
    """The skip that keeps the default store out of a watch on the home directory.

    `glob.glob` skips dotted components (`include_hidden=False`); `Path.glob`
    does not. So replacing one with the other had to carry the skip back by
    hand, and this is the test that says why it is a rule rather than an
    inherited quirk: the default store is `~/.gitmemory`, `roots = ["~"]` is a
    legal watch, and a daemon that saw dotted paths would read its own segments
    back as fresh sessions at full poll rate. `_roots` rejects a root that
    *contains* the store, but only the store it was told about.
    """
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _write(str(root / ".gitmemory" / "raw" / "seg.jsonl"), TURN)
    _write(str(root / "sub" / ".hidden.jsonl"), TURN)
    found = daemon.discover([daemon.Watch(agent="a", roots=(str(root),))])
    assert [os.path.basename(p) for _, p in found] == ["a.jsonl"]


def test_a_dotted_directory_the_pattern_names_is_still_discovered(tmp_path):
    """The skip above is `glob.glob`'s, and `glob.glob`'s skip has an exception.

    `include_hidden=False` suppresses hidden names matched by a *wildcard*. A
    dotted component written out in the pattern is matched literally and
    traversed — measured on 3.13.12, where the old matcher returned both files
    below and the blanket skip that replaced it returned neither. `_pattern`
    allows `.kimi/**/*.jsonl` (not absolute, does not climb), so the failure
    mode was a watch that matched nothing while looking correctly configured,
    which is the same shape as an agent that has not run yet.
    [review: gemini 1]
    """
    root = tmp_path / "proj"
    _write(str(root / ".kimi" / "a.jsonl"), TURN)
    _write(str(root / ".kimi" / "sub" / "b.jsonl"), TURN)
    _write(str(root / ".other" / "c.jsonl"), TURN)

    assert [os.path.basename(p) for p in daemon._hits(str(root), ".kimi/**/*.jsonl")] == [
        "a.jsonl",
        "b.jsonl",
    ]
    assert daemon._hits(str(root), "**/*.jsonl") == []


def test_a_root_that_is_itself_dotted_discovers_everything_under_it(tmp_path):
    """The skip is relative to the root, and the shipped default root is dotted.

    `roots = ["~/.claude/projects"]` is what the README tells people to write,
    so the absolute path of every transcript on a stock install carries a
    dotted component. A filter over `p.parts` rather than
    `p.relative_to(root).parts` therefore discovers *nothing* on the default
    configuration while every test in this file passes: `tmp_path` has no
    dotted component, so the difference between the two spellings is invisible
    to all of them. [review: tests 1]
    """
    root = tmp_path / ".claude" / "projects" / "-Users-x-work"
    _write(str(root / "a.jsonl"), TURN)
    assert [os.path.basename(p) for p in daemon._hits(str(root), "**/*.jsonl")] == ["a.jsonl"]


# --- capture ----------------------------------------------------------------


def test_capture_one_records_the_compaction_boundary(tmp_path):
    home = str(tmp_path / "home")
    boundary = '{"type":"system","subtype":"compact_boundary","uuid":"c1","sessionId":"s"}\n'
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN + boundary + TURN)
    cap = daemon.capture_one(home, src, "claude-code")
    assert cap.appended == os.path.getsize(src)
    assert store.sessions(home)[0].boundaries == (len(TURN),)


def test_a_transcript_the_adapter_cannot_parse_is_still_captured(tmp_path):
    """The parse is only ever for boundaries. Losing the bytes over it is the real failure.

    "Cannot parse" here means *yields nothing*, not *raises*: `iter_records`
    skips a line JSON refuses, so this input reaches the adapter and comes back
    as a session with zero events, and no exception is ever raised. Worth
    keeping — bytes in, no boundaries out, which is the degraded mode the
    design promises — but it is not a test of the exception path, and a vacuity
    audit proved it: making a parse failure cost the capture leaves this green.
    The raising path is `test_an_agent_with_no_adapter_still_captures_its_bytes`
    and `test_the_watcher_survives_an_adapter_that_raises_anything_at_all`.
    [E4, review: vacuity audit]
    """
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
    """The interval has to out-run the epoch, or the sentinel answers instead.

    A never-seen session is `(-1, 0.0)` — size and manifest mtime — and the gate
    is `size < 0 or now - mtime >= interval`. With `interval=1e9` the second
    clause is `now - 0.0`, which is the age of the Unix epoch in seconds and is
    larger, so the session came through whether or not the first clause existed.
    A vacuity audit deleted `size < 0` and the suite stayed green: this test,
    the only one that names the property, could not see it.

    `1e12` is about thirty thousand years, which no clock here reaches, so the
    first-sighting rule is the only thing left that can explain the capture.
    [E4, review: vacuity audit]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    result = daemon.tick(home, daemon.load_watches(home), interval=1e12)
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
    """The observable answer, not the short circuit that makes it cheap.

    Deleting the unchanged-file fast path leaves this green: the store's own
    no-op path still appends nothing and still commits nothing, so the visible
    result is identical and only the work is wasted. That cost is pinned by
    `test_an_unchanged_transcript_is_not_rehashed_on_every_pass`.
    [E4, review: vacuity audit]
    """
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


def test_a_same_length_rewrite_that_also_keeps_the_mtime_is_recovered_on_the_next_append(tmp_path):
    """The blind spot in the test above, and how far it actually goes.

    `changed` is `size != stored or mtime > recorded`, so a rewrite that changes
    neither is invisible to the pass — `rsync -a`, a restore from an archive, an
    editor that preserves times. The test above advances the mtime by ten
    seconds and so never meets this.

    Measured rather than assumed, because the assumption was that those bytes
    were lost. They are not. The pass that misses the rewrite is followed by one
    that sees the *next* append, and the store compares prefixes rather than
    trusting the length: it forks a generation, keeps the original bytes in the
    old one, and `verify` stays clean. So this is the same shape as the hook —
    latency, not loss — and it is bounded by the next byte the agent writes.

    What is genuinely lost is the case with no next byte: a transcript rewritten
    to the same length and then never touched again keeps its original capture.
    Closing that means hashing a tail on every pass for every session, every
    five seconds, against a fault no agent this adapter targets can produce; the
    contiguity proof is what makes that trade safe to take. [E4, review: F7]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    assert daemon.tick(home, watches, interval=0).appended == len(TURN)

    before = os.stat(src)
    _write(src, TURN.replace("hello", "world"))
    os.utime(src, (before.st_atime, before.st_mtime))
    missed = daemon.tick(home, watches, interval=0, now=time.time() + 7200)
    assert missed.appended == 0, "neither size nor mtime moved; the pass cannot see it"

    _write(src, TURN, append=True)
    recovered = daemon.tick(home, watches, interval=0, now=time.time() + 14400)

    assert recovered.appended == 2 * len(TURN), "the fork re-copies the rewritten file whole"
    assert [(s.generation, s.size) for s in store.sessions(home)] == [
        (0, len(TURN)),
        (1, 2 * len(TURN)),
    ]
    assert store.verify(home) == []


def test_an_infinite_interval_still_captures_a_forced_record(tmp_path):
    """The gate and its override, separated by making the gate unarguable.

    Every other interval test picks a number and then picks a `now` far enough
    past it, which tests the arithmetic as much as the rule. `inf` cannot be
    waited out, so what gets captured here got captured because a hook record
    said to. [E4, review: F5]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    assert daemon.tick(home, watches, interval=0).appended == len(TURN)

    _write(src, TURN, append=True)
    assert daemon.tick(home, watches, interval=float("inf")).appended == 0

    _spool(home, "4242-PreCompact.json", {"transcript_path": src})
    assert daemon.tick(home, watches, interval=float("inf")).appended == len(TURN)


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
    """`git=False`: the repository has to be one `run` made. [E4, review: F3]"""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root], git=False)
    assert not os.path.exists(os.path.join(home, ".git"))
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
    assert wanted == {_key(src): True}


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
        # The grammar the shim writes: `<pid>-<event>[-<n>].json`.
        ("4242-PreCompact.json", "PreCompact"),
        ("4242-PreCompact-1.json", "PreCompact"),
        ("4242-SessionEnd.json", "SessionEnd"),
        ("4242-Stop.json", "Stop"),
        ("4242-unknown.json", "unknown"),
        # The grammar the shim wrote before the `date` fork came off the hot
        # path. Records outlive an upgrade by up to one poll, so both are read.
        ("1758300000-4242-PreCompact.json", "PreCompact"),
        ("1758300000-4242-PreCompact-1.json", "PreCompact"),
        # A clock set before 1970 made `date +%s` negative, so the name led with
        # `-` and every field shifted: `fields[2]` was the pid, and this case
        # expected "" — the positional reader degrading to not-forcing, which
        # silently cost that compaction its doorbell. Finding the event instead
        # of counting to it answers correctly, so the expectation changed with
        # the parser. [E4, Gemini 07; corrected: shell MAJOR]
        ("-100-4242-PreCompact.json", "PreCompact"),
        # Nothing numeric can be mistaken for an event, wherever it sits, which
        # is the whole reason the scan is unambiguous.
        ("1758300000-4242-4242.json", ""),
        ("4242-4242.json", ""),
        ("nonsense.json", ""),
        ("4242-PreCompact", ""),
    ],
)
def test_only_a_real_event_name_parses_as_an_event(name, expected):
    assert daemon._event_of(name) == expected


def test_a_record_whose_fields_are_shifted_is_still_read_as_the_event_it_is(tmp_path):
    """A clock set before 1970, which is what made the positional parse fail.

    The original of this test asserted the degraded answer: not forcing, because
    the pid had landed where the event was counted to be. Degrading was the best
    a positional reader could do, and it still cost that compaction its doorbell
    — the session waited out the interval for a capture the hook had already
    asked for. Finding the event rather than counting to it answers correctly,
    so this now asserts the capture is forced. The property the old name was
    reaching for holds either way and holds for every shifted name, not just
    this one: no numeric field matches an event, so a pid cannot masquerade as
    one. [E4, Gemini 07; corrected: shell MAJOR]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "-100-4242-PreCompact.json", {"transcript_path": src})

    wanted, _ = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {_key(src): True}

    # The masquerade itself, checked directly rather than through the shift that
    # used to cause it: a name whose every field is a number names no event.
    assert daemon._event_of("-100-4242-4242.json") == ""


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


# --- E4 standalone review round ---------------------------------------------


@pytest.mark.parametrize(
    ("body", "expect"),
    [
        ("[[watch]\nnot toml", "not valid TOML"),
        ('[[watch]]\nagent = "claude-code"\nroots = "/tmp/x"\n', "list `roots`"),
        ('[[watch]]\nroots = ["/tmp/x"]\n', "string `agent`"),
        ('[[watch]]\nagent = "claude-code"\nroots = [42]\n', "is not a path"),
        ('[[watch]]\nagent = "nope"\nroots = ["/tmp/x"]\n', "no adapter"),
    ],
)
def test_each_way_of_breaking_the_config_says_which_one_it_was(tmp_path, body, expect):
    """Ten distinct faults, one sentence — and it named only one of them.

    Five produced no output at all, the worst being the path typo: a misspelled
    root and a correctly-configured watcher that has not seen its first session
    were byte-identical experiences. This module's docstring promises a
    misconfigured watcher does nothing *loudly*. [E4, review: CLI 2]
    """
    home = str(tmp_path / "home")
    _write(os.path.join(home, "config.toml"), body)
    said: list[str] = []
    daemon.load_watches(home, log=said.append)
    assert [m for m in said if expect in m], said


def test_a_missing_config_is_itself_worth_saying(tmp_path):
    said: list[str] = []
    assert daemon.load_watches(str(tmp_path / "home"), log=said.append) == []
    assert [m for m in said if "watching nothing" in m], said


def test_an_agent_with_no_adapter_still_captures_its_bytes(tmp_path):
    """Loud and degraded beats silent and degraded; it does not beat capturing nothing.

    An adapter supplies *boundaries*. Discovery globs `pattern` and the store
    copies bytes, and neither needs one — so refusing the watch would trade a
    store with no boundaries for no store at all, against this module's rule
    that bytes outrank boundaries. It would also refuse `agent = "kimi-code"`
    written the day before that adapter lands. [E4, review: CLI 5]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root], agent="kimi-code")
    watches = daemon.load_watches(home)
    assert [w.agent for w in watches] == ["kimi-code"]
    assert daemon.tick(home, watches, interval=0).appended == len(TURN)
    assert store.verify(home) == []


def test_a_doorbell_for_the_same_file_under_another_spelling_is_still_heard(tmp_path):
    """The case fix reached `_covers` and stopped there. [E4, review: concurrency 9]

    `_covers` asks the kernel, via `os.path.samefile`, so the record survived
    the containment check — and was then discarded by `tick`'s string lookup
    against the spelling `discover` produced. The doorbell rang, the pass could
    not hear it, and the compaction waited for the interval. Device and inode
    is the identity both halves now ask for.
    """
    if not _case_insensitive(tmp_path):
        pytest.skip("this filesystem is case-sensitive; the two names are two files")
    home = str(tmp_path / "home")
    root = tmp_path / "Proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=1e9)

    _write(src, TURN, append=True)
    shouted = str(tmp_path / "PROJ" / "a.jsonl")
    _spool(home, "1-2-PreCompact.json", {"transcript_path": shouted})
    assert daemon.tick(home, watches, interval=1e9).appended == len(TURN)


def test_a_spool_record_that_cannot_be_parsed_at_all_is_dropped_not_replayed(tmp_path):
    """`json.loads` on deep nesting raises `RecursionError`, a `RuntimeError`.

    It escaped `drain_spool`'s `except (OSError, ValueError)`, escaped `tick`,
    escaped `run`'s `while True` — and because the record was unlinked only
    *after* the parse, it did it again on every restart, for ever, with no
    session anywhere being captured. [E4, review: concurrency 1]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "1-2-Stop.json", "[" * 200_000)

    wanted, tick_result = daemon.drain_spool(home, daemon.load_watches(home))
    # `spool_unreadable`, not `spool_dropped`: the record never became JSON, so
    # nothing about it is a statement about the watch config. [E7 fs-F3]
    assert wanted == {} and (tick_result.spool_dropped, tick_result.spool_unreadable) == (0, 1)
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == [], "a replayed record is for ever"
    # Consumed as well as dropped, and the negative control is why it is
    # asserted: the outer floor catches a `RecursionError` too and produces the
    # same drop, the same unlink and the same empty spool, so without this line
    # the named guard around the parse could be deleted and nothing would
    # notice. The counters are where the two differ, and the difference is a
    # diagnosis. An unparseable record that is *consumed* reports a hook writing
    # garbage. The same record reaching only the floor reports `consumed=0`,
    # which reads as a broken watcher and sends whoever is on the other end of
    # it to the wrong component.
    assert tick_result.spool_consumed == 1, "the watcher read this record and decided about it"


def test_a_spool_record_whose_path_cannot_even_be_resolved_is_dropped(tmp_path):
    """A NUL in the path makes `os.path.realpath` raise `ValueError` from outside
    the guard the parse was wrapped in. Same permanent-replay outcome."""
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "1-2-Stop.json", {"transcript_path": "/tmp/a\x00b.jsonl"})

    wanted, tick_result = daemon.drain_spool(home, daemon.load_watches(home))
    assert wanted == {} and tick_result.spool_dropped == 1
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == []


def test_a_spool_record_that_fails_somewhere_nobody_guarded_is_still_dropped(tmp_path, monkeypatch):
    """The outer floor, which the two tests above do not reach.

    Both of them are caught by the narrow guard around the parse itself, so
    they pin that guard and leave `drain_spool`'s `except Exception` untested —
    which its own negative control is what noticed. The floor is there for the
    step *after* the parse: any of `_payload_path`, `_covers`, or `_file_key`
    raising something nobody predicted has the same permanent-replay outcome as
    an unparseable payload, because the record is unlinked before them but the
    exception still escapes `tick` and `run`. [E4, review: concurrency 1]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "1-2-Stop.json", {"transcript_path": src})

    def boom(_payload):
        raise RecursionError("nobody predicted this")

    monkeypatch.setattr(daemon, "_payload_path", boom)
    wanted, tick_result = daemon.drain_spool(home, daemon.load_watches(home))

    assert wanted == {} and tick_result.spool_dropped == 1
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == [], "a replayed record is for ever"


def test_a_temp_whose_mtime_is_in_the_future_is_not_swept_as_a_corpse(tmp_path):
    """Which a store copied off a fast-clock machine is full of.

    Named for the forward clock jump at first, and that was wrong twice over:
    the guard it was written to pin (`and age > 0`) is dead code, and a forward
    jump makes `age` large and *positive*, so no mtime test can catch it. Both
    found by this test's own negative control, which the original mutant
    survived. What survives here is the subtraction: a negative age fails
    `> STALE_TMP` unaided, and the mutant below is the plausible wrong repair.
    [E4, review: concurrency 6]
    """
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    live = _spool(home, ".tmp-4242", "half a payload")
    ahead = time.time() + daemon.STALE_TMP * 2
    os.utime(live, (ahead, ahead))

    daemon.drain_spool(home, daemon.load_watches(home))
    assert os.path.exists(live), "a temp from the future is a moved clock, not a corpse"


def test_a_manifest_mtime_in_the_future_does_not_suspend_capture(tmp_path):
    """`now - mtime` goes negative, so the interval gate can never fire.

    Measured at 0 B captured over eight simulated hours, with `verify` clean and
    the hook left doing 100% of the capture — which inverts the property this
    whole module exists for: hook off, nothing; hook on, everything. It takes no
    clock manipulation to reach. A store restored from a machine whose clock ran
    fast carries the future mtimes with it, and `tar -p` and `rsync -a` both
    preserve them faithfully. [E4, review: concurrency 2]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=1e9)

    sid = store.session_id_for(src)
    man = os.path.join(home, "sessions", "claude-code", sid, "g00.json")
    os.utime(man, (time.time() + 86_400,) * 2)
    _write(src, TURN, append=True)

    # One pass notices the corrupt timestamp and corrects it. It does not
    # capture on that pass — the session now reads as captured just now — and
    # that is the point: the wait is bounded by `interval` instead of by the
    # skew, which here would have been a day.
    seen = time.time() + 7200
    assert daemon.tick(home, watches, interval=3600, now=seen).appended == 0
    assert os.path.getmtime(man) <= seen, "a timestamp read every pass has to be repaired once"
    assert daemon.tick(home, watches, interval=3600, now=seen + 3601).appended == len(TURN)


def test_a_pass_that_only_finished_a_crashed_capture_still_commits(tmp_path):
    """Adoption writes a manifest without copying a byte, so `appended` is 0.

    The pass's commit test read `appended` alone: the repair landed on disk and
    never reached git, and if the session had ended it never would.
    [E4, review: store-contract 4, concurrency 4]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)

    base = os.path.getsize(src)
    _write(src, TURN, append=True)
    end = os.path.getsize(src)
    with open(src, "rb") as fh:
        fh.seek(base)
        tail = fh.read()
    sid = store.session_id_for(src)
    seg = os.path.join(home, "raw", "claude-code", sid, "g00", f"{base:012d}-{end:012d}.jsonl")
    with open(seg, "wb") as fh:
        fh.write(tail)
    os.utime(src, (0, 0))  # nothing to append; adoption is the only work left

    result = daemon.tick(home, watches, interval=0)
    assert result.appended == 0
    assert result.commit is not None, "the repair has to reach git"
    assert store.verify(home) == []


def test_the_hundredth_generation_does_not_resurrect_the_ninety_ninth(tmp_path):
    """ "Last in sorted order" is lexicographic: `"g100.json" < "g99.json"`.

    A store that had forked a hundred times would start treating g99 as the live
    generation for ever, so the watcher would compare every new byte against a
    stale size and re-capture the whole session on every pass. Reachable only by
    a hundred divergences in one session, which is why it is small — but "the
    live generation" has an exact definition and taking the maximum is no more
    code than assuming the sort matches it. [E4, review: store-contract 7]
    """
    home = str(tmp_path / "home")
    sessions = os.path.join(home, "sessions", "claude-code", "s")
    os.makedirs(sessions)
    for gen, size in ((99, 10), (100, 20)):
        name = f"{0:012d}-{size:012d}.jsonl"
        rel = os.path.join("raw", "claude-code", "s", f"g{gen}", name)
        _write(os.path.join(home, rel), "x" * size)
        with open(os.path.join(sessions, f"g{gen}.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": store.SCHEMA,
                    "agent": "claude-code",
                    "session_id": "s",
                    "source_path": "",
                    "generation": gen,
                    "diverged_from": None,
                    "size": size,
                    "file_sha256": store.EMPTY_SHA256,
                    "prev_manifest_sha256": None,
                    "segments": [{"path": rel, "start": 0, "end": size, "sha256": ""}],
                    "compact_boundaries": [],
                },
                fh,
            )
    assert daemon._recorded(home, time.time())[("claude-code", "s")][0] == 20


def test_the_watcher_survives_an_adapter_that_raises_anything_at_all(tmp_path):
    """The parse is for boundaries, and boundaries are what we are allowed to lose.

    The list was `(OSError, RecursionError, ValueError, KeyError)` and it was
    already wrong: the Claude Code adapter raises `AttributeError` on a content
    block whose `text` is not a string, and those blocks come from
    `toolUseResult` and `attachment` payloads that third-party MCP servers fill
    in and the adapter copies verbatim. [E4, review: store-contract 1]
    """
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN)

    class Boom:
        def parse(self, _path):
            raise AttributeError("'dict' object has no attribute 'strip'")

    said: list[str] = []
    orig = daemon.get_adapter
    daemon.get_adapter = lambda _name: Boom()
    try:
        cap = daemon.capture_one(home, src, "claude-code", log=said.append)
    finally:
        daemon.get_adapter = orig
    assert cap.appended == len(TURN), "bytes outrank boundaries"
    assert [m for m in said if "parse failed" in m], said


def test_a_transcript_rewritten_while_it_is_parsed_loses_its_boundaries_not_its_bytes(tmp_path):
    """The parse and the copy are two reads, and the offsets belong to the first.

    `store.capture` notices the rewrite and forks a generation, which drops the
    boundaries *carried* from the old manifest — and then writes the ones this
    function just parsed into the fresh manifest, where the next capture carries
    them forward for the life of the session. Nothing in the store checks an
    offset against the size it indexes into, so `byte_offset=138` survives into
    a 69-byte generation as a recorded fact. [E4, review]
    """
    home = str(tmp_path / "home")
    src = _write(str(tmp_path / "proj" / "a.jsonl"), TURN * 3)

    class Rewriter:
        """Parses, and replaces the file on the way out — a compaction, roughly."""

        def parse(self, path):
            _write(path, TURN)
            return Rewriter.Parsed()

        class Parsed:
            events = (type("E", (), {"kind": "compaction", "byte_offset": 2 * len(TURN)})(),)

    said: list[str] = []
    orig = daemon.get_adapter
    daemon.get_adapter = lambda _name: Rewriter()
    try:
        cap = daemon.capture_one(home, src, "claude-code", log=said.append)
    finally:
        daemon.get_adapter = orig

    assert cap.appended == len(TURN), "bytes outrank boundaries"
    manifests = [s.manifest for s in store.sessions(home)]
    assert len(manifests) == 1, manifests
    with open(manifests[0], encoding="utf-8") as fh:
        assert json.load(fh)["compact_boundaries"] == []
    assert store.verify(home) == []
    assert [m for m in said if "changed while parsing" in m], said


def test_a_pass_that_fails_in_a_way_nobody_predicted_does_not_kill_the_watcher(
    tmp_path, monkeypatch
):
    """The floor. A guarantee needs one, not a list of the ways people have
    fallen through so far. [E4, review]"""
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    monkeypatch.setattr(daemon, "tick", lambda *a, **kw: 1 / 0)
    lines: list[str] = []
    assert daemon.run(home, once=True, log=lines.append) == 1
    assert [line for line in lines if "pass failed" in line], lines


def test_the_error_rate_limiter_is_the_interval_not_the_poll(tmp_path, monkeypatch):
    """One error per stuck session per pass, forever.

    Measured at 100 unwritable sessions and the default five-second poll:
    2.06 M lines and 239 MB of stderr a day, all of it the same hundred
    sentences — 720x the rate the source comment beside it claimed. Said once
    when it appears, again when it changes, and never in between.
    [E4, review: CLI 4]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    bad = _write(str(root / "bad.jsonl"), TURN)
    os.chmod(bad, 0o000)
    _config(home, [root])
    lines: list[str] = []
    passes = {"n": 0}

    def stop_after_five():
        passes["n"] += 1
        if passes["n"] >= 5:
            raise KeyboardInterrupt

    _on_pass(monkeypatch, stop_after_five)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run(home, poll=0, interval=1e9, log=lines.append)
    finally:
        os.chmod(bad, 0o600)
    assert len([line for line in lines if "error:" in line]) == 1, lines


def test_a_git_that_is_briefly_unavailable_is_retried_rather_than_fatal(tmp_path, monkeypatch):
    """Two watchers starting within a few milliseconds collided and one died
    outright, six times out of six measured: `init` is four `git config` calls
    and each takes `.git/config`'s lock. [E4, review: concurrency 7]

    The retry is only visible across two passes, so `tick` stops the loop on its
    *second* call rather than its first. It used to stop on the first, which was
    enough only because a failed `init` skipped the pass entirely — the very
    thing daemon 7 fixed. Now both are asserted here: the pass whose `init`
    failed still ran, and the one after it re-tried the `init`.
    """
    home = str(tmp_path / "home")
    _config(home, [tmp_path / "proj"])
    calls = {"n": 0}
    real = gitrepo.init

    def flaky(h=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gitrepo.GitError("could not lock config file .git/config")
        return real(h)

    monkeypatch.setattr(daemon.gitrepo, "init", flaky)
    _on_pass(monkeypatch, lambda: None)
    lines: list[str] = []
    passes = {"n": 0}

    def counting_tick(*a, **kw):
        passes["n"] += 1
        if passes["n"] >= 2:
            raise KeyboardInterrupt
        return daemon.Tick()

    monkeypatch.setattr(daemon, "tick", counting_tick)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(home, poll=0, log=lines.append)
    assert calls["n"] == 2, "a transient init failure was not retried"
    assert passes["n"] == 2, "the pass whose init failed did no work"
    assert [line for line in lines if "git init" in line], lines


def test_a_repository_deleted_under_a_running_watcher_comes_back(tmp_path, monkeypatch):
    """`.git` is not a start-up fact, and treating it as one stopped versioning.

    `run` inits once per start and then sets a flag. Delete the repository while
    it is running and the flag is still True, so it never inits again: every
    later pass captures the bytes correctly, fails to commit, logs the same
    sentence, and leaves the store unversioned for ever. `verify` stays clean
    throughout, which is exactly why nothing notices.

    The first version of this test drove two `run(once=True)` calls with a real
    `rm -rf` between them, and its negative control caught it: `started` is a
    local, so every `once=True` call inits unconditionally and the test passed
    against the unfixed code. The flag only persists *within* one `run`, so the
    bug is only reachable across passes of a single call — which is what this
    does, deleting the repository from inside `sleep` between pass one and pass
    two and leaving through the `KeyboardInterrupt` that `run` re-raises.
    [E4, review: CLI 9]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = str(root / "a.jsonl")
    _write(src, TURN)
    _config(home, [root], git=False)

    # The pass boundary is `_on_pass`, which is where the reasoning about why
    # `time.sleep` cannot simply be counted now lives. [E4, review: CLI 9]
    passes: list[int] = []
    deleted: list[int] = []

    def on_pass():
        passes.append(1)
        assert len(passes) < 10, "run never reached a pass that could commit"
        if not deleted:
            # `isdir` as well: `run` sleeps in two places, the end of a pass and
            # the retry after `init` fails, and deleting what a failed init left
            # behind would raise here instead of testing anything.
            if os.path.isdir(os.path.join(home, ".git")):
                shutil.rmtree(os.path.join(home, ".git"))
                _write(src, TURN, append=True)
                deleted.append(1)
            return None
        raise KeyboardInterrupt

    _on_pass(monkeypatch, on_pass)
    lines: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        daemon.run(home, poll=0, interval=0, log=lines.append)

    assert deleted, "the repository was never deleted; the test proves nothing"
    assert os.path.isdir(os.path.join(home, ".git")), "the repository was not rebuilt"
    committed = [line for line in lines if "commit=" in line and "commit=None" not in line]
    assert len(committed) == 2, f"both passes must commit: {lines}"
    assert store.verify(home) == []


def test_two_names_for_one_file_are_discovered_once(tmp_path):
    """Deduplication is by device and inode, because paths lie about identity.

    The motivating case is APFS, where `~/Projects` and `~/projects` are one
    directory and `realpath` corrects neither spelling — two watch roots, two
    strings per file, two `session_id_for` values, and the same bytes captured
    into two sessions that both look healthy to `verify`. It is awkward to force
    a case collision in a test that must also run on a case-sensitive
    filesystem, so the same condition is made with a hard link: two names, one
    inode, and capturing it twice is duplication either way. [E4, review]
    """
    root = tmp_path / "proj"
    root.mkdir()
    _write(str(root / "a.jsonl"), TURN)
    os.link(root / "a.jsonl", root / "b.jsonl")

    found = daemon.discover([daemon.Watch(agent="claude-code", roots=(str(root),))])

    assert [os.path.basename(p) for _, p in found] == ["a.jsonl"], "one file, one entry"


# --- E4 review round: the ten watcher findings --------------------------------


def test_one_unreadable_session_does_not_suspend_every_other_one(tmp_path, monkeypatch):
    """The capture guarantee cannot be conditional on an exception taxonomy.

    `capture_one`'s own docstring makes this argument and then `tick` caught
    `(OSError, RuntimeError, ValueError)` around the call to it — a type
    denylist. A `KeyError` from one session's manifest escapes `tick` into
    `run`'s floor, which reports `pass failed` and abandons the *whole* pass. So
    one damaged session stops capture for every session on the machine, on every
    pass, for ever, while `verify` stays clean. [E4, review: daemon 1]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    bad = _write(str(root / "bad.jsonl"), TURN)
    good = _write(str(root / "good.jsonl"), TURN)
    _config(home, [root])

    real = daemon.capture_one

    def one_bad(h, source, agent, **kw):
        if source == os.path.realpath(bad):
            raise KeyError("diverged_from")
        return real(h, source, agent, **kw)

    monkeypatch.setattr(daemon, "capture_one", one_bad)
    result = daemon.tick(home, daemon.load_watches(home), interval=0)

    assert [k for k in result.captured if "good" in k], result
    assert any("diverged_from" in e for e in result.errors), result.errors
    assert store.sessions(home), "nothing was captured at all"
    assert os.path.realpath(good)  # the source is still there either way


def test_a_gitignore_that_is_not_utf8_does_not_make_the_watcher_unstartable(tmp_path):
    """`init` rewrites `.gitignore` because it is ours; it has to be able to read it.

    The read is `encoding="utf-8"` under `except OSError`, and a
    `UnicodeDecodeError` is a `ValueError`. It escaped `init`, escaped `run`'s
    `(GitError, OSError, SubprocessError)`, and exited 2 — on every restart,
    because the byte is still in the file. [E4, review: daemon 2]
    """
    home = str(tmp_path / "home")
    os.makedirs(home)
    with open(os.path.join(home, ".gitignore"), "wb") as fh:
        fh.write(b"raw/\n\xff\xfe not text\n")

    gitrepo.init(home)

    with open(os.path.join(home, ".gitignore"), encoding="utf-8") as fh:
        assert fh.read() == gitrepo.GITIGNORE


def test_a_config_that_is_not_utf8_is_reported_without_being_printed(tmp_path):
    """`load_watches` promises absent, unreadable and malformed all mean `[]`.

    `tomllib.load` raises `UnicodeDecodeError` on a non-UTF-8 byte, which is a
    `ValueError` and not a `TOMLDecodeError`, so it escaped the function that
    documents itself as total. `run`'s floor caught it and logged `{exc!r}` —
    and `repr(UnicodeDecodeError)` carries the offending object, so the whole
    config file went to stderr once per pass. [E4, review: daemon 3]
    """
    home = str(tmp_path / "home")
    os.makedirs(home)
    with open(os.path.join(home, "config.toml"), "wb") as fh:
        fh.write(b'[[watch]]\nagent = "claude-code"\nroots = ["/tmp/SECRETMARKER"]\n\xff\n')

    said: list[str] = []
    assert daemon.load_watches(home, log=said.append) == []
    assert said, "a config that cannot be read must be reported"
    assert not any("SECRETMARKER" in m for m in said), said


def test_a_root_that_cannot_be_resolved_skips_only_itself(tmp_path):
    """`os.path.realpath` raises `ValueError` on an embedded NUL. [E4, review: daemon 3]"""
    home = str(tmp_path / "home")
    good = tmp_path / "proj"
    good.mkdir()
    _write(
        os.path.join(home, "config.toml"),
        f'[[watch]]\nagent = "claude-code"\nroots = ["/tmp/a\\u0000b", {json.dumps(str(good))}]\n',
    )

    said: list[str] = []
    got = daemon.load_watches(home, log=said.append)

    assert [w.roots for w in got] == [(os.path.realpath(str(good)),)]
    assert any("a\x00b" not in m and "skipped" in m for m in said), said


def test_an_unchanged_transcript_is_not_rehashed_on_every_pass(tmp_path, monkeypatch):
    """A source mtime ahead of its manifest re-read the whole file for ever.

    `changed` is `size differs or source mtime > manifest mtime`, and a capture
    that appends nothing leaves the manifest byte-identical *and* untouched — so
    the manifest's mtime never catches up, `now - mtime` only grows, and every
    pass after the first interval re-hashes the entire transcript. Measured on a
    21 MB file: 2766 ms per pass, 55% of a core, nothing logged.
    [E4, review: daemon 4]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)

    # The condition: the source looks newer than the manifest, with the same
    # bytes. A `touch` is the ordinary way to reach it; a restore is the common
    # one.
    ahead = time.time() + 60
    os.utime(src, (ahead, ahead))

    calls = {"n": 0}
    real = daemon.capture_one

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(daemon, "capture_one", counting)
    for i in range(3):
        daemon.tick(home, watches, interval=0, now=ahead + 120 + i)

    assert calls["n"] == 1, "the store re-read a transcript it had already captured"


def test_an_empty_transcript_does_not_bypass_the_interval_gate(tmp_path, monkeypatch):
    """A zero-byte transcript is a session, and the gate has to know it.

    `size < 0` means "never captured" and bypasses the interval, so a session
    the store does not list runs at full poll rate for ever — which is what an
    empty transcript did, because `store.sessions` dropped a generation with no
    segments. A pin rather than a fix: the store's own review round closed this
    from the other side, and its negative control is here — restore
    `if not run: continue` in `sessions()` and this test fails.
    [E4, review: daemon 5, closed by store 6]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "empty.jsonl"), "")
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)

    calls = {"n": 0}
    real = daemon.capture_one

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(daemon, "capture_one", counting)
    for _ in range(3):
        daemon.tick(home, watches, interval=1e9)

    assert calls["n"] == 0, "an empty transcript was captured on every pass"


def test_the_most_specific_watch_claims_a_transcript(tmp_path):
    """Nested roots are resolved by depth, not by the order they were written in.

    `discover` shared one `seen` set across watches and took the first one to
    reach a file. So a general root listed above a nested, more specific one
    claimed that one's transcripts and filed them under the wrong agent — which
    means the wrong adapter, which means no boundaries, silently.
    [E4, review: daemon 6]
    """
    outer = tmp_path / "agents"
    inner = outer / "kimi"
    src = _write(str(inner / "s.jsonl"), TURN)

    found = daemon.discover(
        [
            daemon.Watch(agent="claude-code", roots=(str(outer),)),
            daemon.Watch(agent="kimi-code", roots=(str(inner),)),
        ]
    )

    assert [(w.agent, p) for w, p in found] == [("kimi-code", os.path.realpath(src))]


def test_a_git_init_failure_does_not_cost_the_pass_its_bytes(tmp_path, monkeypatch):
    """ "A git failure is reported, never raised" — `tick` keeps that rule; `run` did not.

    A transient `init` failure made `run` `continue`, skipping the capture as
    well as the commit. The bytes are the part that cannot be recovered later; a
    commit can always be made by the next pass. [E4, review: daemon 7]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    monkeypatch.setattr(
        daemon.gitrepo, "init", lambda h=None: (_ for _ in ()).throw(gitrepo.GitError("locked"))
    )

    lines: list[str] = []
    daemon.run(home, once=True, interval=0, log=lines.append)

    assert store.sessions(home), f"the bytes were lost to a git failure: {lines}"
    assert any("git init" in line for line in lines), lines


def test_a_hook_record_no_watch_covers_is_reported(tmp_path, monkeypatch):
    """`spool_dropped` is the only signal that the hook and the config disagree.

    It was counted and thrown away, so a hook installed against one path and a
    watcher configured for another looked exactly like a quiet machine.
    [E4, review: daemon 8]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    _spool(home, "1-PreCompact.json", {"transcript_path": str(tmp_path / "elsewhere.jsonl")})

    lines: list[str] = []
    _on_pass(monkeypatch, lambda: (_ for _ in ()).throw(KeyboardInterrupt))
    daemon.run(home, once=True, interval=0, log=lines.append)

    assert any("no watch covers" in line for line in lines), lines


def test_the_error_rate_limit_survives_interval_zero(tmp_path, monkeypatch):
    """`--interval 0` is a documented, parser-blessed capture setting.

    It was also the error-log rate limit, so choosing it re-enabled the flood
    the limit exists to stop — measured at 2.06 M lines a day. Two knobs, one
    name. [E4, review: daemon 9]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    bad = _write(str(root / "bad.jsonl"), TURN)
    os.chmod(bad, 0o000)
    _config(home, [root])
    lines: list[str] = []
    passes = {"n": 0}

    def stop_after_five():
        passes["n"] += 1
        if passes["n"] >= 5:
            raise KeyboardInterrupt

    _on_pass(monkeypatch, stop_after_five)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run(home, poll=0, interval=0, log=lines.append)
    finally:
        os.chmod(bad, 0o600)
    assert len([line for line in lines if "error:" in line]) == 1, lines


def test_an_absolute_pattern_is_refused_rather_than_walked(tmp_path):
    """`os.path.join(root, pattern)` discards the root when the pattern is absolute.

    The watch then globs from `/` every poll — 0.743 s a pass, measured — and
    `_covers` throws every result away, so it is pure cost in silence. Nothing
    escapes; what leaks is the walk and the quiet. [E4, review: daemon 10]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    root.mkdir()
    _config(home, [root], pattern="/**/*.jsonl")

    said: list[str] = []
    got = daemon.load_watches(home, log=said.append)

    assert [w.pattern for w in got] == [daemon.PATTERN]
    assert any("pattern" in m for m in said), said


def test_a_pattern_the_matcher_refuses_does_not_stop_every_watch(tmp_path):
    """One watch's `pattern = "."` stopped capture for the whole machine, for ever.

    `glob.glob` took `.`, `./` and `./.` and matched the root itself, which
    `_covers` then dropped. `Path.glob` raises `ValueError: Unacceptable
    pattern` on all three — `PurePath` parses them to no components — and
    `_hits` runs inside `discover`, which `tick` calls outside any `try`. So the
    raise reached `run`'s floor as `pass failed`, and since the config is re-read
    every pass it kept reaching it: one bad pattern, every watch dark, no
    capture, rc=1 and nothing else said. An embedded NUL, which `tomllib`
    accepts, does the same thing one layer down in `lstat`. [review: paths 2]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    _write(str(root / "a.jsonl"), TURN)

    for bad in (".", "./", "./.", "a\x00b"):
        _config(home, [root], pattern=bad)
        said: list[str] = []
        got = daemon.load_watches(home, log=said.append)
        assert [w.pattern for w in got] == [daemon.PATTERN], bad
        assert any("names nothing" in m for m in said), (bad, said)
        # And the pass it would have killed runs: the root is still watched.
        assert daemon.tick(home, got, interval=0).appended == len(TURN), bad
        shutil.rmtree(os.path.join(home, "raw"), ignore_errors=True)
        shutil.rmtree(os.path.join(home, "sessions"), ignore_errors=True)


def test_a_root_of_slash_is_refused_for_being_slash(tmp_path):
    """`docs/watching.md` documents this refusal; the code could not reach it.

    `_inside(root, home)` asks whether the store is at or below the root, and
    everything is at or below `/`, so the store check above caught it first and
    said the wrong thing: "contains the store itself" — true, and not the
    reason. The branch that names the actual hazard, a filesystem-wide walk
    every poll, was dead code. [E4, review: docs 8]
    """
    home = str(tmp_path / "home")
    _write(
        os.path.join(home, "config.toml"),
        '[[watch]]\nagent = "claude-code"\nroots = ["/"]\n',
    )

    said: list[str] = []
    assert daemon.load_watches(home, log=said.append) == []
    assert any("whole filesystem" in m for m in said), said


# --- E7 fs-F3: blocking is not an exception ----------------------------------


def _drain_within(home: str, seconds: float = 5.0):
    """`drain_spool` on a thread, so a wedge is a failure and not a hung suite.

    The bug under test is an `open()` that never returns. Calling it directly
    would not fail this test, it would hang the whole run — and the mutation
    harness scores a wedged mutant as BROKEN, not as caught. A daemon thread
    that outlives the assertion is exactly right here: the interpreter will not
    wait for it. [E7 fs-F3]
    """
    import threading

    out: list = []
    t = threading.Thread(
        target=lambda: out.append(daemon.drain_spool(home, daemon.load_watches(home))),
        daemon=True,
    )
    t.start()
    t.join(seconds)
    assert out, f"drain_spool did not return within {seconds}s — the pass is wedged"
    return out[0]


def test_a_fifo_in_the_spool_does_not_wedge_the_pass(tmp_path):
    """One `mkfifo` used to stop every capture on the machine, for ever.

    `open(path, "rb")` on a FIFO waits for a writer. The floor around the read
    catches `(OSError, ValueError, RecursionError)` and then everything, and it
    caught nothing, because blocking raises nothing. Measured through the CLI
    before the fix: `gitmemory watch --once` with one FIFO in the spool was
    still running after eight seconds, having logged nothing, captured nothing
    and committed nothing — and the FIFO is unlinked only after the read, so
    the next start does it again. Anything that can make a directory entry in
    `spool/` triggers it. [E7 fs-F3]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    os.makedirs(os.path.join(home, daemon.SPOOL), exist_ok=True)
    os.mkfifo(os.path.join(home, daemon.SPOOL, "1-Stop.json"))
    _spool(home, "2-Stop.json", {"transcript_path": src})

    wanted, tick = _drain_within(home)

    assert tick.spool_unreadable == 1, "a FIFO is not a record"
    assert os.listdir(os.path.join(home, daemon.SPOOL)) == [], "an unread record is for ever"
    # The record behind it still rang the doorbell: one unusable entry must not
    # cost the pass the records it could have used.
    assert wanted == {_key(src): False}


def test_a_symlinked_spool_record_is_not_read_through(tmp_path):
    """`O_NOFOLLOW`, so a link cannot aim the read outside the spool.

    Same open, same flags, and the reason it is here rather than assumed: a
    FIFO check that used `os.stat` instead of an `fstat` on the opened
    descriptor would pass the test above and still follow this link. [E7 fs-F3]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    target = _write(str(tmp_path / "elsewhere" / "real.json"), json.dumps({"path": src}))
    os.makedirs(os.path.join(home, daemon.SPOOL), exist_ok=True)
    os.symlink(target, os.path.join(home, daemon.SPOOL, "1-Stop.json"))

    wanted, tick = _drain_within(home)

    assert (tick.spool_consumed, tick.spool_unreadable) == (1, 1)
    assert wanted == {}, "the watcher read a file the spool only pointed at"
    assert os.path.exists(target), "the link was removed, not the file it named"


def test_a_fifo_with_a_writer_is_not_a_record_even_though_it_reads(tmp_path):
    """`O_NONBLOCK` stops the wait; only the `fstat` stops the injection.

    A FIFO with a live writer attached hands back whatever that process wrote,
    without blocking — measured: `read()` returned the payload whole. So the
    flag alone leaves a spool record whose bytes come from a running process
    rather than from a file a hook atomically renamed into place, which is a
    different trust story and the one the record format exists to pin down.
    The `fstat` is on the opened descriptor, not on the path, so there is no
    window between the question and the read. [E7 fs-F3]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    os.makedirs(os.path.join(home, daemon.SPOOL), exist_ok=True)
    fifo = os.path.join(home, daemon.SPOOL, "1-PreCompact.json")
    os.mkfifo(fifo)

    # Reader first: a writer-only open of a FIFO nobody is reading is ENXIO.
    rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    wfd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(wfd, json.dumps({"transcript_path": src}).encode())
        wanted, tick = _drain_within(home)
    finally:
        os.close(wfd)
        os.close(rfd)

    assert wanted == {}, "a live process fed the watcher a record and it took it"
    assert tick.spool_unreadable == 1


# --- E7 fs-F4: a repair that reads as a quiet pass ---------------------------


def test_an_adoption_only_pass_names_what_it_reclaimed(tmp_path):
    """`result.captured` is empty and `+0B` is the headline, so nothing else says it.

    Adoption attests to bytes found in the generation directory rather than
    copied out of the source. The manifest records that permanently; this is so
    the operator hears it at the time, because the pass that does it looks from
    the outside exactly like a pass that did nothing. [E7 fs-F4]
    """
    home = str(tmp_path / "home")
    root = tmp_path / "proj"
    src = _write(str(root / "a.jsonl"), TURN)
    _config(home, [root])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)

    base = os.path.getsize(src)
    _write(src, TURN.replace("hello", "second"), append=True)
    with open(src, "rb") as fh:  # the orphan a killed capture leaves behind
        fh.seek(base)
        tail = fh.read()
    name = f"{base:012d}-{base + len(tail):012d}.jsonl"
    sid = os.listdir(os.path.join(home, "raw", "claude-code"))[0]
    _write(os.path.join(home, "raw", "claude-code", sid, "g00", name), "")
    with open(os.path.join(home, "raw", "claude-code", sid, "g00", name), "wb") as fh:
        fh.write(tail)

    first: list[str] = []
    daemon.run(home, once=True, poll=0, interval=0, log=first.append)
    after: list[str] = []
    daemon.run(home, once=True, poll=0, interval=0, log=after.append)

    said = [line for line in first if "adopted" in line]
    assert said == [
        f"adopted 1 segment(s) found on disk, not copied from the source: "
        f"claude-code/{sid}/g00/{name}"
    ], first
    assert not any("captured" in line for line in first), "this pass copies nothing out"
    assert not any("adopted" in line for line in after), after


# --- E7 fs-F10: the window between discover and the read ---------------------


def test_a_link_planted_after_discover_does_not_reach_the_store(tmp_path):
    """The attack shape, at the two call sites `tick` performs in this order.

    `discover` resolves and bounds-checks every path it returns; `capture_one`
    then reads it. Between those, the name can change. Reproduced before
    fixing: the deterministic swap below stored the key every time, and 400
    ordinary `tick` calls against an uncooperative thread flipping the name
    stored it 7 times — as attested segments that `verify` called clean.

    The regression this pins is not only the missing `O_NOFOLLOW`. The first
    fix also put a `realpath` in `store.capture`, which re-resolved the link a
    moment before the open that was meant to refuse it; the deterministic arm
    still leaked, and so did 3 of 400 ticks. A path resolved twice is a path
    resolved at the wrong time. [E7 fs-F10]
    """
    home, root = str(tmp_path / "h"), tmp_path / "proj"
    secret = tmp_path / "private" / "id_rsa"
    os.makedirs(secret.parent, mode=0o700)
    secret.write_text("-----BEGIN PRIVATE KEY-----\nsk-live-AAAABBBBCCCC\n")
    src = _write(root / "a.jsonl", TURN)
    _config(home, [str(root)])
    watches = daemon.load_watches(home)

    found = daemon.discover(watches)
    assert [p for _, p in found] == [os.path.realpath(src)]

    os.unlink(src)
    os.symlink(str(secret), src)  # the window

    for watch, path in found:
        with pytest.raises(RuntimeError, match="is a symlink"):
            daemon.capture_one(home, path, watch.agent)

    held = b"".join(
        Path(r, f).read_bytes()
        for r, _, fs in os.walk(os.path.join(home, "raw"))
        for f in fs
    )
    assert b"sk-live" not in held


def test_the_session_recovers_once_the_real_transcript_is_back(tmp_path):
    """The other direction: a refusal is a skipped pass, not a broken session.

    The raise is new and it comes from a place nothing raised from before —
    inside the read, after the session lock is taken and after `_adopt_orphans`
    has already moved files. If it left the lock held, a stray `.incoming`
    behind, or a half-written generation, the watcher would be stuck on that
    session for good and the next pass would say so. [E7 fs-F10]
    """
    home, root = str(tmp_path / "h"), tmp_path / "proj"
    secret = _write(tmp_path / "private" / "id_rsa", "sk-live-AAAABBBBCCCC\n")
    src = _write(root / "a.jsonl", TURN)
    _config(home, [str(root)])
    watches = daemon.load_watches(home)
    daemon.tick(home, watches, interval=0)

    real = os.path.realpath(src)
    os.unlink(src)
    os.symlink(secret, src)
    # Two defences, two windows: a link that is already there when the sweep
    # runs never reaches `capture_one` at all, because `discover` resolves it
    # and `_covers` sees it leave the root. The refusal below is for the link
    # that arrives after that check has passed.
    assert daemon.discover(watches) == []
    with pytest.raises(RuntimeError, match="is a symlink"):
        daemon.capture_one(home, real, "claude-code")

    os.unlink(src)
    _write(root / "a.jsonl", TURN * 2)

    assert daemon.tick(home, watches, interval=0).captured
    assert store.verify(home) == []
    assert not list(tmp_path.glob("h/raw/**/.incoming*"))


# --- E7 fs-F1: a refused init must stop the commit, not just log ------------


def _foreign_config(home: str, tmp_path) -> None:
    """Make `_assert_no_foreign_config` fire: a setting arriving from outside.

    `core.excludesFile` matching `*.jsonl` is the reviewer's own demonstration,
    and it is the worst case rather than an arbitrary one — it excludes exactly
    the segments, so the manifest gets committed and the bytes it vouches for
    do not.
    """
    excludes = tmp_path / "excludes"
    excludes.write_text("*.jsonl\n")
    include = tmp_path / "inc"
    include.write_text(f"[core]\n\texcludesFile = {excludes}\n")
    with open(os.path.join(home, ".git", "config"), "a", encoding="utf-8") as fh:
        fh.write(f"[include]\n\tpath = {include}\n")


def _in_history(home: str) -> list[str]:
    """What git would actually send. `gitrepo.tracked` is a different question —
    it includes unignored files that have never been committed."""
    got = subprocess.run(
        ["git", "-C", home, "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
    )
    return got.stdout.split() if got.returncode == 0 else []


def test_a_watcher_whose_init_is_refused_captures_and_does_not_commit(tmp_path):
    """`init` refusing and `run` committing anyway cancelled each other out.

    Measured before fixing, through `run` and not through the parameter: the
    watcher logged `error: git init: … is not isolated` and on the next line
    `captured 1 … commit=81f6cf53`, leaving the manifest tracked and the
    segment it names untracked, with `verify` returning clean — the scenario
    `_assert_no_foreign_config`'s docstring describes, reached with the
    assertion firing correctly and being ignored. [E7 fs-F1]
    """
    home, root = str(tmp_path / "h"), tmp_path / "proj"
    _write(root / "a.jsonl", TURN)
    _config(home, [str(root)])
    _foreign_config(home, tmp_path)

    lines: list[str] = []
    daemon.run(home, once=True, poll=0, interval=0, log=lines.append)

    said = "\n".join(lines)
    assert "git init:" in said and "not isolated" in said
    assert "captured, not committed" in said
    assert "commit=None" in said
    assert _in_history(home) == []
    assert store.verify(home) == []  # the bytes are in the store regardless


def test_the_backlog_is_committed_whole_once_init_succeeds(tmp_path):
    """Waiting is only acceptable because nothing is lost by waiting.

    git commits the tree, not the pass, so the first successful `init` picks up
    every segment the refused passes captured. Without this the fix would be a
    worse bug than the one it closes. [E7 fs-F1]
    """
    home, root = str(tmp_path / "h"), tmp_path / "proj"
    _write(root / "a.jsonl", TURN)
    _config(home, [str(root)])
    _foreign_config(home, tmp_path)
    for _ in range(3):
        _write(root / "a.jsonl", TURN, append=True)
        daemon.run(home, once=True, poll=0, interval=0, log=lambda _m: None)
    assert _in_history(home) == []

    cfg = os.path.join(home, ".git", "config")
    Path(cfg).write_text(Path(cfg).read_text().split("[include]")[0])
    _write(root / "a.jsonl", TURN, append=True)
    daemon.run(home, once=True, poll=0, interval=0, log=lambda _m: None)

    shipped = _in_history(home)
    assert [p for p in shipped if p.startswith("raw/")], shipped
    # Part (b) of the finding, closed by construction rather than by a second
    # guard: `init` writes `.gitignore`, so a store whose `init` never succeeded
    # had none, and three refused passes put a half-written `.incoming` segment
    # and a `.tmp` manifest into history for ever. Nothing commits now until
    # `init` has run, and `init` writes the file before it returns.
    assert not [p for p in shipped if ".incoming" in p or ".tmp" in p], shipped
    assert store.verify(home) == []


def test_a_standing_init_failure_is_not_logged_once_per_poll(tmp_path, monkeypatch):
    """~17k lines a day, at the default interval, from the one error not throttled.

    Every other repeating error in this loop goes through `ERROR_REPEAT`; this
    one was logged directly, which is also how the same loop measured 2.06 M
    lines a day before that limit existed. [E7 fs-F1]
    """
    home, root = str(tmp_path / "h"), tmp_path / "proj"
    _write(root / "a.jsonl", TURN)
    _config(home, [str(root)])
    _foreign_config(home, tmp_path)

    lines: list[str] = []
    # Stopped from the poll sleep, not from `log`: the whole point is that the
    # later passes say nothing, so a counter on the log would never fire.
    #
    # Through `_on_pass`, and that is the [E7 pair review] half of this test. It
    # was written as a bare `monkeypatch.setattr(daemon.time, "sleep", ...)`,
    # which is the exact patch `_on_pass` exists to replace — `subprocess`'s own
    # wait loop sleeps too, `gitrepo` always passes a timeout, and a `git
    # config` slow enough to be polled twice arrives here as a pass boundary. It
    # flaked about one run in twenty of this file and once in a full suite,
    # sometimes as one log line short and sometimes as none at all, because the
    # ten passes were spent inside `gitrepo.init` before the first one finished.
    # The helper's docstring is the write-up of that same bug, found in three
    # other tests two epochs ago; this one was written afterwards and did not
    # use it.
    passes = iter(range(10))
    _on_pass(monkeypatch, lambda: next(passes))
    with pytest.raises(StopIteration):
        daemon.run(home, poll=0, interval=0, log=lines.append)

    # Two, not one: the pass that captures reports two errors and the quiet
    # passes after it report one, and a *changed* error list is said
    # immediately by design. Ten passes, two lines — before the fix it was ten.
    assert sum("git init:" in m for m in lines) == 2, lines


def test_the_pass_counter_counts_passes_and_not_every_sleep(monkeypatch):
    """A floor under `_on_pass`, which seven tests above now stop themselves with.

    The helper is a test helper and had no test, so the one thing it does — tell
    `run`'s poll apart from `subprocess`'s wait loop by duration — was held up
    by nothing but the comment explaining it. The test above was written without
    the helper and reproduced the flake it was written for, two epochs later, so
    "everybody knows" is not holding this. Deterministic, because it asks the
    discriminator directly rather than racing a `git` subprocess for it.
    [E7 pair review]
    """
    fired: list[float] = []
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    _on_pass(monkeypatch, lambda: fired.append(0.0), poll=0)

    time.sleep(0)  # `run`'s own poll
    assert fired == [0.0] and slept == []

    # `subprocess.Popen._wait`'s doubling sequence, which starts at 0.0005 and
    # is never zero. Passed through to the real sleep — which this test has
    # itself replaced, so the assertion is that it arrives there and not here.
    for delay in (0.0005, 0.001, 0.002):
        time.sleep(delay)
    assert fired == [0.0], "a subprocess wait was counted as a pass"
    assert slept == [0.0005, 0.001, 0.002]
