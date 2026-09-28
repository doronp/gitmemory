# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""End-to-end QA accuracy: retrieve, an LLM reads, an LLM judges. [E9]

The LLM lives only here, in the benchmark harness; `src/gitmemory/` stays
deterministic. Retrieval is the same code `peer.py` and `locomo.py` score.
Prompts are copied from the code that produced the numbers we compare against:

- lme: reader = xiaowu0162/LongMemEval src/generation/run_generation.py:57 (no
  CoT, `nl` history format, lines 239-252, sessions sorted by date, line 225);
  judge = src/evaluation/evaluate_qa.py:24-60, parsed `'yes' in response.lower()`
  (line 113), abstention when `'_abs' in question_id` (line 101).
- locomo: reader = mem0ai/mem0 evaluation/prompts.py:65-108 `ANSWER_PROMPT`,
  memories rendered as in evaluation/src/memzero/search.py:98-106 (one list per
  speaker, `"{timestamp}: {memory}"`, json.dumps indent 4); judge =
  evaluation/metrics/llm_judge.py:12-35 `ACCURACY_PROMPT`, JSON mode, label ==
  "CORRECT" (lines 38-52). Both at b3ede5b, the parent of the commit that
  retired mem0's in-repo evaluation/.

Reader and judge default to Gemini, not the gpt-4o / gpt-4o-mini judges the
leaderboards use, so the accuracy is not directly comparable with theirs.

    python -m bench.qa --bench lme --dataset "$GITMEMORY_LONGMEMEVAL" --split dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from gitmemory.adapters import claude_code as cc

from . import arms
from .locomo import _turn_text, in_split, ranked_turns, sessions, to_conversation
from .longmemeval import Instance
from .longmemeval import load as load_lme
from .peer import ARMS, ranked_sessions, split_of
from .synth import to_transcript

DEPTH = 200  # turns retrieved before dedupe, as in peer.py
NOT_COMPARABLE = ("Judge is not the leaderboard's (gpt-4o for LongMemEval, gpt-4o-mini for "
                  "Mem0's LoCoMo J); not directly comparable.")

LME_READER = (
    "I will give you several history chats between you and a user. Please answer the question "
    "based on the relevant chat history.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\n"
    "Question: {}\nAnswer:"
)
# run_generation.py:55, the same template with the official chain-of-thought instruction.
LME_READER_COT = (
    "I will give you several history chats between you and a user. Please answer the question "
    "based on the relevant chat history. Answer the question step by step: first extract all "
    "the relevant information, and then reason over the information to get the answer.\n\n\n"
    "History Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
)

_LME_BASE = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
    "equivalent to the correct answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a subset of the "
    "information required by the answer, answer no. "
)
_LME_TAIL = ("\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
             "Is the model response correct? Answer yes or no only.")
LME_JUDGE = {
    "single-session-user": _LME_BASE + _LME_TAIL,
    "single-session-assistant": _LME_BASE + _LME_TAIL,
    "multi-session": _LME_BASE + _LME_TAIL,
    "temporal-reasoning": _LME_BASE + (
        "In addition, do not penalize off-by-one errors for the number of days. If the question "
        "asks for the number of days/weeks/months, etc., and the model makes off-by-one errors "
        "(e.g., predicting 19 days when the answer is 18), the model's response is still "
        "correct. ") + _LME_TAIL,
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. Please "
        "answer yes if the response contains the correct answer. Otherwise, answer no. If the "
        "response contains some previous information along with an updated answer, the "
        "response should be considered as correct as long as the updated answer is the "
        "required answer.") + _LME_TAIL,
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and a response "
        "from a model. Please answer yes if the response satisfies the desired response. "
        "Otherwise, answer no. The model does not need to reflect all the points in the rubric. "
        "The response is correct as long as it recalls and utilizes the user's personal "
        "information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\n"
        "Is the model response correct? Answer yes or no only."),
    "abstention": (
        "I will give you an unanswerable question, an explanation, and a response from a model. "
        "Please answer yes if the model correctly identifies the question as unanswerable. The "
        "model could say that the information is incomplete, or some other information is "
        "given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\n"
        "Model Response: {}\n\nDoes the model correctly identify the question as unanswerable? "
        "Answer yes or no only."),
}

