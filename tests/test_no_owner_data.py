# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
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

import os
import re
import subprocess
from pathlib import Path

import pytest

from gitmemory import gitrepo

ROOT = Path(__file__).resolve().parent.parent

# What is actually forbidden is an *absolute* path naming a specific account:
# it only works on one machine, and it names its owner. Tilde-relative paths are
# a different thing and are deliberately not matched — `~/.claude/projects` is
# where Claude Code puts transcripts, so the adapter's docstring, the README's
# usage line, and every brief that says "never read this" all have to be able to
# write it down. The first run of this scanner flagged nine such lines; each one
# was the product describing its own interface or recording the constraint, and
# the pattern that caught them was the thing that was wrong. [E4]
#
# No trailing `/` on the two home patterns. It was there, and `HOME=/Users/x`
# and "run it from /Users/x" — the two most likely pastes — both went through.
# It bought nothing the `[a-z][a-z0-9._-]*` does not already buy. [E7 S13]
FORBIDDEN = {
    "a macOS home directory": re.compile(r"/Users/[a-z][a-z0-9._-]*", re.I),
    "a Linux home directory": re.compile(r"/home/[a-z][a-z0-9._-]*", re.I),
    # No product meaning at all: a private directory name from the author's own
    # setup. Unlike the two above there is no legitimate reason to write it.
    # `re.I` for parity: this was the one pattern without it, so a capitalised
    # `.Claude-Auto-Memory` was the cheapest way past it. [E7 S13]
    "the author's private memory tree": re.compile(r"\.claude-auto-memory\b", re.I),
}

# The three patterns above are shapes a path takes. None of them is the thing
# they are proxies for, which is *the account name*, and the proxies have the
# hole you would expect: Claude Code names a project directory by flattening the
# absolute path — `/Users/someone/work/x` becomes `-Users-someone-work-x` — and
# not one of the three matches that. It is not an exotic encoding. It is the
# name of every directory under `~/.claude/projects`, which is to say the single
# most likely spelling for an owner path to arrive in here by accident. A review
# built the whole chain under `/tmp` — a corpus path pointed at sessions, the
# manifest written from it, the manifest committed — and all three patterns
# reported nothing, in the checkout and in the object graph both. [E7b L4-F1]
#
# So ask the question directly instead of through a spelling of it. The name is
# read off `~` at run time and is *never written down*: a literal here would put
# the account name in a tracked file, which is the thing being forbidden, and
# would also be wrong on anybody else's machine. Any encoding is caught —
# flattened, slashed, bare, inside a URL — because the account name is in all of
# them.
#
# What is deliberately *not* here is a generic `-Users-<anything>-` pattern.
# This repository legitimately contains 173 of those: the corpus manifest keys
# each item by its path inside the pinned third-party clone, and those directory
# names are the upstream repository's own, public at the commit we cite. A
# pattern that cannot tell a public fixture locator from an account leak would
# be answered by an allowlist over the file that has the most to hide, which is
# the wrong end of the trade. [E7b L4-F1, rejected half]
GENERIC_ACCOUNTS = {
    "user", "users", "home", "root", "admin",
    "runner", "ubuntu", "build", "vagrant", "docker",
}


def _account() -> str | None:
    """This machine's account name, or `None` when it is too generic to scan for.

    A CI runner's home is `/home/runner` and a container's is `/root`; matching
    those as bare substrings would fire on prose everywhere and the guard would
    be turned off within a day. Short names have the same problem for the same
    reason. When the name is unusable the other patterns still run, and the case
    below says so out loud rather than leaving a silent gap — a scan that is off
    and looks on is how the last three holes in this file lasted as long as they
    did.
    """
    name = os.path.basename(os.path.expanduser("~"))
    return None if len(name) < 4 or name.lower() in GENERIC_ACCOUNTS else name


ACCOUNT = _account()
if ACCOUNT:
    FORBIDDEN["this machine's account name"] = re.compile(re.escape(ACCOUNT), re.I)

# This file has to contain the strings it forbids, to prove it can catch them.
# Named explicitly rather than honoured as a marker comment any file could claim.
ALLOWED = {"tests/test_no_owner_data.py"}

