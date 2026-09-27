"""Synthetic transcript generator for LongMemEval instances."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any

from .longmemeval import Instance

DEFAULT_CWD = "/workspace/gitmemory"


@dataclass(frozen=True)
class Transcript:
    bytes_data: bytes
    evidence_byte_offsets: list[int]
    session_ids: list[str]


def deterministic_uuid(content: str) -> str:
    """Generate a deterministic UUID string based on the given content string."""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def to_transcript(
    instance: Instance, *, seed: int, compaction: str | None, plain: bool = False
) -> Transcript:
    """Replay one LongMemEval instance as a Claude-Code-shaped JSONL transcript.

    Timestamps come from haystack_dates and question_date.
    compaction controls where compaction boundaries are injected:
      - None: no compaction
      - "before_evidence": a boundary right before the first evidence turn
      - "after_evidence": a boundary right after the first evidence turn
      - "every_n" or "every_<N>": boundary injected every N turns (default N=10)

    Returns ground truth as byte offsets into the transcript.
    """
    rng = random.Random(seed)

    # First and *last* evidence turn, not just the first. `after_evidence` has
    # to put its boundary past all of the evidence or an instance with three
    # evidence turns keeps two of them inside the live window, which quietly
    # turns the compaction arm into a partial-credit measurement of nothing in
    # particular. `before_evidence` wants the first for the mirrored reason. [E3]
    coords = [
        (s_idx, t_idx)
        for s_idx, session in enumerate(instance.haystack_sessions)
        for t_idx, turn in enumerate(session)
        if turn.has_answer
    ]
    first_evidence = coords[0] if coords else None
    last_evidence = coords[-1] if coords else None

    # Determine compaction interval if every_n is requested
    every_n: int | None = None
    if compaction == "every_n":
        every_n = 10
    elif isinstance(compaction, str) and compaction.startswith("every_"):
        try:
            every_n = int(compaction.split("_")[1])
        except ValueError:
            every_n = 10

    accumulated_bytes = bytearray()
    evidence_byte_offsets: list[int] = []

    session_ids = list(instance.haystack_session_ids)
    total_turns_written = 0
    next_compaction_turn = every_n if every_n is not None else -1

    def write_line(obj: dict[str, Any]) -> int:
        nonlocal accumulated_bytes
        offset = len(accumulated_bytes)
        # Ensure canonical dump with LF
        line_str = json.dumps(obj, separators=(",", ":")) + "\n"
        accumulated_bytes.extend(line_str.encode("utf-8"))
        return offset

    def inject_compaction(sid: str, last_uuid: str | None) -> None:
        boundary_suffix = f"_compact_boundary_{len(accumulated_bytes)}"
        boundary_uuid = deterministic_uuid(f"{instance.question_id}{boundary_suffix}")
        boundary_line = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": boundary_uuid,
            "sessionId": sid,
            "parentUuid": None,
            "compactMetadata": {"trigger": "manual"},
            "timestamp": None,
        }
        write_line(boundary_line)

        summary_suffix = f"_compact_summary_{len(accumulated_bytes)}"
        summary_uuid = deterministic_uuid(f"{instance.question_id}{summary_suffix}")
        summary_line = {
            "type": "user",
            "uuid": summary_uuid,
            "sessionId": sid,
            "isCompactSummary": True,
            "parentUuid": None,
            "logicalParentUuid": last_uuid,
            "timestamp": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "This is a summary of the previous conversation."}
                ],
            },
        }
        write_line(summary_line)

    for s_idx, session in enumerate(instance.haystack_sessions):
        sid = instance.haystack_session_ids[s_idx]
        s_date = instance.haystack_dates[s_idx]
        last_uuid: str | None = None

        for t_idx, turn in enumerate(session):
            is_evidence = turn.has_answer

            # Check for "before_evidence" compaction
            if compaction == "before_evidence" and first_evidence == (s_idx, t_idx):
                inject_compaction(sid, last_uuid)

            # Check for "every_n" compaction
            if every_n is not None and total_turns_written >= next_compaction_turn:
                inject_compaction(sid, last_uuid)
                while next_compaction_turn <= total_turns_written:
                    next_compaction_turn += every_n

            # Base properties for the replayed turn
            turn_uuid = deterministic_uuid(f"{instance.question_id}_{s_idx}_{t_idx}_{turn.role}")
            role = turn.role

            # Construct the line object
            line_obj: dict[str, Any] = {
                "type": role,
                "uuid": turn_uuid,
                "sessionId": sid,
                "timestamp": s_date,
                "parentUuid": last_uuid,
                "cwd": DEFAULT_CWD,
                "version": "0.2.29",
            }

            # Assistant turns should have usage and model info, and sometimes tool usage
            if role == "assistant":
                usage = {
                    "input_tokens": rng.randint(50, 1000),
                    "output_tokens": rng.randint(20, 500),
                }
                model = rng.choice(["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"])

                # With 20% probability, let's inject a tool_use in the assistant turn
                # [E9] drawn even when `plain`, so the rng stream, and every
                # non-plain transcript, stays byte-identical.
                has_tool = rng.random() < 0.2
                if has_tool and not plain:
                    tool_use_id = f"tu_{s_idx}_{t_idx}"
                    content_blocks = [
                        {"type": "text", "text": turn.content},
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "bash",
                            "input": {"command": "git status"},
                        },
                    ]
                    line_obj["message"] = {
                        "role": "assistant",
                        "content": content_blocks,
                        "usage": usage,
                        "model": model,
                    }

                    # Write the assistant turn
                    offset = write_line(line_obj)
                    if is_evidence:
                        evidence_byte_offsets.append(offset)

                    last_uuid = turn_uuid
                    total_turns_written += 1

                    # Now write the corresponding tool result
                    tool_result_key = f"{instance.question_id}_{s_idx}_{t_idx}_tool_result"
                    tool_result_uuid = deterministic_uuid(tool_result_key)
                    tool_result_line = {
                        "type": "user",
                        "uuid": tool_result_uuid,
                        "sessionId": sid,
                        "timestamp": s_date,
                        "parentUuid": last_uuid,
                        "cwd": DEFAULT_CWD,
                        "version": "0.2.29",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": "On branch main\nYour branch is up to date.",
                                }
                            ],
                        },
                    }
                    write_line(tool_result_line)
                    last_uuid = tool_result_uuid
                    total_turns_written += 1

                else:
                    # Pure assistant turn
                    line_obj["message"] = {
                        "role": "assistant",
                        "content": [{"type": "text", "text": turn.content}],
                        "usage": usage,
                        "model": model,
                    }
                    offset = write_line(line_obj)
                    if is_evidence:
                        evidence_byte_offsets.append(offset)
                    last_uuid = turn_uuid
                    total_turns_written += 1
            else:
                # Claude Code writes a user message's content as a bare string
                # *or* as a list of blocks depending on how it was composed, and
                # the adapter has to read both. Alternating on the turn index
                # exercises both shapes in every transcript instead of pinning
                # the benchmark to whichever one happened to be written. [E3]
                content: Any = turn.content
                if t_idx % 2 == 1:
                    content = [{"type": "text", "text": turn.content}]
                line_obj["message"] = {
                    "role": "user" if role == "user" else "system",
                    "content": content,
                }
                offset = write_line(line_obj)
                if is_evidence:
                    evidence_byte_offsets.append(offset)
                last_uuid = turn_uuid
                total_turns_written += 1

            # Check for "after_evidence" compaction
            if compaction == "after_evidence" and last_evidence == (s_idx, t_idx):
                inject_compaction(sid, last_uuid)

    # The question is deliberately *not* written into the transcript. It used to
    # be appended as a final user turn, and since it shares most of its words
    # with the evidence turn it answers, BM25 put it at rank 1 for every query —
    # capping the candidate arm's MRR at 0.5 while the oracle arm, which never
    # goes through retrieval, sat at 1.0. The handicap fell on exactly one arm.
    # A LongMemEval question is the query, not haystack content; it belongs in
    # the call to `retrieve`, which is where it now is. [E3]

    return Transcript(
        bytes_data=bytes(accumulated_bytes),
        evidence_byte_offsets=evidence_byte_offsets,
        session_ids=session_ids,
    )
