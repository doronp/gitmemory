# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""Recall's default scope: this project's user and assistant text.

A recalled line is read back into an agent's context, so what recall returns
by default is a decision about what reaches it. These tests pin which
directory is which project, and that the narrowing refills `-k`, is counted
when it hides something, and is silent when it hides nothing. Every
repository here is a fake `.git` in tmp_path: identity is read from the
filesystem and git is never run.
"""

from __future__ import annotations

import os
import threading

import pytest
from test_index import assistant, nowhere, user, write

from gitmemory import index, scope, store
from gitmemory.__main__ import main


def repo(path) -> str:
    """A repository as `project_id` sees one: a directory holding `.git/`."""
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    return str(path)


def gitfile(path, target: str) -> str:
    """A directory whose `.git` is a file, as a worktree or a submodule has."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, ".git"), "w") as fh:
        fh.write(f"gitdir: {target}\n")
    return str(path)


def gitdir(path) -> str:
    """What a `.git` file points at: a directory holding `HEAD`."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    return str(path)


# --------------------------------------------------------------------------- #
# which project a directory is in
# --------------------------------------------------------------------------- #


def test_a_subdirectory_is_in_its_repositorys_project_and_a_sibling_is_not(tmp_path):
    a = repo(tmp_path / "a")
    os.makedirs(tmp_path / "a" / "src" / "deep")
    ab = repo(tmp_path / "ab")
    assert scope.project_id(os.path.join(a, "src", "deep")) == scope.project_id(a)
    assert scope.project_id(ab) != scope.project_id(a)


def test_a_linked_worktree_joins_its_repository(tmp_path):
    """Inside the repo (`.claude/worktrees/`) or beside it (`git worktree add
    ../x`), relative gitdir or absolute: one repository, one project."""
    a = repo(tmp_path / "a")
    for name in ("inner", "outer"):
        gitdir(os.path.join(a, ".git", "worktrees", name))
    inner = gitfile(
        tmp_path / "a" / ".claude" / "worktrees" / "inner", "../../../.git/worktrees/inner"
    )
    outer = gitfile(tmp_path / "outer", os.path.join(a, ".git", "worktrees", "outer"))
    assert scope.project_id(inner) == scope.project_id(a)
    assert scope.project_id(outer) == scope.project_id(a)


def test_a_worktree_joins_the_repository_its_commondir_names(tmp_path):
    """Git writes `commondir` into a linked worktree's gitdir, and it is the
    answer wherever that gitdir lives; the `worktrees` parent is only the
    fallback for a gitdir without one."""
    main_ = repo(tmp_path / "main")
    meta = gitdir(tmp_path / "meta" / "wt1")
    with open(os.path.join(meta, "commondir"), "w") as fh:
        fh.write("../../main/.git\n")
    wt = gitfile(tmp_path / "wt", meta)
    assert scope.project_id(wt) == scope.project_id(main_)
    lone = gitfile(tmp_path / "lone", gitdir(tmp_path / "meta" / "wt2"))
    assert scope.project_id(lone) != scope.project_id(main_)


def test_a_deleted_worktree_still_resolves_to_its_repository(tmp_path):
    """A session's directory outlives it: a removed worktree is looked up
    from its nearest ancestor that still exists."""
    a = repo(tmp_path / "a")
    gone = os.path.join(a, ".claude", "worktrees", "gone", "src")
    assert scope.project_id(gone) == scope.project_id(a)


def test_a_deleted_directory_outside_any_repository_is_only_itself(tmp_path):
    """The ancestor is where to look for a `.git`, not what the directory was:
    two removed temp dirs under one parent are two projects, and neither is
    the parent's."""
    os.makedirs(tmp_path / "plain")
    ids = [scope.project_id(str(tmp_path / "plain" / n)) for n in ("gone-a", "gone-b", "")]
    assert len(set(ids)) == 3
    assert scope.project_id(str(tmp_path / "plain" / "gone-a")) == ids[0]
    assert scope.project_id("/no-such-root-a/x") != scope.project_id("/no-such-root-b/x")