# Byte for byte, trailing spaces and typographic quotes included; test_qa.py pins it.
LOCOMO_READER = """
    You are an intelligent memory assistant tasked with retrieving accurate information from conversation memories.

    # CONTEXT:
    You have access to memories from two speakers in a conversation. These memories contain 
    timestamped information that may be relevant to answering the question.

    # INSTRUCTIONS:
    1. Carefully analyze all provided memories from both speakers
    2. Pay special attention to the timestamps to determine the answer
    3. If the question asks about a specific event or fact, look for direct evidence in the memories
    4. If the memories contain contradictory information, prioritize the most recent memory
    5. If there is a question about time references (like "last year", "two months ago", etc.), 
       calculate the actual date based on the memory timestamp. For example, if a memory from 
       4 May 2022 mentions "went to India last year," then the trip occurred in 2021.
    6. Always convert relative time references to specific dates, months, or years. For example, 
       convert "last year" to "2022" or "two months ago" to "March 2023" based on the memory 
       timestamp. Ignore the reference while answering the question.
    7. Focus only on the content of the memories from both speakers. Do not confuse character 
       names mentioned in memories with the actual users who created those memories.
    8. The answer should be less than 5-6 words.

    # APPROACH (Think step by step):
    1. First, examine all memories that contain information related to the question
    2. Examine the timestamps and content of these memories carefully
    3. Look for explicit mentions of dates, times, locations, or events that answer the question
    4. If the answer requires calculation (e.g., converting relative time references), show your work
    5. Formulate a precise, concise answer based solely on the evidence in the memories
    6. Double-check that your answer directly addresses the question asked
    7. Ensure your final answer is specific and avoids vague time references

    Memories for user {{speaker_1_user_id}}:

    {{speaker_1_memories}}

    Memories for user {{speaker_2_user_id}}:

    {{speaker_2_memories}}

    Question: {{question}}

    Answer:
    """  # noqa: E501

LOCOMO_JUDGE = """
Your task is to label an answer to a question as ’CORRECT’ or ’WRONG’. You will be given the following data:
    (1) a question (posed by one user to another user), 
    (2) a ’gold’ (ground truth) answer, 
    (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT. 

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG. 
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label".
"""  # noqa: E501


def lme_judge_prompt(inst: Instance, response: str) -> str:
    kind = "abstention" if "_abs" in inst.question_id else inst.question_type
    return LME_JUDGE[kind].format(inst.question, inst.answer, response)


def lme_verdict(text: str) -> bool:
    return "yes" in text.lower()


def locomo_verdict(text: str) -> bool:
    """mem0's `json.loads(extract_json(c))["label"] == "CORRECT"`; unparseable is wrong."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0))["label"] == "CORRECT" if m else False
    except (ValueError, KeyError, TypeError):
        return False


def lme_history(inst: Instance, sids: list[str]) -> str:
    """run_generation.py:225-252: sessions sorted by date, `nl` format."""
    idx = {sid: i for i, sid in enumerate(inst.haystack_session_ids)}
    chunks = sorted(((inst.haystack_dates[idx[s]], inst.haystack_sessions[idx[s]]) for s in sids),
                    key=lambda c: c[0])  # date only, as upstream; ties keep rank order
    out = ""
    for i, (date, turns) in enumerate(chunks):
        body = "".join(f"\n\n{t.role}: {t.content.strip()}" for t in turns)
        out += f"\n### Session {i + 1}:\nSession Date: {date}\nSession Content:\n{body}\n"
    return out


def locomo_memories(sample: dict, dia_ids: list[str]) -> tuple[str, str, str, str]:
    """(speaker_a, speaker_a memories, speaker_b, speaker_b memories), chronological."""
    conv = sample["conversation"]
    lists: dict[str, list[str]] = {conv["speaker_a"]: [], conv["speaker_b"]: []}
    want = set(dia_ids)
    for key in sorted((k for k in conv if re.fullmatch(r"session_\d+", k)),
                      key=lambda k: int(k.split("_")[1])):
        for turn in conv[key]:
            if turn["dia_id"] in want:
                lists[turn["speaker"]].append(f"{conv[f'{key}_date_time']}: {_turn_text(turn)}")
    a, b = conv["speaker_a"], conv["speaker_b"]
    return a, json.dumps(lists[a], indent=4), b, json.dumps(lists[b], indent=4)


def render(template: str, **kw: str) -> str:
    """The `{{name}}` subset of jinja2's Template.render that ANSWER_PROMPT uses."""
    for k, v in kw.items():
        template = template.replace("{{" + k + "}}", v)
    return template


