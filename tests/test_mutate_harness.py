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

import pytest
from mutate_index import verdict

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
