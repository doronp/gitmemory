# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""The LoCoMo harness on a synthetic sample shaped like the pinned release. [E9]"""

from __future__ import annotations

import json

from bench.locomo import (
    in_split,
    load,
    load_mempalace,
    measure,
    parse_evidence,
    ranked_turns,
    sessions,
    to_conversation,
)

SAMPLE = {
    "sample_id": "conv-x",
    "conversation": {
        "speaker_a": "Ann",
        "speaker_b": "Bo",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {"speaker": "Ann", "dia_id": "D1:1", "text": "hi"},
            {"speaker": "Bo", "dia_id": "D1:2", "text": "look", "blip_caption": "a dog"},
        ],
        "session_2_date_time": "2:00 pm on 9 May, 2023",
        "session_2": [{"speaker": "Ann", "dia_id": "D2:1", "text": "bye"}],
    },
    "qa": [
        {"question": "q1", "answer": "a", "category": 1, "evidence": ["D1:2; D2:1"]},
        {"question": "q2", "answer": 3, "category": 2, "evidence": ["D:2:01"]},
        {"question": "q3", "category": 5, "adversarial_answer": "n", "evidence": ["D1:1"]},
        {"question": "q4", "answer": "a", "category": 4, "evidence": ["D9:9"]},
    ],
}


def test_malformed_evidence_ids_are_repaired():
    assert parse_evidence(["D8:6; D9:17", "D9:1 D4:4", "D:11:26", "D30:05"]) == {
        "D8:6", "D9:17", "D9:1", "D4:4", "D11:26", "D30:5"}


def test_transcript_is_one_line_per_turn_with_roles_and_caption():
    conv = to_conversation(SAMPLE)
    lines = [json.loads(ln) for ln in conv.transcript.splitlines()]
    assert conv.dia_ids == ["D1:1", "D1:2", "D2:1"]
    assert [ln["type"] for ln in lines] == ["user", "assistant", "user"]
    assert lines[1]["message"]["content"][0]["text"] == "Bo: look [shares an image: a dog]"
    assert lines[0]["timestamp"] == "2023-05-08T13:56:00Z"
    assert lines[2]["sessionId"] == "conv-x-s2"


def test_load_drops_category_five_and_unresolvable_evidence(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps([SAMPLE]))
    _, qs, dropped = load(str(p))
    assert [q.question for q in qs] == ["q1", "q2"]
    assert qs[1].evidence == frozenset({"D2:1"})
    assert dropped == {"category 5 (adversarial)": 1, "evidence ids naming no turn": 1,
                       "questions with no resolvable evidence": 1}


def test_mempalace_protocol_keeps_everything_and_reads_only_a_leading_id(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps([SAMPLE]))
    qs = load_mempalace(str(p))
    # locomo_bench.py:944-948: re.match on each raw string — first id only, `D:2:01` lost.
    assert [q.evidence for q in qs] == [
        frozenset({"D1"}), frozenset(), frozenset({"D1"}), frozenset({"D9"})]


def test_measure_and_the_empty_gold_convention():
    assert measure(["a", "b", "c"], frozenset({"a", "c"}), 2) == (0.5, 1.0, 0.0)
    assert measure(["a"], frozenset(), 1) == (1.0, 1.0, 1.0)


def test_offsets_map_to_distinct_turns_then_sessions_in_rank_order():
    conv = to_conversation(SAMPLE)
    s = conv.line_starts
    ranked = ranked_turns(conv, [s[2] + 3, s[0], s[2], s[1] + 1, len(conv.transcript) + 5])
    assert ranked == ["D2:1", "D1:1", "D1:2"]
    assert sessions(ranked) == ["D2", "D1"]


def test_split_by_conversation_is_a_partition():
    ids = [f"conv-{i}" for i in range(50)]
    dev = {i for i in ids if in_split(i, "dev")}
    test = {i for i in ids if in_split(i, "test")}
    assert dev and test and not dev & test and dev | test == set(ids)
    assert all(in_split(i, "all") for i in ids)
