"""Scoring, calibration, and the ship gate for the retrieval benchmark.

The gate is the point of this file. A benchmark that prints numbers is a
benchmark that can print wrong numbers; what makes the numbers worth quoting is
a set of conditions the harness itself has to satisfy before it is willing to
report a verdict at all. Those are `_gate` below, and the reason they exist in
this shape is written there. [E3]
"""

from __future__ import annotations

import bisect
import contextlib
import itertools
import math
import random
import statistics
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gitmemory.adapters import claude_code as cc

from .longmemeval import Instance
from .synth import to_transcript

Retrieve = Callable[[str, int], list[int]]
Factory = Callable[[Instance, bytes], Retrieve]

# Four numbers the gate is built out of. They are policy, not measurement, so
# they live here named rather than inline, where a reader can disagree with one
# without first having to find it.
MIN_INSTANCES = 20  # below this no permutation test can reach ALPHA, so refuse a verdict
MIN_EFFECT = 0.05  # a significant difference this small is not worth shipping on
LEAK_MARGIN = 0.05  # how far `shuffled` may exceed `none` and still count as equivalent
ALPHA = 0.05
EXACT_MAX_N = 14  # 2**14 sign assignments; above this the normal form is used

# The compaction axis, in the order `__main__` prints it.
COMPACTION_MODES: list[str | None] = [None, "before_evidence", "after_evidence", "every_n"]

# The mode whose boundary falls between the evidence and the end of the
# transcript, which is the only arrangement under which a live context window
# has actually lost the evidence. The product claim is tested there and nowhere
# else, because nowhere else is there anything to lose.
CLAIM_MODE = "after_evidence"


@dataclass(frozen=True)
class ArmMetrics:
    turn_recall: float  # fraction of this instance's evidence turns retrieved within k
    turn_mrr: float  # reciprocal rank of the first evidence turn
    session_recall: float  # fraction of answer sessions retrieved within k
    session_mrr: float
    unmatched: int  # offsets that landed in no turn at all


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sign_flip_test(a: list[float], b: list[float]) -> tuple[float, float]:
    """One-sided paired randomisation test: is `a` greater than `b`?

    Returns `(mean_difference, p)` for H1 `mean(a - b) > 0`.

    A sign-flip test and not the z-test that was here, because the z-test had to
    special-case zero variance and the only honest value to return in that case
    does not exist: every arm in this harness can produce a run where all the
    paired differences are identical, and `p = 0` for such a run is a fabricated
    certainty that the gate then acts on. Under the randomisation null each
    difference is as likely to have come out negative, so a constant positive
    difference over n items has `p = 2**-n` — small for a large n, exactly 0.5
    for n = 1, and never zero. A one-observation run can no longer be
    significant, which is the property the old test lacked. [E3]

    Exact by enumeration while that is cheap, and by the same statistic's normal
    form above `EXACT_MAX_N`; the two agree to three decimals by the time they
    swap over. `sum(d**2) == 0` means the arms did not differ anywhere, which is
    `p = 1.0` and not a division.
    """
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    n = len(diffs)
    if n == 0:
        return 0.0, 1.0
    total = math.fsum(diffs)
    mean_diff = total / n
    sumsq = math.fsum(d * d for d in diffs)
    if sumsq == 0.0:
        return mean_diff, 1.0
    if n <= EXACT_MAX_N:
        atleast = sum(
            1
            for signs in itertools.product((1.0, -1.0), repeat=n)
            # `>= total` up to float slop: the all-plus assignment is the
            # observed one and must always count itself.
            if math.fsum(s * d for s, d in zip(signs, diffs, strict=True)) >= total - 1e-12
        )
        return mean_diff, atleast / (2.0**n)
    return mean_diff, 1.0 - normal_cdf(total / math.sqrt(sumsq))