# One exact matched span that is a placeholder rather than an account, and it is
# here only because of history. `tests/mutate_index.py` carried a deliberately
# narrowed copy of the macOS pattern as a mutant, and dropping the trailing `/`
# below turned that mutant into a hit — in the checkout, which was edited, and in
# every historical blob of that file, which cannot be. Compared whole rather than
# as a prefix: `/Users/xavier` is a different span and is still a finding.
#
# "Whole span" is not the same as "whole path", and this comment claimed it was
# for one round. The pattern's character class stops at `/`, so the span matched
# in `/Users/x/private_key.pem` is `/Users/x` — identical to the placeholder —
# and the whole path went through. `_hits` now asks what follows the span; see
# there. [E7 pair review, finding 1]
#
# The control samples are deliberately *not* in here. They live in this file,
# which the allowlist covers by name in both scans, so exempting their text as
# well would blind the scanner everywhere for nothing. [E7 S13]
PLACEHOLDERS = {"/Users/x"}

# Binaries that ship, each by name, and each read by `_scan` as bytes decoded
# with `errors="replace"` (the way `_history_scan` reads every blob) instead of
# being skipped by `_is_text`. That sees what a file carries as bytes: a GIF's
# comment and application blocks, a PNG's text chunks, the places a tool that
# knows a path would write it. It does not see text the picture *shows*, which
# is compressed pixels. For `docs/assets/demo.gif` that half was checked on the
# asciicast it was rendered from, before rendering. Named rather than inferred
# from a file extension, so the next binary is still a deliberate edit here.
SCANNED_AS_BYTES = {"docs/assets/demo.gif"}

# The two lines of a commit object that are a git identity rather than content.
# Matched at the start, so a message line reading "author of the patch" is not
# one of them. [E7b L4-F6]
_IDENTITY = re.compile(r"(author|committer) ")


def _hits(pattern: re.Pattern, text: str) -> bool:
    """Does `text` match, ignoring spans that are known placeholders?

    A placeholder followed by `/` is not a placeholder. It is the first
    component of a real path, and the span the pattern reports is the same
    either way, so comparing the span alone excused everything underneath it:
    a fixture repository whose only file read `the key is at
    /Users/x/private_key.pem` scanned clean through `_scan`, `_tracked` and all.
    Every occurrence in this repository's history is a quoted or backticked
    token — no blob anywhere in the object graph contains `/Users/x/` — so
    looking at the next character costs nothing and closes it.
    [E7 pair review, finding 1]
    """
    return any(
        m.group(0) not in PLACEHOLDERS or text[m.end() : m.end() + 1] == "/"
        for m in pattern.finditer(text)
    )


# One string per pattern that the pattern must match. Module-level rather than
# local to the control test, because the scan cases assert against it too: a
# pattern narrowed until it no longer catches its own sample should fail *that
# pattern's* case, not only the sibling control. [E4, vacuity pass 2: F2]
SAMPLES = {
    "a macOS home directory": "/Users/someone/work/gitmemory",
    "a Linux home directory": "/home/someone/work/gitmemory",
    "the author's private memory tree": "notes/.claude-auto-memory/MEMORY.md",
}
if ACCOUNT:
    # Built rather than written, for the reason the pattern is: the sample is
    # the flattened spelling, because that is the one the three patterns above
    # miss and the one this case exists to prove is caught.
    SAMPLES["this machine's account name"] = f"-Users-{ACCOUNT}-work-gitmemory"


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
        # The name is shipped too, and `_scan` never looked at it: a leak in the
        # *filename* went through with an empty body. [E7 S13]
        if _hits(pattern, rel):
            hits.append(f"{rel}:name")
        path = root / rel
        # A symlink is its target *string*, and that string is what git commits.
        # `is_file()` and `read_text()` both follow, so this scanner — whose whole
        # purpose is that the owner's files are never read — was opening a file
        # outside the repository and reporting matches found in it, while the
        # bytes git would actually ship went unread. Both halves wrong in the
        # same direction. A dangling link naming the owner's tree was skipped
        # entirely, and a dangling link is the easy one to plant. [E7 S12]
        if path.is_symlink():
            if _hits(pattern, os.readlink(path)):
                hits.append(f"{rel}:link")
            continue
        if rel in SCANNED_AS_BYTES and path.is_file():
            text = path.read_bytes().decode("utf-8", "replace")
        elif path.is_file() and _is_text(path):
            text = path.read_text(encoding="utf-8")
        else:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _hits(pattern, line):
                hits.append(f"{rel}:{n}")
    return hits


