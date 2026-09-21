"""The mutation harness scores itself correctly.

`tests/mutate_index.py` is not run by pytest — it is a hand-run tool, and every
negative control in this repository rests on it. That makes its scoring rule the
one piece of code whose failure is invisible: a harness that reports CAUGHT on
no evidence turns the whole mutation record into decoration and nothing goes
red. So the rule is a pure function of two pytest exit codes, and this is it.

The case that motivated the split is `BROKEN`. A mutant that deletes a group out
of a regex leaves a module that does not compile; pytest then fails at
*collection*, every test in the suite goes red including the one named for the
behaviour, and the old green/red boolean scored that as a clean CAUGHT with
perfect attribution. One row in the index was in exactly that state.
"""

from __future__ import annotations

import re

import pytest
from mutate_index import MUTANTS, ROOT, SRC, verdict

# pytest's documented exit codes, named so the cases below read as English.
GREEN, FAILED, INTERRUPTED, NO_TESTS = 0, 1, 2, 5


def test_a_mutant_the_suite_does_not_notice_survived():
    caught, tag, _ = verdict(GREEN, GREEN, "test_whatever")
    assert (caught, tag) == (False, "SURVIVED")


def test_a_mutant_caught_by_its_own_test_is_caught():
    caught, tag, note = verdict(FAILED, FAILED, "test_whatever")
    assert (caught, tag) == (True, "CAUGHT")
    assert "test_whatever" in note


def test_a_mutant_caught_by_some_other_test_is_missed():
    """The attribution half. The suite went red, the named test did not, so the
    test written for this behaviour is decorative and the behaviour is held in
    place by something incidental."""
    caught, tag, _ = verdict(FAILED, GREEN, "test_whatever")
    assert (caught, tag) == (False, "MISSED")


def test_a_mutant_that_stops_the_module_importing_is_not_credited():
    """The whole reason this function exists. Collection failed, so the mutant
    never ran, so nothing was demonstrated about anything."""
    caught, tag, note = verdict(INTERRUPTED, INTERRUPTED, "test_whatever")
    assert (caught, tag) == (False, "BROKEN")
    assert "does not import" in note


def test_a_row_naming_a_test_that_does_not_exist_says_so():
    """pytest exits 5 for "no tests ran", which the old boolean read as green
    and reported as MISSED — sending you to read the behaviour when the defect
    is a stale test name in the row."""
    caught, tag, note = verdict(FAILED, NO_TESTS, "test_renamed_last_week")
    assert (caught, tag) == (False, "NO TEST")
    assert "test_renamed_last_week" in note


@pytest.mark.parametrize("suite", [INTERRUPTED, 3, 4])
def test_every_exit_code_above_one_is_broken_not_caught(suite):
    """3 is an internal error and 4 a usage error. Neither is evidence, and the
    boolean counted all of them as a red suite."""
    assert verdict(suite, FAILED, "test_whatever")[1] == "BROKEN"


def test_the_only_verdict_that_counts_as_success_is_caught():
    """A guard against a future tag being added as `True` by reflex."""
    outcomes = {
        verdict(s, i, "t")[1]: verdict(s, i, "t")[0]
        for s, i in [(0, 0), (1, 1), (1, 0), (2, 2), (1, 5)]
    }
    assert {tag for tag, ok in outcomes.items() if ok} == {"CAUGHT"}


# --------------------------------------------------------------------------- #
# every row still points at something
# --------------------------------------------------------------------------- #
#
# A row whose anchor no longer resolves prints `SKIP` and holds nothing. That is
# the harness's own version of the defect it exists to find — a check that passes
# for a reason unrelated to the behaviour it is named for — and until now the
# only thing that noticed was a full hour-long pass nobody runs after a refactor.
# Two rows had been rotten for some time and six more were broken and repaired
# inside a single round of edits, which is the rate that makes this worth two
# seconds on every commit. [round 3, finding 3]


def _path(filename: str):
    """A bare name is a file in the package; a path is relative to the repo."""
    return ROOT / filename if "/" in filename else SRC / filename


def test_every_mutation_row_anchors_exactly_once():
    """Each of the four checks below reports *every* offender rather than
    parametrising, because the way these break is in batches — one refactor
    invalidated six rows at once — and a list is what you act on. It also keeps
    186 rows of a lint-shaped check out of the test count the README publishes.
    """
    bad = []
    for name, filename, find, _, _ in MUTANTS:
        path = _path(filename)
        if not path.exists():
            bad.append(f"{name}: {filename} does not exist")
            continue
        if (n := path.read_text().count(find)) != 1:
            bad.append(f"{name}: anchor appears {n}x in {filename}")
    assert not bad, "rows that would SKIP, holding nothing:\n  " + "\n  ".join(bad)


def test_every_mutation_row_actually_changes_the_file():
    """A mutant identical to the original reports on an unmodified tree. One row
    in this index already shipped as a semantic no-op (`setdefault` for
    `__setitem__`, where the dedup lives in the key); that one at least differed
    textually, and this catches only the degenerate case — which is why the row
    above it still needs a human to ask *what breaks*."""
    bad = [name for name, _, find, replace, _ in MUTANTS if find == replace]
    assert not bad, f"the mutant is the original: {bad}"


def test_every_mutation_row_names_a_test_that_exists():
    """The `NO TEST` verdict, an hour earlier. A renamed test leaves the row
    pointing at nothing and `-k` exits 5. Both `testpaths` are searched: the
    E3 rows name tests that live beside the harness in `bench/`.

    The field is a `-k` expression, not always a bare name: nine E7 rows each
    delete one detector out of a table and select the one parametrisation that
    covers it (`test_… and slack_token`), because otherwise all nine rows name
    the same function and a row that mutated the wrong line still reads CAUGHT.
    So every `test_`-shaped identifier in the expression has to exist, and the
    parameter halves are left alone — pytest matches those as substrings.
    """
    defined = set()
    for d in ("tests", "bench"):
        for p in sorted((ROOT / d).glob("test_*.py")):
            defined.update(re.findall(r"^def (test_\w+)\(", p.read_text(), re.M))
    bad = sorted(
        f"{name}: {ident}"
        for name, _, _, _, expr in MUTANTS
        for ident in re.findall(r"\btest_\w+", expr)
        if ident not in defined
    )
    assert not bad, "rows naming a test that does not exist:\n  " + "\n  ".join(bad)
    # A row whose expression has no `test_…` in it at all selects by parameter
    # alone, which is how a typo becomes a row that silently runs the suite.
    nameless = [n for n, _, _, _, expr in MUTANTS if not re.search(r"\btest_\w+", expr)]
    assert not nameless, f"rows selecting no test function: {nameless}"


def test_every_mutation_row_has_a_distinct_name():
    """The name is the filter (`mutate_index.py "some words"`) and the line in
    the report. Two rows sharing one means a verified row and an unverified row
    are indistinguishable in both places."""
    names = [m[0] for m in MUTANTS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicated: {dupes}"
