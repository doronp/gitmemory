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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_the_readme_test_count_is_the_test_count():
    """ "732 tests" in a file nobody runs is a claim, not a fact.

    Collected rather than run: the count is what the suite *contains*, this test
    included, and collection is a second of subprocess against twenty of a full
    run. `-p no:cacheprovider` so the collection does not write a `.pytest_cache`
    into the repository it is measuring. [E4, review: docs 6]
    """
    claimed = re.search(r"(\d[\d,]*) tests", _read("README.md"))
    assert claimed, "the README no longer states a test count"

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found = re.search(r"(\d+)(?:/\d+)? tests? collected", out)
    assert found, out[-2000:]

    assert int(claimed.group(1).replace(",", "")) == int(found.group(1)), (
        f"README says {claimed.group(1)} tests; the suite collects {found.group(1)}"
    )


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
