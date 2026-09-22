"""The probe scores, pinned as a floor.

These are not a gate. The gate is `docs/DESIGN.md`'s pre-registered
precision >= 0.85 / recall >= 0.60 on a held-out split, and the extractor has
passed it at 1.0000/1.0000 through four rounds of real defects — which is the
problem these probes exist to state out loud. The generator writes in one
register. Everything wrong with the extractor so far has been invisible to it.

So the numbers here are the second measurement, and they are deliberately
*not* round. 26 of 32, 27 of 32 and 14 of 32 on shapes the rules claim to cover,
in words the corpus does not contain.

A and B are spent: each set the brief for a round and then scored its result, so
they are training data now. C and D are not — each was written blind, frozen,
and scored once — and 14 is what the extractor is worth on text nobody shaped
for it, twice, in two unrelated domains.

That is why these assertions are a floor and not an equality: a regression is a
defect worth a red test. An improvement in A or B is not evidence of anything.
An improvement in C is not evidence either, unless it came from a change that
was never shown C's miss list. Do not tune against them.
"""

from __future__ import annotations

import pytest

from bench.probes import PROBES, score

# Measured 2026-09-21 on 831ef6e, after guard round 3. Before that round:
# A 23/32, B 26/32. The dev fixture read 1.0000/1.0000 both times.
#
# C is 14 and that is not a typo. A and B each set the brief for a round and
# then scored its result, so they are training data; C and D were each written
# blind, frozen, and scored once, and they are the only generalisation numbers
# here. Raising either by editing the extractor against its own miss list would
# convert an honest measurement into a fourth piece of training data. Don't.
# `docs/benchmarks/E5-probe-C.md` and `E5-probe-D.md` are the records.
#
# D is 14 as well, measured 2026-09-22 on b63f4f7, and the coincidence is the
# finding: same total as C, same split inside it — 10/10 non-decisions, 4/11
# directives, 0/11 reversals — in a domain (aviation line maintenance) chosen
# with no knowledge of C's (theatrical show control). C's shape was not its
# domain's doing.
#
# **A 26 -> 23 and B 27 -> 25, lowered deliberately by E5 fix 2.** Five class
# items, all in the assistant `reversals` group, and the trade is priced in
# `docs/benchmarks/E5-secondary-set.md`:
#
#   - three are bare substitution — "We'll swap the regex validator for a real
#     parser", "Replacing the hand-written loop with `itertools.groupby`",
#     "Let me pull the caching out of the handler and put it behind the
#     repository interface instead". That is the shape the fix exists to stop
#     scoring, because on real sessions it is how an assistant narrates an
#     ordinary edit: 31 of 52 wrong calls, adjudicated 3-0. These are a probe
#     author writing a course change in the register of a diff, and the
#     extractor cannot tell the two apart from one block.
#   - two are genuine withdrawals the new evidence list does not reach —
#     "Rolling back to the synchronous client for now" (`_ABANDON` fires, but
#     the replacement is a `to`-complement rather than the `_ADOPT` half the
#     pair requires) and "The mmap approach isn't paying for itself — switching
#     to a plain buffered read" (a verdict `_RECANT` does not list). These are
#     collateral, not the trade, and they are the seed for the next round.
#
# Lowering a floor is the thing this file exists to make hard, so: the reason
# it is legitimate here is that **A and B are spent**. Their numbers are
# training data, a regression floor and nothing else, and the honest response to
# a precision fix that costs them is to re-pin them and say so — not to add the
# six sentences back and call the extractor improved. The measurement that
# decides whether fix 2 was right is probe E, which is unwritten, blind, and
# will be scored once. Until then the claim is precision on real text
# (0.1475 -> 0.7778) against recall on fiction, and it is stated as a trade
# rather than as a win.
FLOOR = {"A": 23, "B": 25, "C": 14, "D": 14}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_the_probe_score_has_not_regressed(name):
    class_ok, class_n, _, _, misses = score(PROBES[name])
    assert class_n == 32, "every probe is 32 class items; a changed denominator changes the floor"
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
