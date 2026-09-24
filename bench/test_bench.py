"""Tests for the benchmark and calibration harness.

Every test here is meant to fail against a specific mutation of the code it
covers; `tests/mutate_index.py` carries those mutants and the attribution. The
previous version of this file passed against seven separate semantic mutations
of `bench/`, including deleting the statistics and making the control arm *be*
the candidate arm, which is the failure mode a benchmark suite has to not have.
[E3]

The synthetic fixture uses a distinct nonsense token per instance on purpose. A
unit test of the harness is asking whether the machinery is wired correctly —
whether the oracle is exact, whether the control is deaf, whether the gate
trips — and a fixture where retrieval is ambiguous cannot answer any of those.
Retrieval difficulty is what the real corpus is for: `pytest -m corpus`.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import os
import tempfile
from pathlib import Path

import pytest

from bench import longmemeval as lm
from bench import score, synth
from bench.arms import gitmemory_factory
from gitmemory.adapters import claude_code as cc

# Above score.MIN_INSTANCES, so the gate is willing to return a verdict at all.
N = score.MIN_INSTANCES + 4
FILLER = 6  # filler turns per session; enough that the evidence turn is not half the corpus


def key(i: int) -> str:
    """The one token that appears in instance `i`'s evidence turn and nowhere else."""
    return f"zylophant{i:02d}"


def make_instance(
    i: int,
    *,
    evidence_turns: int = 1,
    question_id: str | None = None,
    question_type: str = "single-session-user",
) -> lm.Instance:
    qid = question_id if question_id is not None else f"q_{i:02d}"
    token = key(i)
    first = [
        lm.LongMemTurn(role="user", content=f"Filler note {i}-{t} about nothing.", has_answer=False)
        for t in range(FILLER)
    ]
    second: list[lm.LongMemTurn] = []
    for e in range(evidence_turns):
        # Deliberately shares no vocabulary with the question except `token`, so
        # that a question belonging to another instance matches this corpus on
        # nothing at all. Any shared word — "ledger", "filed" — would make the
        # control arm retrieve the evidence too, and a control that scores like
        # the candidate cannot detect a leak in the candidate.
        second.append(
            lm.LongMemTurn(
                role="user",
                content=f"Part {e}: {token} sits in drawer {e}.",
                has_answer=True,
            )
        )
        second.append(
            lm.LongMemTurn(role="assistant", content=f"Noted, part {e}.", has_answer=False)
        )
    second += [
        lm.LongMemTurn(role="user", content=f"Later note {i}-{t}, unrelated.", has_answer=False)
        for t in range(FILLER)
    ]
    return lm.Instance(
        question_id=qid,
        question_type=question_type,
        question=f"Where was {token} stored?",
        answer=f"drawer for {token}",
        question_date="2026-09-20T12:00:00Z",
        haystack_dates=["2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z"],
        haystack_session_ids=[f"s1_{i}", f"s2_{i}"],
        haystack_sessions=[first, second],
        answer_session_ids=[f"s2_{i}"],
    )


def corpus(n: int = N) -> list[lm.Instance]:
    return [make_instance(i) for i in range(n)]


def as_dict(inst: lm.Instance) -> dict:
    return {
        "question_id": inst.question_id,
        "question_type": inst.question_type,
        "question": inst.question,
        "answer": inst.answer,
        "question_date": inst.question_date,
        "haystack_dates": inst.haystack_dates,
        "haystack_session_ids": inst.haystack_session_ids,
        "haystack_sessions": [
            [{"role": t.role, "content": t.content, "has_answer": t.has_answer} for t in s]
            for s in inst.haystack_sessions
        ],
        "answer_session_ids": inst.answer_session_ids,
    }


def parse_bytes(raw: bytes, tmp_path, name: str = "t.jsonl"):
    p = tmp_path / name
    p.write_bytes(raw)
    return cc.parse(str(p))


def checks_named(report: dict, name: str) -> list[dict]:
    return [c for c in report["calibration_checks"] if c["check"] == name]


# --------------------------------------------------------------------------
# loader
# --------------------------------------------------------------------------


def test_longmemeval_loader_validates_types(tmp_path):
    inst = make_instance(0)
    good = as_dict(inst)
    p = tmp_path / "valid.json"
    p.write_text(json.dumps([good]))
    loaded = list(lm.load(p))
    assert len(loaded) == 1
    assert loaded[0].question_id == "q_00"

    p_bad = tmp_path / "bad1.json"
    p_bad.write_text(json.dumps(good))
    with pytest.raises(ValueError, match="must be a JSON list"):
        list(lm.load(p_bad))

    missing = {k: v for k, v in good.items() if k != "question_id"}
    p_bad = tmp_path / "bad2.json"
    p_bad.write_text(json.dumps([missing]))
    with pytest.raises(ValueError, match="missing required field 'question_id'"):
        list(lm.load(p_bad))

    wrong = dict(good, haystack_sessions=[[{"role": "user", "content": 123}]])
    p_bad = tmp_path / "bad3.json"
    p_bad.write_text(json.dumps([wrong]))
    with pytest.raises(ValueError, match="'role' and 'content' must be strings"):
        list(lm.load(p_bad))


