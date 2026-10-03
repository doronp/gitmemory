# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""The QA harness without a network: prompts, parsing, context, cache. [E9]"""

from __future__ import annotations

import hashlib
import json

from bench import qa
from bench.longmemeval import Instance, LongMemTurn


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_prompts_are_upstreams_byte_for_byte():
    # Checked against the upstream files by diff when written (2026-09-27); these
    # pin that text so an edit, even to trailing whitespace, is caught.
    assert _sha(qa.LME_READER) == "e427ff913456e51a132ec865b1b5038d562bdc36890976943ad421cc9b365c9d"
    assert _sha(qa.LOCOMO_READER) == (
        "f3c786104816bba61d03b7f41c2be45009e6c8c296a04d9712ab1a59e4ae2399")
    assert _sha(qa.LOCOMO_JUDGE) == (
        "62395dd312a631dfd9355026a0b69cc936018274c3198b6365b5c2a5c9bca9e0")
    assert _sha("".join(qa.LME_JUDGE[k] for k in sorted(qa.LME_JUDGE))) == (
        "cdf80ad3e06d58fd3ecdb061c11273077fd794a69fd62ef6df653cacd5458b69")


def _inst(qid="q1", qtype="temporal-reasoning"):
    return Instance(qid, qtype, "Q?", "A", "2023/06/01 (Thu) 10:00",
                    ["2023/05/21 (Sun) 09:00", "2023/05/20 (Sat) 09:00"], ["late", "early"],
                    [[LongMemTurn("user", " hi ", False)], [LongMemTurn("assistant", "yo", False)]],
                    ["late"])


def test_abstention_is_chosen_by_question_id_not_type():
    assert "unanswerable" in qa.lme_judge_prompt(_inst("q1_abs"), "R")
    assert "off-by-one" in qa.lme_judge_prompt(_inst(), "R")


def test_verdict_parsing_follows_upstream():
    assert qa.lme_verdict("Yes.") and not qa.lme_verdict("No")
    assert qa.locomo_verdict('It matches.\n```json\n{"label": "CORRECT"}\n```')
    assert not qa.locomo_verdict('{"label": "WRONG"}')
    assert not qa.locomo_verdict("CORRECT")  # no JSON: mem0's json.loads would raise
    assert not qa.locomo_verdict("{not json}")


def test_lme_history_is_date_sorted_in_the_nl_format():
    h = qa.lme_history(_inst(), ["late", "early"])
    assert h == ("\n### Session 1:\nSession Date: 2023/05/20 (Sat) 09:00\nSession Content:\n"
                 "\n\nassistant: yo\n"
                 "\n### Session 2:\nSession Date: 2023/05/21 (Sun) 09:00\nSession Content:\n"
                 "\n\nuser: hi\n")


SAMPLE = {"sample_id": "c", "conversation": {
    "speaker_a": "Ann", "speaker_b": "Bo",
    "session_10": [{"speaker": "Bo", "dia_id": "D10:1", "text": "late"}],
    "session_10_date_time": "t10",
    "session_2": [{"speaker": "Ann", "dia_id": "D2:1", "text": "early"},
                  {"speaker": "Bo", "dia_id": "D2:2", "text": "skip"}],
    "session_2_date_time": "t2"}}


def test_locomo_memories_split_by_speaker_in_session_order():
    a, mem_a, b, mem_b = qa.locomo_memories(SAMPLE, ["D10:1", "D2:1"])
    assert (a, b) == ("Ann", "Bo")
    assert json.loads(mem_a) == ["t2: Ann: early"]
    assert json.loads(mem_b) == ["t10: Bo: late"]  # session 10 after 2, D2:2 not retrieved


def test_render_fills_every_slot():
    out = qa.render(qa.LOCOMO_READER, speaker_1_user_id="A", speaker_1_memories="[]",
                    speaker_2_user_id="B", speaker_2_memories="[]", question="Q?")
    assert "{{" not in out and "Question: Q?" in out


def test_cache_answers_a_repeat_without_calling(tmp_path):
    calls = []

    def client(prompt, **kw):
        calls.append(prompt)
        return "r"

    client.model = "m"
    path = str(tmp_path / "c.jsonl")
    assert qa.Cache(path).ask(client, "p") == "r"
    assert qa.Cache(path).ask(client, "p") == "r"  # reloaded from disk
    qa.Cache(path).ask(client, "p", json_mode=True)  # different call, different key
    assert calls == ["p", "p"]


def test_sessions_sharing_a_date_keep_rank_order():
    inst = _inst()
    inst.haystack_dates[1] = inst.haystack_dates[0]
    h = qa.lme_history(inst, ["late", "early"])
    assert h.index("user: hi") < h.index("assistant: yo")


def test_a_call_that_fails_every_retry_scores_wrong_and_is_counted():
    def boom(_):
        raise RuntimeError("m: 6 attempts failed")

    r = qa.scored_wrong_on_failure(boom, _inst("q2_abs"))
    assert r == {"id": "q2_abs", "type": "abstention", "correct": False,
                 "error": "m: 6 attempts failed"}
    r = qa.scored_wrong_on_failure(boom, (SAMPLE, None, 4, {"category": 3}))
    assert (r["id"], r["type"], r["correct"]) == ("c#4", "category 3", False)

def test_full_history_arm_passes_all_sids_without_retrieving(monkeypatch):
    inst = _inst()
    called = []
    monkeypatch.setattr(qa, "_retrieve", lambda *a: called.append(a))
    def dummy_client(prompt, **kw): return "Yes."
    dummy_client.model = "dummy"
    c = qa.Cache("/dev/null")
    ans = qa.lme_one(inst, "full_history", 1, dummy_client, dummy_client, c)
    assert ans["retrieved"] == ["late", "early"]
    assert not called