def _gitlinks(ls_files_s_z: str) -> list[str]:
    """Submodule paths in `git ls-files -s -z` output. [E7 pair review, finding 2]"""
    return [e.split("\t", 1)[-1] for e in ls_files_s_z.split("\0") if e.startswith("160000 ")]


def _lfs_attributes(root: Path, paths: set[str]) -> list[str]:
    """Tracked `.gitattributes` that route content through LFS. [E7 pair review, finding 2]"""
    return sorted(
        p
        for p in paths
        if os.path.basename(p) == ".gitattributes"
        and (root / p).is_file()
        and "filter=lfs" in (root / p).read_text()
    )


def _opaque(root: Path, files: list[str]) -> list[str]:
    """Files in the scan's own enumeration that the scan cannot read. [E7 S2]"""
    return [
        rel
        for rel in files
        if not (root / rel).is_symlink() and not _is_text(root / rel)
    ]


def _history_scan(root: Path, pattern: re.Pattern, allowed: set[str]) -> list[str]:
    """The same question, asked of the object graph instead of the checkout.

    `_tracked` reads the working tree, and the working tree is not what `git
    push` sends. A path committed once and deleted in the next commit is
    invisible to every assertion above and still in the pack. So is a *binary*
    blob holding the bytes, which `_is_text` skips on purpose — defensible for
    crash-safety in a walk of the checkout, indefensible once it is committed,
    because a committed binary ships verbatim. [E7 S2]

    `gitrepo.pushable_objects` is the function the egress gate already uses for
    exactly this distinction, and reusing it means one definition of "what would
    leave this machine" rather than two that can drift. Blobs, trees *and*
    commits come through it, so a commit message and a path name that only ever
    existed in history are both covered — and a symlink's committed bytes are
    its target string, which is the half of [E7 S12] the working-tree scan
    cannot see even after the fix, because the link may no longer exist.

    Decoded with `errors="replace"`: there is no `_is_text` gate here. A blob
    that does not decode is still shipped byte for byte.

    A commit's `author` and `committer` lines are skipped, and only those two,
    and only before the blank line that ends a commit's headers. They carry the
    account name of whoever made the commit — that is what a git identity is —
    so the account pattern added above would otherwise report every commit in
    the repository, forever, for something no edit can remove: authorship is
    metadata `git push` sends by construction and every public repository
    publishes. The commit *message* is still read, which is the half that has
    caught a real leak. [E7b L4-F6]
    """
    hits = []
    for label, data in gitrepo.pushable_objects(str(root)):
        if label in allowed:
            continue
        if _hits(pattern, label):
            hits.append(f"{label}:name")
        header = label.startswith("<commit ")
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if header and not line:
                header = False
            if header and _IDENTITY.match(line):
                continue
            if _hits(pattern, line):
                hits.append(f"{label}:{n}")
    return hits


def test_the_account_name_scan_is_on_or_says_why_it_is_not():
    """A scan that is off and looks on is the failure mode this file keeps having.

    `_account` declines a name a substring match would drown in — a CI runner's
    `runner`, a container's `root`, anything under four characters. That is the
    right call and it is also a hole with a friendly face, because the two cases
    below simply have one fewer pattern to run and say nothing about it. The
    skip line in the test output is the saying-so.
    """
    if ACCOUNT is None:
        pytest.skip(
            "the account name of this machine is generic or too short to match on; "
            f"the other {len(FORBIDDEN)} patterns still ran"
        )
    assert "this machine's account name" in FORBIDDEN
    # And the encoding that motivated it: the three path patterns miss the
    # flattened form, this one must not. [E7b L4-F1]
    flattened = f"-Users-{ACCOUNT}-work-gitmemory"
    assert FORBIDDEN["this machine's account name"].search(flattened)
    others = (p for k, p in FORBIDDEN.items() if k != "this machine's account name")
    assert not any(p.search(flattened) for p in others)