def _deranged(instances: list[Instance], rng: random.Random) -> dict[str, str]:
    """Map each `question_id` to a *different* instance's question.

    Shuffle then rotate, rather than reshuffling until no item sits still. The
    rejection loop this replaces could not terminate at all when two instances
    shared a `question_id` — no permutation makes a duplicate move — and it was
    a `while True` on data read from a file. A rotation of a list of length two
    or more has no fixed point by construction, so there is nothing to retry
    and nothing to bound. [E3]

    Grouped by `question_type` so the control arm is asked questions of the same
    difficulty as the candidate arm. A group of one cannot be deranged within
    itself and falls back to the global rotation.
    """
    by_type: dict[str, list[Instance]] = {}
    for inst in instances:
        by_type.setdefault(inst.question_type, []).append(inst)
    groups = [g for g in by_type.values() if len(g) > 1]
    singletons = [g[0] for g in by_type.values() if len(g) == 1]
    if len(singletons) > 1:
        groups.append(singletons)

    mapping: dict[str, str] = {}
    for group in groups:
        order = list(group)
        rng.shuffle(order)
        for idx, inst in enumerate(order):
            mapping[inst.question_id] = order[(idx + 1) % len(order)].question
    # One instance in total, or one lone question_type: no derangement exists.
    for inst in instances:
        mapping.setdefault(inst.question_id, inst.question)
    return mapping


def _measure(
    offsets: list[int],
    starts: list[int],
    turns: list[Any],
    evidence: set[int],
    answer_sessions: set[str],
) -> ArmMetrics:
    """Score one ranked list of byte offsets against one instance's ground truth.

    Recall counts *distinct* evidence turns found, not whether any was found:
    the loop this replaces broke on the first hit, so an instance with three
    evidence turns scored 1.0 for finding one of them. An offset landing in no
    turn — past the end, or in the newline between two turns — still consumes
    its rank, which is the honest treatment of a retriever that returns
    garbage, and is counted so that a run producing many of them is visible
    rather than merely mediocre. [E3]
    """
    found_turns: set[int] = set()
    found_sessions: set[str] = set()
    turn_mrr = 0.0
    session_mrr = 0.0
    unmatched = 0
    for rank, offset in enumerate(offsets, start=1):
        idx = bisect.bisect_right(starts, offset) - 1
        turn = turns[idx] if idx >= 0 else None
        if turn is None or offset >= turn.byte_offset + turn.byte_len:
            unmatched += 1
            continue
        if turn.byte_offset in evidence:
            if not found_turns:
                turn_mrr = 1.0 / rank
            found_turns.add(turn.byte_offset)
        if turn.session_id in answer_sessions:
            if not found_sessions:
                session_mrr = 1.0 / rank
            found_sessions.add(turn.session_id)
    return ArmMetrics(
        turn_recall=len(found_turns) / len(evidence) if evidence else 0.0,
        turn_mrr=turn_mrr,
        session_recall=len(found_sessions) / len(answer_sessions) if answer_sessions else 0.0,
        session_mrr=session_mrr,
        unmatched=unmatched,
    )


def _live_context(retrieve: Retrieve, cutoff: int) -> Retrieve:
    """The ablation the product claim is measured against.

    What an agent can still see after a compaction: everything from the last
    boundary onward, and nothing before it. Same retriever, same corpus, one
    window — so the difference between this arm and `candidate` is the value of
    having kept the bytes, with ranking quality held constant. [E3]
    """

    def retrieve_live(query: str, k: int) -> list[int]:
        # Ask for more than k, because the filter is applied after ranking:
        # a budget of k spent on pre-boundary turns is not a smaller window, it
        # is the wrong measurement.
        return [o for o in retrieve(query, k * 4) if o >= cutoff][:k]

    return retrieve_live


