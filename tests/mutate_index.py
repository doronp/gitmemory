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

# Generous against the ~1 minute the offline suite takes, and short against the
# thirty-three minutes a wedged mutant cost before there was a bound at all.
SUITE_TIMEOUT = 600

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
        "    hits.dropped = dropped\n    return hits",
        "    hits.dropped = 0\n    return hits",
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
        "the inserts are outside the per-generation guard again",
        "index.py",
        '        db.execute("SAVEPOINT generation")',
        '        db.execute("SAVEPOINT generation") if False else None',
        "test_a_block_sqlite_refuses_costs_its_own_generation_and_no_other",
    ),
    (
        # Without the rollback the savepoint is bookkeeping: the failed
        # generation's turn rows stay in the database and the index reports a
        # generation it does not hold.
        "a generation that failed halfway leaves its rows behind",
        "index.py",
        '            db.execute("ROLLBACK TO generation")',
        "            pass",
        "test_a_block_sqlite_refuses_costs_its_own_generation_and_no_other",
    ),
    (
        # The digest is held in `chunk` for the same reason the rows are held in
        # the savepoint. Folding it in as it goes puts a failed generation's
        # turns into the hash of a build that does not contain them.
        "a failed generation still reaches the digest",
        "index.py",
        "        for part in chunk:\n            digest.update(part)",
        "        for part in chunk:\n            pass",
        "test_the_same_bytes_captured_in_two_passes_are_not_the_same_index",
    ),
    (
        "blocks are not counted",
        "index.py",
        "        blocks += len(rows)",
        "        blocks += 0",
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
        "    _sweep_partials(parent, keep=target)",
        "    pass",
        "test_a_partial_index_left_by_a_kill_is_swept",
    ),
    (
        # The sweep gained a second job — deleting indexes named for a schema
        # version that is no longer current — and `keep`, which spares the
        # build's own target. It runs before the rename, so on a build that
        # succeeds `keep` changes nothing; what it protects is the previous
        # index when the build *fails*, which is the promise temp-plus-rename
        # makes. Two earlier versions of this row's test asserted the
        # successful path and were green without `keep`.
        "a failed build sweeps away the index it was replacing",
        "index.py",
        "    _sweep_partials(parent, keep=target)",
        "    _sweep_partials(parent)",
        "test_a_failed_build_leaves_the_index_it_was_replacing",
    ),
    (
        "an index from a superseded schema is left on disk forever",
        "index.py",
        "            stale = superseded and int(superseded.group(1)) < SCHEMA",
        "            stale = False",
        "test_an_index_from_an_older_schema_is_swept",
    ),
    (
        # The three rows above are the sweep doing its job. These two are the
        # sweep doing it to things that are not its own. [E7]
        "a newer schema's index is swept as a leftover",
        "index.py",
        "            stale = superseded and int(superseded.group(1)) < SCHEMA",
        "            stale = superseded and superseded.group(1) != str(SCHEMA)",
        "test_an_index_from_a_newer_schema_is_not_mistaken_for_a_leftover",
    ),
    (
        "the sweep matches the prefix, not the name the build writes",
        "index.py",
        "            if (_PARTIAL_RE.match(entry.name) or stale) and entry.is_file():",
        '            if (entry.name.startswith(".building-") or stale) and entry.is_file():',
        "test_the_sweep_deletes_what_the_build_writes_and_not_what_it_finds",
    ),
    (
        "every reader opens the index for writing, creating it if absent",
        "index.py",
        '        db = sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True)',
        "        db = sqlite3.connect(path)",
        "test_a_reader_cannot_create_or_scribble_on_an_index",
    ),
    (
        "a hostile `meta` runs for as long as it likes",
        "index.py",
        "        with _step_budget(db):\n            row = db.execute",
        "        if True:\n            row = db.execute",
        "test_a_meta_that_is_not_a_table_cannot_run_forever",
    ),
    (
        # `contextlib.nullcontext` and not deleting the `with`: the body stays
        # indented, so the mutant is the same program minus the lock rather
        # than a re-indentation the harness would have to score as BROKEN.
        "two builds in one directory sweep each other's temp away",
        "index.py",
        "    with store._lockfile(os.path.join(parent, LOCK_NAME)):",
        "    with contextlib.nullcontext():",
        "test_a_second_build_waits_instead_of_sweeping_the_first_one_away",
    ),
    (
        "the journals beside a partial index are left behind",
        "index.py",
        r'_PARTIAL_RE = re.compile(r"\A\.building-.+\.db(-journal|-wal|-shm)?\Z")',
        r'_PARTIAL_RE = re.compile(r"\A\.building-.+\.db\Z")',
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
        "records.py",
        "    return UNSAFE.sub(",
        "    return text or UNSAFE.sub(",
        "test_a_terminal_escape_in_a_transcript_does_not_reach_the_terminal",
    ),
    (
        "a lone surrogate survives the one transform that removes it",
        "records.py",
        '        text.encode("utf-8", "replace").decode("utf-8"),',
        "        text,",
        "test_a_non_utf8_byte_in_a_transcript_does_not_reach_the_artifacts",
    ),
    (
        "the zero-width characters are not in the unsafe class",
        "records.py",
        '    "\\u061c\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069"',
        '    "\\u061c\\u200e-\\u200f\\u202a-\\u202e\\u2066-\\u2069"',
        "test_a_terminal_escape_in_a_transcript_does_not_reach_the_artifacts",
    ),
    (
        "the committed artifacts are written without the render transform",
        "derive.py",
        "    payload = _renderable(payload)",
        "    payload = payload",
        "test_a_terminal_escape_in_a_transcript_does_not_reach_the_artifacts",
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
        # The anchor used to span the import *and* the check, with a blank line
        # asserted between them — so the comment that grew in the gap silently
        # retired the control. The mechanism is one line: nothing may turn an
        # ImportError from inside derive into "not written yet", and `hasattr`
        # only swallows AttributeError. Anchor on the line that does the work,
        # not on its neighbours. [round 3, finding 3]
        '    if not hasattr(derive, "decisions"):',
        '    try:\n        _has = hasattr(derive, "decisions")\n'
        "    except ImportError:\n        _has = False\n    if not _has:",
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
        "        sqlite3.Error,\n    ) as exc:",
        "    ) as exc:",
        "test_search_rejects_nothing_it_can_reach_the_database_with",
    ),
    (
        # `-k` is `type=int` and a Python int has no width; SQLite's is 64-bit.
        # `OverflowError` is not an `OSError`, a `ValueError` or a
        # `sqlite3.Error`, so a typo printed a traceback carrying absolute
        # install paths. [E7 carry-in]
        "an int too big for sqlite tracebacks out of the CLI",
        "__main__.py",
        "        OverflowError,  # `recall -k 99999999999999999999`: SQLite's int is 64-bit\n",
        "",
        "test_a_k_too_big_for_sqlite_is_an_error_message_not_a_traceback",
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
        # Rotten for some time and reported as SKIP, which is one line in an
        # hour-long pass nobody reads to the end. `_prose` grew a second value —
        # it yields `turn, block` so `decisions` can see the role — and the
        # anchor went on naming the old shape, so this control held nothing.
        # `test_mutate_harness.py` now fails on an unresolvable anchor at commit
        # time instead of whispering SKIP an hour in. [round 3, finding 3]
        """    for turn in session.turns:
        for block in turn.blocks:
            if block.kind == "text" and block.text.strip():
                yield turn, block""",
        """    pairs = {
        b.text: (t, b)
        for t in session.turns
        for b in t.blocks
        if b.kind == "text" and b.text.strip()
    }
    for body in set(pairs):
        yield pairs[body]""",
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
        # Re-anchored: the row used to span the loop *and* the summary line
        # after it, so adding a counter to the summary retired the control. The
        # `stats =` line stays in the anchor only because `_index` has a
        # byte-identical loop and something has to say which one this is — the
        # summary that churns is out of it now. [round 3, finding 3]
        "a skipped generation stops being reported",
        "__main__.py",
        """    stats = derive.build(args.home, count=args.ideas)
    for line in stats.skipped:
        print(f"skipped {line}", file=sys.stderr)""",
        "    stats = derive.build(args.home, count=args.ideas)",
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
        """    leaks = _leaks(data, os.path.basename(path)) or _leaks(
        json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass"),
        os.path.basename(path),
    )""",
        """    leaks = []""",
        "test_the_write_door_refuses_a_secret_no_matter_who_built_the_payload",
    ),
    (
        # Only the second scan is deleted, and the first one is a real gate, so
        # this mutant still refuses every ASCII secret. What it lets through is
        # the one shape the escape hides: a non-ASCII character immediately
        # before the token, which `ensure_ascii` turns into a `\\uXXXX` ending in
        # a hex digit and so eats the `\\b` the rule anchors on. [E7 carry-in]
        "the door scans only the escaped bytes, so one accent walks a token past it",
        "derive.py",
        """ or _leaks(
        json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass"),
        os.path.basename(path),
    )""",
        "",
        "test_the_write_door_does_not_open_for_a_non_ascii_character",
    ),
    # --- E5:4 an environment fault is not N pieces of bad data ---
    (
        # Re-anchored: the row spanned the comment, the call and the next
        # statement, so the local import that grew between them retired the
        # control. The call is the whole mechanism. [round 3, finding 3]
        "a missing extra fails the build instead of skipping every generation",
        "derive.py",
        "    _sumy()\n",
        "",
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
        """            try:
                shutil.rmtree(out)
            except FileNotFoundError:""",
        """            try:
                pass
            except FileNotFoundError:""",
        "test_a_generation_that_stops_parsing_loses_its_stale_artifacts",
    ),
    (
        "a symlink in the chain is already a directory",
        "store.py",
        "            mode = os.lstat(path).st_mode",
        "            mode = os.stat(path).st_mode",
        "test_a_symlinked_store_directory_is_refused_before_anything_is_written",
    ),
    (
        "a symlinked derived leaf is followed, not refused",
        "store.py",
        "        if stat.S_ISDIR(mode):\n            break\n        kind =",
        "        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):\n            break\n        kind =",
        "test_a_symlinked_derived_leaf_is_written_through_not_followed",
    ),
    (
        "a rollback that could not run is reported as if it had",
        "derive.py",
        """            except OSError as rm:
                stats.skipped.append(f"{stored.key}: rollback left artifacts behind: {rm!r}")""",
        """            except OSError:
                pass""",
        "test_a_rollback_that_cannot_run_says_so",
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
        """        _BACKREF.search(text)""",
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
        r'''    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)"
    r"(?=\s*[:,;—–]|\s+(?:that|to|we|you|i|it|this|these|the|our|your|what"
    r"|when|how|why|never|no|always)\b)"
    r"|(?:please )?(?:do ?n[o']t|never) forget\b"''',
        r'''    r"|remember(?:,| that\b)"''',
        "test_being_asked_to_remember_a_rule_is_not_a_new_rule",
    ),
    (
        "any verb of comparing leaves the set open",
        "derive.py",
        r'''    r"|(?:choos|select|pick|decid)\w* (?:\w+ )?(?:between|among|amongst"
    r"|about whether|whether)"
    r"|(?:deliberat|debat|compar|evaluat|assess|mull|agonis|agoniz|wonder|think"
    r"|ponder)\w* (?:\w+ )?(?:between|among|amongst|over|about whether|whether)"''',
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
        r'''    rf" (?:{_ADV} )?{_MIND}\b"''',
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
        r'''    rf"|\b(?:i|we)(?: {_ADV})?(?:'?ve| have|'?d| had)(?: {_ADV})? never\b"''',
        r'''    r""''',
        "test_reporting_never_having_seen_it_is_not_forbidding_it",
    ),
    (
        "an attitude is held, not forbidden",
        "derive.py",
        r"""    rf"|\b(?:i|we)(?: {_ADV})?(?:'?ve| have| had|'?d) no (?:\w+ )?"
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
        # `(?:)` rather than deleting the group: dropping `(?![\w-]` leaves an
        # unbalanced paren, the module stops importing, and the whole suite goes
        # red at collection — which the harness used to score as a clean CAUGHT
        # with perfect attribution. A mutant has to be a program. [E5]
        r'''    r"(?![\w-]))[\w-]+\b"''',
        r'''    r"(?:))[\w-]+\b"''',
        "test_a_compound_noun_is_still_a_prohibition",
    ),
    # --- the graph emitter ---------------------------------------------------
    (
        "a decision naming a block from elsewhere becomes a blank node",
        "graph.py",
        "            if d.source_ref not in text_of:",
        "            if False:",
        "test_a_decision_naming_a_block_from_somewhere_else_is_an_error",
    ),
    (
        # The first version of this row swapped `setdefault` for `__setitem__`
        # and SURVIVED, which is the row being wrong rather than the test: both
        # write the same key, and two transcripts that share a `block_id` share
        # the text by construction, so first-wins and last-wins are the same
        # bytes. Dedup lives in the dict *key*, so that is what the mutant has
        # to break. [E5]
        "a replayed turn is drawn as two decisions",
        "graph.py",
        "                d.source_ref,",
        "                (session.source_path, d.source_ref),",
        "test_the_same_block_replayed_into_a_second_transcript_is_one_node",
    ),
    (
        "the emission depends on the order the store was walked",
        "graph.py",
        '        "nodes": [nodes[i] for i in sorted(nodes)],',
        '        "nodes": list(nodes.values()),',
        "test_emission_does_not_depend_on_the_order_the_store_was_walked",
    ),
    (
        "a decision repeated back to back draws an edge to itself",
        "graph.py",
        "            if a.source_ref != b.source_ref:",
        "            if True:",
        "test_a_decision_is_never_joined_to_itself",
    ),
    (
        "a label is cut mid-word instead of on a space",
        "graph.py",
        '    cut = flat.rfind(" ", 0, LABEL_CHARS)',
        "    cut = -1",
        "test_a_label_is_one_line_and_ends_on_a_word",
    ),
    (
        "a whitespace-only block gets a blank label",
        "graph.py",
        "        return NO_TEXT",
        "        return flat",
        "test_a_block_with_no_visible_text_gets_a_caption_not_a_blank_node",
    ),
    (
        # The Gemini review's finding 1, as a control: the first version sent
        # every keyword to `extraction`, so a graphify option died on a
        # TypeError naming a function the caller never called.
        "a graphify option is swallowed by the extractor call",
        "graph.py",
        "    return build_from_json(extraction(sessions, extract=extract), **kw)",
        "    return build_from_json(extraction(sessions, extract=extract, **kw))",
        "test_a_graphify_option_reaches_graphify",
    ),
    (
        # Scanning the label instead of the block: the prefix is clean whenever
        # the key sits past `LABEL_CHARS`, so this publishes the node and, if the
        # cut lands mid-key, a fragment of the key with it.
        "the secret scan runs on the label rather than on the block",
        "graph.py",
        '    if _leaks(text.encode("utf-8", "surrogatepass")):\n        return REDACTED\n'
        '    flat = _WS.sub(" ", text).strip()',
        '    flat = _WS.sub(" ", text).strip()\n'
        '    if _leaks(flat[:LABEL_CHARS].encode("utf-8", "surrogatepass")):\n'
        "        return REDACTED",
        "test_a_key_past_the_cut_still_redacts_the_whole_label",
    ),
    (
        # The emitter is the first thing to carry raw block text through the
        # write door, and the door's answer to a secret is to refuse the whole
        # artifact. Without the label rule a leaked key in one decision costs
        # that generation its ideas and its timeline too.
        "a leaked key in one decision block skips the whole generation",
        "graph.py",
        "        return REDACTED",
        "        pass",
        "test_a_key_in_a_decision_block_costs_its_label_and_not_the_generation",
    ),
    (
        "the decision graph is emitted but never published",
        "derive.py",
        '            _write(os.path.join(out, "graph.json"), payload_graph)',
        "            pass",
        "test_the_decision_graph_is_published_beside_the_ideas",
    ),
    (
        # Counting anything but the payload that was written is how a stat comes
        # to disagree with the file it describes.
        "the decision count is the number of transcripts, not of nodes",
        "derive.py",
        '        stats.decisions += len(payload_graph["nodes"])',
        "        stats.decisions += 1",
        "test_the_decision_count_stats_reports_is_the_count_on_disk",
    ),
    (
        # A published graph.json that is not a function of the session: the file
        # exists, it is canonical, it rebuilds identically, and it is empty. Both
        # of the other guards on this artifact pass with this mutant in place.
        "the published graph is not the one this session produced",
        "derive.py",
        "            payload_graph = graph.extraction([session])",
        '            payload_graph = {"nodes": [], "edges": []}',
        "test_the_decision_graph_is_published_beside_the_ideas",
    ),
    # --- the dashboard -------------------------------------------------------
    (
        # The 2.79x over-count. A sum over turns instead of over requests is the
        # single most plausible way this view goes wrong, and the number it
        # produces is wrong in the direction that flatters the project.
        "cumulative usage is summed per turn instead of per request",
        "index.py",
        "        PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC",
        "        PARTITION BY agent, request_id, turn_id ORDER BY generation DESC, seq DESC",
        "test_repeated_cumulative_usage_is_billed_once",
    ),
    (
        # HIGH-1. `seq` is per generation (`enumerate(kept)`, restarting at 0),
        # so ordering by it alone picks whichever generation was *longer* — and a
        # compaction that drops turns makes that the superseded one.
        "a fork bills whichever generation was longer",
        "index.py",
        "        PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC",
        "        PARTITION BY agent, request_id ORDER BY seq DESC",
        "test_a_fork_bills_the_newest_generation_not_the_longest",
    ),
    (
        # HIGH-2. Usage is cumulative, so within one generation the last turn of
        # a request holds the total. Dropping the tie-break leaves it to insert
        # order, which is right today by accident and is not a rule.
        "the first turn of a request is billed instead of the last",
        "index.py",
        "        PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC",
        "        PARTITION BY agent, request_id ORDER BY generation DESC, seq ASC",
        "test_two_turns_of_one_request_bill_the_last_one_not_the_first",
    ),
    (
        # MEDIUM-1. A subagent transcript is a separate session and the same API
        # call. Partitioning by session bills it once per file it appears in.
        "a subagent file double-bills its parent's request",
        "index.py",
        "        PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC",
        "        PARTITION BY agent, session_id, request_id ORDER BY generation DESC, seq DESC",
        "test_a_subagent_file_does_not_double_bill_its_parents_request",
    ),
    (
        # MEDIUM-2. Cumulative usage with no request id cannot be deduplicated;
        # counting it in full is the 2.79x error the view exists to avoid.
        "usage with no request id is billed as if it were a request",
        "index.py",
        "    WHERE usage <> '{}' AND request_id IS NOT NULL",
        "    WHERE usage <> '{}'",
        "test_usage_with_no_request_id_is_shown_as_unbilled_not_billed_or_dropped",
    ),
    (
        # MEDIUM-3. `<synthetic>` is Claude Code's marker for a turn produced
        # without an API call. `billable_usage` has always excluded it.
        "a synthetic turn is billed as a real request",
        "index.py",
        "      AND role = 'assistant' AND COALESCE(model, '') <> '<synthetic>'",
        "      AND role = 'assistant'",
        "test_a_synthetic_turn_is_not_billed",
    ),
    (
        # The other half of MEDIUM-2/3: excluded usage that is *also* invisible
        # makes `dash_spend` disagree with the store by an unknowable amount.
        "excluded usage vanishes instead of being named",
        "index.py",
        "  AND (request_id IS NULL OR role <> 'assistant' OR model = '<synthetic>')",
        "  AND 0",
        "test_spend_plus_unbilled_accounts_for_every_usage_block",
    ),
    (
        # MEDIUM-5. The digest was fed block rows only, so an assistant turn
        # that emitted no content — a tool call, a cancelled reply — fed it
        # nothing while carrying a usage block worth any number of tokens.
        "a turn with no blocks leaves no trace in the digest",
        "index.py",
        '                chunk.append(_insert(db, "turns", _turn_row(stored, turn)))',
        '                _insert(db, "turns", _turn_row(stored, turn))',
        "test_a_turn_that_produced_no_blocks_still_reaches_the_digest",
    ),
    (
        # The other half of MEDIUM-5: segmentation is part of what the index is,
        # and two captures of one turn each hold the same blocks as one capture
        # of two turns while `dash_contiguity` shows a different shape.
        "how a store was segmented leaves no trace in the digest",
        "index.py",
        "            chunk.append(\n"
        "                _generation_row(db, stored, turns=len(session.turns), "
        "blocks=len(rows), reason=None)\n"
        "            )",
        "            _generation_row(db, stored, turns=len(session.turns), "
        "blocks=len(rows), reason=None)",
        "test_the_same_bytes_captured_in_two_passes_are_not_the_same_index",
    ),
    (
        # MEDIUM-9 / HIGH-3's neighbour. `strftime` and `substr` both return
        # *something* for junk: the first NULL by luck, the second a nine-
        # character prefix of the garbage, bucketed as if it were a day.
        "an unparseable timestamp is bucketed as a day",
        "index.py",
        "FROM turns WHERE datetime(ts) IS NOT NULL GROUP BY day, agent ORDER BY day;",
        "FROM turns WHERE ts IS NOT NULL GROUP BY day, agent ORDER BY day;",
        "test_growth_buckets_by_utc_day_and_drops_unreadable_stamps",
    ),
    (
        "growth buckets by the local clock rather than UTC",
        "index.py",
        "SELECT substr(datetime(ts), 1, 10) AS day, agent,",
        "SELECT substr(ts, 1, 10) AS day, agent,",
        "test_growth_buckets_by_utc_day_and_drops_unreadable_stamps",
    ),
    (
        # MEDIUM-4. A fork replays turns, so this is the store's size and not a
        # conversation's length — and it was labelled `turns`, which is the
        # second number. The rename is the fix; the caption carries the caveat.
        "the stored-turn count is labelled as a turn count",
        "index.py",
        "       SUM(turns)       AS stored_turns,  -- per-generation sum; see dash_corpus",
        "       SUM(turns)       AS turns,",
        "test_a_fork_makes_the_stored_count_exceed_the_conversation",
    ),
    (
        "the stored-block count is labelled as a block count",
        "index.py",
        "       SUM(blocks)                 AS stored_blocks,",
        "       SUM(blocks)                 AS blocks,",
        "test_a_fork_makes_the_stored_count_exceed_the_conversation",
    ),
    (
        # HIGH-3. The panel is where a reader looks for what is *not* known, so
        # a gap the front page admits and this view omits reads as measured.
        "the injection-cost refusal is dropped from the panel",
        "index.py",
        "UNION ALL SELECT 'injection cost', 'NOT BUILT',",
        "UNION ALL SELECT 'hook latency, again', 'NOT BUILT',",
        "test_the_unmeasured_panel_names_what_is_not_known",
    ),
    (
        "a replayed generation is billed again",
        "index.py",
        "CREATE VIEW dash_requests AS\nSELECT agent, session_id,",
        "CREATE VIEW dash_requests AS\nSELECT agent, session_key AS session_id,",
        "test_a_replayed_generation_is_not_billed_twice",
    ),
    (
        "a generation that would not parse leaves no trace",
        "index.py",
        "            digest.update("
        "_generation_row(db, stored, turns=0, blocks=0, reason=repr(exc)))",
        "            pass",
        "test_a_generation_that_would_not_parse_is_still_counted",
    ),
    (
        "the cache share counts output tokens in its denominator",
        "index.py",
        "           / SUM(input_tokens + cache_write_tokens + cache_read_tokens), 1",
        "           / SUM(input_tokens + cache_write_tokens + cache_read_tokens\n"
        "                 + output_tokens), 1",
        "test_cache_share_is_a_share_of_what_went_in",
    ),
    (
        # A share of nothing is unanswerable, not zero. The deleted `NULLIF` was
        # a no-op — SQLite returns NULL for `x/0` — so the thing worth pinning is
        # not the guard but the NULL: a well-meant `COALESCE(..., 0.0)` here
        # would report a model that sent no input at all as 0% cached, which is
        # a number where there is no number.
        "a zero denominator reads as 0% rather than NULL",
        "index.py",
        "       ROUND(\n"
        "           100.0 * SUM(cache_read_tokens)\n"
        "           / SUM(input_tokens + cache_write_tokens + cache_read_tokens), 1\n"
        "       ) AS cache_read_pct",
        "       COALESCE(ROUND(\n"
        "           100.0 * SUM(cache_read_tokens)\n"
        "           / SUM(input_tokens + cache_write_tokens + cache_read_tokens), 1\n"
        "       ), 0.0) AS cache_read_pct",
        "test_a_share_of_nothing_is_null_not_zero",
    ),
    (
        "the dashboard binds to every interface by default",
        "dashboard.py",
        'HOST = "127.0.0.1"',
        'HOST = "0.0.0.0"  # noqa: S104',
        "test_the_command_is_immutable_and_loopback",
    ),
    (
        # Renamed. It was "the dashboard opens the index writable", which
        # overstated the flag: Datasette executes queries read-only either way.
        # `--immutable` adds the promise that nothing *else* writes, which is
        # what lets SQLite cache — and what LOW-1's digest line exists for. The
        # name is the only record of the decision, so it says what the flag does.
        # [E6 review, NIT-2]
        "the index is opened without the no-other-writer promise",
        "dashboard.py",
        '        "--immutable",',
        '        "--load-extension=",',
        "test_the_command_is_immutable_and_loopback",
    ),
    (
        # NIT-3. The number has a stated reason in a comment beside it.
        "the dashboard takes datasette's default port",
        "dashboard.py",
        "PORT = 8081",
        "PORT = 8001",
        "test_the_port_is_not_datasettes_default",
    ),
    (
        # NIT-3. The front page is where HIGH-3's claim lived; dropping it from
        # the metadata takes the refusals off the page with it.
        "the front page never reaches datasette",
        "dashboard.py",
        '        "description_html": _DESCRIPTION.strip(),',
        "",
        "test_the_metadata_lands_on_the_database_it_was_written_for",
    ),
    (
        # NIT-4. The coverage check read `type = 'view'` only.
        "a table is added to the schema with no caption",
        "index.py",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
        "CREATE TABLE undescribed (x TEXT);",
        "test_every_table_the_index_defines_is_described_too",
    ),
    (
        # LOW-3. "Tokens by model" with every model in one row.
        "the spend panel merges every model into one row",
        "index.py",
        "FROM dash_requests GROUP BY agent, model;",
        "FROM dash_requests GROUP BY agent;",
        "test_spend_keeps_one_row_per_model",
    ),
    (
        "a forked conversation is counted as two conversations",
        "index.py",
        "       COUNT(DISTINCT session_id)  AS sessions,",
        "       COUNT(session_id)           AS sessions,",
        "test_corpus_counts_conversations_once_and_generations_every_time",
    ),
    (
        "the stored byte count is a placeholder",
        "index.py",
        '            "bytes": stored.size,',
        '            "bytes": 0,',
        "test_corpus_counts_conversations_once_and_generations_every_time",
    ),
    (
        # LOW-3. `unparseable: 1` with no reason beside it is a number nobody
        # can act on, and this column is the only route the reason has to a page.
        "why a generation could not be read never reaches the page",
        "index.py",
        "       group_concat(skip_reason, ' | ') AS skipped",
        "       NULL AS skipped",
        "test_contiguity_names_why_a_generation_could_not_be_read",
    ),
    (
        "the model a turn used is not recorded",
        "index.py",
        '        "model": turn.model,',
        '        "model": None,',
        "test_a_turn_row_carries_the_fields_the_views_group_by",
    ),
    (
        "a subagent turn is indistinguishable from its parent's",
        "index.py",
        '        "is_sidechain": int(turn.is_sidechain),',
        '        "is_sidechain": 0,',
        "test_a_turn_row_carries_the_fields_the_views_group_by",
    ),
    (
        "usage on a non-assistant turn is thrown away",
        "index.py",
        '        "usage": canonical_json(turn.usage).decode(),',
        '        "usage": canonical_json(turn.usage).decode()'
        ' if turn.role == "assistant" else "{}",',
        "test_a_user_turn_carries_no_usage_and_is_not_billed",
    ),
    (
        "a missing datasette is installed instead of reported",
        "dashboard.py",
        '    if shutil.which("datasette"):\n        return args',
        "    if True:\n        return args",
        "test_uvx_is_the_fallback_not_the_default",
    ),
    (
        # MEDIUM-6. `uvx datasette` resolves against PyPI at run time and
        # installs whatever it serves that minute, during what the user ran as a
        # read-only look at a local file. The range is what makes it reviewable.
        "the uvx fallback runs an unpinned datasette",
        "dashboard.py",
        'UVX_SPEC = "datasette<2"',
        'UVX_SPEC = "datasette"',
        "test_the_command_is_immutable_and_loopback",
    ),
    (
        # LOW-5. `--immutable` constrains writes and says nothing about reads.
        # Datasette otherwise offers the whole `.db` — every transcript byte,
        # unredacted — as a single file link.
        "the whole store is downloadable from the dashboard",
        "dashboard.py",
        '        "--setting",\n        "allow_download",\n        "off",',
        "",
        "test_the_command_is_immutable_and_loopback",
    ),
    (
        # MEDIUM-8. Tested against the default value rather than the set, so
        # `--host localhost` warned "not loopback", which is false — and a
        # warning that cries wolf on the safe spelling is one people learn to
        # click past before they ever meet 0.0.0.0.
        "a loopback alias is warned about as if it were public",
        "__main__.py",
        "    if args.host not in dashboard.LOOPBACK:",
        "    if args.host != dashboard.HOST:",
        "test_a_bind_outside_loopback_warns_and_a_loopback_alias_does_not",
    ),
    (
        "a public bind is not warned about at all",
        "__main__.py",
        "    if args.host not in dashboard.LOOPBACK:",
        "    if False:",
        "test_a_bind_outside_loopback_warns_and_a_loopback_alias_does_not",
    ),
    (
        # LOW-1. Datasette holds the file open and `--immutable` entitles SQLite
        # to cache it, while `build` renames a fresh file over the path — so a
        # page left open across a rebuild serves the old inode, confidently.
        "the page does not say which build it is serving",
        "dashboard.py",
        '        print(f"content {_digest(path)} — restart after a rebuild;',
        '        print(f"content unknown — restart after a rebuild;',
        "test_the_start_up_line_names_the_build_being_served",
    ),
    (
        "an unreadable index takes the server down with it",
        "dashboard.py",
        "    except Exception:  # noqa: BLE001 - a start-up caption must not block the server\n"
        '        return "unknown"',
        "    except Exception:\n        raise",
        "test_the_start_up_line_survives_an_index_it_cannot_read",
    ),
    # ======================================================================= #
    # E5: the guards against hand-written chat.
    #
    # A reviewer ran the extractor over two probes written in registers the
    # corpus generator does not produce — multi-sentence blocks, hedges, dated
    # anecdotes. Four mechanisms failed, and every row below reverts one of the
    # fixes to the shape it had when it failed. They matter more than most rows
    # here because the dev fixture scores 1.0000 either way: none of these can
    # be noticed by the gate.
    # ======================================================================= #
    (
        # The restatement guard was `.match`, so the frame had to be the first
        # thing in the block. Chat puts it in the second sentence or the fifth,
        # and each one came back as a fresh directive.
        "a restatement may open any sentence, not only the block",
        "derive.py",
        """        _BACKREF.search(text)""",
        """        _BACKREF.match(text)""",
        "test_a_restatement_is_one_wherever_its_sentence_starts",
    ),
    (
        # The other half of unanchoring: a connective opens a unit, and a
        # concession is a unit-opener with no punctuation of its own. Without it
        # "sorry to nag, but as we agreed" has its comma one word too early.
        "a concession opens a unit the punctuation does not",
        "derive.py",
        r'''_OPENS_A_UNIT = rf"(?:\A|[.!?;:,\n)]|[—–]|\s-\s|\b{_CONCESSIVE}\b)"''',
        r'''_OPENS_A_UNIT = r"(?:\A|[.!?;:,\n)]|[—–]|\s-\s)"''',
        "test_a_restatement_is_one_wherever_its_sentence_starts",
    ),
    (
        # Unanchoring made two ordinary words dangerous, and this is the cost of
        # getting that wrong: a scheduler's reminder job stops being a directive.
        "a reminder is a frame and not a noun",
        "derive.py",
        r'''    r"remind(?:er|ing)(?=\s*[:,;—–]|\s+(?:that|to)\b)"''',
        r'''    r"remind(?:er|ing)\b"''',
        "test_a_reminder_is_a_frame_and_not_a_noun",
    ),
    (
        # The same hole on the other word, and worse, because this project
        # reports a recall figure in half its sentences about itself.
        "recall is a frame and not a noun",
        "derive.py",
        r'''    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)"
    r"(?=\s*[:,;—–]|\s+(?:that|to|we|you|i|it|this|these|the|our|your|what"
    r"|when|how|why|never|no|always)\b)"''',
        r'''    r"|(?:please |just )?(?:remember|recall|bear in mind|keep in mind)\b"''',
        "test_a_reminder_is_a_frame_and_not_a_noun",
    ),
    (
        # Finding 2, the frame it was reported against. Without the slot the
        # hedge misses, the negation is left standing, and `_PROHIBIT` files an
        # opinion as a rule.
        "a hedge may sit between the subject and the auxiliary",
        "derive.py",
        r'''    rf"\b(?:i|we)(?: {_ADV})? ?{_NOT}"''',
        r'''    rf"\b(?:i|we) ?{_NOT}"''',
        "test_a_hedge_is_a_hedge_on_either_side_of_the_auxiliary",
    ),
    (
        # The third position, which a list of whole contracted forms cannot
        # hold: "I'm genuinely not convinced" puts the adverb inside `_NOT`.
        "a hedge may sit inside the negated auxiliary",
        "derive.py",
        r'''    rf"|am|'?m|'?re|'?ve|'?d|'?ll) ?(?:{_ADV} )?n[o']t)"''',
        r'''    r"|am|'?m|'?re|'?ve|'?d|'?ll) ?n[o']t)"''',
        "test_a_hedge_is_a_hedge_on_either_side_of_the_auxiliary",
    ),
    (
        # The same asymmetry in the certainty branch. Checking one frame and not
        # its siblings is how the slot came to exist in one place only.
        "the certainty frame takes the same two slots",
        "derive.py",
        r'''    rf"|\b(?:i|we)(?: {_ADV})? ?{_NOT} (?:{_ADV} )?"''',
        r'''    rf"|\b(?:i|we) ?{_NOT} "''',
        "test_a_hedge_is_a_hedge_on_either_side_of_the_auxiliary",
    ),
    (
        "the perfect frame takes the same two slots",
        "derive.py",
        r'''    rf"|\b(?:i|we)(?: {_ADV})?(?:'?ve| have|'?d| had)(?: {_ADV})? never\b"''',
        r'''    r"|\b(?:i|we)(?:'?ve| have|'?d| had) never\b"''',
        "test_a_hedge_is_a_hedge_on_either_side_of_the_auxiliary",
    ),
    (
        # `-ly` is productive, and a list of the six that turned up is a list of
        # six. The citation slot and all four hedge frames read it.
        "the adverb class is productive, not the literals that turned up",
        "derive.py",
        r'''    r"(?:\w+ly|quite|rather|somewhat|even|much|all that|at all|so|too|just|ever"''',
        r'''    r"(?:clearly|explicitly|repeatedly|specifically|originally|initially"
    r"|quite|rather|somewhat|even|much|all that|at all|so|too|just|ever"''',
        "test_a_citation_keeps_its_shape_when_words_are_added",
    ),
    (
        # Finding 3. One verb reached the frame, and the frame means the same
        # with any verb that ranks one thing above another.
        "preference is not one verb",
        "derive.py",
        r"""_PREFER = (
    r"(?:prefer\w*|favou?r\w*|choos\w*|chose|chosen|pick(?:s|ed|ing)?"
    r"|select\w*|opt(?:s|ed|ing)?|prioriti[sz]\w*|privileg\w*|recommend\w*"
    r"|advocat\w*|rank\w*)"
)""",
        r'''_PREFER = r"(?:prefer\w*)"''',
        "test_preferring_one_thing_to_another_is_not_one_verb",
    ),
    (
        # The collision that made Finding 3 a no-op until it was split: with the
        # two verb groups merged, a verb of choosing takes "over" as deliberation
        # and "pick Parquet over CSV" is a decision taken, filed as one deferred.
        "after a verb of choosing, `over` names the loser",
        "derive.py",
        r'''    r"|(?:choos|select|pick|decid)\w* (?:\w+ )?(?:between|among|amongst"
    r"|about whether|whether)"
    r"|(?:deliberat|debat|compar|evaluat|assess|mull|agonis|agoniz|wonder|think"
    r"|ponder)\w* (?:\w+ )?(?:between|among|amongst|over|about whether|whether)"''',
        r'''    r"|(?:choos|select|pick|decid|deliberat|debat|compar|evaluat|assess|mull"
    r"|agonis|agoniz|wonder|think|ponder)\w* (?:\w+ )?(?:between|among|amongst"
    r"|over|about whether|whether)"''',
        "test_preferring_one_thing_to_another_is_not_one_verb",
    ),
    (
        # Finding 4. A list of whole phrasings is how the singular went missing:
        # "years ago" was admitted and "a year ago" was not.
        "a measured distance back is a quantity and a unit",
        "derive.py",
        r'''    rf"|{_TIME_UNIT} ago|{_QTY} {_TIME_UNIT} (?:ago|back)"''',
        r'''    r"|(?:years|months|weeks|days|hours|releases|sprints|quarters) ago"''',
        "test_a_block_that_dates_itself_to_the_past_is_a_report",
    ),
    (
        # The other half of Finding 4: the past named as a period rather than as
        # a distance. "Back when I joined", "yesterday", "in older releases".
        "the past can be named as a period, not only as a distance",
        "derive.py",
        # Re-anchored: the row used to end on the `in <det> <artefact>` frame,
        # which round 4 moved out to `_RETRO_STAGE` because it points both ways.
        # The three lines left are the period half of the finding and the whole
        # of what this control was ever about. [round 4]
        r'''    r"|back when|when (?:we|i) (?:first|originally|initially|started|began)"
    r"|yesterday|the other (?:day|week|night|morning|afternoon)"
    r"|started (?:out|off)|in the early days"''',
        r'''    r""''',
        "test_a_block_that_dates_itself_to_the_past_is_a_report",
    ),
    (
        # A clause-final "earlier" is the commonest adjunct of the lot and the
        # list reached every position of it except that one.
        "a clause-final `earlier` dates the block too",
        "derive.py",
        r'''    r"|earlier (?:in|today|this|on|we|i|you|they|the|that|it)|earlier(?=[,;])"''',
        r'''    r"|earlier (?:in|today|this|on|we|i|you|they|the|that|it)"''',
        "test_a_block_that_dates_itself_to_the_past_is_a_report",
    ),
    (
        # "last Tuesday" dates a clause exactly as "last week" does, and a list
        # of generic units reaches none of the named days or months.
        "a named day dates a clause as a generic unit does",
        "derive.py",
        r'''    rf"|last (?:year|month|week|quarter|sprint|time|release|cycle|session"
    rf"|iteration|night|day|{_CALENDAR})"''',
        r'''    r"|last (?:year|month|week|quarter|sprint|time|release|cycle|session"
    r"|iteration|night|day)"''',
        "test_a_block_that_dates_itself_to_the_past_is_a_report",
    ),
    # --- round 4: the pair review --------------------------------------------
    (
        # The largest of the round. Without the tense cue "We never had that
        # problem." is a standing prohibition, and the dev gate says 1.0000
        # either way.
        "past-tense `never` is read as a prohibition",
        "derive.py",
        r'''    rf"\b(?:never(?!\s+{_PAST}\b)"''',
        r'''    r"\b(?:never"''',
        "test_a_report_of_what_never_happened_is_not_a_rule",
    ),
    (
        # The exception, in the direction that costs recall: with the copula
        # branch gone the tense cue eats the passive rule as well, because
        # "allowed" is past-shaped wherever it sits.
        "a passive rule is read as a report of the past",
        "derive.py",
        r'''    r"|(?:(?<=\bis )|(?<=\bare ))never"''',
        r'''    r""''',
        "test_a_report_of_what_never_happened_is_not_a_rule",
    ),
    (
        # The other half of the same finding. `avoided` is the report in a
        # different verb, and putting it back is a one-character edit.
        "`we avoided threads` is read as a rule against threads",
        "derive.py",
        r'''    r"|shall not|should ?n[o']t|no longer|avoid(?:s|ing)?"''',
        r'''    r"|shall not|should ?n[o']t|no longer|avoid(?:s|ed|ing)?"''',
        "test_a_report_of_what_never_happened_is_not_a_rule",
    ),
    (
        # The stage frame deciding on its own, in the direction it was wrong in
        # before the round: every rule scoped to an early stage is suppressed.
        "an early stage always dates a report",
        "derive.py",
        "        or (_RETRO_STAGE.search(text) and _PAST_FINITE.search(text))",
        "        or _RETRO_STAGE.search(text)",
        "test_an_early_stage_dates_a_report_and_scopes_a_rule",
    ),
    (
        # And in the opposite direction, which is what deleting "the first" from
        # the frame would have done: the report becomes a directive. Both
        # mutants are one line and the test has to fail on each, or the conjunct
        # is half held.
        "an early stage never dates a report",
        "derive.py",
        "        or (_RETRO_STAGE.search(text) and _PAST_FINITE.search(text))",
        "        or False",
        "test_an_early_stage_dates_a_report_and_scopes_a_rule",
    ),
    (
        "a question about a rule is read as the rule",
        "derive.py",
        "        or _QUESTION.search(text)",
        "        or False",
        "test_a_question_about_a_rule_is_not_the_rule",
    ),
    (
        # Shape or mark. Keying on the mark alone suppresses the tag question —
        # "use Parquet instead of CSV, ok?" — which is a directive with a
        # question mark on the end, not a question.
        "the question mark alone makes a block a question",
        "derive.py",
        r"""    r"\A\s*(?:who|what|when|where|why|how|which|whose|whom"
    r"|do(?! not\b)|does|did|is|are|was|were|am|will|would|shall|should"
    r"|can|could|may|might|must|have|has|had|any(?:one|body)?)\b"
    r"[^.!?]*\?\s*\Z",""",
        r"""    r"\?\s*\Z",""",
        "test_a_question_about_a_rule_is_not_the_rule",
    ),
    (
        "the protasis of a conditional is read as the rule",
        "derive.py",
        '    text = _PROTASIS.sub(" ", text)',
        "    text = text",
        "test_a_condition_is_not_the_rule_it_carries",
    ),
    (
        # The cut has to reach a trailing protasis too. Stopping at the comma
        # handles "if X, never Y" and leaves "the database hangs if we never
        # release the lock" exactly as it was.
        "only a fronted condition is cut out",
        "derive.py",
        r"""    r"|whenever|when)\b[^,.;:!?]*(?:,|(?=[.;:!?]|\Z))",""",
        r"""    r"|whenever|when)\b[^,.;:!?]*,",""",
        "test_a_condition_is_not_the_rule_it_carries",
    ),
    (
        "`same request` is read as a back-reference",
        "derive.py",
        r'''    r"|same (?:as (?:before|above|last time)|rule|point|thing|deal)\b"''',
        r'''    r"|same (?:as (?:before|above|last time)|rule|point|thing|request|deal)\b"''',
        "test_the_same_request_is_not_the_same_rule",
    ),
    # --- the probes ----------------------------------------------------------
    (
        # The flattering direction. Folding the ceiling into the class score
        # makes the extractor look worse at shapes it never claimed, and the
        # opposite mutant — scoring everything as ceiling — makes it look
        # perfect at all of them. The split is the measurement.
        "a documented ceiling is scored against the class",
        "bench/probes.py",
        "        if group in ASIDE:",
        "        if False:",
        "test_a_ceiling_miss_is_not_counted_as_a_class_miss",
    ),
    (
        # Probe C's 8 contested items sit apart from its 32 for the same reason
        # A and B's ceilings do. Dropping `hard` out of the aside set moves C's
        # denominator from 32 to 40 without moving the floor, so 14/32 would
        # quietly become 16/40 and the published number would stop meaning what
        # the document says it means. [probe C]
        "the contested items are folded into probe C's 32",
        "bench/probes.py",
        'ASIDE = frozenset({"ceiling", "hard"})',
        'ASIDE = frozenset({"ceiling"})',
        "test_the_probe_score_has_not_regressed",
    ),
    (
        # The same mutant as "a restatement may open any sentence", aimed at a
        # different test on purpose. That row proves `test_derive.py` holds the
        # fix; this one proves the *probe floor* does — that the pinned 26 and
        # 27 are load-bearing rather than a number nobody's change can trip.
        # The dev fixture scores 1.0000 with this mutant in place, which is the
        # whole argument for having the probes at all.
        "the probe floor does not notice a guard regressing",
        "derive.py",
        """        _BACKREF.search(text)""",
        """        _BACKREF.match(text)""",
        "test_the_probe_score_has_not_regressed",
    ),
    # --- E7 security round: what the gate attests to ----------------------- #
    (
        # The three sources of `gate` are three different questions. This one
        # asks what `git push` transmits, and it is the only one that can see a
        # credential that was committed and then deleted.
        "the gate reads the object graph and scans none of it",
        "redact.py",
        """        seen_objects = True
        findings += scan_bytes(data, f"history:{safe_path(label)}")""",
        """        seen_objects = True""",
        "test_a_credential_deleted_from_the_worktree_still_blocks_the_push",
    ),
    (
        "push builds the gate without the history source",
        "__main__.py",
        ", objects=gitrepo.pushable_objects(home)",
        "",
        "test_a_credential_deleted_from_the_worktree_still_blocks_the_push",
    ),
    (
        # `spool/` and `index/` are gitignored and are full of exactly the bytes
        # a detector fires on. Scanning them refuses a push over bytes that were
        # never being sent, and there is no override flag anywhere in the CLI.
        "the worktree source stops asking git which files are its own",
        "gitrepo.py",
        '"ls-files", "-z", "--cached", "--others", "--exclude-standard"',
        '"ls-files", "-z", "--cached", "--others"',
        "test_a_credential_in_a_gitignored_directory_does_not_block_the_push",
    ),
    (
        "the push credential's own file goes back to being committed",
        "gitrepo.py",
        """
/config.toml
\"\"\"""",
        """
\"\"\"""",
        "test_the_store_does_not_commit_the_credential_it_pushes_with",
    ),
    (
        # A `.gitignore` line does not untrack a tracked file, so without this
        # every store that predates the rule goes on committing its own push
        # credential while the fix reads as applied.
        "the ignore rule is added but the tracked copy is left tracked",
        "gitrepo.py",
        '    _git(home, "rm", "--cached", "--quiet", "--ignore-unmatch", '
        '"config.toml", check=False)\n',
        "",
        "test_init_untracks_a_config_that_an_older_store_already_committed",
    ),
    (
        "an inline credential in a remote url is allowed through",
        "redact.py",
        '    if ":" in userinfo:',
        "    if False:",
        "test_a_remote_url_password_is_never_printed",
    ),
    (
        # The over-refusing version of the same check, which is what the first
        # draft did: `https://host:8443/p.git` has no userinfo at all, and
        # `ssh://git@host:22/p` is a port behind a username. A store that cannot
        # push is as broken as one that pushes a token.
        "the userinfo check reads a port number as a password",
        "redact.py",
        '    userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""',
        '    userinfo = parts.netloc.rsplit("@", 1)[0]',
        "test_a_remote_url_password_is_never_printed",
    ),
    (
        # The finding is printed to stderr, into a terminal an agent transcribes
        # into the transcript this store then commits. A gate that prints what
        # it caught has filed a second permanent copy of it.
        "findings wear the unmasked path they were found in",
        "redact.py",
        "    label = safe_path(path)",
        "    label = path",
        "test_a_finding_whose_match_is_the_path_does_not_print_the_path",
    ),
    (
        "overlapping matches are masked one per detector",
        "redact.py",
        "        if spans and start <= spans[-1][1]:",
        "        if False:",
        "test_a_finding_whose_match_is_the_path_does_not_print_the_path",
    ),
    (
        # One false positive is a block that merely *names* a PEM header. With
        # no count, a store where the gate misfires on every block looks exactly
        # like a store full of secrets, and both look like a store with none.
        "the graph stops saying how many labels it redacted",
        "graph.py",
        '        "labels_redacted": sum(1 for n in nodes.values() if n["label"] == REDACTED),\n',
        "",
        "test_the_extraction_says_how_many_labels_it_redacted",
    ),
    # Nine confirmed misses, one row each, because each is one independently
    # deletable line in a table and the parametrised test is the only thing that
    # says which shape went missing. [E7]
    (
        "a PGP private key block is not a private key block",
        "redact.py",
        "PRIVATE KEY(?: BLOCK)?-----",
        "PRIVATE KEY-----",
        "test_the_shapes_a_transcript_actually_contains_are_seen and private_key_block",
    ),
    (
        "the browser-session slack tokens are dropped again",
        "redact.py",
        'rb"\\bxox[baprsecd]-[A-Za-z0-9-]{12,}"',
        'rb"\\bxox[baprse]-[A-Za-z0-9-]{12,}"',
        "test_the_shapes_a_transcript_actually_contains_are_seen and slack_token",
    ),
    (
        "an incoming-webhook url is not a credential",
        "redact.py",
        '    ("slack_webhook", re.compile(rb"\\bhttps://hooks\\.slack\\.com/services/'
        '[A-Za-z0-9/_+-]{20,}")),\n',
        "",
        "test_the_shapes_a_transcript_actually_contains_are_seen and slack_webhook",
    ),
    (
        "only the gcloud api key is seen, not the token gcloud prints",
        "redact.py",
        '    ("google_oauth_token", re.compile(rb"\\bya29\\.[A-Za-z0-9_-]{20,}")),\n',
        "",
        "test_the_shapes_a_transcript_actually_contains_are_seen and google_oauth_token",
    ),
    (
        "a huggingface token walks",
        "redact.py",
        '    ("huggingface_token", re.compile(rb"\\bhf_[A-Za-z0-9]{30,}\\b")),\n',
        "",
        "test_the_shapes_a_transcript_actually_contains_are_seen and huggingface_token",
    ),
    (
        # The keyword is not adjacent to the `=` in `AWS_SECRET_ACCESS_KEY=…`,
        # and `_` is a word character, so the `\\b`-anchored form saw neither
        # edge of `SECRET` nor the assignment past it.
        "the assignment rule goes back to needing the keyword against the separator",
        "redact.py",
        "            [A-Za-z0-9_.-]{0,40} [\"']? \\s* [:=] \\s*",
        "            [\"']? \\s* [:=] \\s*",
        "test_the_shapes_a_transcript_actually_contains_are_seen and assigned_secret",
    ),
    (
        # `"authorization": "Bearer …"` is how a transcript records a header.
        # The `"` between the name and the value is not `\\s`.
        "the authorization rule needs a bare colon again",
        "redact.py",
        """            rb\"\"\"(?ix) \\b authorization [\"']? \\s* [:=] \\s* [\"']?
            \\s* (?: bearer | basic ) \\s+ [A-Za-z0-9._~+/=-]{20,}\"\"\"
""",
        """            rb\"\"\"(?ix) \\b authorization \\s* : \\s*
            bearer \\s+ [A-Za-z0-9._~+/-]{20,}\"\"\"
""",
        "test_the_shapes_a_transcript_actually_contains_are_seen and bearer_header",
    ),
    (
        # `https://user:pw@` is the form this store's own `config.toml` uses and
        # the three-scheme alternation did not contain it.
        "only three url schemes may carry a password",
        "redact.py",
        '        re.compile(rb"(?i)\\b[a-z][a-z0-9+.-]*://[^\\s:@/]+:[^\\s@/]+@"),',
        '        re.compile(rb"(?i)\\b(?:postgres(?:ql)?|mysql|mongodb)://[^\\s:@/]+:[^\\s@/]+@"),',
        "test_the_shapes_a_transcript_actually_contains_are_seen and url_with_password",
    ),
    (
        # The whole tuple, not just its pattern: `jwt` is a two-line entry, and
        # deleting the `re.compile` alone leaves a one-element tuple that blows
        # up `scan_bytes` on unpacking. A mutant that crashes is credited with
        # being caught by whatever test ran into the crash first, which is
        # attribution on no evidence.
        "a jwt outside an authorization header is nothing",
        "redact.py",
        r"""    (
        "jwt",
        re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
""",
        "",
        "test_the_shapes_a_transcript_actually_contains_are_seen and jwt",
    ),
    # S1: a manifest the gate cannot read used to opt its generation out of the
    # seam scan while `push` went on printing "gate passed". Four rows, because
    # the fix is four independently deletable pieces and three of them fail
    # silently. [E7]
    (
        # The one that restores the original bug exactly: `strict` still exists,
        # `segment_groups` still asks for it, and the raise is gone.
        "an unreadable manifest goes back to being a skip",
        "store.py",
        "            if strict:",
        "            if False:",
        "test_a_manifest_the_gate_cannot_read_refuses_the_push_instead_of_passing_it",
    ),
    (
        # The same bug from the caller's side, and the one that reads most
        # innocent in a diff: the machinery is all present, nobody asks for it.
        "the gate reads the store as leniently as a reader does",
        "store.py",
        "    return [list(s.segments) for s in sessions(home, strict=True)]",
        "    return [list(s.segments) for s in sessions(home)]",
        "test_a_manifest_the_gate_cannot_read_refuses_the_push_instead_of_passing_it",
    ),
    (
        # `_Skip` exists so the two hand-written rejections leave by the same
        # door as a `JSONDecodeError`. Returning instead is how the `strict`
        # check gets added to one path and forgotten on the others.
        "a non-list segments field skips without telling the gate",
        "store.py",
        "                raise _Skip(f\"'segments' is {type(raw_segments).__name__}, not a list\")",
        "                continue",
        "test_a_manifest_the_gate_cannot_read_refuses_the_push_instead_of_passing_it "
        "and segments as a dict",
    ),
    (
        # The lenience `strict` is carved out of. A reader that raised would make
        # one half-written manifest — what a full disk leaves behind —
        # un-indexable for the whole store, which is the E2 sweep bug.
        "the readers become as strict as the gate",
        "store.py",
        "def sessions(home: str, *, strict: bool = False) -> list[Stored]:",
        "def sessions(home: str, *, strict: bool = True) -> list[Stored]:",
        "test_the_readers_still_skip_what_the_gate_refuses",
    ),
    (
        # A `JSONDecodeError` says "line 1 column 2" and names nothing. Without
        # the prefix the refusal sends an operator to `verify` with no idea which
        # of a thousand manifests to look for.
        "the refusal stops naming which manifest it cannot read",
        "store.py",
        '                raise UnreadableManifest(f"{path}: {exc}") from exc',
        "                raise UnreadableManifest(str(exc)) from exc",
        "test_the_readers_still_skip_what_the_gate_refuses",
    ),
    (
        # F3's rule reaching the channel F3 did not exist for: the refusal prints
        # a path, a generation is named after a session id, and a session id can
        # be the token.
        "the refusal prints a session id that is itself a credential",
        "__main__.py",
        '        print(f"refusing to push: {_safe_exc(exc)}", file=sys.stderr)',
        '        print(f"refusing to push: {exc}", file=sys.stderr)',
        "test_the_refusal_masks_a_session_id_that_is_itself_a_credential",
    ),
    (
        # Found by this round's own mutation run: the last mutant's captured
        # stderr read `error: <the whole path>`. An `OSError` carries the path it
        # failed on, and nobody asked to see it.
        "the top-level handler prints an exception's credential",
        "__main__.py",
        '        print(f"error: {_safe_exc(exc)}", file=sys.stderr)',
        '        print(f"error: {exc}", file=sys.stderr)',
        "test_a_credential_in_an_exception_message_is_masked_on_the_way_out",
    ),
    (
        # And the helper itself, which is where forgetting one call site would
        # otherwise still leave the other two green.
        "exception text stops being masked at all",
        "__main__.py",
        "    return redact.safe_path(str(exc))",
        "    return str(exc)",
        "test_a_credential_in_an_exception_message_is_masked_on_the_way_out",
    ),
    (
        # The third call site, and the one that fires first: a missing source
        # prints the parse failure before the error. Both lines carried the path.
        "the parse-failure warning prints the path it could not read",
        "__main__.py",
        'f"parse failed ({_safe_exc(exc)}); capturing bytes without boundaries",',
        'f"parse failed ({exc}); capturing bytes without boundaries",',
        "test_a_credential_in_an_exception_message_is_masked_on_the_way_out",
    ),
    (
        # Uncaught, it reaches the CLI's top-level handler and prints `error:`
        # with a traceback's vocabulary. The store still does not push, so this
        # one is cosmetic — but "error" and "refusing to push" are different
        # sentences to the person reading them at 3am.
        "an unreadable manifest leaves by the traceback handler",
        "__main__.py",
        "    except (store.EscapingSegment, store.UnreadableManifest) as exc:",
        "    except store.EscapingSegment as exc:",
        "test_push_names_the_unreadable_manifest_instead_of_tracebacking",
    ),
    # S4 and S12: `open` is the wrong verb for anything that is not a regular
    # file. A FIFO never finishes opening; a symlink opens the wrong bytes. [E7]
    (
        # The anchor takes the `if` and its comment with it. Deleting only the
        # two body lines leaves `if not _regular(full):` followed by a comment,
        # which is an IndentationError — and that scored CAUGHT for a round,
        # because `-x` reports a collection error as exit 1 and the BROKEN
        # branch was only reading `suite > 1`. Both halves are fixed; this is
        # the half that makes the row a program. [E7]
        "verify opens a segment before asking what it is",
        "store.py",
        "        if not _regular(full):\n"
        "            # Before the open, not inside the `except`: a FIFO does not fail to\n"
        "            # open, it never finishes opening, and this read holds the session\n"
        "            # lock. [E7]\n"
        "            out.append(f\"{rel}: segment {seg['path']} is not a regular file\")\n"
        "            return out\n",
        "",
        "test_a_fifo_named_as_a_segment_does_not_hang_verify",
    ),
    (
        "adoption rehashes a run it cannot read",
        "store.py",
        '            raise ValueError(f"segment is not a regular file: {p!r}")',
        "            pass",
        "test_a_fifo_named_as_a_segment_does_not_hang_orphan_adoption",
    ),
    (
        # `_regular` answers False for a missing path too, which is what its
        # `except OSError` is for. `True` keeps every honest store green and
        # restores both hangs.
        "the file-type check answers yes to everything",
        "store.py",
        "        return stat.S_ISREG(os.lstat(path).st_mode)",
        "        return True",
        "test_a_fifo_named_as_a_segment_does_not_hang_verify",
    ),
    (
        "the gate follows a symlink instead of reading its text",
        "redact.py",
        "    if stat.S_ISLNK(mode):\n"
        '        return os.readlink(path).encode("utf-8", "surrogateescape")\n',
        "",
        "test_the_gate_scans_a_symlinks_text_and_not_what_it_points_at",
    ),
    (
        "the gate opens whatever it is handed again",
        "redact.py",
        '    if not stat.S_ISREG(mode):\n        return b""\n',
        "",
        "test_a_fifo_in_the_gates_file_list_does_not_block_it",
    ),
    (
        # The seam scan is a second reader of the same path and had its own
        # `open`. A symlink's text is short enough to carry whole either side.
        "the seam scan opens what the file scan does not",
        "redact.py",
        "    if not stat.S_ISREG(os.lstat(path).st_mode):\n"
        "        # Link text and nothing are both short enough to carry whole.\n"
        "        data = contents(path)\n"
        "        return data[:n], data[-n:]\n",
        "",
        "test_the_seam_scan_reads_a_symlink_the_same_way_the_file_scan_does",
    ),
    (
        # The only mutation of `_deadline` that is a program rather than a
        # wedge. Removing the timer, lengthening it, or making the handler
        # return all end the same way — the FIFO open blocks and the row that
        # was meant to score the deadline hangs on it instead, which is the
        # WEDGED verdict and not a negative control. A leaked timer is the one
        # failure that still returns, and it is a real one: it fires inside
        # whatever test runs next, which reads as that test being flaky.
        "the deadline leaks its timer into the next test",
        "tests/test_store.py",
        "        signal.setitimer(signal.ITIMER_REAL, 0)\n",
        "",
        "test_the_deadline_fires_on_something_that_really_blocks",
    ),
    # ----------------------------------------------------------------- #
    # a `.git` the store did not make [E7]
    # ----------------------------------------------------------------- #
    (
        # The shape that never refused at all: `isdir` follows the link, so
        # `is_repo` says yes, `init` is skipped, and `_assert_no_foreign_config`
        # compares the foreign config against itself and passes.
        "a symlinked git dir is treated as a git dir",
        "gitrepo.py",
        "    if stat.S_ISDIR(mode):\n        return\n",
        "    if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):\n        return\n",
        "test_a_git_dir_the_store_did_not_make_is_refused_before_anything_is_written and symlink",
    ),
    (
        # `exists` follows the link, so a `.git` pointing at nothing looks
        # absent and `git init` creates a repository at the far end.
        "the ownership check follows the link before deciding",
        "gitrepo.py",
        "    if not os.path.lexists(dot):",
        "    if not os.path.exists(dot):",
        "test_a_git_dir_symlinked_to_nothing_does_not_make_a_repository_somewhere_else",
    ),
    (
        # Removing the call leaves `_assert_no_foreign_config`, which does refuse
        # the gitfile — fourth, after four `git config` calls have already
        # written into the foreign repository. The test asserts the absence of
        # those writes, which is the half the ordering buys.
        "init refuses a foreign git dir only after writing to it",
        "gitrepo.py",
        "    _assert_own_git_dir(home)\n    if not is_repo(home):",
        "    if not is_repo(home):",
        "test_a_git_dir_the_store_did_not_make_is_refused_before_anything_is_written and gitfile",
    ),
    (
        # `run` carries on after a failed `init` on purpose, so the commit is
        # where the transcript actually lands in someone else's history.
        "the commit trusts that init already refused",
        "gitrepo.py",
        '    _assert_own_git_dir(home)\n    try:\n        _git(home, "add", "--all")',
        '    try:\n        _git(home, "add", "--all")',
        "test_the_commit_refuses_the_same_git_dir_init_refused",
    ),
    # --- E7 index-F9: index/ inherits the store's permission stance --------- #
    (
        "index/ and the parents a --db path needs are owner-only",
        "index.py",
        "    store._mkdir(parent)\n    with store._lockfile(",
        "    os.makedirs(parent, exist_ok=True)\n    with store._lockfile(",
        "test_the_index_directory_is_owner_only or test_a_db_path_creates_its_parents_owner_only",
    ),
    (
        # The other half of the same branch, and they pull in opposite
        # directions: resolving too little refuses the caller's own `/tmp`,
        # resolving too much follows a symlink planted at a name only
        # gitmemory ever writes.
        "a --db path is resolved before the symlink refusal sees it",
        "index.py",
        "        parent = os.path.realpath(parent)",
        "        parent = parent",
        "test_a_db_path_through_a_symlinked_directory_is_still_allowed",
    ),
    (
        "the symlink refusal is skipped for <home>/index too",
        "index.py",
        "    if path:\n        parent = os.path.realpath(parent)",
        "    if True:\n        parent = os.path.realpath(parent)",
        "test_a_symlink_planted_at_the_index_directory_is_refused",
    ),
    # --- E7 index-F10: the cap is announced on every query, not the first --- #
    (
        # The old channel was `warnings.warn`, and this is what it did.
        "the truncation count is remembered, so only the first query says so",
        "index.py",
        "    hits.dropped = dropped\n    return hits",
        '    hits.dropped = dropped * (not getattr(search, "_said", 0))\n'
        "    search._said = 1\n"
        "    return hits",
        "test_every_over_long_query_says_so_not_just_the_first",
    ),
    (
        "recall does not mention that it dropped terms",
        "__main__.py",
        "    if hits.dropped:",
        "    if False:",
        "test_recall_says_a_query_was_truncated_before_it_says_no_matches",
    ),
]