def test_a_submodule_and_a_nested_clone_are_their_own_projects(tmp_path):
    """A vendored clone and a submodule are other repositories: their files
    are someone else's words, so they are not this project's."""
    a = repo(tmp_path / "a")
    gitdir(os.path.join(a, ".git", "modules", "sub"))
    sub = gitfile(tmp_path / "a" / "sub", "../.git/modules/sub")
    nested = repo(tmp_path / "a" / "vendor" / "lib")
    ids = {scope.project_id(p) for p in (a, sub, nested)}
    assert len(ids) == 3
    assert scope.project_id(os.path.join(sub, "x")) == scope.project_id(sub)


def test_an_unreadable_git_file_is_still_a_repository(tmp_path):
    """No `gitdir:` line is a repository nobody can follow: it stops the walk
    there, so the directory above it is not where its lines go."""
    a = repo(tmp_path / "a")
    odd = tmp_path / "a" / "odd"
    odd.mkdir()
    (odd / ".git").write_text("not a pointer\n")
    os.makedirs(odd / "src")
    assert scope.project_id(str(odd / "src")) == scope.project_id(str(odd))
    assert scope.project_id(str(odd)) != scope.project_id(a)


def test_a_git_file_that_points_at_no_repository_is_its_own(tmp_path):
    """A `gitdir:` line is followed only to a directory holding `HEAD`, and
    only when it is one clean line: anything else would let one stray file
    alias its directory to whatever it names."""
    main_ = repo(tmp_path / "main")
    os.makedirs(tmp_path / "bare-meta")
    bad = {
        "missing": f"gitdir: {tmp_path / 'nowhere'}\n",
        "headless": f"gitdir: {tmp_path / 'bare-meta'}\n",
        "aliased": f"gitdir: {os.path.join(main_, '.git')}\n",
        "nul": f"gitdir: {os.path.join(main_, '.git')}\0x\n",
    }
    for name, text in bad.items():
        d = tmp_path / name
        os.makedirs(d / "src")
        (d / ".git").write_text(text)
        assert scope.project_id(str(d)) != scope.project_id(main_), name
        assert scope.project_id(str(d / "src")) == scope.project_id(str(d)), name


def test_a_fifo_named_git_does_not_stop_recall(tmp_path):
    """Stored cwds come from transcripts, and every one is resolved: a `.git`
    that is not a regular file must never be opened and waited on."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    d = tmp_path / "piped"
    d.mkdir()
    os.mkfifo(d / ".git")
    out = []
    t = threading.Thread(target=lambda: out.append(scope.project_id(str(d / "sub"))), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        # Free the reader: opening the write end lets its open() return.
        os.close(os.open(d / ".git", os.O_WRONLY | os.O_NONBLOCK))
    assert out, "project_id blocked on a FIFO named .git"
    assert out[0] == scope.project_id(str(d))


def test_a_dotfiles_repository_in_home_is_a_project_and_the_walk_stops_there(
    tmp_path, monkeypatch
):
    """`~` is looked at like any level, so a dotfiles repository there is a
    project with what is under it; nothing above `~` is, so a `.git` beside
    the home directories does not gather every user's work into one."""
    home = tmp_path / "outer" / "home"
    os.makedirs(home / "one")
    os.makedirs(home / "two")
    repo(tmp_path / "outer")
    monkeypatch.setenv("HOME", str(home))
    one, two = (scope.project_id(str(home / n)) for n in ("one", "two"))
    outer = scope.project_id(str(tmp_path / "outer"))
    assert len({one, two, scope.project_id(str(home)), outer}) == 4
    assert one[0] == "dir"
    assert scope.project_id("~") == scope.project_id(str(home))
    repo(home)
    assert scope.project_id(str(home))[0] == "git"
    assert scope.project_id(str(home / "one")) == scope.project_id("~")
    assert scope.project_id("/") != scope.project_id(str(tmp_path))


def test_a_checkout_with_no_git_entry_is_scoped_by_directory(tmp_path):
    """A `--separate-git-dir` work tree or a bare repository leaves no `.git`
    to find, and nothing else is guessed: each directory is its own scope,
    matched by equality."""
    work = tmp_path / "work"
    os.makedirs(work / "a")
    os.makedirs(work / "b")
    ids = [scope.project_id(str(p)) for p in (work, work / "a", work / "b")]
    assert len(set(ids)) == 3
    assert all(i[0] == "dir" for i in ids)
    assert scope.project_id(str(work / "a" / ".")) == ids[1]


