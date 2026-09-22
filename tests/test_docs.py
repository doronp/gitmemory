"""Claims the documentation makes that the repository can check for itself.

A docs review round found ten wrong statements, and the common shape of them was
a number or a path that was true when it was written. A document nobody can run
rots at the rate the code changes. So the checkable ones are checked here, and
the ones that failed are the reason this file exists. [E4, review: docs]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest
from test_claude_code import CC_FIXTURES, _corpus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _collect(**env_extra: str) -> int:
    """Collected rather than run: the count is what the suite *contains*, this
    test included, and collection is a second of subprocess against twenty of a
    full run. `-p no:cacheprovider` so the collection does not write a
    `.pytest_cache` into the repository it is measuring. [E4, review: docs 6]
    """
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, **env_extra),
    ).stdout
    found = re.search(r"(\d+)(?:/\d+)? tests? collected", out)
    assert found, out[-2000:]
    return int(found.group(1))


def test_the_readme_test_count_is_the_test_count():
    """ "732 tests" in a file nobody runs is a claim, not a fact.

    Collected **with the conformance corpus switched off**, and that is the
    whole of [E7 pair review]. `tests/test_claude_code.py` parametrises two
    tests over every `.jsonl` in claude-code-log's `test/test_data` — 322 cases
    — and the default location for that clone is a directory in `/tmp`. So the
    headline number was 1127 on the machine that had cloned it and 805 on a
    fresh checkout, and this test, whose entire job is to stop the README
    claiming a number nobody else sees, was the thing asserting the unreachable
    one. It passed for two epochs because the clone happened to still be there;
    it failed the moment a scratch cleanup removed it.

    The README states the number a fresh checkout gets, and names the corpus
    separately with the command to fetch it — the treatment the LongMemEval
    download already had.
    """
    claimed = re.search(r"(\d[\d,]*) tests", _read("README.md"))
    assert claimed, "the README no longer states a test count"

    offline = _collect(GITMEMORY_CC_FIXTURES=os.path.join(ROOT, "no-such-corpus"))
    assert int(claimed.group(1).replace(",", "")) == offline, (
        f"README says {claimed.group(1)} tests; a checkout with no corpus collects {offline}"
    )


def test_the_readme_negative_control_count_is_the_row_count():
    """The other number in the same table, and it had drifted for four commits.

    The README said 466 negative controls while the index held 480. It went
    stale at the first commit after the sentence was written and stayed stale
    through three more, because the test count beside it is pinned and this one
    was not — so the half of the row that nobody could check is the half that
    was wrong, which is the finding the test above was written for, repeated in
    the next column. `mutate_index` already pins its own docstring against
    `len(MUTANTS)`; this points the README at the same number.

    Undercounting is the benign direction and that is not a defence: what the
    claim is for is telling a reader how much of the suite is load-bearing, and
    a number that drifts one way this time drifts the other way next time.
    [E5 root cause 4]
    """
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from mutate_index import MUTANTS

    claimed = re.search(r"\*\*([\d,]+)\*\* negative controls", _read("README.md"))
    assert claimed, "the README no longer states a negative-control count"
    assert int(claimed.group(1).replace(",", "")) == len(MUTANTS)


def test_the_readme_conformance_count_is_the_conformance_count():
    """The other half, and it only runs where the corpus is.

    Skipped rather than asserted-away on a machine without the clone, because
    the alternative is the failure above: a number in the README that only one
    machine can check, checked by a test that quietly passes everywhere else.
    [E7 pair review]
    """
    if not _corpus():
        pytest.skip("claude-code-log's corpus is not cloned; see the README for the command")

    claimed = re.search(r"(\d[\d,]*) conformance cases", _read("README.md"))
    assert claimed, "the README no longer states a conformance count"

    offline = _collect(GITMEMORY_CC_FIXTURES=os.path.join(ROOT, "no-such-corpus"))
    assert int(claimed.group(1).replace(",", "")) == _collect() - offline


def test_the_design_document_does_not_promise_a_file_that_is_not_written():
    """`manifest.json` was the name in every version of the verification recipe.

    The store has never written one — a manifest is
    `sessions/<agent>/<session_id>/g<NN>.json`, one per generation — so the
    headline "a stranger with only the repo can verify this" recipe did not run.
    That is the product's central claim and it was the most wrong sentence in
    the document. [E4, review: docs 1]

    In the fenced blocks only. Prose is allowed to say what the old recipe got
    wrong — the correction note in §2.4 does — but a block someone is invited to
    paste has to name a file that exists.
    """
    for rel in ("docs/DESIGN.md", "README.md", "docs/watching.md"):
        for block in re.findall(r"^```.*?^```", _read(rel), re.S | re.M):
            assert "manifest.json" not in block, f"{rel} tells the reader to run:\n{block}"


def test_the_documented_manifest_fields_are_the_fields():
    """ "carries exactly" was six fields; a manifest carries eleven.

    Read off `store._FIELDS`, which is the writer's own floor and what `verify`
    now applies as the reader's. [E4, review: docs 4]
    """
    from gitmemory import store

    design = _read("docs/DESIGN.md")
    for field in store._FIELDS:
        assert field in design, f"DESIGN.md does not mention the manifest field {field!r}"


def test_the_documented_shim_length_is_the_shim_length():
    """ "~20 lines POSIX sh" outlived the sentence by a factor of five.

    Checked loosely — within ten lines — because the point is that the document
    is in the same order of magnitude as the file, not that every comment edit
    is a documentation change. [E4, review: docs 9]
    """
    shim = _read("hook/gitmemory-hook.sh").splitlines()
    total = len(shim)
    body = len([line for line in shim if line.strip() and not line.lstrip().startswith("#")])

    design = _read("docs/DESIGN.md")
    claimed = re.search(r"(\d+) lines of POSIX sh,\s*\n?\s*(\d+) of them not comments", design)
    assert claimed, "DESIGN.md no longer states the shim's length"
    assert abs(int(claimed.group(1)) - total) <= 10, f"shim is {total} lines"
    assert abs(int(claimed.group(2)) - body) <= 10, f"shim has {body} non-comment lines"


def test_the_published_latency_is_one_measurement_quoted_three_times():
    """The hook's p50 and p99 are in `hook/README.md`, `README.md` and
    `DESIGN.md`, and only the first of the three is next to the tool that
    produces them. Re-measuring at the E7 close moved all three — and moved the
    table's `max` from 37.18 ms to 14, which is what a maximum does — so the two
    downstream copies are now checked against the table rather than against a
    reader's memory. Rounded to 0.1 ms, because the downstream two quote one
    decimal and the table two. [E7]
    """
    dash = r"\s*[-–]\s*"
    table = _read("hook/README.md")

    def published(metric: str) -> tuple[float, float]:
        row = re.search(rf"\|\s*{metric}\s*\|\s*([\d.]+){dash}([\d.]+) ms", table)
        assert row, f"hook/README.md no longer publishes a {metric} range"
        return round(float(row.group(1)), 1), round(float(row.group(2)), 1)

    for doc, pattern in [
        ("README.md", rf"p50 \*\*([\d.]+){dash}([\d.]+) ms\*\*, p99 \*\*([\d.]+){dash}([\d.]+) ms"),
        ("docs/DESIGN.md", rf"([\d.]+){dash}([\d.]+) ms and\s*\n?\s*([\d.]+){dash}([\d.]+) ms"),
    ]:
        quoted = re.search(pattern, _read(doc))
        assert quoted, f"{doc} no longer quotes the hook's p50/p99"
        got = tuple(round(float(n), 1) for n in quoted.groups())
        assert got == published("p50") + published("p99"), f"{doc} quotes {got}"


def test_the_design_document_cites_nothing_in_tmp():
    """A citation only a scratch directory can resolve is not a citation.

    Two of the E0 findings — the whole argument for generations, and the
    comparison table under §6 — pointed at clones in `/tmp/gm-e0/`. That
    directory survived long enough to be quoted and not much longer, and a
    reader who cannot open the source has to take the quotation on faith, which
    is the thing this project is against. Replaced with `repo@commit:path:line`,
    which a stranger can fetch.

    `DESIGN-v0.md` is exempt: it is kept verbatim as the audit trail of a review
    and is corrected nowhere. [E4, review: docs — E0 citations]
    """
    for rel in ("docs/DESIGN.md", "README.md", "hook/README.md", "docs/watching.md"):
        for n, line in enumerate(_read(rel).splitlines(), 1):
            assert "/tmp/" not in line, f"{rel}:{n} cites a scratch path: {line.strip()}"


def test_no_tracked_file_contains_an_invisible_character():
    """A character you cannot see is a character nobody can review.

    `derive._INVISIBLE` is a class of zero-width format characters, and the
    first version of it was written with the characters themselves: the line
    rendered as `re.compile(r"[]")`, a reviewer could not tell which of the nine
    were in it, and dropping one would have produced a blank diff. Two test
    files had the same thing, one of them a test *about* invisible characters
    that spelled `\\x1b` and `\\x00` as escapes and the bidi overrides beside
    them as raw codepoints.

    So the rule is the file, not the class: every one of these is written
    `\\uXXXX` in tracked source, and this test is what makes that true a year
    from now. It is also the cheap half of a supply-chain check — a bidi
    override in source is how a line reads as one thing and compiles as
    another (CVE-2021-42574) — but the reason it is here is legibility.

    Content under test is a different matter: the conformance corpus and
    anything a hook captured are data, and data is allowed to contain whatever
    a person typed. Only tracked text files are scanned. [E5 review round]
    """
    banned = {
        0x00AD: "soft hyphen",
        0x180E: "mongolian vowel separator",
        0x200B: "zero width space",
        0x200C: "zero width non-joiner",
        0x200D: "zero width joiner",
        0x200E: "left-to-right mark",
        0x200F: "right-to-left mark",
        0x202A: "left-to-right embedding",
        0x202B: "right-to-left embedding",
        0x202C: "pop directional formatting",
        0x202D: "left-to-right override",
        0x202E: "right-to-left override",
        0x2060: "word joiner",
        0x2066: "left-to-right isolate",
        0x2067: "right-to-left isolate",
        0x2068: "first strong isolate",
        0x2069: "pop directional isolate",
        0xFEFF: "zero width no-break space",
    }
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    found = []
    for rel in filter(None, tracked):
        try:
            text = _read(rel)
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # a binary or a submodule; nothing to read a codepoint out of
        for n, line in enumerate(text.splitlines(), 1):
            found += [
                f"{rel}:{n} U+{ord(c):04X} {banned[ord(c)]}" for c in line if ord(c) in banned
            ]
    assert not found, "write these as escapes:\n" + "\n".join(found)


def test_no_environment_default_points_into_a_scratch_directory():
    """The rule above, applied to the code the documents describe.

    `tests/test_claude_code.py` defaulted `GITMEMORY_CC_FIXTURES` to
    `/tmp/gm-e0/claude-code-log/test/test_data`, and 322 of the suite's cases
    are parametrised over whatever is there. So the suite had two sizes, the
    README quoted the larger one, and the test that checks the README agreed —
    on one machine, for as long as nobody cleaned `/tmp`. When somebody did, the
    count test failed and the 322 cases vanished silently, which is the worse
    half: a parametrisation over an empty list is not an error.

    The ban is on the *default* rather than on the string. A `/tmp` path passed
    to a function under test is an inert argument and there are dozens; a `/tmp`
    path behind `os.environ.get` is what the suite does when nobody says
    otherwise. [E7 pair review]
    """
    default = re.compile(r"environ\.get\([^)]*?/tmp/")
    # The floor, because a scan that matches nothing passes loudest. Assembled
    # from pieces for the reason the mutation index does it: a sample written
    # out in full is a violation in the file being scanned, and this test found
    # its own first draft.
    assert default.search('os.environ.get("X", "/tm' + 'p/gm-e0/x")')
    assert not default.search('shutil.rmtree("/tm' + 'p/gm-scratch")')

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
            hit = default.search(_read(rel))
            assert not hit, f"{rel} defaults an environment variable to {hit.group(0)!r}"

    # The shape that gets past the regex: a default bound to a name first is not
    # inside the `environ.get(...)` span, and that is the very form the corpus
    # default took after being written out over five lines. Asserted on the
    # value, which is the only thing that cannot be reworded around.
    assert str(CC_FIXTURES).startswith(ROOT + os.sep), CC_FIXTURES


def test_the_unbuilt_retrieval_arms_are_not_described_as_built():
    """§2.7 named the dense and rerank stack in the present tense. Neither ran.

    `docs/benchmarks/E3-longmemeval.md` is the gate report and says so — *"arm
    not available — dense: no numpy, model2vec"*. The design document described
    the same components as decided architecture, with a latency figure attached
    to one of them. A number for a thing that has never been run is the exact
    shape of claim this file exists to stop.

    Checked by paragraph, not by document, so §2.7 can go on naming them as the
    plan they are. [E4, review: docs — 79%/5.8 ms]
    """
    extra = re.search(r"hybrid = \[(.*?)\]", _read("pyproject.toml"), re.S)
    assert extra, "pyproject.toml no longer declares the hybrid extra"
    names = re.findall(r'"([a-z0-9_-]+)', extra.group(1))
    assert names, extra.group(1)

    hedges = (
        "optional",
        "not wired",
        "not built",
        "did not run",
        "never been run",
        "does not exist",
        "extra",
        "unmeasured",
    )
    for para in re.split(r"\n\s*\n", _read("docs/DESIGN.md")):
        named = [n for n in names if n in para.lower()]
        if named and not any(h in para.lower() for h in hedges):
            raise AssertionError(f"{named} described as built:\n{para}")


def test_the_secondary_write_up_quotes_what_it_says_it_quotes():
    """The one document that reproduces a stranger's sentences says how many.

    It used to say "no transcript text is committed", in the file doing the
    committing — the manifest carries no text, and the paragraph had quietly
    inherited the manifest's promise. Eight verbatim runs is a citation; the
    point of pinning the count is that nobody adds the ninth in silence.

    Matched against the real clone with whitespace normalised, so the number is
    of text that is actually in somebody else's session and not of every pair
    of quotation marks in the prose. [E7b L4-F3]
    """
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    from secondary import corpus_root, transcripts

    root = corpus_root()
    if root is None:
        pytest.skip("third-party corpus missing; run tests/fetch_fixtures.sh")

    def flat(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    corpus = flat("\n".join(p.read_text(errors="replace") for p in transcripts(root)))
    doc = _read("docs/benchmarks/E5-secondary-set.md")
    quoted = {
        run
        for m in re.finditer(r'[“"]([^”"]{20,})[”"]', doc)
        if len(run := flat(m.group(1)).strip("*… ").replace("`", "")) >= 40
    }
    verbatim = sorted(q for q in quoted if q in corpus)
    claimed = int(re.search(r"quotes \*\*(\d+)\*\* runs of\n40 characters", doc).group(1))
    assert len(verbatim) == claimed, verbatim