def test_a_repeated_question_id_is_rejected_by_the_loader(tmp_path):
    """Two instances under one id used to hang the run, not fail it.

    `question_id` is the key the shuffled arm deranges on, and no permutation
    can move a duplicate off itself — the rejection loop that built the pairing
    spun forever on data read from a file. The loop is gone, and the duplicate
    is now refused at the door, which is the layer that can say which index it
    was. [E3]
    """
    p = tmp_path / "dup.json"
    p.write_text(json.dumps([as_dict(make_instance(0)), as_dict(make_instance(0))]))
    with pytest.raises(ValueError, match="repeats question_id 'q_00'"):
        list(lm.load(p))


def test_the_fixture_question_shares_only_its_token_with_the_transcript(tmp_path):
    """Pins the property every gate test below leans on.

    The control arm is only deaf if another instance's question matches this
    instance's corpus on nothing. Two earlier versions of this fixture failed
    that quietly: first on `ledger` and `filed`, which the evidence turn also
    used, and then on `is` and `the`, which appear in the compaction summary
    boilerplate — short enough that BM25 ranked it above the evidence. Neither
    broke a test; they moved a number. So the property is asserted rather than
    assumed, against the transcript with *every* injected line in it. [E3]
    """
    inst = make_instance(0)
    raw = synth.to_transcript(inst, seed=1, compaction="after_evidence").bytes_data
    words = set()
    for turn in parse_bytes(raw, tmp_path).turns:
        words |= {w.lower() for w in "".join(b.text for b in turn.blocks).split()}
    shared = {w.strip("?.,:").lower() for w in inst.question.split()} & {
        w.strip("?.,:") for w in words
    }
    assert shared == {key(0)}


def test_the_derangement_terminates_and_moves_every_instance():
    """No fixed points, and no rejection sampling to get there."""
    import random

    instances = corpus(8)
    mapping = score._deranged(instances, random.Random(42))
    assert len(mapping) == 8
    for inst in instances:
        assert mapping[inst.question_id] != inst.question
        assert mapping[inst.question_id] in {i.question for i in instances}


def test_the_derangement_pairs_within_question_type():
    """The control arm gets a question of the same kind, not merely a different one."""
    import random

    instances = [
        make_instance(i, question_type="temporal" if i % 2 else "single-session-user")
        for i in range(8)
    ]
    by_q = {i.question: i.question_type for i in instances}
    mapping = score._deranged(instances, random.Random(7))
    for inst in instances:
        assert by_q[mapping[inst.question_id]] == inst.question_type


def test_a_lone_question_type_is_still_deranged():
    """A type holding one instance used to be handed back its own question.

    Singletons are pooled, but a *lone* singleton had no pool to join and fell
    through to a `setdefault` that mapped it to itself — a control arm that is
    the candidate arm, for that instance, silently. LongMemEval's six types are
    all well populated so it never fired there, which is not a reason for it to
    be reachable. Found by Gemini in pair review. [E3]
    """
    import random

    instances = [make_instance(i, question_type="a" if i < 7 else "b") for i in range(8)]
    assert [i.question_type for i in instances].count("b") == 1, "one lone singleton type"
    mapping = score._deranged(instances, random.Random(3))
    for inst in instances:
        assert mapping[inst.question_id] != inst.question


def test_a_single_instance_has_no_control_arm_and_says_so():
    """Not a silent self-mapping: there is no derangement of one thing."""
    import random

    with pytest.raises(ValueError, match="at least two instances"):
        score._deranged([make_instance(0)], random.Random(1))


# --------------------------------------------------------------------------
# synth
# --------------------------------------------------------------------------


def test_transcript_determinism():
    inst = make_instance(0)
    a = synth.to_transcript(inst, seed=42, compaction="every_n")
    b = synth.to_transcript(inst, seed=42, compaction="every_n")
    assert a.bytes_data == b.bytes_data
    assert a.evidence_byte_offsets == b.evidence_byte_offsets
    assert a.session_ids == b.session_ids


def test_synth_round_trips_through_adapter(tmp_path):
    inst = make_instance(0)
    for comp in score.COMPACTION_MODES:
        parsed = parse_bytes(
            synth.to_transcript(inst, seed=123, compaction=comp).bytes_data,
            tmp_path,
            f"t_{comp}.jsonl",
        )
        assert len(parsed.turns) > 0
        assert parsed.skipped == {}


