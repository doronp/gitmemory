"""The secondary set, pinned.

The scores here are `==`, not `>=`. A floor is right for a probe, where the
only movement anyone expects is upward; this set exists to be a *record* of
what real text does to the extractor, and a change in either direction is news
that should stop a test run until somebody writes down which way it went.
"""

from __future__ import annotations

import json
import os

import pytest

from bench import secondary

pytestmark = pytest.mark.skipif(
    secondary.corpus_root() is None,
    reason="the pinned claude-code-log clone is not present; see README.md",
)


def test_the_corpus_is_present_or_explicitly_absent():
    """Same escape hatch as the conformance run: a skip is fine locally, and
    anywhere that must not silently lose the only real-text measurement sets
    GITMEMORY_REQUIRE_CORPUS=1.
    """
    if os.environ.get("GITMEMORY_REQUIRE_CORPUS"):
        assert secondary.corpus_root() is not None


def test_the_manifest_labels_every_block_and_nothing_else():
    """`items()` raises on drift, so reaching 140 is the assertion. The counts
    are restated here so a manifest edited by hand shows up as a test failure
    rather than as a different score.
    """
    items = secondary.items()
    assert len(items) == 140
    gold = {}
    for item in items:
        gold[item["gold"]] = gold.get(item["gold"], 0) + 1
    assert gold == {"none": 135, "directive": 2, "reversal-by-user": 3}
    assert sum(i["machine"] for i in items) == 55


def test_three_annotators_agreed_on_all_but_one_item():
    """The agreement rate is part of the result, not trivia. Two directives out
    of 140 is a very sparse positive class, and a sparse class labelled by
    annotators who disagreed would be worth nothing. They disagreed once, 2-1,
    on a single sentence that pairs a standing constraint with a question.
    """
    manifest = json.loads(secondary.MANIFEST.read_text())
    assert manifest["annotators"] == 3
    assert manifest["unanimous"] == 139
    split = [i for i in manifest["items"] if i["votes"] < 3]
    assert len(split) == 1 and split[0]["gold"] == "directive"


def test_the_extractor_scores_zero_on_real_text():
    """**Precision 0.0000, recall 0.0000 on 140 real human turns**, and it read
    0.0000/0.0000 before the first fix too.

    The same extractor reads 1.0000/1.0000 on the held-out synthetic split and
    23/32 and 25/32 on the two spent probes. This is the number that says what
    those are worth. It is pinned exactly, including the false-positive count,
    because "it got better" and "it got worse" are both things a reader of this
    file needs to be told rather than allowed to assume.

    **30 -> 12 false positives** is the injected-block filter, the one fix this
    set has produced so far. Precision did not move, because it is 0/12 rather
    than 0/30 — there is still no true positive, and both gold directives are
    plain positive standing rules of the shape probes C and D each missed seven
    times. The set is spent as a generalisation measure from the moment of that
    fix; it is a regression floor now.

    See `docs/benchmarks/E5-secondary-set.md`.
    """
    s = secondary.score(secondary.items())
    assert (s["tp"], s["fp"], s["fn"]) == (0, 12, 2)
    assert s["precision"] == 0.0
    assert s["recall"] == 0.0


def test_the_injected_block_filter_took_out_eighteen_of_the_nineteen():
    """19 of the original 30 false positives came from blocks no person typed —
    IDE notifications, slash-command wrappers, command stdout. `_PROHIBIT`
    matches `may not` in "This may or may not be related to the current task",
    and that one sentence is injected into the user role on every file the
    editor opens. `derive._injected` now keeps those out of the prose stream and
    **one** is left.

    The survivor is the `/init` expansion — "Please analyze this codebase and
    create a CLAUDE.md file…" — which the CLI writes into the user role as
    ordinary prose with no marker on it. Catching it needs the preceding
    `<command-name>` block as context, which is an adapter change and not this
    one; `derive._injected` says so in its docstring.

    A probe cannot find any of this. Every probe item is a sentence somebody
    wrote on purpose, so the entire category is absent from A, B, C and D.
    """
    s = secondary.score(secondary.items())
    assert s["machine_fp"] == 1


def test_seven_of_the_nine_assistant_reversals_left_are_reversals():
    """The largest thing the extractor emitted on real sessions was also the
    least right: 61 distinct assistant blocks called `reversal`, of which three
    adjudicators kept 9. Precision 0.1475.

    **Fix 2 withdrew 52 of the 61.** Nine are still emitted and seven of those
    are reversals — precision 0.7778 against the same denominator, which is why
    `n` stays 61. A withdrawal and a correction can only be told apart if the
    population is held at what the extractor emitted when the labels were
    written; see `assistant_score`.

    Two true positives went with the 52, and they are the price: `d439a8fe`, a
    2-1 split with no marker in the block at all, and `e926d873`, which only the
    first-person-ownership family caught — the family that was tried and
    rejected for putting two retrospective summaries back in. Both are recorded
    in `docs/benchmarks/E5-secondary-set.md`.

    This is an adjudication, not a labelling round — the items *are* the
    output — so there is no recall figure and none is claimed. A reversal the
    extractor never flagged is invisible here by construction.
    """
    a = secondary.assistant_score(secondary.assistant_items())
    assert (a["still_emitted"], a["right"], a["n"]) == (9, 7, 61)
    assert round(a["precision"], 4) == 0.7778
    assert a["unanimous"] == 58
    manifest = json.loads(secondary.MANIFEST.read_text())
    assert manifest["unanimous_assistant"] == 58


def test_the_declared_reversal_ceiling_costs_more_than_a_miss():
    """Three of the 140 turns are the user revoking their own instruction — the
    shape `docs/DESIGN.md` declares out of scope. Two of the three do not come
    back as nothing: they come back as `directive`, so the revocation is
    recorded as a rule. Probe C predicted exactly this and probe D measured it
    twice; here it is on real text, at a base rate of 2 in 140.
    """
    s = secondary.score(secondary.items())
    assert (s["aside_n"], s["aside_labelled_something"]) == (3, 2)