# Two blobs in the object graph, and the reason they are not being rewritten out
# of it. Both are a doc that spelled the account name *inside a sentence saying
# the account name is absent* — one a review finding reporting a clean scan, one
# a brief telling reviewers what not to write. Both are fixed in the working
# tree, so the tracked-file case above holds with no exception at all.
#
# History is left alone because rewriting it would be theatre: the account name
# is inside the author email of 138 of this repository's 141 commits, which
# `git push` sends by construction and which no edit to a file removes. Scrubbing
# two prose mentions while authorship carries the same string 138 times buys
# nothing and costs every commit id cited across `docs/reviews/`, which is most
# of them. Scoped to these two labels rather than to the pattern, so a *new*
# historical hit still fails this case. [E7b L4-F6]
KNOWN_HISTORY = {
    "this machine's account name": {
        "docs/reviews/E3-correctness-findings.md",
        "docs/tasks/E3-round3-brief.md",
    }
}


# Parametrised over the keys, not the items: the id pytest builds from a
# compiled pattern is its source text, so `sorted(FORBIDDEN.items())` printed the
# account name into every test report and log that ran this file — including
# the one that proves the account name appears nowhere. [E7b L4-F6]
@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_no_tracked_file_contains_owner_data(what: str):
    # This case owns its pattern: narrowing it until it stops catching its own
    # sample fails here, not only in the sibling control test. [E4, pass 2: F2]
    pattern = FORBIDDEN[what]
    assert pattern.search(SAMPLES[what]), f"the {what} pattern matches nothing"
    hits = _scan(ROOT, _tracked(), pattern, ALLOWED)
    assert not hits, f"{what} appears in tracked files: {', '.join(hits[:20])}"


@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_no_object_this_repository_would_push_contains_owner_data(what: str):
    pattern = FORBIDDEN[what]
    assert pattern.search(SAMPLES[what]), f"the {what} pattern matches nothing"
    known = KNOWN_HISTORY.get(what, set())
    hits = [h for h in _history_scan(ROOT, pattern, ALLOWED) if h.rsplit(":", 1)[0] not in known]
    assert not hits, f"{what} is in this repository's history: {', '.join(hits[:20])}"


