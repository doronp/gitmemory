"""The probe scores, pinned as a floor.

These are not a gate. The gate is `docs/DESIGN.md`'s pre-registered
precision >= 0.85 / recall >= 0.60 on a held-out split, and the extractor has
passed it at 1.0000/1.0000 through four rounds of real defects — which is the
problem these probes exist to state out loud. The generator writes in one
register. Everything wrong with the extractor so far has been invisible to it.

So the numbers here are the second measurement, and they are deliberately
*not* round. 26 of 32 and 27 of 32 on shapes the rules claim to cover, in words
the corpus does not contain.

Both probes are spent: each set the brief for a round and then scored its
result, so they are training data now. That is why these assertions are a floor
and not an equality — a regression is a defect worth a red test, and an
improvement is not evidence of anything until a third probe nobody has scored
says so. Do not tune against them.
"""

from __future__ import annotations

import pytest

from bench.probes import PROBES, score

# Measured 2026-09-21 on 831ef6e, after guard round 3. Before that round:
# A 23/32, B 26/32. The dev fixture read 1.0000/1.0000 both times.
FLOOR = {"A": 26, "B": 27}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_the_probe_score_has_not_regressed(name):
    class_ok, class_n, _, _, misses = score(PROBES[name])
    assert class_n == 32, "both probes are 32 class items; a changed denominator changes the floor"
    assert class_ok >= FLOOR[name], "\n".join(
        [f"probe {name}: {class_ok}/{class_n}, floor {FLOOR[name]}"]
        + [f"  [{g}] expected {e}, got {got}: {t}" for g, _r, t, e, got in misses]
    )


def test_a_ceiling_miss_is_not_counted_as_a_class_miss():
    """The split is the whole point of the scorer. A bare imperative with no
    obligation frame ("Kill the Flask endpoints") is a documented limit, and
    folding it into the class score would make the extractor look broken at
    something it never claimed."""
    cases = [
        ("ceiling", "user", "Kill the Flask endpoints.", "directive"),
        ("directives", "user", "The worker must never touch the production bucket.", "directive"),
    ]
    class_ok, class_n, ceil_ok, ceil_n, misses = score(cases)
    assert (class_ok, class_n) == (1, 1)
    assert (ceil_ok, ceil_n) == (0, 1)
    assert len(misses) == 1, "a ceiling miss is still reported, just not scored against the class"


def test_every_probe_case_is_labelled_with_one_of_three_verdicts():
    """A typo in an expected value ("directives" for "directive") would make an
    item unpassable and quietly lower the ceiling everyone is measured against."""
    bad = [
        (name, text)
        for name, cases in PROBES.items()
        for _g, _r, text, expected in cases
        if expected not in (None, "directive", "reversal")
    ]
    assert not bad, f"unlabelled expectations: {bad}"