def test_both_user_content_shapes_appear_and_both_parse(tmp_path):
    """Claude Code writes user content as a string *and* as a block list."""
    raw = synth.to_transcript(make_instance(0), seed=1, compaction=None).bytes_data
    shapes = set()
    for line in raw.splitlines():
        obj = json.loads(line)
        if obj.get("type") == "user":
            shapes.add(type(obj["message"]["content"]).__name__)
    assert shapes == {"str", "list"}
    assert parse_bytes(raw, tmp_path).skipped == {}


def test_the_question_is_never_written_into_the_haystack():
    """The query is not corpus, and indexing it handicapped exactly one arm.

    It used to be appended as a final user turn. Sharing most of its words with
    the evidence turn, it took rank 1 for every query and pinned the candidate
    arm's MRR at 0.5 — while the oracle arm, which never goes through retrieval,
    was untouched. [E3]
    """
    inst = make_instance(3)
    transcript = synth.to_transcript(inst, seed=5, compaction=None)
    assert inst.question.encode() not in transcript.bytes_data

    retriever = gitmemory_factory(inst.question_id, transcript.bytes_data)
    try:
        ranked = retriever(inst.question, 10)
    finally:
        retriever.close()
    assert ranked[0] == transcript.evidence_byte_offsets[0]


def test_a_ground_truth_offset_is_a_turn_start_exactly(tmp_path):
    """Exactly the start, not merely inside the turn.

    The containment window is wide enough to swallow an off-by-one applied to
    every evidence offset, so a test written as `is it inside a turn` passes on
    a systematically shifted ground truth. Equality is the claim. [E3]
    """
    transcript = synth.to_transcript(make_instance(0), seed=9, compaction="before_evidence")
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    starts = {t.byte_offset for t in parsed.turns}
    assert transcript.evidence_byte_offsets
    for offset in transcript.evidence_byte_offsets:
        assert offset in starts
    texts = {t.byte_offset: "".join(b.text for b in t.blocks) for t in parsed.turns}
    for offset in transcript.evidence_byte_offsets:
        assert key(0) in texts[offset]


def test_every_evidence_turn_gets_a_ground_truth_offset():
    """Three evidence turns, three offsets — not one."""
    transcript = synth.to_transcript(make_instance(0, evidence_turns=3), seed=11, compaction=None)
    assert len(transcript.evidence_byte_offsets) == 3


def test_the_after_evidence_boundary_follows_the_last_evidence_turn(tmp_path):
    """Placed after the *first* evidence turn, the rest stayed inside the live window.

    Which silently turned the compaction arm into partial credit for an
    ablation that had not actually ablated anything. [E3]
    """
    transcript = synth.to_transcript(
        make_instance(0, evidence_turns=3), seed=11, compaction="after_evidence"
    )
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    boundaries = [e.byte_offset for e in parsed.events if e.kind == "compaction"]
    assert len(boundaries) == 1
    assert max(transcript.evidence_byte_offsets) < boundaries[0]


def test_compaction_boundaries_are_emitted_once_per_mode(tmp_path):
    """Counts, over several seeds, so no single lucky seed is load-bearing."""
    for seed in range(1, 6):
        inst = make_instance(0)

        def events(comp, seed=seed, inst=inst):
            raw = synth.to_transcript(inst, seed=seed, compaction=comp).bytes_data
            parsed = parse_bytes(raw, tmp_path, f"c_{comp}_{seed}.jsonl")
            return [e for e in parsed.events if e.kind == "compaction"]

        assert len(events(None)) == 0
        assert len(events("before_evidence")) == 1
        assert len(events("after_evidence")) == 1
        # 2 sessions x (6 filler + 2 evidence-ish) turns, a boundary every 10.
        assert len(events("every_10")) >= 1


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


def measure(offsets, parsed, evidence, sessions):
    turns = sorted(parsed.turns, key=lambda t: t.byte_offset)
    return score._measure(offsets, [t.byte_offset for t in turns], turns, evidence, sessions)


def test_an_offset_inside_a_turn_matches_that_turn(tmp_path):
    """Containment, not equality: a retriever may point mid-turn."""
    inst = make_instance(0)
    transcript = synth.to_transcript(inst, seed=3, compaction=None)
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    evidence = set(transcript.evidence_byte_offsets)
    inside = min(evidence) + 5
    m = measure([inside], parsed, evidence, set(inst.answer_session_ids))
    assert m.turn_recall == 1.0
    assert m.unmatched == 0


