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
        '''return " OR ".join(''',
        '''return " ".join(''',
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
        "results not deduped to turns",
        "index.py",
        "GROUP BY b.turn_id",
        "GROUP BY b.rowid",
        "test_a_turn_is_returned_once_however_many_of_its_blocks_match",
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
        '''{"tool_use": "tool_use", "tool_result": "tool_result"}''',
        '''{"tool_use": "tool_result", "tool_result": "tool_result"}''',
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
        "index built in place, not renamed over",
        "index.py",
        '    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), '
        'prefix=".building-", suffix=".db")',
        "    fd, tmp = os.open(target, os.O_CREAT | os.O_WRONLY), target",
        "test_a_failed_build_never_replaces_a_working_index",
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
        "                agent=_text(man.get(\"agent\")),",
        "                agent=_safe(man[\"agent\"], \"agent\"),",
        "test_a_hostile_manifest_field_cannot_opt_a_segment_run_out_of_the_seam_scan",
    ),
]


def run(args: list[str]) -> bool:
    """True when pytest is green."""
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *args], cwd=ROOT).returncode == 0


def main() -> int:
    bad = []
    for name, filename, find, replace, test in MUTANTS:
        path = SRC / filename
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