def run(args: list[str]) -> int:
    """pytest's exit code. 0 green, 1 tests failed, >=2 it never got that far.

    `tests bench` explicitly, not pytest's configured `testpaths`: naming them
    here keeps the harness honest about what it ran even if `testpaths` changes
    under it. The corpus test is deselected by `addopts`, so this is the offline
    suite and it takes about a second. [E3]

    The exit code, not a green/red boolean, because the difference between 1 and
    2 is the difference between a mutant that was caught and one that was never
    run. See `BROKEN` in `main`. [E5]

    The timeout is not defensive tidiness, it is a repair. The offline suite is
    about a minute, and this call had no bound at all until a mutant that
    removes a file-type check met a test that names a FIFO as a segment: the
    suite blocked in `open`, under the session lock, and the whole pass sat
    there for thirty-three minutes with nothing on stdout. A harness that can
    hang is a harness that stops being run, and the verdict it owed — WEDGED —
    is the most interesting one a mutant can earn, because it says the mutant
    removed a guard against blocking and the test could only prove it by
    blocking too. `-1` because no pytest exit code is negative. [E7]
    """
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests", "bench", *args],
            cwd=ROOT,
            timeout=SUITE_TIMEOUT,
        ).returncode
    except subprocess.TimeoutExpired:
        return -1