def test_recall_counts_every_evidence_turn_not_just_the_first(tmp_path):
    """Finding one of three used to score 1.0.

    The loop broke on the first hit, so `turn_recall` was a hit-rate wearing
    recall's name and every multi-evidence instance scored against the wall. [E3]
    """
    inst = make_instance(0, evidence_turns=3)
    transcript = synth.to_transcript(inst, seed=13, compaction=None)
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    evidence = set(transcript.evidence_byte_offsets)
    sessions = set(inst.answer_session_ids)

    one = measure(sorted(evidence)[:1], parsed, evidence, sessions)
    assert one.turn_recall == pytest.approx(1 / 3)
    assert one.turn_mrr == 1.0

    allof = measure(sorted(evidence), parsed, evidence, sessions)
    assert allof.turn_recall == 1.0


def test_hit_and_all_are_the_two_thresholds_recall_averages_over(tmp_path):
    """One of three is a hit and is not an `all`, and nothing is neither.

    `turn_recall` is a mean of fractions, which is not the metric anyone else
    publishes: a paper reporting Recall@10 on this benchmark almost always
    means "did any evidence turn come back". Both thresholds are printed beside
    the fraction so a reader comparing against another system compares the same
    quantity — and so the multi-hop gap between them is visible, which a single
    averaged number hides.
    """
    inst = make_instance(0, evidence_turns=3)
    transcript = synth.to_transcript(inst, seed=13, compaction=None)
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    evidence = set(transcript.evidence_byte_offsets)
    sessions = set(inst.answer_session_ids)

    one = measure(sorted(evidence)[:1], parsed, evidence, sessions)
    assert (one.turn_hit, one.turn_recall_all) == (1.0, 0.0)

    allof = measure(sorted(evidence), parsed, evidence, sessions)
    assert (allof.turn_hit, allof.turn_recall_all) == (1.0, 1.0)

    # An offset past the end of the transcript retrieves nothing at all.
    none = measure([10**9], parsed, evidence, sessions)
    assert (none.turn_hit, none.turn_recall_all) == (0.0, 0.0)

    # No evidence to find is not a perfect score. `turn_recall` already reads
    # 0.0 for an empty ground truth rather than dividing by zero, and `all`
    # has the same trap the other way round: `set() >= set()` is True.
    empty = measure(sorted(evidence), parsed, set(), sessions)
    assert (empty.turn_hit, empty.turn_recall_all, empty.turn_recall) == (0.0, 0.0, 0.0)


def test_the_session_thresholds_are_the_ones_other_systems_publish(tmp_path):
    """Session Hit@k and All@k, because the published numbers are session-level.

    A vendor reporting "at least one correct past session landed in the top K"
    is reporting `session_hit`; one reporting "the full set of correct sessions
    in the top K" is reporting `session_recall_all`. Neither is the fractional
    `session_recall`, so the two thresholds exist for the same reason their
    turn-level twins do: to stop a reader comparing a mean of fractions against
    someone else's thresholded rate.

    The empty-ground-truth trap is the one that matters here. `set() >= set()`
    is True, so an instance with no answer sessions would score a perfect
    `session_recall_all` for retrieving nothing, and a sweep over a corpus with
    any such instance would report a number inflated by them.
    """
    inst = make_instance(0, evidence_turns=3)
    transcript = synth.to_transcript(inst, seed=13, compaction=None)
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    evidence = set(transcript.evidence_byte_offsets)
    sessions = set(inst.answer_session_ids)

    allof = measure(sorted(evidence), parsed, evidence, sessions)
    assert (allof.session_hit, allof.session_recall_all) == (1.0, 1.0)

    none = measure([10**9], parsed, evidence, sessions)
    assert (none.session_hit, none.session_recall_all) == (0.0, 0.0)

    empty = measure(sorted(evidence), parsed, evidence, set())
    assert (empty.session_hit, empty.session_recall_all, empty.session_recall) == (0.0, 0.0, 0.0)


def test_an_unmatched_offset_consumes_its_rank_and_is_counted(tmp_path):
    """Past the end and in the gap between two turns: both are misses, both count."""
    inst = make_instance(0)
    transcript = synth.to_transcript(inst, seed=3, compaction=None)
    parsed = parse_bytes(transcript.bytes_data, tmp_path)
    evidence = set(transcript.evidence_byte_offsets)
    sessions = set(inst.answer_session_ids)
    turns = sorted(parsed.turns, key=lambda t: t.byte_offset)

    # The newline between two turns belongs to neither.
    gap = turns[0].byte_offset + turns[0].byte_len
    assert gap < turns[1].byte_offset

    m = measure([10**9, gap, min(evidence)], parsed, evidence, sessions)
    assert m.turn_recall == 1.0
    assert m.turn_mrr == pytest.approx(1 / 3)
    assert m.unmatched == 2


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def test_one_observation_can_never_be_significant():
    """The z-test returned p = 0.0 for a single paired difference.

    Zero variance was special-cased to `p = 0 if the means differ`, which is a
    fabricated certainty and was the only branch the old gate's one test ever
    exercised. Under the randomisation null a single difference is as likely to
    have come out negative: p = 0.5. [E3]
    """
    assert score.sign_flip_test([1.0], [0.0]) == (1.0, 0.5)


