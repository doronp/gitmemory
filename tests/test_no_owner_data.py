"""The repository must not contain anyone's personal paths, data, or history.

gitmemory is a generic memory system. It was built on a machine with 1.1 GB of
the author's own agent transcripts sitting one directory away, and the single
rule of the project is that those are *reference architecture, never data*: the
system's shape was learned from them, nothing was read out of them, and nothing
from them ships.

That rule is easy to state and easy to violate by accident. The install example
in `hook/README.md` shipped `/Users/<owner>/work/gitmemory` twice before review
caught it [E4, A1] — not from carelessness about privacy but because the author
pasted a path that worked. This file is the enforcement, so the next one fails
the suite instead of the review.

Scope is every tracked file. Not `src/` — a leak in a doc, a fixture, or a test
is the same leak.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What is actually forbidden is an *absolute* path naming a specific account:
# it only works on one machine, and it names its owner. Tilde-relative paths are
# a different thing and are deliberately not matched — `~/.claude/projects` is
# where Claude Code puts transcripts, so the adapter's docstring, the README's
# usage line, and every brief that says "never read this" all have to be able to
# write it down. The first run of this scanner flagged nine such lines; each one
# was the product describing its own interface or recording the constraint, and
# the pattern that caught them was the thing that was wrong. [E4]
FORBIDDEN = {
    "a macOS home directory": re.compile(r"/Users/[a-z][a-z0-9._-]*/", re.I),
    "a Linux home directory": re.compile(r"/home/[a-z][a-z0-9._-]*/", re.I),
    # No product meaning at all: a private directory name from the author's own
    # setup. Unlike the two above there is no legitimate reason to write it.
    "the author's private memory tree": re.compile(r"\.claude-auto-memory\b"),
}

# This file has to contain the strings it forbids, to prove it can catch them.
# Named explicitly rather than honoured as a marker comment any file could claim.
ALLOWED = {"tests/test_no_owner_data.py"}

# One string per pattern that the pattern must match. Module-level rather than
# local to the control test, because the scan cases assert against it too: a
# pattern narrowed until it no longer catches its own sample should fail *that
# pattern's* case, not only the sibling control. [E4, vacuity pass 2: F2]
SAMPLES = {
    "a macOS home directory": "/Users/someone/work/gitmemory",
    "a Linux home directory": "/home/someone/work/gitmemory",
    "the author's private memory tree": "~/memory/.claude-auto-memory/MEMORY.md",
}


def _tracked(root: Path = ROOT) -> list[str]:
    """Tracked files **and** untracked ones git would let you add.

    `ls-files` alone was the first version and it had the hole you would expect:
    a brand-new module is untracked right up until the commit that ships it, so
    the files most likely to contain a path someone pasted from their own
    machine were the exact files this scanner could not see. It found nothing
    while `daemon.py` carried `/Users/me/.claude/projects` in a docstring.
    `--others --exclude-standard` closes it without dragging in `.gitignore`d
    build output. [E4]
    """
    args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def _is_text(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _scan(root: Path, files: list[str], pattern: re.Pattern, allowed: set[str]) -> list[str]:
    """`rel:line` for every match, so the scan can be aimed somewhere other than us.

    Pulled out of the test body so a fixture repository can be scanned by the
    *same* code that scans this one. It was inline, and inline meant every test
    here asserted "no hits" over a corpus that genuinely has none — which passes
    identically when the scanner is broken. Five separate one-line ways of
    disabling it left the whole suite green. [E4, vacuity pass 2: F1]
    """
    hits = []
    for rel in files:
        if rel in allowed:
            continue
        path = root / rel
        if not path.is_file() or not _is_text(path):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{n}")
    return hits


@pytest.mark.parametrize("what,pattern", sorted(FORBIDDEN.items()))
def test_no_tracked_file_contains_owner_data(what: str, pattern: re.Pattern):
    # This case owns its pattern: narrowing it until it stops catching its own
    # sample fails here, not only in the sibling control test. [E4, pass 2: F2]
    assert pattern.search(SAMPLES[what]), f"the {what} pattern matches nothing"
    hits = _scan(ROOT, _tracked(), pattern, ALLOWED)
    assert not hits, f"{what} appears in tracked files: {', '.join(hits[:20])}"


def test_the_scan_actually_has_files_to_scan():
    """The other half of the negative control below, and the half that was missing.

    `test_the_patterns_would_actually_catch_something` proves the patterns work.
    It says nothing about whether they were pointed at anything. Every assertion
    in this file is "no hits", which passes just as cleanly over an empty corpus
    — and `_tracked()` can return one: `git -C` against a path that is not a
    repository, a checkout where everything is somehow ignored, a future edit
    that filters the list too hard. `check=True` catches git failing, not git
    succeeding with nothing to say.

    The floor is deliberately far below the real count (51 files at the time of
    writing) so it fails on collapse rather than drifting into a maintenance
    chore. The named files are the ones that would carry a pasted path.
    [E4, review: F4]
    """
    tracked = set(_tracked())
    assert len(tracked) > 40, f"the owner-data scan saw only {len(tracked)} files"
    for rel in ("src/gitmemory/daemon.py", "README.md", "hook/gitmemory-hook.sh"):
        assert rel in tracked, f"{rel} is not in the scanned set"


def test_the_scanner_finds_leaks_that_are_really_there(tmp_path):
    """The positive control: point the real scanner at a repository that does leak.

    Every other assertion in this file is "no hits" over a corpus that has none,
    and that shape passes just as cleanly when the scanner is broken. Pass 2 of
    the vacuity audit planted a leak and then disabled the scanner five separate
    ways — dropping `--others --exclude-standard`, allowlisting the leaking file,
    `_is_text` returning `False`, `pattern.search(...)` replaced by `False`, and
    narrowing the walk to `src/`. All 784 tests stayed green for each. The first
    of those is the exact hole E4 found and fixed, so the fix was one careless
    revert away from being undone in silence.

    The floor test below checks that the scan *enumerated* something. This checks
    that it *reads, matches and reports* — the three things enumeration does not
    cover. Four planted files, one per property:

    - untracked, outside `src/`  — the two scope holes, and matching itself
    - staged but never committed — `--cached` still counts
    - binary containing the bytes — `_is_text` skips it rather than crashing
    - allowlisted                — the allowlist is honoured, not ignored

    Asserting the exact hit set rather than a count is deliberate: a scanner that
    reports the right number of wrong files is still broken. [E4, pass 2: F1]
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    leak = SAMPLES["a macOS home directory"]

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "untracked_leak.md").write_text(f"see {leak}/notes\n")
    (tmp_path / "staged_leak.txt").write_text(f"install into {leak}\n")
    (tmp_path / "allowed_leak.md").write_text(f"deliberate: {leak}\n")
    # Invalid UTF-8 around a real match, so a scanner that stops gating on
    # `_is_text` raises `UnicodeDecodeError` here instead of quietly passing.
    (tmp_path / "icon.bin").write_bytes(b"\xff\xfe" + leak.encode() + b"\x00\xff")

    subprocess.run(["git", "-C", str(tmp_path), "add", "staged_leak.txt"], check=True)

    hits = _scan(
        tmp_path,
        _tracked(tmp_path),
        FORBIDDEN["a macOS home directory"],
        {"allowed_leak.md"},
    )
    assert set(hits) == {"docs/untracked_leak.md:1", "staged_leak.txt:1"}, hits