def _gate(
    per_mode: dict[str | None, dict[str, list[ArmMetrics]]],
    modes: list[str | None],
    n: int,
) -> tuple[bool, list[dict[str, Any]]]:
    """Four conditions, each of which has to hold before any score is quotable.

    The gate this replaces asked one question — does the `shuffled` control beat
    the `none` floor — and `none` is the constant-zero arm, so a retriever that
    returned nothing for every query produced all-zero scores and *passed*. It
    was green on a dead index, and red on the real one, since the control kept
    its signal on a fixture whose questions were near-identical. A gate that
    only checks the control cannot notice that the thing under test is broken,
    so the conditions here cover the apparatus, the signal, the control, and the
    claim separately. [E3]

    1. **Apparatus.** The oracle arm is handed the ground-truth offsets, so it
       must score exactly 1.0. Anything else means the offsets, the parser, or
       the matcher disagree, and no other number in the run means anything.
    2. **Signal.** `candidate` beats `none` by more than `MIN_EFFECT`, one-sided
       and Bonferroni-corrected across the modes tested.
    3. **No leakage.** `shuffled` stays within `LEAK_MARGIN` of `none`. This is
       an equivalence bound and deliberately not a significance test: failing to
       reject a null is not evidence for it, and "the control did not
       *significantly* beat the floor" is exactly the sentence a small sample
       makes free.
    4. **Claim.** Under `CLAIM_MODE` the evidence lies before the boundary, so
       `candidate` must beat `live_context` — the ablation that keeps only what
       survived compaction. Without this the suite measures BM25 and calls it a
       memory system.
    """
    checks: list[dict[str, Any]] = []
    alpha = ALPHA / max(1, len(modes))

    def record(name: str, mode: str | None, passed: bool, detail: str) -> None:
        checks.append(
            {
                "check": name,
                "compaction": mode,
                "passed": passed,
                "detail": detail,
                "alpha": alpha,
            }
        )

    if n < MIN_INSTANCES:
        record(
            "sample_size",
            None,
            False,
            f"{n} instances; {MIN_INSTANCES} is the fewest that can reach alpha={alpha:.4f}",
        )
        return False, checks
    record("sample_size", None, True, f"{n} instances")

    for mode in modes:
        arms = per_mode[mode]
        ref = statistics.mean(m.turn_recall for m in arms["reference"])
        record(
            "apparatus",
            mode,
            ref == 1.0,
            f"reference turn_recall={ref:.4f}, must be exactly 1.0",
        )

        cand = [m.turn_recall for m in arms["candidate"]]
        none = [m.turn_recall for m in arms["none"]]
        diff, p = sign_flip_test(cand, none)
        record(
            "signal",
            mode,
            p < alpha and diff > MIN_EFFECT,
            f"candidate - none = {diff:.4f} (p={p:.3e}, alpha={alpha:.4f}, "
            f"min effect={MIN_EFFECT})",
        )

        leak = statistics.mean(m.turn_recall for m in arms["shuffled"]) - statistics.mean(none)
        record(
            "no_leakage",
            mode,
            leak <= LEAK_MARGIN,
            f"shuffled - none = {leak:.4f}, must be <= {LEAK_MARGIN}",
        )

        if mode == CLAIM_MODE and "live_context" in arms:
            live = [m.turn_recall for m in arms["live_context"]]
            claim_diff, claim_p = sign_flip_test(cand, live)
            record(
                "claim",
                mode,
                claim_p < alpha and claim_diff > MIN_EFFECT,
                f"candidate - live_context = {claim_diff:.4f} (p={claim_p:.3e}, alpha={alpha:.4f})",
            )

    return all(c["passed"] for c in checks), checks


