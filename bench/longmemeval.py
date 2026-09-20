"""LongMemEval dataset loader and validator."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LongMemTurn:
    role: str
    content: str
    has_answer: bool


@dataclass(frozen=True)
class Instance:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    haystack_dates: list[str]
    haystack_session_ids: list[str]
    haystack_sessions: list[list[LongMemTurn]]
    answer_session_ids: list[str]


def load(path: str | Path) -> Iterator[Instance]:
    """Load and strictly validate a LongMemEval JSON dataset file.

    Raises:
        ValueError: If any instance is malformed, missing fields, or has incorrect types.
        FileNotFoundError: If the file does not exist.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("LongMemEval dataset must be a JSON list at the root.")

    # `question_id` is the key the harness pairs arms on: the shuffled control
    # maps it to another instance's question, and every per-instance score is
    # filed under it. A duplicate silently makes two instances one, so it is
    # rejected here rather than discovered as a wrong number downstream. [E3]
    seen: set[str] = set()

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Instance at index {idx} is not a dictionary.")

        # Required string fields
        for field_name in ("question_id", "question_type", "question", "question_date"):
            if field_name not in item:
                raise ValueError(f"Instance {idx} missing required field '{field_name}'.")
            if not isinstance(item[field_name], str):
                raise ValueError(f"Instance {idx} field '{field_name}' must be a string.")

        if item["question_id"] in seen:
            raise ValueError(f"Instance {idx} repeats question_id {item['question_id']!r}.")
        seen.add(item["question_id"])

        if "answer" not in item:
            raise ValueError(f"Instance {idx} missing required field 'answer'.")
        if not isinstance(item["answer"], (str, int)):
            raise ValueError(f"Instance {idx} field 'answer' must be a string or integer.")
        answer_str = str(item["answer"])

        # Required list of strings fields
        for field_name in ("haystack_dates", "haystack_session_ids", "answer_session_ids"):
            if field_name not in item:
                raise ValueError(f"Instance {idx} missing required field '{field_name}'.")
            val = item[field_name]
            if not isinstance(val, list):
                raise ValueError(f"Instance {idx} field '{field_name}' must be a list.")
            if not all(isinstance(x, str) for x in val):
                raise ValueError(f"Instance {idx} field '{field_name}' must contain only strings.")

        # Validate haystack_sessions
        if "haystack_sessions" not in item:
            raise ValueError(f"Instance {idx} missing required field 'haystack_sessions'.")
        sessions = item["haystack_sessions"]
        if not isinstance(sessions, list):
            raise ValueError(f"Instance {idx} field 'haystack_sessions' must be a list.")

        validated_sessions: list[list[LongMemTurn]] = []
        for s_idx, session in enumerate(sessions):
            if not isinstance(session, list):
                raise ValueError(
                    f"Instance {idx}, session {s_idx} in 'haystack_sessions' must be a list."
                )
            validated_turns: list[LongMemTurn] = []
            for t_idx, turn_dict in enumerate(session):
                if not isinstance(turn_dict, dict):
                    raise ValueError(
                        f"Instance {idx}, session {s_idx}, turn {t_idx} must be a dictionary."
                    )
                if "role" not in turn_dict or "content" not in turn_dict:
                    raise ValueError(
                        f"Instance {idx}, session {s_idx}, turn {t_idx} "
                        "missing 'role' or 'content'."
                    )
                role = turn_dict["role"]
                content = turn_dict["content"]
                if not isinstance(role, str) or not isinstance(content, str):
                    raise ValueError(
                        f"Instance {idx}, session {s_idx}, turn {t_idx} "
                        "'role' and 'content' must be strings."
                    )
                has_answer = turn_dict.get("has_answer", False)
                if not isinstance(has_answer, bool):
                    raise ValueError(
                        f"Instance {idx}, session {s_idx}, turn {t_idx} "
                        "'has_answer' must be a boolean."
                    )
                validated_turns.append(
                    LongMemTurn(role=role, content=content, has_answer=has_answer)
                )
            validated_sessions.append(validated_turns)

        yield Instance(
            question_id=item["question_id"],
            question_type=item["question_type"],
            question=item["question"],
            answer=answer_str,
            question_date=item["question_date"],
            haystack_dates=item["haystack_dates"],
            haystack_session_ids=item["haystack_session_ids"],
            haystack_sessions=validated_sessions,
            answer_session_ids=item["answer_session_ids"],
        )
