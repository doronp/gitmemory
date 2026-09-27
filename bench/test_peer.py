# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""The peer-protocol scorer, against numbers worked by hand. [E9]"""

from __future__ import annotations

import math

from bench.longmemeval import Instance, LongMemTurn
from bench.peer import gold, metrics, ranked_sessions, split_of
from bench.synth import to_transcript


def _inst(qid="q1", answer_ids=("a1",), user_has_answer=True):
    turns = [LongMemTurn("user", "u", user_has_answer), LongMemTurn("assistant", "x", False)]
    return Instance(qid, "multi-session", "Q?", "A", "2023/05/20 (Sat) 10:00",
                    ["2023/05/20 (Sat) 09:00", "2023/05/21 (Sun) 09:00"],
                    ["a1", "s2"], [turns, [LongMemTurn("user", "n", False)]], list(answer_ids))


def test_recall_any_and_all_part_when_one_of_two_gold_sessions_is_found():
    assert metrics(["a", "x", "b"], ["a", "b"], 2, "official")[:2] == (1.0, 0.0)
    assert metrics(["a", "x", "b"], ["a", "b"], 3, "official")[:2] == (1.0, 1.0)


def test_official_ndcg_discounts_ranks_one_and_two_alike():
    # eval_utils.py: dcg = rel[0] + sum(rel[i] / log2(i + 1)), so rank 2 is not discounted.
    assert metrics(["x", "a"], ["a"], 2, "official")[2] == 1.0
    assert math.isclose(metrics(["x", "y", "a"], ["a"], 3, "official")[2], 1 / math.log2(3))


def test_official_ideal_counts_every_gold_session_not_just_the_retrieved():
    # One of two gold found at rank 1: dcg 1, ideal 1 + 1 = 2.
    assert metrics(["a", "x"], ["a", "b"], 2, "official")[2] == 0.5


def test_mempalace_ndcg_ideal_is_the_retrieved_list_resorted():
    # longmemeval_bench.py:61-68: one of two gold found, still perfect at rank 1...
    assert metrics(["a", "x"], ["a", "b"], 2, "mempalace")[2] == 1.0
    # ...and rank 2 is discounted by log2(3).
    assert math.isclose(metrics(["x", "a"], ["a"], 2, "mempalace")[2], 1 / math.log2(3))


def test_official_protocol_skips_abstention_and_answer_sessions_without_a_user_answer():
    assert gold(_inst(), "official") == ["a1"]
    assert gold(_inst(qid="q1_abs"), "official") is None
    assert gold(_inst(user_has_answer=False), "official") is None
    # MemPalace keeps both.
    assert gold(_inst(qid="q1_abs"), "mempalace") == ["a1"]
    assert gold(_inst(user_has_answer=False), "mempalace") == ["a1"]


def test_sessions_keep_first_occurrence_order_and_drop_unmatched_offsets():
    class T:
        def __init__(self, off, sid):
            self.byte_offset, self.byte_len, self.session_id = off, 10, sid

    turns = [T(0, "s1"), T(10, "s2"), T(20, "s1"), T(30, "s3")]
    starts = [t.byte_offset for t in turns]
    assert ranked_sessions([25, 12, 3, 99, 31], starts, turns) == ["s1", "s2", "s3"]


def test_split_is_fixed_and_roughly_halves():
    ids = [f"q{i}" for i in range(400)]
    assert [split_of(i) for i in ids] == [split_of(i) for i in ids]
    assert 150 < sum(split_of(i) == "dev" for i in ids) < 250


def test_plain_drops_tool_noise_and_leaves_the_rng_stream_alone():
    inst = _inst()
    for seed in range(30):
        plain = to_transcript(inst, seed=seed, compaction=None, plain=True).bytes_data
        noisy = to_transcript(inst, seed=seed, compaction=None).bytes_data
        assert b"tool_use" not in plain
        if b"tool_use" in noisy:
            break
    else:
        raise AssertionError("no seed produced tool noise; the test proves nothing")
    # Same rng stream: the plain transcript's lines are a subset of the noisy one's
    # except assistant turns, so the user turns match byte for byte.
    user = [ln for ln in plain.splitlines() if b'"type":"user"' in ln and b"tool_result" not in ln]
    assert user and all(ln in noisy.splitlines() for ln in user)
