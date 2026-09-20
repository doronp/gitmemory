"""Mutation + attribution check for the E3 index, run by hand, not by pytest.

Two questions, because the first one alone is not enough:

1. **Mutation.** Revert one behaviour; does the suite go red?
2. **Attribution.** Does the *test written for that behaviour* go red? A mutant
   caught by some unrelated test means the intended test is decorative, which
   is how a suite passes 451 tests while five `verify` checks are deletable.

    .venv/bin/python tests/mutate_index.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "gitmemory"

# (name, file, find, replace, test that must catch it)
MUTANTS = [
    (
        "query terms unquoted",
        "index.py",
        """return " OR ".join('"' + t.replace('"', '""') + '"' for t in kept), """
        """len(terms) - len(kept)""",
        """return " OR ".join(kept), len(terms) - len(kept)""",
        "test_a_bareword_operator_in_a_query_is_searched_for_not_obeyed",
    ),
    (
        "query tokens are whitespace-split, not \\w+",
        "index.py",
        '_WORD = re.compile(r"\\w+", re.UNICODE)',
        '_WORD = re.compile(r"\\S+", re.UNICODE)',
        "test_a_hyphenated_query_matches_either_half",
    ),
    (
        "query terms ANDed",
        "index.py",
        """return " OR ".join(""",
        """return " ".join(""",
        "test_a_query_is_or_not_and",
    ),
    (
        "truncation goes silent",
        "index.py",
        "    if dropped:",
        "    if False:",
        "test_an_over_long_query_is_truncated_loudly",
    ),
    (
        "MAX_TERMS never actually drops a term",
        "index.py",
        "    kept = terms[:MAX_TERMS]",
        "    kept = terms",
        "test_an_over_long_query_is_truncated_loudly",
    ),
    (
        "query not normalised to NFC",
        "index.py",
        'unicodedata.normalize("NFC", query)',
        "query",
        "test_a_decomposed_query_finds_the_word_it_spells",
    ),
    (
        "underscore is a separator, so an identifier stops being a phrase",
        "index.py",
        '_WORD = re.compile(r"\\w+", re.UNICODE)',
        '_WORD = re.compile(r"[^\\W_]+", re.UNICODE)',
        "test_an_identifier_is_a_phrase_and_a_hyphenation_is_an_or",
    ),
    (
        "results not deduped to turns",
        "index.py",
        "FROM ranked WHERE rn = 1",
        "FROM ranked WHERE rn >= 1",
        "test_a_turn_is_returned_once_however_many_of_its_blocks_match",
    ),
    (
        "one turn in two sessions collapses to one hit",
        "index.py",
        "PARTITION BY b.turn_id, b.agent, b.session_id",
        "PARTITION BY b.turn_id",
        "test_two_sessions_holding_the_same_turn_both_come_back",
    ),
    (
        "a turn carried across a fork answers from the sealed generation",
        "index.py",
        "ORDER BY s.score, b.generation DESC, b.block_seq",
        "ORDER BY s.score, b.generation, b.block_seq",
        "test_a_turn_carried_across_a_fork_comes_back_once_from_the_newest",
    ),
    (
        "ties fall back to whatever order the rows arrive in",
        "index.py",
        "        ORDER BY score, session_key, byte_offset, block_seq",
        "        ORDER BY score",
        "test_equal_scoring_turns_come_back_in_byte_order",
    ),
    (
        "a negative k means unlimited",
        "index.py",
        "(*weights.as_tuple(), expr, max(k, 0)),",
        "(*weights.as_tuple(), expr, k),",
        "test_a_negative_k_returns_nothing_rather_than_the_corpus",
    ),
    (
        "the paths column carries no weight",
        "index.py",
        "    paths: float = 3.0",
        "    paths: float = 0.0",
        "test_the_paths_weight_changes_the_answer_not_just_the_column",
    ),
    (
        "column weights ignored",
        "index.py",
        "return (self.prose, self.tool_use, self.tool_result, self.paths)",
        "return (1.0, 1.0, 1.0, 1.0)",
        "test_weights_are_what_decides_the_order",
    ),
    (
        "tool arguments lose their prose weighting",
        "index.py",
        """{"tool_use": "tool_use", "tool_result": "tool_result"}""",
        """{"tool_use": "tool_result", "tool_result": "tool_result"}""",
        "test_a_tool_argument_outranks_tool_output",
    ),
    (
        "paths from tool arguments dropped",
        "index.py",
        "    for key in _PATH_KEYS:",
        "    for key in ():",
        "test_a_bare_filename_in_a_tool_argument_lands_in_the_paths_column",
    ),
    (
        "paths from text dropped",
        "index.py",
        "    for match in _PATH.finditer(block.text):",
        "    for match in ():",
        "test_a_path_in_prose_lands_in_the_paths_column_too",
    ),
    (
        "segments parsed apart instead of concatenated",
        "index.py",
        "    if len(stored.segments) == 1:",
        "    if stored.segments:",
        "test_an_offset_stays_absolute_across_a_segment_boundary",
    ),
    (
        "one bad generation aborts the build",
        "index.py",
        "        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data",
        "        except ZeroDivisionError as exc:",
        "test_an_unparseable_generation_costs_only_itself",
    ),
    (
        "the guard covers the parse but not the rows built from it",
        "index.py",
        "            rows = [_row(stored, turn, block) "
        "for turn in session.turns for block in turn.blocks]",
        "            rows = (_row(stored, turn, block) "
        "for turn in session.turns for block in turn.blocks)",
        "test_a_row_that_cannot_be_built_costs_only_its_generation",
    ),
    (
        "a tool call's arguments are trusted to be an object",
        "index.py",
        "    if isinstance(args, dict):",
        "    if args:",
        "test_a_malformed_tool_call_costs_nothing",
    ),
    (
        "text goes to sqlite3 unsanitised",
        "index.py",
        '    return value.encode("utf-8", "replace").decode("utf-8")',
        "    return value",
        "test_a_lone_surrogate_costs_nothing",
    ),
    (
        "the path regex is unbounded again",
        "index.py",
        r"(?:[\w.@%+-]{1,255}+[\\/])+[\w.@%+-]{1,255}+",
        r"(?:[\w.@%+-]+[\\/])+[\w.@%+-]+",
        "test_a_separator_free_megabyte_does_not_hang_the_build",
    ),
    (
        "skipped generations leave no trace in the digest",
        "index.py",
        "    for line in skipped:",
        "    for line in ():",
        "test_a_generation_that_could_not_be_parsed_changes_the_digest",
    ),
    (
        "blocks are not counted",
        "index.py",
        "            blocks += 1",
        "            blocks += 0",
        "test_every_block_of_every_turn_is_counted",
    ),
    (
        "turns are counted per generation, not per turn",
        "index.py",
        "        turns += len(session.turns)",
        "        turns += 1",
        "test_every_block_of_every_turn_is_counted",
    ),
    (
        "index built in place, not renamed over",
        "index.py",
        '    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".building-", suffix=".db")',
        "    fd, tmp = os.open(target, os.O_CREAT | os.O_WRONLY), target",
        "test_a_failed_build_never_replaces_a_working_index",
    ),
    (
        "cleanup does not cover Ctrl-C",
        "index.py",
        "    except BaseException:",
        "    except Exception:",
        "test_an_interrupted_build_also_leaves_nothing_behind",
    ),
    (
        "partial indexes from a kill are never swept",
        "index.py",
        "    _sweep_partials(parent)",
        "    pass",
        "test_a_partial_index_left_by_a_kill_is_swept",
    ),
    (
        "a foreign schema is answered instead of refused",
        "index.py",
        "    _check_schema(db)",
        "    pass",
        "test_an_index_from_another_schema_is_refused_not_answered",
    ),
    (
        "a bare --db filename has no directory to create",
        "index.py",
        "    target = os.path.abspath(path or db_path(home))",
        "    target = path or db_path(home)",
        "test_a_bare_filename_is_a_usable_db_path",
    ),
    (
        "the retriever hands back lengths instead of offsets",
        "index.py",
        "        return [h.byte_offset for h in search(db, query, k=k, weights=weights)]",
        "        return [h.byte_len for h in search(db, query, k=k, weights=weights)]",
        "test_the_retriever_hands_back_offsets_in_rank_order",
    ),
    (
        "the retriever ignores k",
        "index.py",
        "        return [h.byte_offset for h in search(db, query, k=k, weights=weights)]",
        "        return [h.byte_offset for h in search(db, query, weights=weights)]",
        "test_the_retriever_honours_k_and_weights",
    ),
    (
        "the retriever ignores weights",
        "index.py",
        "        return [h.byte_offset for h in search(db, query, k=k, weights=weights)]",
        "        return [h.byte_offset for h in search(db, query, k=k)]",
        "test_the_retriever_honours_k_and_weights",
    ),
    (
        "only the live generation is indexed",
        "store.py",
        "        )\n    return out",
        "        )\n    return out[-1:] if out else out",
        "test_both_generations_of_a_forked_session_are_indexed",
    ),
    (
        "a bad metadata field drops a row from the egress scan",
        "store.py",
        '                agent=_text(man.get("agent")),',
        '                agent=_safe(man["agent"], "agent"),',
        "test_a_hostile_manifest_field_cannot_opt_a_segment_run_out_of_the_seam_scan",
    ),
    # --- E3: the trust root -------------------------------------------------- #
    (
        "a truncated manifest is raised on, not skipped",
        "store.py",
        "        except EscapingSegment:",
        "        except ValueError:",
        "test_a_truncated_manifest_is_skipped_not_raised",
    ),
    (
        "a malformed segment entry is dropped instead of costing its generation",
        "store.py",
        "            run = [_segment_path(home, s) for s in segs]",
        "            run = [p for s in segs if (p := _segment_path(home, s)) is not None]",
        "test_a_malformed_segment_entry_costs_its_generation_not_one_segment",
    ),
    (
        "a segment whose start sessions cannot read is still ordered by it",
        "store.py",
        '    if not isinstance(seg.get("start"), int) or isinstance(seg.get("start"), bool):',
        "    if False:",
        "test_a_segment_entry_with_an_unsortable_start_costs_only_its_generation",
    ),
    (
        "segments are read in whatever order the manifest lists them",
        "store.py",
        '            segs = sorted(_seq(man.get("segments")), key=_seg_start)',
        '            segs = list(_seq(man.get("segments")))',
        "test_a_reordered_manifest_is_read_in_offset_order",
    ),
    (
        "an escaping path is dropped quietly during adoption",
        "store.py",
        '            raise EscapingSegment(f"segment path escapes the store: {p!r}")',
        "            continue",
        "test_an_escaping_segment_path_stops_orphan_adoption",
    ),
    (
        "a bool passes for an int, so the next segment tiles from byte 1",
        "store.py",
        "        if not isinstance(val, want) or (want is int and isinstance(val, bool)):",
        "        if not isinstance(val, want):",
        "test_a_manifest_field_of_the_wrong_type_is_refused_not_crashed_on",
    ),
    (
        "adoption reads the last manifest before anyone has checked it",
        "store.py",
        "        _check_manifest(man)",
        "        pass",
        "test_a_manifest_field_of_the_wrong_type_is_refused_not_crashed_on",
    ),
    (
        "directories on the way are left at the ambient umask",
        "store.py",
        "    missing = []",
        "    return os.makedirs(path, mode=0o700, exist_ok=True) or []",
        "test_the_store_is_owner_only_on_disk",
    ),
    (
        "segment files are created world-readable",
        "store.py",
        "os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
        "os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)",
        "test_the_store_is_owner_only_on_disk",
    ),
    (
        "a segment shorter than the seam resets the carry instead of extending it",
        "redact.py",
        "        if len(new_tail) < SEAM:",
        "        if False:",
        "test_a_credential_split_across_three_segments_is_caught_at_the_seams",
    ),
    (
        "recall prints the control characters it was handed",
        "__main__.py",
        '    return _UNSAFE.sub(lambda m: f"\\\\x{ord(m.group()):02x}", text)',
        "    return text",
        "test_a_terminal_escape_in_a_transcript_does_not_reach_the_terminal",
    ),
    (
        "the within-line byte cursor counts characters",
        "jsonl.py",
        "                pos, byte_pos = end, byte_pos + byte_len",
        "                pos, byte_pos = end, byte_pos + (end - pos)",
        "test_multibyte_before_a_second_object_on_the_same_line",
    ),
    (
        "span seeks with the generation offset inside a segment",
        "store.py",
        "                fh.seek(max(0, offset - pos))",
        "                fh.seek(offset)",
        "test_a_hit_in_a_multi_segment_generation_resolves_across_the_cut",
    ),
    # ---- bench/ ----------------------------------------------------------
    # The measuring instrument gets the same treatment as the thing measured.
    # Seven semantic mutations of `bench/` used to leave its whole suite green,
    # including deleting the statistics and making the control arm *be* the
    # candidate arm — so the harness that was supposed to gate E3 could not
    # notice being broken. [E3]
    (
        "a zero-variance difference gets a fabricated p of 0",
        "bench/score.py",
        "    if sumsq == 0.0:\n        return mean_diff, 1.0",
        "    if True:\n        return mean_diff, (0.0 if mean_diff else 1.0)",
        "test_one_observation_can_never_be_significant",
    ),
    (
        "the large-n branch is a different test from the exact one",
        "bench/score.py",
        "    return mean_diff, 1.0 - normal_cdf(total / math.sqrt(sumsq))",
        "    return mean_diff, normal_cdf(total / math.sqrt(sumsq))",
        "test_the_exact_and_normal_branches_agree",
    ),
    (
        "the derangement maps each instance to itself",
        "bench/score.py",
        "            mapping[inst.question_id] = order[(idx + 1) % len(order)].question",
        "            mapping[inst.question_id] = inst.question",
        "test_the_shuffled_arm_is_asked_another_instances_question",
    ),
    (
        "the shuffled arm is asked the candidate's own question",
        "bench/score.py",
        '                    "shuffled": candidate(shuffled_query[inst.question_id], k),',
        '                    "shuffled": candidate(inst.question, k),',
        "test_the_shuffled_arm_is_asked_another_instances_question",
    ),
    (
        "alpha is not corrected for the number of modes",
        "bench/score.py",
        "    alpha = ALPHA / max(1, len(modes))",
        "    alpha = ALPHA",
        "test_the_alpha_is_bonferroni_corrected_across_the_modes_tested",
    ),
    (
        "the oracle arm retrieves nothing",
        "bench/score.py",
        '                    "reference": sorted(evidence)[:k],',
        '                    "reference": [],',
        "test_the_gate_passes_on_the_real_index",
    ),
    (
        "abstention instances are scored like the rest",
        "bench/score.py",
        '    active = [inst for inst in instances if not inst.question_id.endswith("_abs")]',
        "    active = list(instances)",
        "test_abstention_instances_are_excluded",
    ),
    (
        "an offset only matches a turn it starts exactly",
        "bench/score.py",
        "        if turn is None or offset >= turn.byte_offset + turn.byte_len:",
        "        if turn is None or offset != turn.byte_offset:",
        "test_an_offset_inside_a_turn_matches_that_turn",
    ),
    (
        "recall is a hit rate again",
        "bench/score.py",
        "        turn_recall=len(found_turns) / len(evidence) if evidence else 0.0,",
        "        turn_recall=1.0 if found_turns else 0.0,",
        "test_recall_counts_every_evidence_turn_not_just_the_first",
    ),
    (
        "an offset matching no turn is not counted",
        "bench/score.py",
        "            unmatched += 1\n            continue",
        "            continue",
        "test_an_unmatched_offset_consumes_its_rank_and_is_counted",
    ),
    (
        "the live-context window keeps what fell before the boundary",
        "bench/score.py",
        "        return [o for o in retrieve(query, k * 4) if o >= cutoff][:k]",
        "        return [o for o in retrieve(query, k * 4) if o >= 0][:k]",
        "test_the_live_context_arm_loses_what_fell_before_the_boundary",
    ),
    (
        "the sample-size floor does not refuse",
        "bench/score.py",
        "    if n < MIN_INSTANCES:",
        "    if False:",
        "test_the_gate_refuses_a_verdict_below_the_minimum_sample",
    ),
    (
        "the apparatus check accepts an inexact oracle",
        "bench/score.py",
        "            ref == 1.0,",
        "            ref >= 0.0,",
        "test_the_gate_fails_when_the_oracle_arm_is_not_exact",
    ),
    (
        "the signal check always passes (the gate that did not gate)",
        "bench/score.py",
        "            p < alpha and diff > MIN_EFFECT,",
        "            True,",
        "test_the_gate_fails_on_a_retriever_that_returns_nothing",
    ),
    (
        "the leakage bound always passes",
        "bench/score.py",
        "            leak <= LEAK_MARGIN,",
        "            True,",
        "test_the_gate_fails_on_a_retriever_that_ignores_the_query",
    ),
    (
        "the product-claim check always passes",
        "bench/score.py",
        "                claim_p < alpha and claim_diff > MIN_EFFECT,",
        "                True,",
        "test_the_gate_fails_when_the_candidate_finds_only_what_the_live_window_had",
    ),
    (
        "the arms' temporary stores are never closed",
        "bench/score.py",
        "                    if close is not None:",
        "                    if False:",
        "test_a_sweep_leaves_no_temporary_store_behind",
    ),
    (
        "the after-evidence boundary lands after the first evidence turn",
        "bench/synth.py",
        '            if compaction == "after_evidence" and last_evidence == (s_idx, t_idx):',
        '            if compaction == "after_evidence" and first_evidence == (s_idx, t_idx):',
        "test_the_after_evidence_boundary_follows_the_last_evidence_turn",
    ),
    (
        "a repeated question_id is accepted",
        "bench/longmemeval.py",
        '        if item["question_id"] in seen:',
        "        if False:",
        "test_a_repeated_question_id_is_rejected_by_the_loader",
    ),
]


def run(args: list[str]) -> bool:
    """True when pytest is green.

    `tests bench` explicitly, not pytest's configured `testpaths`: naming them
    here keeps the harness honest about what it ran even if `testpaths` changes
    under it. The corpus test is deselected by `addopts`, so this is the offline
    suite and it takes about a second. [E3]
    """
    return (
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests", "bench", *args], cwd=ROOT
        ).returncode
        == 0
    )


def main() -> int:
    bad = []
    for name, filename, find, replace, test in MUTANTS:
        # A bare name is a file in the package; a path is relative to the repo,
        # which is how the `bench/` mutants below address the harness itself.
        path = ROOT / filename if "/" in filename else SRC / filename
        original = path.read_text()
        if original.count(find) != 1:
            print(f"SKIP  {name}: anchor appears {original.count(find)}x in {filename}")
            bad.append(name)
            continue
        path.write_text(original.replace(find, replace))
        try:
            suite = run(["-x", "-q"])
            intended = run(["-q", "-k", test])
        finally:
            path.write_text(original)
        if suite:
            print(f"SURVIVED  {name}")
            bad.append(name)
        elif intended:
            print(f"MISSED    {name}: caught, but not by {test}")
            bad.append(name)
        else:
            print(f"CAUGHT    {name}  <- {test}")
    print(f"\n{len(MUTANTS) - len(bad)}/{len(MUTANTS)} caught by their intended test")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
