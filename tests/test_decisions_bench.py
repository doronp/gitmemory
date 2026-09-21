"""Tests for bench/decisions.py."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import tempfile

import pytest

from bench.decisions import (
    DEV_DIRECTIVES_TEMPLATES,
    DEV_REVERSALS_TEMPLATES,
    DEV_VARS,
    TEST_DIRECTIVES_TEMPLATES,
    TEST_REVERSALS_TEMPLATES,
    TEST_VARS,
    Case,
    Decision,
    baseline_leak,
    baseline_naive,
    baseline_nothing,
    generate,
    resolve_gold_for_case,
    run_gate,
    score_predictions,
)
from gitmemory import adapters

# B3: Committed checksums per split over the concatenated transcript_bytes
# of the full 100-session split.
# Updated with actual computed values after split generation.
CORPUS_SHA256 = {
    "dev": "0c82d7567678913da5375b47107db0419c1d4f8ffd7cc322d01ce68538906a7b",
    "test": "1f424e92a6c612beeace0da6dac60f64ab2c5278af7ab1d9932c75e8cb2b154a",
}


def test_disjoint_splits_vocabulary_and_templates():
    """Verify that dev and test splits share absolutely no vocabulary or templates."""
    dev_keys = set(DEV_VARS.keys())
    test_keys = set(TEST_VARS.keys())
    # Note: "opt_a", "opt_b", "pros_a", "pros_b", "task_desc", "plan_a",
    # "plan_b", "plan_c", "old_narrative", "new_narrative", "file_name",
    # "bad_syntax", "good_syntax" are structural keys that exist in both,
    # but their values must be completely disjoint.
    common_keys = dev_keys.intersection(test_keys)
    for key in common_keys:
        dev_vals = set(DEV_VARS[key])
        test_vals = set(TEST_VARS[key])
        overlap = dev_vals.intersection(test_vals)
        assert not overlap, f"Overlap in vocabulary values for key {key!r}: {overlap}"

    # Also check other keys that are not shared
    for key in dev_keys - test_keys:
        for val in DEV_VARS[key]:
            for t_key, t_vals in TEST_VARS.items():
                assert val not in t_vals, (
                    f"Value {val!r} from DEV_VARS[{key!r}] found in TEST_VARS[{t_key!r}]"
                )

    for key in test_keys - dev_keys:
        for val in TEST_VARS[key]:
            for d_key, d_vals in DEV_VARS.items():
                assert val not in d_vals, (
                    f"Value {val!r} from TEST_VARS[{key!r}] found in DEV_VARS[{d_key!r}]"
                )

    # Templates must also be disjoint
    dev_templates = set(DEV_DIRECTIVES_TEMPLATES + DEV_REVERSALS_TEMPLATES)
    test_templates = set(TEST_DIRECTIVES_TEMPLATES + TEST_REVERSALS_TEMPLATES)
    template_overlap = dev_templates.intersection(test_templates)
    assert not template_overlap, f"Overlap in templates: {template_overlap}"


def test_determinism_under_different_python_hash_seeds():
    """Verify that generation is byte-identical under two different PYTHONHASHSEED values."""
    # Write a small script that generates cases with a fixed seed and prints their byte checksums.
    script_content = """
import hashlib
from bench.decisions import generate
cases = generate(42, 5, "dev")
for i, c in enumerate(cases):
    h = hashlib.sha256(c.transcript_bytes).hexdigest()
    print(f"{i}:{len(c.transcript_bytes)}:{h}")