def test_a_constant_difference_gets_a_real_p_value_not_zero():
    mean, p = score.sign_flip_test([0.02] * 10, [0.0] * 10)
    assert mean == pytest.approx(0.02)
    assert p == pytest.approx(2.0**-10)
    assert p > 0.0


def test_no_difference_anywhere_is_p_one():
    assert score.sign_flip_test([0.5] * 5, [0.5] * 5) == (0.0, 1.0)


def test_the_test_is_one_sided():
    """`a` below `b` is not evidence that `a` is above it."""
    _, up = score.sign_flip_test([1.0] * 6, [0.0] * 6)
    _, down = score.sign_flip_test([0.0] * 6, [1.0] * 6)
    assert up < 0.05 < down
    assert down == pytest.approx(1.0)


def test_the_exact_and_normal_branches_agree(monkeypatch):
    """The cheap form above EXACT_MAX_N has to be the same test, not another one."""
    a = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    b = [0.0] * len(a)
    exact = score.sign_flip_test(a, b)[1]
    monkeypatch.setattr(score, "EXACT_MAX_N", 2)
    approx = score.sign_flip_test(a, b)[1]
    assert math.isclose(exact, approx, abs_tol=0.02)


def test_an_overwhelming_difference_still_reports_a_nonzero_p():
    """`1 - normal_cdf(z)` underflows around z = 38, and the 470-instance sweep gets there.

    Printing `p = 0.000e+00` is exactly what the fabricated-zero branch this
    test family replaced used to print, so a reader cannot tell the honest
    underflow from the bug. Floored at the smallest positive double instead. [E3]
    """
    a = [1.0] * 2000
    b = [0.0] * 2000
    _, p = score.sign_flip_test(a, b)
    assert p > 0.0
    assert p < 1e-300


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def dead_factory(session_id, transcript_bytes):
    return lambda query, k: []


def test_the_gate_fails_on_a_retriever_that_returns_nothing():
    """The headline defect this gate was rewritten for.

    The old gate asked one question — does `shuffled` beat `none` — and `none`
    is the constant-zero floor. A retriever returning `[]` for every query made
    all four arms-under-test score zero, the one comparison came out
    non-significant, and the gate reported success. `python -m bench` exited 0
    on a completely dead index. [E3]
    """
    report = score.score(corpus(), dead_factory, k=10, compaction_modes=[None])
    assert report["calibration_passed"] is False
    assert [c["passed"] for c in checks_named(report, "signal")] == [False]
    # And the apparatus check still holds, so the failure is attributed correctly.
    assert all(c["passed"] for c in checks_named(report, "apparatus"))


def test_the_gate_fails_on_a_retriever_that_ignores_the_query():
    """A leak is a control arm that scores as well as the candidate arm."""

    def leaky(instance, transcript_bytes):
        # Reads the evidence straight out of the bytes it was handed and returns
        # it whatever it is asked, so the control arm scores as well as the
        # candidate arm — which is what a leak looks like from outside.
        offsets = [offset for offset, line in _lines(transcript_bytes) if "sits in drawer" in line]
        return lambda query, k: offsets[:k]

    report = score.score(corpus(), leaky, k=10, compaction_modes=[None])
    assert report["calibration_passed"] is False
    assert [c["passed"] for c in checks_named(report, "no_leakage")] == [False]


def _lines(raw: bytes) -> list[tuple[int, str]]:
    out, offset = [], 0
    for line in raw.splitlines(keepends=True):
        out.append((offset, line.decode()))
        offset += len(line)
    return out


def test_the_gate_refuses_a_verdict_below_the_minimum_sample():
    """Not a pass, not a fail on the merits — a refusal, named as one."""
    report = score.score(corpus(3), dead_factory, k=10, compaction_modes=[None])
    assert report["calibration_passed"] is False
    assert [c["check"] for c in report["calibration_checks"]] == ["sample_size"]


def test_the_gate_fails_when_the_oracle_arm_is_not_exact(monkeypatch):
    """If the reference arm cannot score 1.0, nothing else in the run means anything."""
    real = score._measure

    def blunted(offsets, starts, turns, evidence, sessions):
        m = real(offsets, starts, turns, evidence, sessions)
        # Only the oracle passes exactly the ground truth, so this hits it alone.
        if list(offsets) == sorted(evidence)[: len(offsets)] and offsets:
            # `replace` rather than a positional rebuild: this blunts one field
            # and must keep carrying whatever else `ArmMetrics` grows.
            return dataclasses.replace(m, turn_recall=0.5)
        return m

    monkeypatch.setattr(score, "_measure", blunted)
    report = score.score(corpus(), dead_factory, k=10, compaction_modes=[None])
    assert [c["passed"] for c in checks_named(report, "apparatus")] == [False]