def test_nothing_tracked_is_a_file_the_scanner_cannot_read(tmp_path):
    """`_is_text` skipping a binary is a hole; leaving it unwatched is the defect.

    The skip stays — a scanner that raises `UnicodeDecodeError` on a PNG is a
    scanner nobody keeps. What it costs is that a `.db`, an image or a compiled
    fixture carrying a path is invisible to `_scan`. The history scan above
    covers the committed case. This covers the *pre*-commit case, and it does it
    by making the class empty rather than by reading the files: adding the first
    binary to this repository becomes a deliberate act with this assertion in
    the diff. [E7 S2]

    The first one is `docs/assets/demo.gif`, and it is read another way rather
    than excused: see `SCANNED_AS_BYTES`, and its plant in the positive control.
    """
    opaque = _opaque(ROOT, [rel for rel in _tracked() if rel not in SCANNED_AS_BYTES])
    assert not opaque, (
        f"the owner-data scan cannot read {opaque}; if these must ship, scan them "
        "another way in the same commit"
    )

    # And the control, because "the list is empty" is the shape that also passes
    # when nothing was looked at. A planted binary, a planted symlink and a
    # planted text file, so the answer is one name rather than three or none.
    (tmp_path / "icon.bin").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "plain.md").write_text("text\n")
    os.symlink("/nowhere/at/all", tmp_path / "link.md")
    assert _opaque(tmp_path, ["icon.bin", "plain.md", "link.md"]) == ["icon.bin"]


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
    cover. Eight planted entries, one per property:

    - untracked, outside `src/`  — the two scope holes, and matching itself
    - staged but never committed — `--cached` still counts
    - binary containing the bytes — `_is_text` skips it rather than crashing
    - the same bytes in a binary named in `SCANNED_AS_BYTES` — read, and found
    - allowlisted                — the allowlist is honoured, not ignored
    - the leak in the *filename*, with a clean body [E7 S13]
    - a dangling symlink whose target string is the leak [E7 S12]
    - a live symlink into a directory outside the repository, whose target holds
      a *different* leak — so "no hit for it" is a positive statement that the
      scanner did not read outside, not an absence of anything to find [E7 S12]

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
    listed = min(SCANNED_AS_BYTES)
    (tmp_path / listed).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / listed).write_bytes(b"\xff\xfe" + leak.encode() + b"\x00\xff")
    # The leak in a path *component*, which is the only way a name can hold one:
    # a fixture tree mirroring a real machine. Body deliberately clean.
    named = tmp_path / "fixtures" / "Users" / "someone"
    named.mkdir(parents=True)
    (named / "notes.md").write_text("a clean body\n")
    os.symlink(f"{leak}/memory/vault.md", tmp_path / "dangling.md")

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "vault.md").write_text("/Users/elsewhere/private/notes\n")
    os.symlink(outside / "vault.md", tmp_path / "live_link.md")

    subprocess.run(["git", "-C", str(tmp_path), "add", "staged_leak.txt"], check=True)

    hits = _scan(
        tmp_path,
        _tracked(tmp_path),
        FORBIDDEN["a macOS home directory"],
        {"allowed_leak.md"},
    )
    assert set(hits) == {
        "docs/untracked_leak.md:1",
        "staged_leak.txt:1",
        "fixtures/Users/someone/notes.md:name",
        "dangling.md:link",
        f"{listed}:1",
    }, hits
    # Redundant with the set above, and kept for the one line it says out loud:
    # this is what a scanner that followed the link out of the repository would
    # report, having opened a file it must never open. [E7 S12]
    assert "live_link.md:1" not in hits


def test_the_history_scan_finds_leaks_the_checkout_no_longer_has(tmp_path):
    """The positive control for `_history_scan`, and it needs its own.

    The scan above is "no hits" over a clean object graph, which is the shape
    that passes identically when the scan is broken — the whole lesson of pass 2
    of the vacuity audit. Three planted objects, each one a thing the
    working-tree scan cannot see by construction:

    - committed, then deleted   — absent from `_tracked`, present in the pack
    - a committed binary blob   — `_is_text` skips it, git ships it verbatim
    - a path that only ever existed in a commit that was later reverted

    The clean file is in the fixture so that "found three" is not also "found
    everything". [E7 S2]
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    leak = SAMPLES["a macOS home directory"]

    (tmp_path / "gone.md").write_text(f"install into {leak}/store\n")
    (tmp_path / "icon.bin").write_bytes(b"\xff\xfe" + leak.encode() + b"\x00\xff")
    (tmp_path / "kept.md").write_text("nothing to see\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)

    (tmp_path / "gone.md").unlink()
    (tmp_path / "icon.bin").unlink()
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "two"], check=True)

    assert not _scan(tmp_path, _tracked(tmp_path), FORBIDDEN["a macOS home directory"], set())
    hits = _history_scan(tmp_path, FORBIDDEN["a macOS home directory"], set())
    assert set(hits) == {"gone.md:1", "icon.bin:1"}, hits


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

    # The shapes the trailing `/` and the missing `re.I` used to let through.
    # Pinned here rather than left implicit in the samples, because the samples
    # all carry a path after the home directory and that is the easy case. Both
    # of these are what an actual paste looks like. [E7 S13]
    for what, text in (
        ("a macOS home directory", "export HOME=/Users/someone"),
        ("a Linux home directory", "run it from /home/someone"),
        ("the author's private memory tree", "notes/.Claude-Auto-Memory/MEMORY.md"),
    ):
        assert _hits(FORBIDDEN[what], text), f"{what} misses {text!r}"

    # And the other half of the control: the tilde-relative forms the scanner is
    # deliberately blind to must stay unmatched, or the next tightening of a
    # pattern quietly makes the adapter's own docstring a test failure.
    for legitimate in (
        "~/.claude/projects/<proj>/<session>.jsonl",
        "os.path.expanduser('~/.claude/projects')",
    ):
        for what, pattern in FORBIDDEN.items():
            assert not pattern.search(legitimate), f"{what} falsely matches {legitimate!r}"


def test_a_placeholder_does_not_excuse_the_path_underneath_it(tmp_path):
    """The excuse is for a token, not for a home directory called `x`.

    Reported by the pair reviewer against `_hits`; reproduced here through the
    real scanner instead, because the interesting question is not whether the
    helper returns `False` but whether a repository containing the leak comes
    back clean. Before the fix this fixture produced no hits at all.
    [E7 pair review, finding 1]
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "leak.md").write_text("the key is at /Users/x/private_key.pem\n")
    hits = _scan(tmp_path, _tracked(tmp_path), FORBIDDEN["a macOS home directory"], set())
    assert hits == ["leak.md:1"], hits

    # And the other direction, or the fix is just "delete the excuse": the two
    # shapes the placeholder exists for still have to pass.
    # The two shapes it really takes in this history: a quoted token in a
    # mutation row, and a backticked one in prose. Written a third way here on
    # purpose — spelling the row's own source line out would give that row's
    # anchor a second occurrence and make the harness skip it.
    for excused in ('the mutant writes "/Users/x" as a span', "it failed on `/Users/x` itself"):
        assert not _hits(FORBIDDEN["a macOS home directory"], excused), excused


