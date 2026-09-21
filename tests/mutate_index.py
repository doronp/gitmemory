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
        "test_an_arm_whose_dependency_is_absent_is_skipped_with_a_reason",
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
        "        owners.extend([block.block_id] * len(sentences))",
        "        owners.extend([block.kind] * len(sentences))",
        "test_every_idea_names_a_block_that_exists_in_the_session",
    ),
    (
        "attribution by first occurrence instead of a cursor walk",
        "derive.py",
        """        cursor = 0
        for chosen in summarizer(document, count):
            text = str(chosen)
            while cursor < len(texts) and texts[cursor] != text:
                cursor += 1
            if cursor >= len(texts):  # pragma: no cover - not a subsequence
                break
            picked.append({"text": text, "source_ref": owners[cursor], "rank": len(picked)})
            cursor += 1""",
        """        for chosen in summarizer(document, count):
            text = str(chosen)
            cursor = texts.index(text)
            picked.append({"text": text, "source_ref": owners[cursor], "rank": len(picked)})""",
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
        "        room = MAX_SENTENCES - len(owners)",
        "        room = MAX_SENTENCES",
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