def test_the_alpha_is_bonferroni_corrected_across_the_modes_tested():
    one = score.score(corpus(), dead_factory, k=10, compaction_modes=[None])
    two = score.score(corpus(), dead_factory, k=10, compaction_modes=[None, "before_evidence"])
    assert checks_named(one, "signal")[0]["alpha"] == pytest.approx(score.ALPHA)
    assert checks_named(two, "signal")[0]["alpha"] == pytest.approx(score.ALPHA / 2)


def test_the_shuffled_arm_is_asked_another_instances_question():
    """Observed through `score`, not by re-implementing the derangement beside it.

    The test this replaces built its own copy of the pairing logic and asserted
    the copy had no fixed points, so making the control arm *be* the candidate
    arm — `shuffled_q = inst.question` — left it green. [E3]
    """
    seen: dict[str, list[str]] = {}

    def recording(session_id, transcript_bytes):
        def retrieve(query, k):
            seen.setdefault(session_id, []).append(query)
            return []

        return retrieve

    instances = corpus()
    score.score(instances, recording, k=10, compaction_modes=[None])
    questions = {i.question for i in instances}
    by_id = {i.question_id: i.question for i in instances}
    assert set(seen) == set(by_id)
    for qid, queries in seen.items():
        # candidate, shuffled, and live_context (no boundary, so it re-asks).
        assert by_id[qid] in queries
        others = [q for q in queries if q != by_id[qid]]
        assert len(others) == 1
        assert others[0] in questions


def test_an_arm_is_handed_a_session_id_and_bytes_and_nothing_else():
    """The cheat channel a wider signature left open.

    `Factory` used to take the whole `Instance`, and the only thing any arm
    wanted from it was `question_id` to capture under. The rest came along:
    `answer`, and `answer_session_ids`, which names the very session the metric
    scores you for ranking first. An arm could have returned that session's
    offsets without reading a word of the transcript and scored a perfect MRR.
    None did — this is about what the contract permits, not about a bug.

    So assert the contract itself rather than the absence of a symptom: two
    positional arguments, the id and the bytes, no `Instance` anywhere, and no
    keyword smuggling a third. The question and the answer are checked against
    both arguments as well, which is cheap and catches the obvious re-widening.
    What is *not* claimed: the transcript contains the evidence turn, so an arm
    is still handed the answer's content — it just is not told which session it
    is in, and finding out is the task. [E5]
    """
    seen: list[tuple[tuple, dict]] = []

    def recording(*args, **kwargs):
        seen.append((args, kwargs))
        return lambda query, k: []

    instances = corpus()
    score.score(instances, recording, k=10, compaction_modes=[None])

    assert len(seen) == len(instances), "one build per instance"
    ids = {i.question_id for i in instances}
    for args, kwargs in seen:
        assert not kwargs, f"a third channel opened as a keyword: {kwargs}"
        assert len(args) == 2, f"the contract is (session_id, transcript_bytes): {args}"
        session_id, raw = args
        assert isinstance(session_id, str) and session_id in ids, session_id
        assert isinstance(raw, bytes)
        assert not isinstance(session_id, lm.Instance)
    by_id = {i.question_id: i for i in instances}
    for (session_id, raw), _ in seen:
        inst = by_id[session_id]
        text = raw.decode("utf-8", "replace")
        assert inst.question not in text, "the question reached the index builder"
        assert inst.answer not in text, "the answer reached the index builder"
        assert inst.answer not in session_id


def test_abstention_instances_are_excluded():
    """`_abs` instances have no evidence turn; a retrieval metric has nothing to say."""
    instances = corpus()
    instances.append(make_instance(99, question_id="q_99_abs"))
    report = score.score(instances, dead_factory, k=10, compaction_modes=[None])
    assert report["num_instances_evaluated"] == N


