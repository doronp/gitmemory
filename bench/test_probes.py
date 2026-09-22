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
for it, twice, in two unrelated domains. E is the third, scored once at 19, and
it is spent now too: its miss list became the brief the moment it was read.

That is why these assertions are a floor and not an equality: a regression is a
defect worth a red test. An improvement in A or B is not evidence of anything.
An improvement in C is not evidence either, unless it came from a change that
was never shown C's miss list. Do not tune against them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

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
#
# **E is 19, measured 2026-09-22 on 53c6464, and it settles that paragraph.**
# Written blind in COBOL batch maintenance, aimed at the one boundary fix 2
# moved. Ten of its twelve non-decisions are substitution narration and **all
# twelve came back clean** — fix 2's claim is not an overfit to the corpus it
# was adjudicated on. The price is the other column: 2 of 12 reversals, because
# nine of the twelve were written with no self-correction marker and the
# assistant branch recovers a course change only when the author announces one.
# The trade above is therefore real and now measured on both sides.
# `docs/benchmarks/E5-probe-E.md` is the record, and E is spent as of that page.
#
# **E is pinned at 20, not 19, and the difference is training data.** One of the
# two defects E's miss list turned up was worth fixing — `_PIVOT`'s cancel marker
# only ever worked with a pronoun — and the fix lifts E's own item and probe A's
# ceiling item (aside 2/5 -> 3/5) and moves nothing else on any board. So 19 is
# what E measured blind and 20 is what it scores having been read. Pinning 20
# keeps the floor honest; quoting 20 as a generalisation number would not be.
#
# **C is 13, lowered deliberately, and the item it lost was right by accident.**
# `_PROHIBIT` stopped reading the contracted `don't`/`doesn't` as a prohibition
# unless it is an imperative, which halves the false positives on the only real
# user text here (12 to 6) and costs exactly one probe item:
#
#   "Whatever you end up doing about the moving-head timeout — and I know it's
#   messy, the fixtures don't even agree on what a timeout means — the house
#   lights still have to be up within two seconds of the panic button."
#
# The directive in that sentence is *the house lights have to be up*, a positive
# standing rule, which is the hole probes C, D and E all independently report
# and which nothing in `derive.py` claims. What was scoring it was `don't` inside
# a parenthetical about fixtures disagreeing — a report, in an aside, about
# somebody else's software. So 14 was 13 plus a coincidence, and when the
# positive-rule hole is closed this comes back to 14 on the rule that should
# always have held it. Re-pin it up then, and not before.
#
# **Then is now, and C is 16.** `_PERSIST` claims the one corner of the positive
# standing rule that says so in a verb — a thing *stays* as it is — and it takes
# C to 16, D to 16 and E to 21. It is a corner and not the class: most of what
# those three probes still miss is a subject noun phrase and a simple-present
# verb, which wants a parse. The item above is back, on the rule that should
# have held it and not on the parenthetical.
#
# **Three of C's gains and both of D's are the same sentence shape**, so read
# these as one measurement repeated rather than five. And one of C's is right
# for the wrong reason — *"I'd rather eat the slower timecode sync than keep the
# one that drifts"* fires on the `keep` in the **rejected** alternative — which
# is the third time this file has had to say that a probe item can pass for a
# reason nobody would defend. `docs/benchmarks/E5-secondary-set.md`, fix 7.
FLOOR = {"A": 23, "B": 25, "C": 16, "D": 16, "E": 21}


def test_probe_e_is_the_authors_file_and_not_a_copy_of_it():
    """`probe_e_cases.py` is the probe author's file, copied in byte-for-byte.

    Probe D is read out of a frozen markdown table and refuses to run if the
    digest moves; E gets the same guarantee for the same reason. Thirty-two
    sentences transcribed by hand are thirty-two chances to soften one, and a
    probe whose items drifted after it was scored is worth nothing at all. The
    digest covers the author's prose too — their labelling conventions, the ten
    items they expect to be disputed, and their attestation — because a
    contested label is only arguable while the argument for it is still there.
    """
    path = Path(__file__).resolve().parent / "probe_e_cases.py"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "e327c0050b3320b1f92cf96acd1efa2da65a2d9c97c526d414bc28202c6cf028", (
        f"probe E has been edited since it was scored: {digest}"
    )


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