def test_a_symlinked_spelling_is_the_same_project(tmp_path):
    """macOS's /tmp is /private/tmp: an agent records whichever the shell said."""
    a = repo(tmp_path / "a")
    os.makedirs(os.path.join(a, "src"))
    os.symlink(a, tmp_path / "link")
    assert scope.project_id(str(tmp_path / "link" / "src")) == scope.project_id(a)


def test_a_case_insensitive_volume_reads_either_case_as_one_project(tmp_path):
    a = repo(tmp_path / "Proj")
    os.makedirs(tmp_path / "Plain")
    if not os.path.exists(tmp_path / "pROJ"):
        pytest.skip("tmp_path's volume is case-sensitive: two spellings are two directories")
    assert scope.project_id(str(tmp_path / "pROJ")) == scope.project_id(a)
    # A deleted directory has no inode to compare, so its spelling is folded.
    gone, GONE = (str(tmp_path / "Plain" / n) for n in ("Gone", "gONE"))
    assert scope.project_id(gone) == scope.project_id(GONE)


# --------------------------------------------------------------------------- #
# search: the filters live before the per-turn pick
# --------------------------------------------------------------------------- #


def _store(home, tmp_path, sessions: dict[str, list[dict]]) -> None:
    for name, lines in sessions.items():
        src = tmp_path / "src" / f"{name}.jsonl"
        src.parent.mkdir(exist_ok=True)
        write(str(src), lines)
        store.capture(str(src), "claude-code", name, home=home)
    index.build(home)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "store"
    h.mkdir()
    return str(h)


def test_k_counts_in_scope_turns(home, tmp_path):
    """Filtered after the LIMIT, five better-scoring turns elsewhere would
    leave `-k 2` returning nothing from here."""
    other = [user(f"o{i}", "heron heron heron heron") | {"cwd": "/w/b"} for i in range(5)]
    mine = [user(f"m{i}", f"heron number {i}") | {"cwd": "/w/a"} for i in range(2)]
    _store(home, tmp_path, {"s": other + mine})
    db = index.open_db(index.db_path(home))
    try:
        assert len(index.search(db, "heron", k=2, cwds=["/w/b"])) == 2
        hits = index.search(db, "heron", k=2, cwds=["/w/a"])
        assert [h.text for h in hits] == ["heron number 0", "heron number 1"]
        assert index.search(db, "heron", cwds=[]) == []
    finally:
        db.close()


def test_a_turn_whose_best_block_is_tool_output_is_found_by_its_text(home, tmp_path):
    """The kind filter picks among a turn's blocks, so the turn comes back by
    the block it admits, not dropped because its best block was a tool's."""
    turn = assistant(
        "a1",
        [
            {"type": "text", "text": "the ibis config"},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"c": "ibis ibis ibis"}},
        ],
    ) | {"cwd": "/w/a"}
    _store(home, tmp_path, {"s": [turn]})
    db = index.open_db(index.db_path(home))
    try:
        assert [h.kind for h in index.search(db, "ibis")] == ["tool_use"]
        hits = index.search(db, "ibis", roles=["user", "assistant"], kinds=["text"])
    finally:
        db.close()
    assert [(h.role, h.kind, h.text) for h in hits] == [("assistant", "text", "the ibis config")]


def test_outside_counts_every_hidden_turn_once_beyond_k(home, tmp_path):
    """A count of turns, not blocks and not the top k: six hidden turns, one
    with two matching blocks, are six."""
    lines = [user(f"o{i}", f"egret {i}") | {"cwd": "/w/b"} for i in range(5)]
    lines.append(
        assistant(
            "o9",
            [
                {"type": "text", "text": "egret again"},
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {"c": "egret"}},
            ],
        )
        | {"cwd": "/w/b"}
    )
    lines.append(user("m1", "egret here") | {"cwd": "/w/a"})
    _store(home, tmp_path, {"s": lines})
    db = index.open_db(index.db_path(home))
    try:
        assert index.outside(db, "egret", roles=None, kinds=None, cwds=["/w/a"]) == 6
        assert index.outside(db, "egret", roles=None, kinds=["text"], cwds=None) == 0
        assert index.outside(db, "egret", roles=None, kinds=["tool_use"], cwds=None) == 6
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# recall: the defaults, the flags, the one line that says what was hidden
# --------------------------------------------------------------------------- #