"""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(script_content.encode("utf-8"))
        tmp_path = tmp.name

    try:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        # Run under PYTHONHASHSEED=1111
        env1 = os.environ.copy()
        env1["PYTHONHASHSEED"] = "1111"
        env1["PYTHONPATH"] = project_root
        res1 = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            env=env1,
            check=True,
        )

        # Run under PYTHONHASHSEED=9999
        env2 = os.environ.copy()
        env2["PYTHONHASHSEED"] = "9999"
        env2["PYTHONPATH"] = project_root
        res2 = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            env=env2,
            check=True,
        )

        assert res1.stdout == res2.stdout, "Outputs differed under different PYTHONHASHSEED values!"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def test_scorer_strict_match():
    """Verify the scorer's strict one-to-one matched precision and recall calculations."""
    gold = [
        Decision(kind="directive", source_ref="ref1"),
        Decision(kind="reversal", source_ref="ref2"),
    ]

    # Perfect match
    pred_perfect = [
        Decision(kind="directive", source_ref="ref1"),
        Decision(kind="reversal", source_ref="ref2"),
    ]
    score_p = score_predictions(pred_perfect, gold)
    assert score_p["precision"] == 1.0
    assert score_p["recall"] == 1.0
    assert score_p["passed"] is True

    # Partial match
    pred_partial = [
        Decision(kind="directive", source_ref="ref1"),  # match
        Decision(kind="directive", source_ref="ref2"),  # wrong kind -> miss
    ]
    score_part = score_predictions(pred_partial, gold)
    assert score_part["precision"] == 0.5
    assert score_part["recall"] == 0.5
    assert score_part["passed"] is False

    # Extra duplicate predictions
    pred_dup = [
        Decision(kind="directive", source_ref="ref1"),  # match 1
        Decision(
            kind="directive", source_ref="ref1"
        ),  # duplicate -> miss (no second prediction credit)
    ]
    score_dup = score_predictions(pred_dup, gold)
    assert score_dup["precision"] == 0.5
    assert score_dup["recall"] == 0.5
    assert score_dup["matched"] == 1

    # Empty prediction scores precision 0
    score_empty = score_predictions([], gold)
    assert score_empty["precision"] == 0.0
    assert score_empty["recall"] == 0.0
    assert score_empty["passed"] is False


def test_corpus_size_and_gold_count():
    """Verify size and turn constraints, gold count variation, and >= 300 gold nodes."""
    # DEV split
    dev_cases = generate(seed=42, n=100, split="dev")
    assert len(dev_cases) == 100

    total_gold_dev = 0
    dev_zero_sessions = 0
    has_block_idx_at_least_1 = False

    for case in dev_cases:
        total_gold_dev += len(case.planted_decisions)
        if len(case.planted_decisions) == 0:
            dev_zero_sessions += 1

        for _uuid, block_idx, kind in case.planted_decisions:
            if kind == "directive" and block_idx >= 1:
                has_block_idx_at_least_1 = True

        # Fast verification of turn count via line counts
        num_turns = case.transcript_bytes.count(b"\n")
        assert 20 <= num_turns <= 80, f"Session has {num_turns} turns, expected [20, 80]"

    assert total_gold_dev >= 300, f"Dev split has {total_gold_dev} gold nodes, expected >= 300"
    assert 12 <= dev_zero_sessions <= 25, (
        f"Expected 12-25 sessions with zero gold nodes in dev split, got {dev_zero_sessions}"
    )
    assert has_block_idx_at_least_1, (
        "Expected at least one gold directive with block index >= 1 in dev split"
    )

    # TEST split
    test_cases = generate(seed=20042, n=100, split="test")
    assert len(test_cases) == 100

    total_gold_test = 0
    test_zero_sessions = 0
    has_block_idx_at_least_1_test = False

    for case in test_cases:
        total_gold_test += len(case.planted_decisions)
        if len(case.planted_decisions) == 0:
            test_zero_sessions += 1

        for _uuid, block_idx, kind in case.planted_decisions:
            if kind == "directive" and block_idx >= 1:
                has_block_idx_at_least_1_test = True

        num_turns = case.transcript_bytes.count(b"\n")
        assert 20 <= num_turns <= 80, f"Session has {num_turns} turns, expected [20, 80]"

    assert total_gold_test >= 300, f"Test split has {total_gold_test} gold nodes, expected >= 300"
    assert 12 <= test_zero_sessions <= 25, (
        f"Expected 12-25 sessions with zero gold nodes in test split, got {test_zero_sessions}"
    )
    assert has_block_idx_at_least_1_test, (
        "Expected at least one gold directive with block index >= 1 in test split"
    )


def test_baselines_on_corpus():
    """Verify that baseline_nothing and baseline_naive score below the bar."""
    # Run baseline_nothing
    score_nothing = run_gate(baseline_nothing, seed=42, split="dev", n=100)
    assert score_nothing["precision"] == 0.0
    assert score_nothing["recall"] == 0.0
    assert score_nothing["passed"] is False

    # Run baseline_naive
    score_naive = run_gate(baseline_naive, seed=42, split="dev", n=100)

    # B1/Acceptance 1: The naive heuristic should score between 0.15 and 0.60 precision
    assert 0.15 <= score_naive["precision"] <= 0.60, (
        f"baseline_naive precision too high or low on dev: {score_naive['precision']}"
    )
    assert score_naive["passed"] is False