class Vertex:
    """generateContent over stdlib urllib; credentials from gcloud, refreshed on 401."""

    def __init__(self, model: str):
        self.model, self.project, self.token = model, "", ""

    def _auth(self) -> None:
        run = lambda *a: subprocess.check_output(["gcloud", *a], text=True).strip()  # noqa: E731
        self.project = self.project or run("config", "get-value", "project")
        self.token = run("auth", "application-default", "print-access-token")

    def __call__(self, prompt: str, json_mode: bool = False) -> str:
        if not self.token:
            self._auth()
        cfg: dict = {"temperature": 0}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": cfg}).encode()
        url = (f"https://aiplatform.googleapis.com/v1/projects/{self.project}/locations/global/"
               f"publishers/google/models/{self.model}:generateContent")
        for attempt in range(6):
            req = urllib.request.Request(url, body, {"Authorization": f"Bearer {self.token}",
                                                     "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    cand = (json.load(r).get("candidates") or [{}])[0]
                return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])
                               if not p.get("thought")).strip()
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._auth()
                elif e.code not in (429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(2 ** attempt)
        raise RuntimeError(f"{self.model}: 6 attempts failed")


class Cache:
    """JSONL keyed by sha256(model, prompt), so a rerun or a resume costs nothing."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path, self.lock, self.data = path, threading.Lock(), {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a line torn by an interrupted write
                    self.data[rec["key"]] = rec["response"]

    def ask(self, client, prompt: str, **kw) -> str:
        key = hashlib.sha256(json.dumps([client.model, prompt, kw]).encode()).hexdigest()
        if key not in self.data:
            resp = client(prompt, **kw)
            with self.lock:
                self.data[key] = resp
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "response": resp}) + "\n")
        return self.data[key]


# One retrieval at a time: ONNX already uses every core per call, and eight
# concurrent depth-200 cross-encoder passes grew to 65 GB and were killed by the
# kernel. The model calls, which only wait on the network, stay parallel. [E9]
_RETRIEVE = threading.Lock()


def _retrieve(arm: str, key: str, data: bytes, query: str) -> list[int]:
    with _RETRIEVE:
        factory = getattr(arms, f"{'gitmemory' if arm == 'candidate' else arm}_factory")
        retriever = factory(key, data)
        try:
            return retriever(query, DEPTH)
        finally:
            if hasattr(retriever, "close"):
                retriever.close()


def lme_items(path: str, split: str):
    for inst in load_lme(path):
        if split == "all" or split_of(inst.question_id) == split:
            yield inst


def lme_one(inst: Instance, arm: str, k: int, reader, judge, cache: Cache,
            cot: bool = False) -> dict:
    tx = to_transcript(inst, seed=42, compaction=None, plain=True)
    with tempfile.NamedTemporaryFile(suffix=".jsonl") as tmp:
        tmp.write(tx.bytes_data)
        tmp.flush()
        turns = sorted(cc.parse(tmp.name).turns, key=lambda t: t.byte_offset)
    offsets = _retrieve(arm, inst.question_id, tx.bytes_data, inst.question)
    sids = ranked_sessions(offsets, [t.byte_offset for t in turns], turns)[:k]
    template = LME_READER_COT if cot else LME_READER
    answer = cache.ask(reader, template.format(lme_history(inst, sids), inst.question_date,
                                               inst.question))
    ok = lme_verdict(cache.ask(judge, lme_judge_prompt(inst, answer)))
    kind = "abstention" if "_abs" in inst.question_id else inst.question_type
    return {"id": inst.question_id, "type": kind, "retrieved": sids, "answer": answer,
            "correct": ok}


def locomo_items(path: str, split: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for sample in data:
        if not in_split(sample["sample_id"], split):
            continue
        conv = to_conversation(sample)
        for i, qa in enumerate(sample["qa"]):
            if qa["category"] in (1, 2, 3, 4):  # 5 is adversarial, excluded by mem0 too
                yield sample, conv, i, qa


def locomo_one(item, arm: str, k: int, unit: str, reader, judge, cache: Cache) -> dict:
    sample, conv, i, qa = item
    ranked = ranked_turns(conv, _retrieve(arm, sample["sample_id"], conv.transcript,
                                          qa["question"]))
    if unit == "session":
        keep = set(sessions(ranked)[:k])
        ids = [d for d in conv.dia_ids if d.split(":")[0] in keep]
    else:
        ids = ranked[:k]
    a, mem_a, b, mem_b = locomo_memories(sample, ids)
    answer = cache.ask(reader, render(LOCOMO_READER, speaker_1_user_id=a, speaker_1_memories=mem_a,
                                      speaker_2_user_id=b, speaker_2_memories=mem_b,
                                      question=qa["question"]))
    verdict = cache.ask(judge, LOCOMO_JUDGE.format(question=qa["question"],
                                                   gold_answer=str(qa["answer"]),
                                                   generated_answer=answer), json_mode=True)
    return {"id": f"{sample['sample_id']}#{i}", "type": f"category {qa['category']}",
            "retrieved": ids, "answer": answer, "correct": locomo_verdict(verdict)}


def scored_wrong_on_failure(one, item) -> dict:
    """A call that fails every retry scores the question wrong instead of ending
    the run: a resume would hang on the same item, and wrong is the conservative
    reading. The count is printed beside the score. [E9]"""
    try:
        return one(item)
    except RuntimeError as e:
        if isinstance(item, Instance):
            qid = item.question_id
            kind = "abstention" if "_abs" in qid else item.question_type
        else:
            qid, kind = f"{item[0]['sample_id']}#{item[2]}", f"category {item[3]['category']}"
        return {"id": qid, "type": kind, "correct": False, "error": str(e)}


def summarise(records: list[dict]) -> dict:
    by: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        by[r["type"]].append(r["correct"])
    n = len(records)
    return {"n": n, "accuracy": sum(r["correct"] for r in records) / n if n else 0.0,
            "by_type": {t: (sum(v) / len(v), len(v)) for t, v in sorted(by.items())}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.qa")
    ap.add_argument("--bench", choices=("lme", "locomo"), required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--arm", default="rerank12", choices=ARMS)
    ap.add_argument("--unit", choices=("session", "turn"), default="session",
                    help="locomo only; lme is always sessions, as in the official runs")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cot", action="store_true", help="lme: the official step-by-step reader")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--reader", default="gemini-3.1-pro-preview")
    ap.add_argument("--judge", default="gemini-3.1-pro-preview")
    # Not the shared temp dir: a cache there can be pre-seeded by another user. [SEC-3]
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    ap.add_argument("--cache", default=os.path.join(cache_home, "gitmemory-qa", "cache.jsonl"))
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    reader, judge, cache = Vertex(args.reader), Vertex(args.judge), Cache(args.cache)
    if args.bench == "lme":
        items = list(lme_items(args.dataset, args.split))[:args.limit]
        one = lambda x: lme_one(x, args.arm, args.k, reader, judge, cache, args.cot)  # noqa: E731
    else:
        items = list(locomo_items(args.dataset, args.split))[:args.limit]
        one = lambda x: locomo_one(x, args.arm, args.k, args.unit, reader, judge, cache)  # noqa: E731
    with ThreadPoolExecutor(args.workers) as pool:
        records = list(pool.map(lambda x: scored_wrong_on_failure(one, x), items))
    res = summarise(records)
    failed = sum("error" in r for r in records)
    unit = "session" if args.bench == "lme" else args.unit
    print(f"# {args.bench} QA — {args.split}, n = {res['n']}, arm {args.arm}, top {args.k} {unit}s"
          + (", CoT reader" if args.cot else ""))
    print()
    print("| Type | Accuracy | n |")
    print("|---|---|---|")
    print(f"| **overall** | **{res['accuracy']:.4f}** | {res['n']} |")
    for t, (acc, n) in res["by_type"].items():
        print(f"| {t} | {acc:.4f} | {n} |")
    print()
    print(f"Reader {args.reader}, judge {args.judge}. {NOT_COMPARABLE}")
    if failed:
        print(f"{failed} question(s) scored wrong because the model call failed every retry.")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"args": vars(args), **res, "records": records}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