@pytest.fixture
def two(tmp_path):
    """Two repositories, and a store whose lines are in each, both, neither."""
    return repo(tmp_path / "a"), repo(tmp_path / "b")


def _recall(home, capsys, *argv) -> tuple[list[str], str]:
    capsys.readouterr()
    assert main(["--home", home, "recall", *argv]) == 0
    out, err = capsys.readouterr()
    return [line.split(None, 4)[4] for line in out.splitlines()], err


def test_recall_searches_the_project_of_each_lines_own_directory(home, tmp_path, two, capsys):
    """A turn is in the project of the cwd on its own line: a `cd` into B
    mid-session moves the lines after it. A line with none is where its
    session was; a session with none is in no project at all."""
    a, b = two
    os.makedirs(os.path.join(a, "src"))
    _store(
        home,
        tmp_path,
        {
            "s1": [
                user("u1", "osprey in a") | {"cwd": os.path.join(a, "src")},
                user("u2", "osprey after cd") | {"cwd": b},
                nowhere(user("u3", "osprey with no cwd")),
            ],
            "s2": [nowhere(user("v1", "osprey in no project"))],
        },
    )
    shown, err = _recall(home, capsys, "osprey", "--project", a)
    assert sorted(shown) == ["osprey in a", "osprey with no cwd"]
    assert err == "outside this scope: 2 matching turn(s) (--all-projects, --all-kinds to widen)\n"
    shown, _ = _recall(home, capsys, "osprey", "--project", b)
    assert shown == ["osprey after cd"]
    shown, err = _recall(home, capsys, "osprey", "--all-projects")
    assert len(shown) == 4
    assert err == ""


def test_recall_defaults_to_the_working_directorys_project(
    home, tmp_path, two, capsys, monkeypatch
):
    a, b = two
    _store(
        home,
        tmp_path,
        {"s": [user("u1", "plover a") | {"cwd": a}, user("u2", "plover b") | {"cwd": b}]},
    )
    monkeypatch.chdir(b)
    assert _recall(home, capsys, "plover")[0] == ["plover b"]
    monkeypatch.chdir(a)
    assert _recall(home, capsys, "plover")[0] == ["plover a"]
    assert _recall(home, capsys, "plover", "--project", b)[0] == ["plover b"]


def test_recall_returns_user_and_assistant_text_by_default(home, tmp_path, two, capsys):
    """Tool calls, tool output, thinking and system lines are what a page, a
    file or a hook said, not what was said in the conversation."""
    a, _ = two
    _store(
        home,
        tmp_path,
        {
            "s": [
                user("u1", "kestrel from the user") | {"cwd": a},
                assistant(
                    "a1",
                    [
                        {"type": "thinking", "thinking": "kestrel thought"},
                        {"type": "text", "text": "kestrel from the assistant"},
                    ],
                )
                | {"cwd": a},
                assistant(
                    "a2",
                    [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"c": "kestrel"}}],
                )
                | {"cwd": a},
                user("u2", [{"type": "tool_result", "tool_use_id": "t", "content": "kestrel out"}])
                | {"cwd": a},
                {"type": "summary", "summary": "kestrel summary", "leafUuid": "u1", "cwd": a},
            ]
        },
    )
    capsys.readouterr()
    assert main(["--home", home, "recall", "kestrel", "--project", a]) == 0
    out, err = capsys.readouterr()
    assert sorted(line.split()[3] for line in out.splitlines()) == ["assistant/text", "user/text"]
    assert err == "outside this scope: 3 matching turn(s) (--all-projects, --all-kinds to widen)\n"
    assert main(["--home", home, "recall", "kestrel", "--project", a, "--all-kinds"]) == 0
    out, err = capsys.readouterr()
    assert sorted(line.split()[3] for line in out.splitlines()) == [
        "assistant/thinking",
        "assistant/tool_use",
        "system/text",
        "user/text",
        "user/tool_result",
    ]
    assert err == ""


def test_no_matches_in_scope_still_says_what_is_outside(home, tmp_path, two, capsys):
    """`no matches` is about this scope now; the line after it says whether
    there is anything elsewhere, as a number and the flag that widens it."""
    a, b = two
    _store(home, tmp_path, {"s": [user("u1", "avocet") | {"cwd": b}]})
    for flags, tail in (
        ([], "(--all-projects, --all-kinds to widen)"),
        (["--all-kinds"], "(--all-projects to widen)"),
    ):
        shown, err = _recall(home, capsys, "avocet", "--project", a, *flags)
        assert shown == []
        assert err == f"no matches\noutside this scope: 1 matching turn(s) {tail}\n"