def test_baseline_leak_on_corpus():
    """Verify that baseline_leak runs successfully on the corpus and achieves high precision."""
    score_naive = run_gate(baseline_naive, seed=42, split="dev", n=100)
    score_leak = run_gate(baseline_leak, seed=42, split="dev", n=100)
    assert score_naive["precision"] < score_leak["precision"] < 1.0
    assert score_leak["recall"] > 0.1
    assert score_leak["passed"] is False


def test_disjoint_pools_equality():
    """Verify that opening texts, first commands, and tool errors have equal sets of values."""
    for split, seed in [("dev", 42), ("test", 20042)]:
        cases = generate(seed=seed, n=100, split=split)
        gold_openings = set()
        gold_commands = set()
        gold_errors = set()

        dist_openings = set()
        dist_commands = set()
        dist_errors = set()

        for case in cases:
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
                tmp.write(case.transcript_bytes)
                tmp_path = tmp.name
            try:
                session = adapters.get("claude-code").parse(tmp_path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

            for i in range(len(session.turns) - 3):
                t0 = session.turns[i]
                t1 = session.turns[i + 1]
                t2 = session.turns[i + 2]
                t3 = session.turns[i + 3]

                # Shape of a 4-turn sequence
                if t0.role != "assistant" or len(t0.blocks) < 2:
                    continue
                if t0.blocks[0].kind != "text" or t0.blocks[1].kind != "tool_use":
                    continue

                if t1.role != "user" or len(t1.blocks) < 1 or t1.blocks[0].kind != "tool_result":
                    continue

                if t2.role != "assistant" or len(t2.blocks) < 2:
                    continue
                if t2.blocks[0].kind != "text" or t2.blocks[1].kind != "tool_use":
                    continue

                if t3.role != "user" or len(t3.blocks) < 1 or t3.blocks[0].kind != "tool_result":
                    continue

                if "Exit code 0" not in t3.blocks[0].text:
                    continue

                op = t0.blocks[0].text
                cmd = t0.blocks[1].text
                if cmd.startswith("command: "):
                    cmd = cmd[len("command: ") :]
                err = t1.blocks[0].text

                f_cmd = t2.blocks[1].text
                if f_cmd.startswith("command: "):
                    f_cmd = f_cmd[len("command: ") :]

                if cmd == f_cmd:
                    dist_openings.add(op)
                    dist_commands.add(cmd)
                    dist_errors.add(err)
                else:
                    gold_openings.add(op)
                    gold_commands.add(cmd)
                    gold_errors.add(err)

        assert gold_openings == dist_openings, f"Opening text pool inequality in {split}"
        assert gold_commands == dist_commands, f"First command pool inequality in {split}"
        assert gold_errors == dist_errors, f"Tool error content pool inequality in {split}"


def walk_leaves(obj, leaves: list[str]) -> None:
    """Recursively find and append all leaf string values inside obj."""
    if isinstance(obj, str):
        leaves.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            walk_leaves(v, leaves)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            walk_leaves(item, leaves)


def normalise_leaf(s: str) -> str:
    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    s = re.sub(uuid_pattern, "<UUID>", s)
    s = re.sub(r"\d+", "<NUM>", s)
    return s


def test_general_leak_check():
    """Verify that general string leaves have equal sets of values between gold and distractor.

    This test excludes exactly one thing: the decision-bearing text block of the
    third turn (t2.blocks[0]), containing either the gold reversal sentence or the
    retry sentence. Set-equality is unachievable over this block because gold
    and distractor windows must differ precisely in this sentence (reversal vs. retry)
    for the model to detect the decision.

    This skipped part is covered instead by test_disjoint_splits_vocabulary_and_templates,
    which ensures complete disjointness of the templates and vocabularies used to
    generate these sentences between dev and test splits, and by the E5 decision
    gate evaluation itself which validates decision extraction.
    """
    for split, seed in [("dev", 42), ("test", 20042)]:
        cases = generate(seed=seed, n=100, split=split)
        gold_leaves = []
        dist_leaves = []

        for case in cases:
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
                tmp.write(case.transcript_bytes)
                tmp_path = tmp.name
            try:
                session = adapters.get("claude-code").parse(tmp_path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

            resolved_gold = resolve_gold_for_case(case)
            gold_reversal_block_ids = {d.source_ref for d in resolved_gold if d.kind == "reversal"}

            for i in range(len(session.turns) - 3):
                t0 = session.turns[i]
                t1 = session.turns[i + 1]
                t2 = session.turns[i + 2]
                t3 = session.turns[i + 3]

                if t0.role != "assistant" or len(t0.blocks) < 2:
                    continue
                if t0.blocks[0].kind != "text" or t0.blocks[1].kind != "tool_use":
                    continue

                if t1.role != "user" or len(t1.blocks) < 1 or t1.blocks[0].kind != "tool_result":
                    continue

                if t2.role != "assistant" or len(t2.blocks) < 2:
                    continue
                if t2.blocks[0].kind != "text" or t2.blocks[1].kind != "tool_use":
                    continue

                if t3.role != "user" or len(t3.blocks) < 1 or t3.blocks[0].kind != "tool_result":
                    continue

                if "Exit code 0" not in t3.blocks[0].text:
                    continue

                seq_leaves = []
                for turn in (t0, t1, t2, t3):
                    if turn is t2:
                        import copy

                        t2_native = copy.deepcopy(turn.native)
                        with contextlib.suppress(KeyError, IndexError, TypeError):
                            t2_native["message"]["content"][0]["text"] = ""
                        walk_leaves(t2_native, seq_leaves)
                        for block in turn.blocks[1:]:
                            walk_leaves(block.native, seq_leaves)
                    else:
                        walk_leaves(turn.native, seq_leaves)
                        for block in turn.blocks:
                            walk_leaves(block.native, seq_leaves)

                norm_leaves = [normalise_leaf(lf) for lf in seq_leaves]

                is_gold = t2.blocks[0].block_id in gold_reversal_block_ids
                if is_gold:
                    gold_leaves.extend(norm_leaves)
                else:
                    dist_leaves.extend(norm_leaves)

        gold_set = set(gold_leaves)
        dist_set = set(dist_leaves)

        print(
            f"\n[{split}] Gold distinct leaves: {len(gold_set)}, "
            f"Distractor distinct leaves: {len(dist_set)}"
        )

        diff_gold = gold_set - dist_set
        diff_dist = dist_set - gold_set
        if diff_gold or diff_dist:
            print(f"[{split}] Difference Gold - Distractor: {sorted(list(diff_gold))[:10]}")
            print(f"[{split}] Difference Distractor - Gold: {sorted(list(diff_dist))[:10]}")

        assert gold_set == dist_set, f"General leak detected in {split}!"


def test_resolve_gold_raises_on_unresolvable_plant():
    """Verify that resolve_gold_for_case raises ValueError on unresolvable plants."""
    cases = generate(seed=42, n=1, split="dev")
    valid_case = cases[0]

    # Non-existent turn UUID
    bad_case_uuid = Case(
        transcript_bytes=valid_case.transcript_bytes,
        planted_decisions=[("non-existent-uuid-12345", 0, "directive")],
    )
    with pytest.raises(ValueError, match="Planted turn UUID .* not found"):
        resolve_gold_for_case(bad_case_uuid)

    # Out of range block index
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp.write(valid_case.transcript_bytes)
        tmp_path = tmp.name
    try:
        session = adapters.get("claude-code").parse(tmp_path)
        real_uuid = session.turns[0].uuid
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    bad_case_block = Case(
        transcript_bytes=valid_case.transcript_bytes,
        planted_decisions=[(real_uuid, 99, "directive")],
    )
    with pytest.raises(ValueError, match="Planted block index .* out of range"):
        resolve_gold_for_case(bad_case_block)


def test_corpus_sha256_checksums():
    """Assert that the generated splits match the committed CORPUS_SHA256 checksums."""
    dev_cases = generate(seed=42, n=100, split="dev")
    dev_concatenated = b"".join(case.transcript_bytes for case in dev_cases)
    dev_hash = hashlib.sha256(dev_concatenated).hexdigest()

    test_cases = generate(seed=20042, n=100, split="test")
    test_concatenated = b"".join(case.transcript_bytes for case in test_cases)
    test_hash = hashlib.sha256(test_concatenated).hexdigest()

    assert dev_hash == CORPUS_SHA256["dev"], (
        "Dev split checksum mismatch! Recompute it by running hashlib.sha256 "
        "on the concatenated transcript bytes of the generated dev split."
    )
    assert test_hash == CORPUS_SHA256["test"], (
        "Test split checksum mismatch! Recompute it by running hashlib.sha256 "
        "on the concatenated transcript bytes of the generated test split."
    )
