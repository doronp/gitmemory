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

    The same extractor reads 1.0000/0.7428 on the held-out synthetic split and
    23/32 and 25/32 on the two spent probes. This is the number that says what
    those are worth. It is pinned exactly, including the false-positive count,
    because "it got better" and "it got worse" are both things a reader of this
    file needs to be told rather than allowed to assume.

    **30 -> 12 false positives** was the injected-block filter, the first fix
    this set produced, and **12 -> 6** is the second: the contracted negation
    stopped being a prohibition outside imperative position, because on these
    89 user prose blocks `don't`/`doesn't` occurs 21 times and 18 of them report
    that something is broken. Precision did not move for either, because it was
    0/6 as it was 0/30 — no true positive, and both gold directives are plain
    positive standing rules of the shape probes C and D each missed seven times.
    The set is spent as a generalisation measure from the moment of the first
    fix; it is a regression floor now.

    **0/6/2 -> 1/7/1 is the first true positive this set has ever produced**, and
    it is worth exactly one sentence of celebration: `_PERSIST` reads *"I'd like
    to keep it the same 3 simple files"* as a standing rule, which it is. The
    credit is to the block and not to that clause, though: the verb matches
    twice in it, and the first hit is *"while keeping it simple and dependency
    free"* — a condition on the task being asked for, not a standing rule. The
    verdict is block-level, so the right one being in there is enough to score
    it; a span-level extractor would have to choose, and nothing here does. The
    new
    false positive beside it is *"Keep it same overall length as the pros or
    cons"* — a lexical twin of the true positive, in the same register, from the
    same kind of turn. No rule in this module separates them, three annotators
    labelled them differently, and pretending the gain is clean would be the
    dishonest way to write 1/7/1 up. Precision is 0.1250 and recall 0.5000.

    See `docs/benchmarks/E5-secondary-set.md`, fixes 1, 6 and 7.
    """
    s = secondary.score(secondary.items())
    assert (s["tp"], s["fp"], s["fn"]) == (1, 7, 1)
    assert round(s["precision"], 4) == 0.1250
    assert round(s["recall"], 4) == 0.5000


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


def test_the_seven_assistant_reversals_left_are_all_reversals():
    """The largest thing the extractor emitted on real sessions was also the
    least right: 61 distinct assistant blocks called `reversal`, of which three
    adjudicators kept 9. Precision 0.1475.

    **Fix 2 withdrew 52 of the 61**, leaving nine emitted of which seven were
    reversals — 0.7778. **Scoping fix 2's conjunction to the paragraph withdrew
    the other two**, and both were wrong, so seven are emitted and all seven are
    reversals. Precision 1.0000 against the same denominator, which is why `n`
    stays 61: a withdrawal and a correction can only be told apart if the
    population is held at what the extractor emitted when the labels were
    written; see `assistant_score`.

    **1.0000 on seven items is not a precision claim and must not be quoted as
    one.** The denominator here is the extractor's own output, so it shrinks
    every time the extractor gets shyer, and a predicate that emitted nothing
    would read 1.0000 on an empty set. That is exactly why the assertion below
    is on the three counts and not on the ratio. What the number says is narrow
    and worth having: of what it still writes into the graph on real sessions,
    nothing is known to be wrong.

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
    assert (a["still_emitted"], a["right"], a["n"]) == (7, 7, 61)
    assert round(a["precision"], 4) == 1.0000
    assert a["unanimous"] == 58
    manifest = json.loads(secondary.MANIFEST.read_text())
    assert manifest["unanimous_assistant"] == 58


def test_the_declared_reversal_ceiling_costs_more_than_a_miss():
    """Three of the 140 turns are the user revoking their own instruction — the
    shape `docs/DESIGN.md` declares out of scope. All three now come back as
    nothing, which is the *declared* behaviour: the ceiling is a miss, and a
    miss is what it should cost.

    It read 2 of 3 until fix 6, and neither of the two was labelled for a reason
    that had anything to do with revocation. One was scored by a `doesn't`
    inside a line of a pasted diff — a code comment, on a deleted line, in
    somebody else's file. The other was *"Never mind!"*, scored by `never`, the
    formula for dropping a request read as a standing prohibition. Both are now
    excluded, so the base rate of a revocation being recorded as a rule is 0 in
    140 rather than 2, and what remains is the declared miss.

    Probe C predicted the mislabelling and probe D measured it twice. Neither
    predicted *this*: that the cause would be pasted machine text rather than
    the revocation frame.

    **The guard on this paragraph said to check here when the positive-rule hole
    closed, and one corner of it now has: still 0.** The three real revocations
    carry no persistence verb, so `_PERSIST` does not reach them. This is not the
    all-clear it looks like. On probes C and D the same rule turns three
    user-reverses-own-instruction items — one and two — from a silent miss into
    `directive`, because there the revocation and the surviving rule are written
    into one sentence, the shape this corpus happens not to have in its three. The
    ceiling still costs more than a miss; it costs it somewhere this census
    cannot see. `docs/benchmarks/E5-secondary-set.md`, fix 7.
    """
    s = secondary.score(secondary.items())
    assert (s["aside_n"], s["aside_labelled_something"]) == (3, 0)
