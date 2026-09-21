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
        "            segs = sorted(raw_segments, key=_seg_start)",
        "            segs = list(raw_segments)",
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
        "        _check_manifest(man)\n        if forked:",
        "        if forked:",
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
        "an arm is handed the whole Instance, answer and evidence label included",
        "bench/score.py",
        "            candidate = retrieve_factory(inst.question_id, transcript.bytes_data)",
        "            candidate = retrieve_factory(inst, transcript.bytes_data)",
        "test_an_arm_is_handed_a_session_id_and_bytes_and_nothing_else",
    ),
    # The E5 fixture is what keeps the extractor's author away from the corpus
    # generator. Four ways that could quietly stop being true. [E5]
    (
        "the dumped labels come from a different generation than the dumped bytes",
        "bench/fixture.py",
        "        gold[stem] = [[d.kind, d.source_ref] for d in resolve_gold_for_case(case)]",
        "        gold[stem] = [[d.kind, d.source_ref] for d in resolve_gold_for_case(\n"
        "            generate(seed=seed + 1, n=n, split=split)[i]\n"
        "        )]",
        "test_a_dumped_gold_ref_names_a_block_that_is_in_the_dumped_bytes",
    ),
    (
        "the fixture labels carry the session index back to the author",
        "bench/fixture.py",
        "        gold[stem] = [[d.kind, d.source_ref] for d in resolve_gold_for_case(case)]",
        "        gold[stem] = [[d.kind, d.source_ref, stem] for d in resolve_gold_for_case(case)]",
        "test_the_gold_labels_carry_no_hint_of_how_the_corpus_was_made",
    ),
    (
        "the fixture scorer matches nothing",
        "bench/gate.py",
        "        if gold_counts[key] > 0:",
        "        if False:",
        "test_a_perfect_extractor_scores_one_and_an_empty_one_scores_zero",
    ),
    (
        "the scorer reaches back into the corpus generator",
        "bench/gate.py",
        '    gold_by_stem = json.loads((fixture_dir / "gold.json").read_text())',
        "    from bench.decisions import generate  # noqa: F401\n\n"
        '    gold_by_stem = json.loads((fixture_dir / "gold.json").read_text())',
        "test_the_scorer_runs_with_the_corpus_generator_unimportable",
    ),
    # A standalone review of the gate found six defects in it. Each fix gets a
    # mutant here, because a scorer nobody can break is a scorer nobody is
    # holding in place — and this is the code that decides whether E5 passed.
    # [E5:R]
    (
        "a prediction is credited to gold in a different transcript",
        "bench/gate.py",
        '    return [(kind, f"{scope}\\x00{ref}") for kind, ref in (_pair(d) for d in decisions)]',
        "    return [_pair(d) for d in decisions]",
        "test_a_prediction_cannot_satisfy_gold_planted_in_another_transcript",
    ),
    (
        "a green test run counts as a failure again",
        "bench/gate.py",
        "    m = _EXIT_CODE.search(block.text)",
        '    if "failed" in block.text or "Exit code 1" in block.text:\n'
        "        return True\n"
        "    m = _EXIT_CODE.search(block.text)",
        "test_a_green_test_run_is_not_a_failure_and_a_red_one_is",
    ),
    (
        "predictions outside every slice go back to being dropped in silence",
        "bench/gate.py",
        '        "predicted": len(predicted) - sum(len(b["preds"]) for b in buckets.values()),',
        '        "predicted": 0,',
        "test_predictions_the_slices_cannot_hold_are_reported",
    ),
    (
        "gold may name any block of the right turn",
        "bench/decisions.py",
        "        if block.text != text:",
        "        if False:",
        "test_gold_must_name_the_block_that_holds_the_planted_text",
    ),
    (
        "an unlabelled transcript is scored past in silence",
        "bench/gate.py",
        "    if on_disk != set(gold_by_stem):",
        "    if False:",
        "test_a_transcript_with_no_label_is_refused_rather_than_skipped",
    ),
    (
        "a string decision is read as a two-character pair",
        "bench/gate.py",
        "    if isinstance(d, (tuple, list)) and len(d) == 2:\n        return (d[0], d[1])",
        "    if True:\n        return (d[0], d[1])",
        "test_a_decision_that_is_neither_a_pair_nor_an_object_is_named",
    ),
    (
        "an empty slice prints as a slice that scored zero",
        "bench/gate.py",
        '            if not s["gold"]',
        "            if False",
        "test_a_slice_with_no_gold_does_not_read_as_a_failed_one",
    ),
    (
        "any failure inside derive is reported as an unwritten extractor",
        "bench/gate.py",
        '    from gitmemory import derive\n\n    if not hasattr(derive, "decisions"):',
        "    try:\n        from gitmemory.derive import decisions  # noqa: F401\n"
        "    except ImportError:\n        derive = None\n    if derive is None:",
        "test_a_derive_that_fails_to_import_is_not_reported_as_unwritten",
    ),
    (
        "the report does not name the file it scored",
        "bench/gate.py",
        '    print(f"scoring {derive.__file__}", file=sys.stderr)',
        "    pass",
        "test_the_report_names_the_file_it_scored",
    ),
    (
        "a stray file in the dump hands the author something extra",
        "bench/fixture.py",
        '    (out_dir / "gold.json").write_text('
        'json.dumps(gold, indent=1, sort_keys=True) + "\\n")',
        '    (out_dir / "meta.json").write_text(f"{split} {seed}\\n")\n'
        '    (out_dir / "gold.json").write_text('
        'json.dumps(gold, indent=1, sort_keys=True) + "\\n")',
        "test_the_gold_labels_carry_no_hint_of_how_the_corpus_was_made",
    ),
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
        "    return mean_diff, max(1.0 - normal_cdf(total / math.sqrt(sumsq)), sys.float_info.min)",
        "    return mean_diff, max(normal_cdf(total / math.sqrt(sumsq)), sys.float_info.min)",
        "test_the_exact_and_normal_branches_agree",
    ),
    (
        "the normal branch is allowed to underflow to a p of exactly 0",
        "bench/score.py",
        "    return mean_diff, max(1.0 - normal_cdf(total / math.sqrt(sumsq)), sys.float_info.min)",
        "    return mean_diff, 1.0 - normal_cdf(total / math.sqrt(sumsq))",
        "test_an_overwhelming_difference_still_reports_a_nonzero_p",
    ),
    (
        "a lone singleton question_type is left to map to itself",
        "bench/score.py",
        "    elif singletons:\n"
        "        if not groups:\n"
        '            raise ValueError("a derangement needs at least two instances")\n'
        "        max(groups, key=len).extend(singletons)",
        "    elif singletons:\n        groups.append(singletons)",
        "test_a_lone_question_type_is_still_deranged",
    ),
    (
        "an arm is registered without checking its dependency is importable",
        "bench/__main__.py",
        "        missing = [m for m in modules if importlib.util.find_spec(m) is None]",
        "        missing = []",
        # Was `..._is_skipped_with_a_reason`, which SURVIVED here and would have
        # been CAUGHT in the development virtualenv: that test asks the machine
        # what is installed, and this worktree has the hybrid extras while the
        # main tree does not. The replacement forces the absence instead of
        # hoping for it. [E5, full mutation pass]
        "test_an_arm_whose_dependency_is_absent_names_the_module_it_is_missing",
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
    # --- E4: the hook, the watcher, the git layer ---
    #
    # Every one of these reverts a fix from the E4 review round, so each is a
    # defect that shipped once. The point of putting them here rather than
    # trusting the tests written alongside them is attribution: a review fix
    # whose test the *next* refactor deletes is a fix with nothing holding it,
    # and the suite stays green either way.
    (
        "an absolute watch pattern is walked instead of refused",
        "daemon.py",
        "    if os.path.isabs(value) or os.pardir in value.split(os.sep):",
        "    if False:",
        "test_an_absolute_pattern_is_refused_rather_than_walked",
    ),
    (
        "watches claim transcripts in config order, not deepest-root-first",
        "daemon.py",
        "    pairs.sort(key=lambda wr: wr[1].count(os.sep), reverse=True)",
        "    pass",
        "test_the_most_specific_watch_claims_a_transcript",
    ),
    (
        "one session's failure suspends the whole pass again",
        "daemon.py",
        "        except Exception as exc:  # noqa: BLE001 - one session cannot suspend the rest",
        "        except KeyboardInterrupt as exc:",
        "test_one_unreadable_session_does_not_suspend_every_other_one",
    ),
    (
        "the error log's repeat rate is the capture interval again",
        "daemon.py",
        "now - last_said >= ERROR_REPEAT",
        "now - last_said >= interval",
        "test_the_error_rate_limit_survives_interval_zero",
    ),
    (
        "an unchanged transcript is rehashed every pass",
        "daemon.py",
        "                os.utime(cap.manifest_path, (now, now))",
        "                pass",
        "test_an_unchanged_transcript_is_not_rehashed_on_every_pass",
    ),
    (
        "the / root check goes back below the store check, where it cannot run",
        "daemon.py",
        "            if resolved == os.sep:",
        "            if False:",
        "test_a_root_of_slash_is_refused_for_being_slash",
    ),
    (
        "a config that is not UTF-8 is repr'd to the log",
        "daemon.py",
        "    except ValueError as exc:\n        # `tomllib.load` decodes as UTF-8",
        "    except SystemExit as exc:\n        # `tomllib.load` decodes as UTF-8",
        "test_a_config_that_is_not_utf8_is_reported_without_being_printed",
    ),
    (
        "a root that cannot be resolved takes the whole config with it",
        "daemon.py",
        "            except ValueError as exc:\n                # An embedded NUL.",
        "            except SystemExit as exc:\n                # An embedded NUL.",
        "test_a_root_that_cannot_be_resolved_skips_only_itself",
    ),
    (
        "a doorbell no watch covers is silent again",
        "daemon.py",
        "        if result.spool_dropped != last_dropped:",
        "        if False:",
        "test_a_hook_record_no_watch_covers_is_reported",
    ),
    (
        "a .gitignore that is not UTF-8 makes the watcher unstartable",
        "gitrepo.py",
        "    except (OSError, ValueError):",
        "    except OSError:",
        "test_a_gitignore_that_is_not_utf8_does_not_make_the_watcher_unstartable",
    ),
    (
        "the manifest-temp ignore is unanchored, so a filename can match it",
        "gitrepo.py",
        "sessions/*/*/*.tmp.*",
        "*.tmp.*",
        "test_a_session_named_like_the_stores_own_temp_files_still_reaches_history",
    ),
    (
        "the shim's O_EXCL is dropped and only the [ -h ] test is left",
        "hook/gitmemory-hook.sh",
        "\nset -C\n",
        "\nset +C\n",
        "test_set_c_alone_stops_the_write_through",
    ),
    (
        "the shim echoes an environment variable's control characters",
        "hook/gitmemory-hook.sh",
        "'$(printf '%s' \"$H\" | tr -d '\\000-\\037')'",
        "'$H'",
        "test_a_refusal_cannot_rewrite_the_agents_terminal",
    ),
    # --- the vacuity audit ---
    #
    # This index answers "does the named test catch its mutation?". It cannot
    # answer "is there a mutation nobody named?", so a separate audit went the
    # other way: take each test's claim, mutate the behaviour it describes, and
    # see whether the suite notices. This is the one that came back with the
    # suite fully green. Listed here so the second attempt at pinning it is
    # held the same way the first one was not.
    (
        "the no-op early return is deleted; the manifest is rewritten every time",
        "store.py",
        "        if prior and not diverged and kept_boundaries == carried:",
        "        if False:",
        "test_nothing_new_rewrites_nothing",
    ),
    (
        "a never-seen session no longer bypasses the interval gate",
        "daemon.py",
        " or size < 0 or now - mtime >= interval):",
        " or now - mtime >= interval):",
        "test_a_first_sighting_is_captured_without_waiting_for_the_interval",
    ),
    # The three below are the *inner* lock of a pair. Each was unobservable
    # while `_env`'s config isolation held, which it does in every other test
    # here, so all three could be deleted with the suite green. They are pinned
    # now by tests that switch the outer lock off first — see
    # `_without_the_global_isolation`. `core.hooksPath` is the one that was
    # already pinned, by two tests of its own.
    (
        "the empty --template= is dropped; a global init.templateDir applies",
        "gitrepo.py",
        '        _git(home, "init", "--quiet", "--template=", "--initial-branch=main")',
        '        _git(home, "init", "--quiet", "--initial-branch=main")',
        "test_the_empty_template_declines_a_template_the_isolation_let_through",
    ),
    (
        "--no-verify is dropped, leaving only core.hooksPath against a pre-commit hook",
        "gitrepo.py",
        '    _git(home, "commit", "--quiet", "--no-verify", "--message", message)',
        '    _git(home, "commit", "--quiet", "--message", message)',
        "test_no_verify_alone_stops_a_pre_commit_hook",
    ),
    (
        "the repository no longer pins commit.gpgSign = false",
        "gitrepo.py",
        '    "commit.gpgSign": "false",\n',
        "",
        "test_the_pinned_gpgsign_survives_a_global_the_isolation_let_through",
    ),
    # The second pass of the same audit. Both tests below named their property
    # and could not see it: the first handed itself back the boundary it was
    # checking got carried, and the second exercised a corrupt database that
    # `_check_schema` converts to a `ValueError` long before `sqlite3.Error` in
    # `main`'s handler can be reached.
    (
        "a manifest's own boundaries are no longer carried into the next capture",
        "store.py",
        "    kept_boundaries, dropped = _clean_boundaries([*carried, *(boundaries or [])], end)",
        "    kept_boundaries, dropped = _clean_boundaries([*(boundaries or [])], end)",
        "test_compact_boundaries_accumulate_within_a_generation",
    ),
    (
        "main stops handling sqlite3.Error, so a query that reaches the database tracebacks",
        "__main__.py",
        "    except (OSError, RecursionError, RuntimeError, ValueError, sqlite3.Error) as exc:",
        "    except (OSError, RecursionError, RuntimeError, ValueError) as exc:",
        "test_search_rejects_nothing_it_can_reach_the_database_with",
    ),
    # --- E5: derivation ---
    #
    # These were run as negative controls *before* the module was committed, one
    # per test in `tests/test_derive.py`, and two of them came back VACUOUS on
    # the first attempt:
    #
    #   - "a timeline mark stops naming its turn" changed nothing, because the
    #     fixture was `conversation()`, which produces no events at all. The test
    #     iterated an empty list and asserted nothing. Fixed by giving it a
    #     fixture with a compaction in it and a count assertion on the marks.
    #   - "the empty-document guard goes" changed nothing either, because sumy
    #     returns an empty tuple for an empty document — so the guard is speed,
    #     not safety, and the test was renamed to pin what it actually holds up:
    #     an empty generation gets its artifacts written rather than skipped.
    #
    # Both are the shape the E4 vacuity audit exists to find, caught before the
    # commit rather than two epochs later.
    (
        "an idea cites something that is not a block id",
        "derive.py",
        "        owners.extend([block.block_id] * len(kept))",
        "        owners.extend([block.kind] * len(kept))",
        "test_every_idea_names_a_block_that_exists_in_the_session",
    ),
    (
        "attribution by first occurrence instead of a cursor walk",
        "derive.py",
        """            while cursor < len(texts) and texts[cursor] != text:
                cursor += 1""",
        """            cursor = texts.index(text)""",
        "test_an_idea_is_attributed_to_the_block_it_was_read_out_of",
    ),
    (
        "a timeline mark stops naming its turn",
        "derive.py",
        '                "source_ref": event.anchor,',
        '                "source_ref": "",',
        "test_every_timeline_mark_names_the_turn_that_caused_it",
    ),
    (
        "the tail claims no turns, so the fold stops accounting for them",
        "derive.py",
        '            "turns_since": len(turns) - i,',
        '            "turns_since": 0,',
        "test_the_timeline_accounts_for_every_turn",
    ),
    (
        "the span boundary is inclusive, so a mark eats the turn it sits on",
        "derive.py",
        "        while i < len(turns) and turns[i].byte_offset < event.byte_offset:",
        "        while i < len(turns) and turns[i].byte_offset <= event.byte_offset:",
        "test_a_compaction_splits_the_timeline_where_the_boundary_is",
    ),
    (
        "the payload grows a build timestamp",
        "derive.py",
        "        out = derived_dir(resolved, stored)",
        '        payload_timeline["built_at"] = __import__("time").monotonic()\n'
        "        out = derived_dir(resolved, stored)",
        "test_deriving_twice_changes_nothing_in_git",
    ),
    (
        "prose order comes out of a set, so it varies with the hash seed",
        "derive.py",
        """    for turn in session.turns:
        for block in turn.blocks:
            if block.kind == "text" and block.text.strip():
                yield block""",
        """    by_text = {
        b.text: b for t in session.turns for b in t.blocks if b.kind == "text" and b.text.strip()
    }
    for body in set(by_text):
        yield by_text[body]""",
        "test_derived_bytes_are_identical_in_a_fresh_interpreter",
    ),
    (
        "derived/ joins index/ in the store's .gitignore",
        "gitrepo.py",
        "/index/\n# Hook drop-box",
        "/index/\n/derived/\n# Hook drop-box",
        "test_derived_is_committed_rather_than_ignored",
    ),
    (
        "prose stops meaning text, so tool payloads are ranked",
        "derive.py",
        '            if block.kind == "text" and block.text.strip():',
        "            if block.text.strip():",
        "test_a_tool_payload_never_becomes_a_key_idea",
    ),
    (
        "ideas are presented in some order other than the one they were said in",
        "derive.py",
        '        "ideas": picked,',
        '        "ideas": picked[::-1],',
        "test_ideas_come_back_in_the_order_they_were_said",
    ),
    (
        "the sentence cap stops being cumulative, so it never binds",
        "derive.py",
        "            if len(owners) + len(kept) >= MAX_SENTENCES:",
        "            if len(kept) >= MAX_SENTENCES:",
        "test_a_session_past_the_sentence_cap_says_so",
    ),
    (
        "a generation with nothing to say is skipped instead of written",
        "derive.py",
        "        out = derived_dir(resolved, stored)",
        '        if not payload_ideas["ideas"]:\n            continue\n'
        "        out = derived_dir(resolved, stored)",
        "test_a_session_with_no_prose_still_gets_its_artifacts_written",
    ),
    (
        "one unparseable generation takes the whole build down",
        "derive.py",
        """            stats.skipped.append(f"{stored.key}: {exc!r}")
            continue""",
        "            raise",
        "test_one_unparseable_generation_does_not_cost_the_others",
    ),
    (
        "a skipped generation stops being reported",
        "__main__.py",
        """    for line in stats.skipped:
        print(f"skipped {line}", file=sys.stderr)
    print(f"{stats.generations} generation(s)  {stats.ideas} idea(s)  {stats.marks} mark(s)")""",
        '    print(f"{stats.generations} generation(s)  '
        '{stats.ideas} idea(s)  {stats.marks} mark(s)")',
        "test_a_skipped_generation_is_reported_on_stderr",
    ),
    (
        "`derive` stops checking that it was pointed at a store",
        "__main__.py",
        """def _derive(args) -> int:
    if (code := _not_a_store(args.home)) is not None:
        return code
""",
        "def _derive(args) -> int:\n",
        "test_cli_derive_refuses_a_path_that_is_not_a_store",
    ),
    (
        "the derived path is built from the manifest's fields, not its location",
        "derive.py",
        """    session_dir, gen_file = os.path.split(stored.manifest)
    agent_dir, session = os.path.split(session_dir)
    agent = os.path.basename(agent_dir)
    return os.path.join(home, "derived", agent, session, os.path.splitext(gen_file)[0])""",
        """    _, gen_file = os.path.split(stored.manifest)
    return os.path.join(
        home, "derived", stored.agent, stored.session_id, os.path.splitext(gen_file)[0]
    )""",
        "test_derived_mirrors_the_manifest_path_not_the_manifest_contents",
    ),
    # --- E4 vacuity pass 2: the owner-data guard ---
    #
    # Pass 2 planted a leak and then disabled the scanner five separate ways.
    # Each one left all 784 tests green, because every assertion in that file was
    # "no hits" over a corpus that genuinely has none — the shape that passes
    # identically when the search is broken. The first mutant below is the exact
    # hole E4 found and fixed, so the fix was one revert away from being undone
    # in silence. These five are the reason `_scan` is now a function and a
    # fixture repository is scanned by it.
    #
    # None of these replacement strings may contain a real owner-shaped path: the
    # scanner reads `mutate_index.py` too, and a mutant that trips it would turn
    # every later row red. The pattern-narrowing mutant therefore edits the
    # character class rather than writing a username.
    (
        "the owner-data scan sees untracked files",
        "tests/test_no_owner_data.py",
        """args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]""",
        """args = ["ls-files", "-z", "--cached"]""",
        "test_the_scanner_finds_leaks_that_are_really_there",
    ),
    (
        "the owner-data allowlist stays one file",
        "tests/test_no_owner_data.py",
        """ALLOWED = {"tests/test_no_owner_data.py"}""",
        """ALLOWED = {"tests/test_no_owner_data.py", "src/gitmemory/daemon.py"}""",
        "test_the_allowlist_only_names_files_that_exist",
    ),
    (
        "the owner-data scan reads the files it enumerates",
        "tests/test_no_owner_data.py",
        """    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True""",
        """    return False""",
        "test_the_scanner_finds_leaks_that_are_really_there",
    ),
    (
        "the owner-data scan actually matches",
        "tests/test_no_owner_data.py",
        """            if pattern.search(line):""",
        """            if False:""",
        "test_the_scanner_finds_leaks_that_are_really_there",
    ),
    (
        "the owner-data scan covers the whole repository",
        "tests/test_no_owner_data.py",
        """    for rel in files:
        if rel in allowed:""",
        """    for rel in files:
        if not rel.startswith("src/"):
            continue
        if rel in allowed:""",
        "test_the_scanner_finds_leaks_that_are_really_there",
    ),
    (
        "each owner-data pattern catches its own sample",
        "tests/test_no_owner_data.py",
        """    "a macOS home directory": re.compile(r"/Users/[a-z][a-z0-9._-]*/", re.I),""",
        """    "a macOS home directory": re.compile(r"/Users/x[a-z0-9._-]*/", re.I),""",
        "test_no_tracked_file_contains_owner_data",
    ),
    # --- E4 vacuity pass 2: canonical JSON ---
    #
    # `ensure_ascii=True` is what makes the committed blob valid UTF-8 when the
    # transcript is not. Both of these survived pass 2, alone and together,
    # because the only fixture with bad bytes put them in block text — and
    # `to_canonical` emits `content_sha256`, never the text. The fixture now
    # carries them in `cwd` and `gitBranch`, which the blob does write.
    (
        "canonical JSON escapes non-ASCII",
        "records.py",
        """        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")""",
        """        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("ascii")""",
        "test_canonical_json_is_ascii_and_reparses_after_surrogates",
    ),
    (
        "canonical JSON is ASCII end to end",
        "records.py",
        """        obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")""",
        """        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8", "surrogateescape")""",
        "test_canonical_json_is_ascii_and_reparses_after_surrogates",
    ),
    # --- E4 vacuity pass 2: the index is not base64 ---
    #
    # The image branch and the generic elision cap are two independent defences
    # and pass 2 removed each of them without a red test. The single 50,000-char
    # fixture covered neither: deleting the branch let `_scrub` answer
    # "[50000 chars elided]", which satisfied every substring assertion by
    # accident, and raising the cap was invisible because no non-image fixture
    # was ever big enough to reach it. The adapter is addressed by repo-relative
    # path because a bare name resolves inside the package root.
    (
        "an image payload never reaches the index",
        "src/gitmemory/adapters/claude_code.py",
        # The branch is disabled rather than deleted, so the block falls through
        # to the unknown-block path exactly as it did when pass 2 removed it.
        """        elif kind == "image":""",
        """        elif False:""",
        "test_image_payload_is_not_inlined_into_the_index",
    ),
    (
        "the elision cap is a cap",
        "src/gitmemory/adapters/claude_code.py",
        """_ELIDE_OVER = 1024""",
        """_ELIDE_OVER = 1_000_000_000""",
        "test_a_long_string_in_an_unknown_block_is_elided_at_the_cap",
    ),
    # --- E4 vacuity pass 2: the untrusted session id ---
    #
    # `session_id` arrives in a hook payload. The charset guard is the only
    # thing between that payload and a glob, and pass 2 deleted it with the
    # whole suite green — the fixture had nothing on disk that a guard-less run
    # could find, so eleven hostile ids all returned None for the wrong reason.
    #
    # `_glob.escape` is a genuine EQUIVALENT mutant and is deliberately not
    # listed: the charset the guard admits contains no glob metacharacter, so
    # removing the escape changes no answer while the guard stands. That is
    # defence in depth, and the honest statement is that its value is
    # conditional on the guard being wrong — which is exactly why the guard
    # itself now has a row. Remove both and `find_session("*")` returns an
    # arbitrary other project's transcript. [E4, vacuity pass 2: L3, D3]
    (
        "an untrusted session id is charset-checked",
        "src/gitmemory/adapters/claude_code.py",
        """    if not _SESSION_ID_RE.match(session_id or ""):
        return None""",
        """    if not session_id:
        return None""",
        "test_find_session_refuses_a_hostile_id",
    ),
    (
        # The docstring on `newest` names this exact defect, and until L4 the
        # test that cited it looped inside one process — where set iteration
        # order never changes, so the tie-break never had to do anything.
        "an mtime tie is broken by path, not by set order",
        "src/gitmemory/adapters/claude_code.py",
        """            return (os.path.getmtime(p), p)""",
        """            return (os.path.getmtime(p), 0)""",
        "test_find_session_breaks_mtime_ties_deterministically",
    ),
    (
        # The shim and the watcher agree on a filename and on nothing else.
        # Change the separator and `_event_of` returns "", so a PreCompact
        # stops forcing — caught by six siblings, but not by the test named
        # after the shim until A2 added a line for it.
        "the shim and the watcher agree on the record name",
        "hook/gitmemory-hook.sh",
        """B="$H/spool/${P}-${E}\"""",
        """B="$H/spool/${P}_${E}\"""",
        "test_a_compaction_fired_through_the_real_shim_lands_in_a_real_commit",
    ),
    # ======================================================================= #
    # E5 standalone review: the derive fix list.
    #
    # Fourteen behaviours in derive.py could be deleted with the whole suite
    # green, including two of the module's three self-declared rules. Every row
    # below is one of those, or one of the nine defects found beside them. The
    # numbers in brackets are findings in docs/reviews/E5-derive-standalone-review.md.
    # ======================================================================= #
    (
        "ideas returns the number it was asked for",
        "derive.py",
        """            with numpy.errstate(invalid="raise", divide="raise"):
                chosen_sentences = summarizer(document, count)""",
        """            with numpy.errstate(invalid="raise", divide="raise"):
                chosen_sentences = summarizer(document, DEFAULT_IDEAS)""",
        "test_ideas_returns_the_number_asked_for",
    ),
    (
        "the --ideas flag reaches the summariser",
        "derive.py",
        """            payload_ideas = ideas(session, count=count)""",
        """            payload_ideas = ideas(session)""",
        "test_the_ideas_flag_reaches_the_artifact",
    ),
    (
        # Rule 3 of the module docstring — "rebuildable and diffable" — had
        # nothing holding it: swapping the serializer for pretty-printed,
        # insertion-ordered JSON left 800 tests green, so `derived/` could start
        # churning on every rebuild and only a human reading a diff would know.
        # The property is exact bytes, so one extra byte is the whole mutant.
        "derived bytes are canonical_json's bytes and nothing else",
        "derive.py",
        """    data = canonical_json(payload)""",
        """    data = canonical_json(payload) + b'\\n'""",
        "test_artifacts_are_canonical_json_on_disk",
    ),
    (
        "every generation is derived, not only the newest",
        "derive.py",
        """    for stored in store.sessions(resolved):""",
        """    for stored in store.sessions(resolved)[-1:]:""",
        "test_every_generation_is_derived_not_only_the_newest",
    ),
    (
        "the counts the CLI prints are the counts on disk",
        "derive.py",
        """        stats.ideas += len(payload_ideas["ideas"])""",
        """        stats.ideas += 0""",
        "test_the_counts_stats_reports_are_the_counts_on_disk",
    ),
    (
        "a mark carries the byte offset of its event",
        "derive.py",
        """                "byte_offset": event.byte_offset,""",
        """                "byte_offset": 0,""",
        "test_a_mark_carries_the_offset_and_the_id_of_the_event_it_stands_for",
    ),
    (
        "a mark carries the id of its event",
        "derive.py",
        """                "event_id": event.event_id,""",
        """                "event_id": "",""",
        "test_a_mark_carries_the_offset_and_the_id_of_the_event_it_stands_for",
    ),
    (
        # No adapter hands `timeline` unsorted input today, which is exactly why
        # deleting both sorts changed nothing. The test builds a Session by hand.
        "the timeline sorts its marks by bytes",
        "derive.py",
        """    marks = sorted(session.events, key=lambda e: (e.byte_offset, e.seq, e.event_id))""",
        """    marks = session.events""",
        "test_the_timeline_sorts_its_own_inputs_by_bytes",
    ),
    (
        "the timeline sorts its turns by bytes",
        "derive.py",
        """    turns = sorted(session.turns, key=lambda t: (t.byte_offset, t.seq))""",
        """    turns = session.turns""",
        "test_the_timeline_sorts_its_own_inputs_by_bytes",
    ),
    (
        # The tail is what makes summing `turns_since` equal `turns`; omitting it
        # when it is empty is the plausible bug, and it was unpinned.
        "the tail mark is emitted even when it is empty",
        "derive.py",
        """    out.append(
        {
            "kind": "tail",""",
        """    if len(turns) - i:
        out.append(
            {
            "kind": "tail",""",
        "test_the_tail_is_emitted_even_when_there_is_nothing_after_the_last_event",
    ),
    (
        "thinking stays out of the prose stream",
        "derive.py",
        """            if block.kind == "text" and block.text.strip():""",
        """            if block.kind in ("text", "thinking") and block.text.strip():""",
        "test_thinking_is_not_admitted_to_the_prose_stream",
    ),
    (
        "an artifact is published by rename, never written in place",
        "derive.py",
        """    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".deriving-", suffix=".json")""",
        """    fd, tmp = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), path""",
        "test_a_failed_publish_leaves_neither_a_partial_artifact_nor_a_temp",
    ),
    (
        "a failed publish unlinks its own temp",
        "derive.py",
        """            os.unlink(tmp)""",
        """            pass""",
        "test_a_failed_publish_leaves_neither_a_partial_artifact_nor_a_temp",
    ),
    (
        # The comment at the cursor walk exists to justify this line. The
        # committed attribution test separates its duplicate sentences by three
        # others, so the cursor was always already past the first copy.
        "the cursor advances past a sentence it has matched",
        "derive.py",
        # The leading newline disambiguates: the copy inside the `while` scan is
        # indented four further spaces, so a bare match would hit both.
        "\n            cursor += 1\n",
        "\n",
        "test_an_adjacent_duplicate_sentence_is_attributed_to_its_own_block",
    ),
    # --- E5:3 redaction at the boundary DESIGN.md says is redacted ---
    (
        "a sentence carrying a key is not ranked",
        "derive.py",
        """            if _leaks(text.encode("utf-8", "surrogatepass")):""",
        """            if False:""",
        "test_a_key_quoted_in_prose_never_reaches_derived",
    ),
    (
        "the single door into derived/ is gated",
        "derive.py",
        """    leaks = _leaks(data, os.path.basename(path))""",
        """    leaks = []""",
        "test_the_write_door_refuses_a_secret_no_matter_who_built_the_payload",
    ),
    # --- E5:4 an environment fault is not N pieces of bad data ---
    (
        "a missing extra fails the build instead of skipping every generation",
        "derive.py",
        """    # Before the loop, not inside it: see `_sumy`. [E5:4]
    _sumy()
    _sweep_temps(resolved)""",
        """    _sweep_temps(resolved)""",
        "test_a_missing_derive_extra_fails_the_build_instead_of_skipping_everything",
    ),
    # --- E5:5 the cap that binds on the axis that costs ---
    (
        "the word budget binds",
        "derive.py",
        """            if words_ranked + len(words) > MAX_WORDS:""",
        """            if False:""",
        "test_a_terminator_free_block_is_capped_by_words_not_by_sentences",
    ),
    (
        # Both cap tests monkeypatch their constant, so without this row
        # `MAX_WORDS = 1_000_000_000` passes every test in the file — the same
        # self-defeating shape the E4 elision-cap test had before L2.
        "the caps are the numbers their ceiling was measured against",
        "derive.py",
        """MAX_WORDS = 50_000""",
        """MAX_WORDS = 1_000_000_000""",
        "test_the_caps_are_the_values_their_comment_was_measured_against",
    ),
    # --- E5:6 the same clamp index.search has ---
    (
        "a negative idea count means none, not almost all",
        "derive.py",
        """    count = max(count, 0)""",
        """    count = int(count)""",
        "test_a_negative_ideas_count_yields_no_ideas_rather_than_almost_all",
    ),
    # --- E5:2 and E5:7 a failure costs its own generation and nothing else ---
    (
        "a write failure is caught per generation",
        "derive.py",
        """        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data""",
        """        except ValueError as exc:  # noqa: BLE001 - a segment run is untrusted data""",
        "test_a_write_failure_costs_one_generation_and_leaves_nothing_torn",
    ),
    (
        "a skipped generation loses its stale artifacts",
        "derive.py",
        """            shutil.rmtree(out, ignore_errors=True)""",
        """            pass""",
        "test_a_generation_that_stops_parsing_loses_its_stale_artifacts",
    ),
    # --- E5:8 the third temp shape, swept and ignored ---
    (
        "a killed write's temp is swept by the next build",
        "derive.py",
        """            os.unlink(stray)""",
        """            pass""",
        "test_a_half_written_artifact_is_swept_by_the_next_build",
    ),
    (
        "a killed write's temp cannot be committed",
        "gitrepo.py",
        """derived/*/*/*/.deriving-*
""",
        """""",
        "test_derived_is_committed_rather_than_ignored",
    ),
    # --- E5:9 derived/ inherits the store's permission stance ---
    (
        "derived/ directories are owner-only",
        "derive.py",
        """    store._mkdir(parent)""",
        """    os.makedirs(parent, exist_ok=True)""",
        "test_derived_directories_are_owner_only",
    ),
    # --- E5:10 prose LexRank finds no signal in ---
    (
        # The first fix for this guessed the condition was "no content words"
        # and was wrong: two sentences sharing no vocabulary have every idf at
        # exactly log(1), which is the same zero matrix with content words in
        # every sentence. Asking numpy is what makes the guard exact.
        "a degenerate ranking is detected rather than written as NaN",
        "derive.py",
        """            with numpy.errstate(invalid="raise", divide="raise"):""",
        """            with contextlib.nullcontext():""",
        "test_prose_with_no_rankable_word_ranks_nothing_and_warns_about_nothing",
    ),
    # ======================================================================= #
    # E5: the decision extractor.
    #
    # `decisions` is the one function in the tree with its own pre-registered
    # gate, and the gate is not a substitute for these rows: it is scored on a
    # synthetic corpus, so an extractor can be perfect there and hold none of
    # the properties below. Each row reverts one of them.
    # ======================================================================= #
    (
        "a user constraint is read as a directive",
        "derive.py",
        """        if _CONTRAST.search(text) or _PROHIBIT.search(text):""",
        """        if False:""",
        "test_a_user_constraint_is_a_directive",
    ),
    (
        "a course change is read as a reversal",
        "derive.py",
        """        if _SWITCH.search(text) or _CONTRAST.search(text) or _PIVOT.search(text):""",
        """        if False:""",
        "test_an_assistant_changing_course_is_a_reversal",
    ),
    (
        # The one-sided fire, which is the whole failure mode: a transcript is
        # mostly plans and mostly instructions, so an extractor that accepts one
        # half of the pair labels half the session.
        "one half of the pair is enough for a reversal",
        "derive.py",
        """        if _ABANDON.search(text) and (_ADOPT.search(text) or _COMMIT.search(text)):""",
        """        if _ABANDON.search(text) or _ADOPT.search(text) or _COMMIT.search(text):""",
        "test_ordinary_conversation_yields_no_decisions",
    ),
    (
        # Retries are excluded structurally — they carry no abandonment — rather
        # than by a rule of their own. This puts "again" in the abandonment
        # class, which is what "repeating it is giving it up" would look like.
        "repeating something is not abandoning it",
        "derive.py",
        r"""    r"|step(?:s|ped|ping)? away from|no longer [\w-]+)\b",""",
        r"""    r"|step(?:s|ped|ping)? away from|again|no longer [\w-]+)\b",""",
        "test_retrying_a_failed_command_is_not_a_reversal",
    ),
    (
        "the decision list outlives the call that built it",
        "derive.py",
        """    out: list[Decision] = []
    for turn, block in _prose(session):""",
        """    out: list[Decision] = decisions.__dict__.setdefault("seen", [])
    for turn, block in _prose(session):""",
        "test_an_empty_session_has_no_decisions",
    ),
    (
        "the same rule stated twice is two nodes, not one",
        "derive.py",
        """            out.append(Decision(kind, block.block_id))""",
        """            if not any(d.kind == kind for d in out):
                out.append(Decision(kind, block.block_id))""",
        "test_the_same_sentence_twice_yields_two_distinct_source_refs",
    ),
    (
        # The provenance floor. A turn id resolves to something real, which is
        # what makes this the plausible version of getting it wrong: the node
        # still looks traceable and no longer names the bytes it was read from.
        "a decision names its block, not its turn",
        "derive.py",
        """            out.append(Decision(kind, block.block_id))""",
        """            out.append(Decision(kind, turn.turn_id))""",
        "test_every_decision_names_a_block_that_exists_in_the_session",
    ),
    (
        "decisions come back in the order they occur",
        "derive.py",
        """            out.append(Decision(kind, block.block_id))
    return out""",
        """            out.append(Decision(kind, block.block_id))
    return out[::-1]""",
        "test_decisions_come_back_in_the_order_they_occur",
    ),
    (
        "who said it is what decides which label it gets",
        "derive.py",
        """    if role == "user":
        # A substitution frame carries both sides in one phrase;""",
        """    if role in ("user", "assistant"):
        # A substitution frame carries both sides in one phrase;""",
        "test_the_same_sentence_is_labelled_by_who_said_it",
    ),
    (
        "a back-reference is a restatement, not a new decision",
        "derive.py",
        """        _BACKREF.match(text)""",
        """        False""",
        "test_restating_an_agreed_rule_is_not_a_new_decision",
    ),
    (
        "naming the alternatives is not picking one",
        "derive.py",
        """        or _DELIBERATION.search(text)""",
        """        or False""",
        "test_weighing_two_approaches_is_not_choosing_between_them",
    ),
    (
        "a past switch is reported, not made",
        "derive.py",
        """        or _RETROSPECTIVE.search(text)""",
        """        or False""",
        "test_narrating_an_old_switch_is_not_making_one",
    ),
    (
        "a decision is immutable once it is read off the block",
        "derive.py",
        """@dataclass(frozen=True, slots=True)""",
        """@dataclass(slots=True)""",
        "test_a_decision_is_frozen_and_hashable",
    ),
    # ======================================================================= #
    # E5: the guards as classes.
    #
    # The rows above revert a rule. These revert a *widening*: each one puts a
    # guard back to the handful of phrasings one corpus happened to contain,
    # which is the shape the extractor failed its first held-out run in. They
    # are the only rows in the file that can distinguish a rule from a lookup
    # table, because the guard still fires on the corpus either way.
    # ======================================================================= #
    (
        "a repair is a repair whoever typed it",
        "derive.py",
        """        or _REPAIR.search(text)""",
        """        or (role == "assistant" and _REPAIR.search(text))""",
        "test_a_typo_correction_from_the_user_is_not_a_directive",
    ),
    (
        # The adverb slot. Without it the frame is a fixed two-word sequence and
        # every citation with a word inside it reads as a fresh instruction.
        "a citation may have words inside it",
        "derive.py",
        r'''    rf"as(?: (?:i|we|you|they|it))?{_MID} {_SAYING}\b"''',
        r'''    rf"as(?: (?:i|we|you|they|it))? {_SAYING}\b"''',
        "test_a_citation_keeps_its_shape_when_words_are_added",
    ),
    (
        # The head-verb class, cut back to the five that appear in the dev dump.
        "any verb of saying opens a citation",
        "derive.py",
        r"""_SAYING = (
    r"(?:noted|noting|mentioned|mentioning|stated|stating|said|say|says|saying"
    r"|discussed|discussing|agreed|agreeing|decided|deciding|established"
    r"|covered|covering|explained|explaining|asked|asking|requested|requesting"
    r"|specified|specifying|flagged|flagging|pointed out|indicated|indicating"
    r"|emphasi[sz]ed|stressed|highlighted|underlined|described|outlined"
    r"|set out|laid out|spelled out|spelt out|wrote|written|told you|put it"
    r"|observed|remarked|reiterated|repeated|confirmed|clarified|warned"
    r"|instructed|directed|insisted|required|advised|suggested|promised)"
)""",
        r'''_SAYING = r"(?:noted|mentioned|stated|said|discussed)"''',
        "test_a_citation_keeps_its_shape_when_words_are_added",
    ),
    (
        "asking to remember is restating",
        "derive.py",
        r'''    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)\b"
    r"|(?:please )?(?:do ?n[o']t|never) forget\b"''',
        r'''    r"|remember(?:,| that\b)"''',
        "test_being_asked_to_remember_a_rule_is_not_a_new_rule",
    ),
    (
        "any verb of comparing leaves the set open",
        "derive.py",
        r'''    r"|(?:choos|select|pick|decid|deliberat|debat|compar|evaluat|assess|mull"
    r"|agonis|agoniz|wonder|think)\w* (?:\w+ )?(?:between|among|amongst|over"
    r"|about whether|whether)"''',
        r'''    r"|(?:choos|decid|pick|deliberat)ing between"''',
        "test_an_unsettled_comparison_is_not_a_decision",
    ),
    (
        "a disjunction is two candidates, not a choice",
        "derive.py",
        r"""    r"|\beither\b[^.;:!?]{1,60}\bor\b",""",
        r"""    r"",""",
        "test_an_unsettled_comparison_is_not_a_decision",
    ),
    (
        # The frame, not the tense: "FYI" and "for context" announce a report
        # whatever the clause behind them looks like.
        "background is announced by its framing",
        "derive.py",
        r"""    r"|for (?:context|background|history)|as (?:context|background)"
    r"|fyi|fwiw|just so you know|for what it'?s worth)\b",""",
        r"""    r")\b",""",
        "test_background_framing_is_not_an_instruction",
    ),
    (
        "an opinion may be hedged",
        "derive.py",
        r'''    rf" (?:(?:\w+ly|quite|even|much|all that|so|too|just) )?{_MIND}\b"''',
        r'''    rf" {_MIND}\b"''',
        "test_a_hedged_opinion_is_still_an_opinion",
    ),
    (
        "any verb of believing reports rather than orders",
        "derive.py",
        r"""_MIND = (
    r"(?:think|believe|know|knew|see|saw|feel|felt|care|mind|want|wanted|wish"
    r"|remember|recall|follow|understand|get|got|buy|tell|reckon|suppose|guess"
    r"|imagine|expect|agree|worry|bother|notice|reproduce|repro|like|love|hate"
    r"|trust|doubt|fancy|mean|intend|fully (?:get|follow)|quite (?:see|follow))"
)""",
        r'''_MIND = r"(?:think|believe|know|see|feel|care|mind|want|remember|get)"''',
        "test_a_hedged_opinion_is_still_an_opinion",
    ),
    (
        # The one-auxiliary difference between "we have never used it" and "we
        # never use it", which is the difference between a memory and a rule.
        "the perfect reports, the present orders",
        "derive.py",
        r'''    r"|\b(?:i|we)(?:'?ve| have|'?d| had) never\b"''',
        r'''    r""''',
        "test_reporting_never_having_seen_it_is_not_forbidding_it",
    ),
    (
        "an attitude is held, not forbidden",
        "derive.py",
        r"""    r"|\b(?:i|we)(?:'?ve| have| had|'?d) no (?:\w+ )?"
    r"(?:view|opinion|preference|objection|idea|clue|issue|problem|feelings?"
    r"|thoughts?|comment|complaint|doubt|memory|recollection|experience"
    r"|visibility|insight|say|stake|context|sense)\b",""",
        r"""    r"",""",
        "test_having_no_opinion_is_not_forbidding_one",
    ),
    (
        # The recall side. A stop list matched on a prefix deletes "no
        # time-based tests" because "no time" is a formula, and nothing on the
        # dev fixture notices.
        "a stop-listed noun is a whole word",
        "derive.py",
        r'''    r"(?![\w-]))[\w-]+\b"''',
        r'''    r"))[\w-]+\b"''',
        "test_a_compound_noun_is_still_a_prohibition",
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
    """Run every mutant, or only those whose name contains an argument.

    The filter exists because a full pass is 146 mutants x two suite runs, which
    is an hour — long enough that adding one row and checking it used to mean
    either waiting for the other 145 or trusting the new one untested.
    """
    wanted = sys.argv[1:]
    selected = [m for m in MUTANTS if not wanted or any(w.lower() in m[0].lower() for w in wanted)]
    if wanted and not selected:
        print(f"no mutant matches {wanted!r}")
        return 2
    bad = []
    for name, filename, find, replace, test in selected:
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
    print(f"\n{len(selected) - len(bad)}/{len(selected)} caught by their intended test")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