def test_nothing_in_this_repository_hides_bytes_from_the_object_graph(tmp_path):
    """`pushable_objects` walks one repository's objects. Two git features move
    shipped bytes outside that walk, and neither is in use here:

    - a **submodule** is a gitlink, 20 bytes of commit id; the blobs it names
      live in another object database that `git rev-list --objects --all` never
      enumerates.
    - **LFS** commits a pointer file; the content is fetched from a server, so
      the history scan reads `oid sha256:...` and calls it clean.

    Neither is a bug to fix — they are assumptions to keep true. Same shape as
    `test_nothing_tracked_is_a_file_the_scanner_cannot_read`: make the class
    empty, so introducing the first one is a deliberate act with this assertion
    in the diff. [E7 pair review, finding 2]
    """
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "-z"],
        capture_output=True, text=True, check=True,
    )
    assert not _gitlinks(out.stdout), (
        f"submodules are invisible to the history scan: {_gitlinks(out.stdout)}"
    )
    # Empty is also what a broken parse returns, so: two entries git would
    # really emit, through the same helper. `\x00` and not `\0`, because `\0`
    # ahead of the `160000` is an octal escape and the first draft of this line
    # fed the helper one record instead of two. [E7 pair review, finding 2]
    zero = "0" * 40
    assert _gitlinks(f"100644 {zero} 0\tREADME.md\x00160000 {zero} 0\tvendor/thing\x00") == [
        "vendor/thing"
    ]

    names = subprocess.run(
        ["git", "-C", str(ROOT), "rev-list", "--objects", "--all"],
        capture_output=True, text=True, check=True,
    ).stdout
    ever = [ln.split(" ", 1)[1] for ln in names.splitlines() if " " in ln]
    assert not [p for p in ever if os.path.basename(p) == ".gitmodules"], (
        "a .gitmodules is in this repository's history; the history scan cannot "
        "see into whatever it points at"
    )

    lfs = _lfs_attributes(ROOT, set(ever))
    assert not lfs, f"LFS-tracked paths ship bytes the history scan never reads: {lfs}"
    # Same again: there is no `.gitattributes` here, so the real call above is
    # an empty list over an empty class, and that passes when the helper is
    # wrong. One planted file, through the same helper.
    (tmp_path / "sub").mkdir()
    (tmp_path / ".gitattributes").write_text("*.bin filter=lfs diff=lfs -text\n")
    (tmp_path / "sub" / ".gitattributes").write_text("*.md text\n")
    assert _lfs_attributes(tmp_path, {".gitattributes", "sub/.gitattributes"}) == [
        ".gitattributes"
    ]