def test_an_active_instance_without_evidence_is_refused_not_scored():
    """Silently scoring the oracle 0 on it would drag the apparatus check down."""
    instances = corpus()
    blank = make_instance(99)
    instances.append(
        lm.Instance(
            **{
                **blank.__dict__,
                "haystack_sessions": [
                    [lm.LongMemTurn(role="user", content="nothing here", has_answer=False)],
                    [],
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="has no evidence turn"):
        score.score(instances, dead_factory, k=10, compaction_modes=[None])


# --------------------------------------------------------------------------
# the product claim
# --------------------------------------------------------------------------


def test_the_live_context_arm_loses_what_fell_before_the_boundary():
    """The ablation the whole store exists to beat.

    Same retriever, same corpus, one window: everything from the last compaction
    boundary onward. Under `after_evidence` the evidence is behind it, so this
    arm scores zero and the candidate does not — which is the difference between
    measuring BM25 and measuring a memory system. [E3]
    """
    report = score.score(corpus(), gitmemory_factory, k=10, compaction_modes=[score.CLAIM_MODE])
    overall = report["overall_metrics"][score.CLAIM_MODE]
    assert overall["live_context"]["turn_recall"] == 0.0
    assert overall["candidate"]["turn_recall"] == 1.0
    assert [c["passed"] for c in checks_named(report, "claim")] == [True]


def test_the_gate_fails_when_the_candidate_finds_only_what_the_live_window_had():
    """The claim is a difference, so a zero difference has to be a failure.

    Measuring the ablation is not the same as gating on it: a mutant that made
    the `claim` check unconditionally true left the arm's own test green,
    because that test asserts the two arms differ on the real index and says
    nothing about what the gate does when they do not. This retriever returns
    only offsets the live window already keeps, so `candidate - live_context` is
    exactly zero and the product claim is unsupported. [E3]
    """

    def post_boundary(instance, transcript_bytes):
        lines = _lines(transcript_bytes)
        cutoff = max(off for off, line in lines if "summary of the previous conversation" in line)
        offsets = [off for off, _ in lines if off >= cutoff]
        return lambda query, k: offsets[:k]

    report = score.score(corpus(), post_boundary, k=10, compaction_modes=[score.CLAIM_MODE])
    overall = report["overall_metrics"][score.CLAIM_MODE]
    assert overall["candidate"] == overall["live_context"], "the fixture has to make them identical"
    assert [c["passed"] for c in checks_named(report, "claim")] == [False]


def test_the_gate_passes_on_the_real_index():
    """The whole apparatus, end to end, against the thing it exists to measure."""
    report = score.score(
        corpus(), gitmemory_factory, k=10, compaction_modes=[None, score.CLAIM_MODE]
    )
    assert report["calibration_passed"] is True, [
        c for c in report["calibration_checks"] if not c["passed"]
    ]
    for mode in (None, score.CLAIM_MODE):
        m = report["overall_metrics"][mode]
        assert m["candidate"]["turn_recall"] == 1.0
        assert m["candidate"]["turn_mrr"] == 1.0
        assert m["shuffled"]["turn_recall"] == 0.0
        assert m["reference"]["turn_recall"] == 1.0
        assert m["none"]["turn_recall"] == 0.0


# --------------------------------------------------------------------------
# the real corpus
# --------------------------------------------------------------------------


@pytest.mark.corpus
@pytest.mark.skipif("GITMEMORY_LONGMEMEVAL" not in os.environ, reason="run fetch_longmemeval.sh")
def test_real_corpus_loads_and_the_oracle_is_exact():
    """On the real haystack the only thing asserted is that the apparatus holds."""
    instances = [
        inst
        for inst in lm.load(os.environ["GITMEMORY_LONGMEMEVAL"])
        if not inst.question_id.endswith("_abs")
    ][: score.MIN_INSTANCES]
    assert len(instances) == score.MIN_INSTANCES
    report = score.score(instances, gitmemory_factory, k=10, compaction_modes=[None])
    assert report["overall_metrics"][None]["reference"]["turn_recall"] == 1.0
    assert all(c["passed"] for c in checks_named(report, "apparatus"))


def test_an_arm_whose_dependency_is_absent_is_skipped_with_a_reason():
    """Probing the factory symbol is not probing the dependency.

    `bench.arms` imports cleanly whether or not numpy is installed — the
    factories import it lazily inside themselves — so the old probe registered
    the arm and the sweep died on the first instance with
    `ImportError: Dense arm dependencies not installed`. Found by running the
    real corpus, which is the only place it could show up. [E3]

    The build below passes `inst.question_id` and not `inst`, and it used to
    pass `inst`. E5 narrowed the factory contract to take a session id rather
    than the whole `Instance` — the record carries `answer` and
    `answer_session_ids`, so handing it over is a channel through which an arm
    could rank the evidence without retrieving anything — and every caller was
    updated except this one, because this branch only executes where the
    `hybrid` extras are installed. On `rerank` it failed with `ValueError:
    unsafe session_id for a path component`, which is `store.capture` refusing
    to make a directory named after a dataclass: the right refusal, three layers
    from the mistake, and the sibling below pins that mechanism where no extras
    are needed to see it.

    **On `dense` it did not fail at all.** `dense_factory` never reads its first
    argument — it embeds the transcript and nothing else — so it accepted the
    whole `Instance`, including the answer, and returned a working retriever.
    Nothing read the answer and nothing was wrong with the result; the contract
    the narrowing exists to enforce was simply not enforced there, which is the
    worse half of this finding and is why the arm is not the place that checks
    it. A branch that runs in one environment is a branch that is tested in one
    environment. [E5 review round]
    """
    from bench.__main__ import _optional_arms

    arms, skipped = _optional_arms()
    for name in ("dense", "rerank"):
        registered = name in arms
        reported = any(line.startswith(f"{name}:") for line in skipped)
        assert registered != reported, f"{name} is neither runnable nor explained"
        if registered:
            # Registered means the dependency resolves, so building the arm
            # must not raise the "not installed" error the old probe missed.
            inst = make_instance(0)
            raw = synth.to_transcript(inst, seed=1, compaction=None).bytes_data
            built = arms[name](inst.question_id, raw)
            close = getattr(built, "close", None)
            if close:
                close()


def test_a_factory_takes_a_session_id_and_not_the_instance_it_came_from():
    """The narrowing the test above got wrong, pinned where it can be seen.

    `gitmemory_factory` needs no optional dependency, so this branch runs
    everywhere, and it is the same `store.capture` call `rerank_factory` reaches
    through its first stage. Passing the whole record is refused — and refused
    by the store rather than by the arm, which is the layer that can actually
    tell a session id from a dataclass.

    The refusal is a side effect of `capture` making a directory per session, so
    it is narrower than the rule it happens to enforce: `dense_factory` ignores
    the argument entirely and would take anything. Widening this into a real
    contract check on all three factories is a change to `bench/arms.py` and is
    not made here. [E5 review round]
    """
    inst = make_instance(0)
    raw = synth.to_transcript(inst, seed=1, compaction=None).bytes_data

    with pytest.raises(ValueError, match="unsafe session_id"):
        gitmemory_factory(inst, raw)

    built = gitmemory_factory(inst.question_id, raw)
    try:
        assert built("anything", 1) is not None
    finally:
        built.close()


def test_an_arm_whose_dependency_is_absent_names_the_module_it_is_missing(monkeypatch):
    """Both halves of that branch, on a machine that is in neither of them.

    The full mutation pass reported `missing = []` SURVIVED, and the reason is
    the environment rather than the assertion. The sibling above asks the
    machine what is installed, so on a machine with the hybrid extras present
    every arm registers, nothing is skipped, and `registered != reported` holds
    whether the probe ran or not; on a machine without them the same test
    catches the mutant easily. The worktree the mutation harness runs in has the
    extras and the development environment does not, so the recorded verdict for
    that row was a fact about a virtualenv.

    So stop asking. `find_spec` is replaced outright: one named module absent,
    every other name present, and the answer no longer depends on what `pip`
    did. A sentinel is enough because `_optional_arms` only `getattr`s the
    factory off an already-imported module — it does not import the dependency.
    [E5, full mutation pass]
    """
    from bench.__main__ import _optional_arms

    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a, **k: None if name == "flashrank" else object()
    )
    arms, skipped = _optional_arms()
    assert "rerank" not in arms, "an arm was registered without its dependency"
    assert "dense" in arms, "the absent module took an unrelated arm down with it"
    assert [line for line in skipped if line.startswith("rerank: no flashrank")], (
        f"the skip has to name the module and the extra that supplies it: {skipped}"
    )


def test_the_store_the_arm_builds_is_cleaned_up():
    """One temp store per instance per mode; `__del__` was not going to do it."""
    inst = make_instance(0)
    raw = synth.to_transcript(inst, seed=1, compaction=None).bytes_data
    retriever = gitmemory_factory(inst.question_id, raw)
    home = retriever.temp_home
    assert Path(home).is_dir()
    retriever.close()
    assert not Path(home).exists()


def test_a_sweep_leaves_no_temporary_store_behind():
    """One store and one open SQLite handle per instance per mode, all closed.

    A full sweep builds hundreds of them. Relying on `__del__` to clean up meant
    relying on refcounts nobody had checked, with a file-descriptor ceiling and
    a disk of copied transcripts as the failure. `score` closes them in a
    `finally`; this counts what is left in TMPDIR to prove it. [E3]

    TMPDIR is redirected at the test rather than read from the environment,
    because the assertion is a before/after diff of a directory the whole
    machine writes to: any other process that touched the system temp
    directory during the sweep failed this test, and one did — a second bench
    run in another shell. Observed twice, once here and once by a reviewer on a
    clean tree. [review: opus note]
    """
    tmp = Path(tempfile.mkdtemp(prefix="sweep-scope-"))
    before = set(os.listdir(tmp))
    tempfile.tempdir = str(tmp)  # the documented override; every arm reads it
    try:
        score.score(corpus(score.MIN_INSTANCES), gitmemory_factory, k=5, compaction_modes=[None])
    finally:
        tempfile.tempdir = None
    new = set(os.listdir(tmp)) - before
    assert not [n for n in new if n.endswith(".jsonl")], "transcript temp files"
    assert not [n for n in new if (tmp / n / "sessions" / "claude-code").is_dir()], "arm stores"