def score(
    instances: list[Instance],
    retrieve_factory: Factory,
    *,
    k: int = 10,
    compaction_modes: list[str | None] | None = None,
    seed: int = 42,
    extra_arms: dict[str, Factory] | None = None,
) -> dict[str, Any]:
    """Run every arm over every compaction mode and return the report plus the verdict.

    `extra_arms` rather than importing the optional retrievers here: whether
    `model2vec` is installed is a fact about the machine, and a scoring function
    that silently grows and shrinks its arm set based on one is a scoring
    function whose reports cannot be compared. `__main__` decides, and says so.
    """
    modes = COMPACTION_MODES if compaction_modes is None else compaction_modes
    extra = extra_arms or {}

    # `_abs` instances are LongMemEval's abstention set: the correct answer is
    # that the haystack does not contain one, so there is no evidence turn to
    # retrieve and a retrieval metric has nothing to say about them.
    active = [inst for inst in instances if not inst.question_id.endswith("_abs")]
    if not active:
        raise ValueError("no active (non-abstention) instances to evaluate")

    shuffled_query = _deranged(active, random.Random(seed))
    arms = ["candidate", "none", "shuffled", "reference", "live_context", *extra]

    per_mode: dict[str | None, dict[str, list[ArmMetrics]]] = {}
    breakdowns: list[dict[str, Any]] = []

    for mode in modes:
        by_arm: dict[str, list[ArmMetrics]] = {arm: [] for arm in arms}
        by_type: dict[str, dict[str, list[ArmMetrics]]] = {}

        for idx, inst in enumerate(active):
            transcript = to_transcript(inst, seed=seed + idx, compaction=mode)
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
                tmp.write(transcript.bytes_data)
                tmp_path = tmp.name
            try:
                parsed = cc.parse(tmp_path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    Path(tmp_path).unlink()

            turns = sorted(parsed.turns, key=lambda t: t.byte_offset)
            starts = [t.byte_offset for t in turns]
            evidence = set(transcript.evidence_byte_offsets)
            if not evidence:
                raise ValueError(
                    f"instance {inst.question_id!r} has no evidence turn; the oracle arm "
                    "would score 0 on it and drag the apparatus check down for a reason "
                    "that has nothing to do with retrieval"
                )
            answer_sessions = set(inst.answer_session_ids)
            boundaries = [e.byte_offset for e in parsed.events if e.kind == "compaction"]

            candidate = retrieve_factory(inst, transcript.bytes_data)
            built = {name: fn(inst, transcript.bytes_data) for name, fn in extra.items()}
            try:
                ranked: dict[str, list[int]] = {
                    "candidate": candidate(inst.question, k),
                    "none": [],
                    "shuffled": candidate(shuffled_query[inst.question_id], k),
                    "reference": sorted(evidence)[:k],
                    "live_context": (
                        _live_context(candidate, max(boundaries))(inst.question, k)
                        if boundaries
                        else candidate(inst.question, k)
                    ),
                }
                for name, fn in built.items():
                    ranked[name] = fn(inst.question, k)
            finally:
                for obj in (candidate, *built.values()):
                    close = getattr(obj, "close", None)
                    if close is not None:
                        close()

            for arm in arms:
                m = _measure(ranked[arm], starts, turns, evidence, answer_sessions)
                by_arm[arm].append(m)
                by_type.setdefault(inst.question_type, {a: [] for a in arms})[arm].append(m)

        per_mode[mode] = by_arm
        for qtype in sorted(by_type):
            entry: dict[str, Any] = {"compaction": mode, "question_type": qtype}
            for arm in arms:
                entry[arm] = _summarise(by_type[qtype][arm])
            breakdowns.append(entry)

    passed, checks = _gate(per_mode, modes, len(active))
    return {
        "overall_metrics": {
            mode: {arm: _summarise(ms) for arm, ms in by_arm.items()}
            for mode, by_arm in per_mode.items()
        },
        "breakdowns": breakdowns,
        "arms": arms,
        "calibration_passed": passed,
        "calibration_checks": checks,
        "k": k,
        "seed": seed,
        "num_instances_evaluated": len(active),
    }


def _summarise(ms: list[ArmMetrics]) -> dict[str, float]:
    return {
        "turn_recall": statistics.mean(m.turn_recall for m in ms),
        "turn_mrr": statistics.mean(m.turn_mrr for m in ms),
        "session_recall": statistics.mean(m.session_recall for m in ms),
        "session_mrr": statistics.mean(m.session_mrr for m in ms),
        "unmatched_per_query": statistics.mean(m.unmatched for m in ms),
    }