def verdict(suite: int, intended: int, test: str) -> tuple[bool, str, str]:
    """What two pytest exit codes mean: `(caught, tag, note)`.

    Split out of the loop so it can be tested without spawning pytest twice per
    case — the harness is the thing every other negative control in this repo
    leans on, and a scoring bug here quietly converts the whole mutation record
    into decoration. [E5]
    """
    if suite == -1 or intended == -1:
        # Not caught. A test that demonstrates "this does not hang" by hanging
        # proves nothing a reader can act on, and it takes the pass down with
        # it. The row is fine; the test needs its own deadline so the mutant
        # makes it *fail*. [E7]
        which = "the suite" if suite == -1 else test
        return False, "WEDGED", f": {which} never returned; give it a deadline"
    if (suite > 1) or (intended > 1 and intended != 5):
        # pytest exits 2 when collection fails, and a mutant that stops the
        # module importing takes the whole suite down with it — including the
        # intended test, which then "fails" and is credited with having caught
        # something. That is attribution on no evidence: the mutant was never
        # run. Deleting a group out of a regex is the usual way in. Rewrite the
        # row so that the mutant is a program.
        #
        # `intended` and not just `suite`, because `-x` is on the suite run and
        # **`-x` turns a collection error into exit 1**, not 2 — so for the two
        # rounds this branch has existed it could not fire on the one shape it
        # was written for. A row whose mutant left an `if` with no body scored
        # CAUGHT: suite 1 from `-x`, intended 2 from the unbounded run, and
        # nothing in between reading either. The intended run has no `-x`, so it
        # is the one that still reports collection honestly. [E7]
        #
        # 5 is excluded because it is not an exit *above* failure, it is "no
        # tests ran", which has its own verdict two lines down.
        which = "the suite" if suite > 1 else test
        code = suite if suite > 1 else intended
        return False, "BROKEN", f": the mutant does not import ({which} exited {code})"
    if suite == 0:
        return False, "SURVIVED", ""
    if intended == 5:
        # pytest's "no tests ran". The row names a test that does not exist —
        # renamed, deleted, or mistyped — and the old green/red boolean read
        # that as green and called the mutant MISSED, which sends you looking
        # at the behaviour instead of at the row.
        return False, "NO TEST", f": nothing matches -k {test}"
    if intended == 0:
        return False, "MISSED", f": caught, but not by {test}"
    return True, "CAUGHT", f"  <- {test}"


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
        caught, tag, note = verdict(suite, intended, test)
        print(f"{tag:<9} {name}{note}")
        if not caught:
            bad.append(name)
    print(f"\n{len(selected) - len(bad)}/{len(selected)} caught by their intended test")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