def test_recall_prints_nothing_extra_when_the_scope_hid_nothing(home, tmp_path, two, capsys):
    a, _ = two
    _store(home, tmp_path, {"s": [user("u1", "curlew") | {"cwd": a}]})
    assert _recall(home, capsys, "curlew", "--project", a) == (["curlew"], "")
    assert _recall(home, capsys, "nothing-like-it", "--project", a) == ([], "no matches\n")


def test_a_pi_session_is_where_its_header_says(home, tmp_path, two, capsys):
    """pi writes cwd only on its session header, so each of its turns is in
    the header's project."""
    a, b = two
    src = tmp_path / "src" / "pi.jsonl"
    src.parent.mkdir(exist_ok=True)
    write(
        str(src),
        [{"type": "session", "id": "p1", "timestamp": "t", "cwd": a}]
        + [
            {
                "type": "message",
                "id": f"e{i}",
                "parentId": f"e{i - 1}" if i else None,
                "timestamp": "t",
                "message": {"role": "user", "content": [{"type": "text", "text": f"godwit {i}"}]},
            }
            for i in range(2)
        ],
    )
    store.capture(str(src), "pi", "p1", home=home)
    index.build(home)
    assert sorted(_recall(home, capsys, "godwit", "--project", a)[0]) == ["godwit 0", "godwit 1"]
    shown, err = _recall(home, capsys, "godwit", "--project", b)
    assert (shown, err) == ([], "no matches\noutside this scope: 2 matching turn(s) "
                            "(--all-projects, --all-kinds to widen)\n")


def test_recall_from_a_deleted_directory(home, tmp_path, two, capsys, monkeypatch):
    """A shell left in a removed worktree has no current directory. Recall
    still answers when told where to look, and says so when it is not."""
    a, _ = two
    _store(home, tmp_path, {"s": [user("u1", "whimbrel") | {"cwd": a}]})
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert _recall(home, capsys, "whimbrel", "--all-projects") == (["whimbrel"], "")
    assert _recall(home, capsys, "whimbrel", "--project", a) == (["whimbrel"], "")
    capsys.readouterr()
    assert main(["--home", home, "recall", "whimbrel"]) == 2
    assert capsys.readouterr().err == (
        "the current directory no longer exists: pass --project DIR or --all-projects\n"
    )


def test_project_must_be_a_directory(home, tmp_path, capsys):
    """An unsubstituted `${CLAUDE_PROJECT_DIR}` arrives as ''. It must stop
    recall, not fall back to some other directory's project."""
    for value in ("", str(tmp_path / "missing")):
        with pytest.raises(SystemExit) as e:
            main(["--home", home, "recall", "x", "--project", value])
        assert e.value.code == 2
        assert f"argument --project: not a directory: {value!r}" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        main(["--home", home, "recall", "x", "--project", str(tmp_path), "--all-projects"])
    assert e.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_project_reads_a_tilde(home, tmp_path, capsys):
    proj = os.path.join(os.environ["HOME"], "proj")
    os.makedirs(proj)
    _store(home, tmp_path, {"s": [user("u1", "dunlin") | {"cwd": proj}]})
    assert _recall(home, capsys, "dunlin", "--project", "~/proj") == (["dunlin"], "")


@pytest.mark.parametrize(
    "flags", [[], ["--all-projects"], ["--all-kinds"], ["--all-projects", "--all-kinds"]]
)
def test_an_old_db_is_refused_under_every_scope_flag(home, tmp_path, flags, capsys):
    """The scope reads `turns.cwd` before anything searches. An index without
    it must answer with the schema refusal, not a `no such column` traceback."""
    path = str(tmp_path / "old.db")
    db = index.open_db(path, write=True)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('schema', '2')")
    db.commit()
    db.close()
    capsys.readouterr()
    assert main(["--home", home, "recall", "x", "--db", path, *flags]) == 2
    assert "index is schema 2, this is gitmemory schema 3" in capsys.readouterr().err