def test_the_allowlist_only_names_files_that_exist():
    """An allowlist entry for a deleted file is a hole nobody notices opening."""
    missing = [rel for rel in ALLOWED if not (ROOT / rel).is_file()]
    assert not missing, f"allowlisted but absent: {missing}"

    # And an allowlist that grew is the cheapest way to make this file pass while
    # meaning nothing, so growing it has to be a deliberate edit to this line
    # rather than a one-word addition to a set. [E4, pass 2: F1]
    assert {"tests/test_no_owner_data.py"} == ALLOWED, (
        "a file was allowlisted out of the owner-data scan; if that is right, "
        "say why here and change this assertion in the same commit"
    )


def test_the_patterns_would_actually_catch_something():
    """The negative control, inline: a scanner that matches nothing passes silently.

    Every check above is an assertion that a search found *no* hits, which is
    exactly the shape that also passes when the search is broken. So: feed each
    pattern a string it must match. [E4]
    """
    assert SAMPLES.keys() == FORBIDDEN.keys(), "a pattern has no control sample"
    for what, text in SAMPLES.items():
        assert FORBIDDEN[what].search(text), f"the {what} pattern matches nothing"

    # And the other half of the control: the tilde-relative forms the scanner is
    # deliberately blind to must stay unmatched, or the next tightening of a
    # pattern quietly makes the adapter's own docstring a test failure.
    for legitimate in (
        "~/.claude/projects/<proj>/<session>.jsonl",
        "os.path.expanduser('~/.claude/projects')",
    ):
        for what, pattern in FORBIDDEN.items():
            assert not pattern.search(legitimate), f"{what} falsely matches {legitimate!r}"
