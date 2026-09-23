"""The mutation harness scores itself correctly.

`tests/mutate_index.py` is not run by pytest — it is a hand-run tool, and every
negative control in this repository rests on it. That makes its scoring rule the
one piece of code whose failure is invisible: a harness that reports CAUGHT on
no evidence turns the whole mutation record into decoration and nothing goes
red. So the rule is a pure function of two pytest exit codes, and this is it.

The case that motivated the split is `BROKEN`. A mutant that deletes a group out
of a regex leaves a module that does not compile; pytest then fails at
*collection*, every test in the suite goes red including the one named for the
behaviour, and the old green/red boolean scored that as a clean CAUGHT with
perfect attribution. One row in the index was in exactly that state.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import mutate_index as mutate
import pytest
from mutate_index import MUTANTS, ROOT, SRC, verdict

# pytest's documented exit codes, named so the cases below read as English.
GREEN, FAILED, INTERRUPTED, NO_TESTS = 0, 1, 2, 5
# Not pytest's: `run` returns it when the suite never returned at all. [E7]
TIMED_OUT = -1


def test_a_mutant_the_suite_does_not_notice_survived():
    caught, tag, _ = verdict(GREEN, GREEN, "test_whatever")
    assert (caught, tag) == (False, "SURVIVED")


def test_a_mutant_caught_by_its_own_test_is_caught():
    caught, tag, note = verdict(FAILED, FAILED, "test_whatever")
    assert (caught, tag) == (True, "CAUGHT")
    assert "test_whatever" in note


def test_a_mutant_caught_by_some_other_test_is_missed():
    """The attribution half. The suite went red, the named test did not, so the
    test written for this behaviour is decorative and the behaviour is held in
    place by something incidental."""
    caught, tag, _ = verdict(FAILED, GREEN, "test_whatever")
    assert (caught, tag) == (False, "MISSED")


def test_a_mutant_that_stops_the_module_importing_is_not_credited():
    """The whole reason this function exists. Collection failed, so the mutant
    never ran, so nothing was demonstrated about anything."""
    caught, tag, note = verdict(INTERRUPTED, INTERRUPTED, "test_whatever")
    assert (caught, tag) == (False, "BROKEN")
    assert "does not import" in note


def test_broken_is_scored_on_the_exit_codes_the_harness_actually_produces():
    """[E7] The case above passes and proved nothing, for two rounds.

    `(2, 2)` is what a collection error looks like if you reason about it from
    pytest's documentation. It is not what this harness sees. The suite run
    carries `-x`, and under `-x` a collection error exits **1 or 2 depending on
    collection order** — the case below measures all three arrangements. The
    intended run has no `-x` and reports 2 in every one of them.

    The row that mutated `store.py` produced `(1, 2)`, which fell through
    `suite > 1`, past `intended == 5`, past `intended == 0`, and landed on
    CAUGHT: a mutant that left an `if` with no body, credited to the test named
    beside it. That is this repository's own named defect — a check that passes
    for a reason unrelated to the behaviour it is named for — sitting in the
    file whose entire job is to refuse exactly that.
    """
    caught, tag, note = verdict(FAILED, INTERRUPTED, "test_whatever")
    assert (caught, tag) == (False, "BROKEN")
    assert "test_whatever" in note, "and it names the run that did not import"


def test_a_module_that_cannot_be_parsed_is_only_reported_honestly_without_x(tmp_path):
    """[E7] Why the branch above had to stop reading the suite run.

    Measured, because the guess was wrong twice. Three arrangements of the same
    two files — one unparseable, one fine — differing only in which name sorts
    first, and whether the good file is there at all:

        bad sorts first   -x -> 1    -k -> 2
        bad sorts second  -x -> 2    -k -> 2
        bad alone         -x -> 2

    So under `-x` the code depends on collection order, which for a mutation row
    means it depends on which module the mutated one happens to sit beside. The
    old branch read only that number and looked for 2; the row that mutated
    `store.py` produced 1 and was scored CAUGHT. The run without `-x` reported 2
    in every arrangement, and it is the one the verdict now trusts.

    This asserts the half the fix rests on. The `-x` spread is left in the table
    above rather than asserted, because a future pytest making it uniform would
    be a fix, not a regression, and a test that goes red on that is noise.
    """

    def run(*flags, files):
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *flags, str(tmp_path)],
            capture_output=True,
            text=True,
        )
        for name in files:
            (tmp_path / name).unlink()
        return out.returncode

    UNPARSEABLE, FINE = "def f(:\n    pass\n", "def test_whatever():\n    assert True\n"
    for bad, good in (("test_a.py", "test_z.py"), ("test_z.py", "test_a.py")):
        files = {bad: UNPARSEABLE, good: FINE}
        assert run("-k", "test_whatever", files=files) == INTERRUPTED, f"{bad} before {good}"
        assert run("-x", files=files) != GREEN, "and the suite run does at least go red"
    assert run("-k", "test_whatever", files={"test_a.py": UNPARSEABLE}) == INTERRUPTED


def test_a_row_naming_a_test_that_does_not_exist_says_so():
    """pytest exits 5 for "no tests ran", which the old boolean read as green
    and reported as MISSED — sending you to read the behaviour when the defect
    is a stale test name in the row."""
    caught, tag, note = verdict(FAILED, NO_TESTS, "test_renamed_last_week")
    assert (caught, tag) == (False, "NO TEST")
    assert "test_renamed_last_week" in note


@pytest.mark.parametrize("suite", [INTERRUPTED, 3, 4])
def test_every_exit_code_above_one_is_broken_not_caught(suite):
    """3 is an internal error and 4 a usage error. Neither is evidence, and the
    boolean counted all of them as a red suite."""
    assert verdict(suite, FAILED, "test_whatever")[1] == "BROKEN"


def test_a_usage_error_is_not_reported_as_a_mutant_that_does_not_import():
    """[E7 pair review] Same tag, different sentence, because the sentence is
    what a reader acts on.

    A row selected one parametrisation by its prose id — `test_… and segments as
    a dict` — and pytest's `-k` has no string literals, so it collected
    everything and exited 4 without running a test. The note said the mutant did
    not import. It imported fine; it was never reached. An hour of subprocess
    was spent on a report about the wrong file, and the row above passes either
    way because it only reads the tag.
    """
    note = verdict(FAILED, 4, "test_whatever")[2]
    assert "-k" in note and "exited 4" in note
    assert "does not import" not in note


@pytest.mark.parametrize("suite,intended", [(TIMED_OUT, GREEN), (FAILED, TIMED_OUT)])
def test_a_run_that_never_returned_is_wedged_not_caught(suite, intended):
    """[E7] The verdict this round paid thirty-three minutes to learn it needed.

    A mutant that removes a guard against *blocking* meets a test that proves
    the guard by not blocking, and the test demonstrates the mutant by blocking
    too. Nothing is printed, no row is scored, and the pass sits there. Whatever
    `verdict` does with it, the one answer that must not appear is CAUGHT: a
    hang is the absence of an answer, and crediting the named test with it is
    the same attribution-on-no-evidence that BROKEN exists to refuse.

    Both positions, because the suite run and the intended run are separate
    subprocesses and either can be the one that wedges.
    """
    caught, tag, note = verdict(suite, intended, "test_that_names_a_hang")
    assert (caught, tag) == (False, "WEDGED")
    assert "deadline" in note


def test_the_wedged_note_names_which_of_the_two_runs_hung():
    """[E7] "something never returned" sends you to read both runs. The whole
    value of the tag is that it points at one of them."""
    assert "the suite" in verdict(TIMED_OUT, GREEN, "test_x")[2]
    assert "test_x" in verdict(FAILED, TIMED_OUT, "test_x")[2]


def test_the_only_verdict_that_counts_as_success_is_caught():
    """A guard against a future tag being added as `True` by reflex."""
    outcomes = {
        verdict(s, i, "t")[1]: verdict(s, i, "t")[0]
        for s, i in [(0, 0), (1, 1), (1, 0), (2, 2), (1, 5), (-1, 0), (1, -1)]
    }
    assert {tag for tag, ok in outcomes.items() if ok} == {"CAUGHT"}
    # And every tag is reachable, or the line above is asserting over a set
    # smaller than the rule it is checking.
    assert set(outcomes) == {"SURVIVED", "CAUGHT", "MISSED", "BROKEN", "NO TEST", "WEDGED"}


# --------------------------------------------------------------------------- #
# every row still points at something
# --------------------------------------------------------------------------- #
#
# A row whose anchor no longer resolves prints `SKIP` and holds nothing. That is
# the harness's own version of the defect it exists to find — a check that passes
# for a reason unrelated to the behaviour it is named for — and until now the
# only thing that noticed was a full hour-long pass nobody runs after a refactor.
# Two rows had been rotten for some time and six more were broken and repaired
# inside a single round of edits, which is the rate that makes this worth two
# seconds on every commit. [round 3, finding 3]


def _path(filename: str):
    """A bare name is a file in the package; a path is relative to the repo."""
    return ROOT / filename if "/" in filename else SRC / filename


def test_the_suite_run_deselects_the_one_test_a_mutant_is_meant_to_break(monkeypatch):
    """`SURVIVED` was unreachable, and this is the test that keeps it reachable.

    `test_every_mutation_row_anchors_exactly_once` reads the file the harness has
    just mutated and asserts every anchor is present. Under a mutant the anchor
    has been replaced, so it is not — for every mutant, on every row, whether or
    not the product behaviour is pinned by anything. The harness read that as
    "the suite went red", so `suite` was never 0, so a row nothing holds scored
    MISSED ("caught, but not by ...") instead of SURVIVED ("caught by nothing").
    Two rows in the last full pass were exactly that, and both turned out to name
    code a later fix had made dead.

    Asserted on the argv rather than by running the suite twice, which is 160
    seconds. The name is checked against the real test too: it is a string, and
    a rename would put the deselect back to deselecting nothing. [E7 pair review]
    """
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        raise AssertionError("unreachable")

    monkeypatch.setattr(mutate.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        mutate.run(["-x"])

    argv = seen["argv"]
    assert "--deselect" in argv, argv
    assert argv[argv.index("--deselect") + 1] == mutate.ANCHOR_TEST, argv

    path, _, name = mutate.ANCHOR_TEST.partition("::")
    assert f"def {name}(" in (ROOT / path).read_text(), f"{mutate.ANCHOR_TEST} names no test"


def test_a_verdict_reaches_the_log_before_the_next_row_runs():
    """[E7 pair review] The log of an hour-long pass has to be readable as a log.

    Python block-buffers `print` when stdout is a file or a pipe; the pytest
    subprocesses inherit that same descriptor and are not buffered by this
    process at all. So the verdicts arrive in batches, after output belonging to
    rows below them, and the obvious reading — *this `FAILED` line explains the
    verdict under it* — is wrong. Half an hour of a triage went into a 2 MB log
    that way, attributing one row's failure to another row three places later.

    Two real interpreters against a pipe, because that is the arrangement that
    has the bug: under `capsys` there is no buffering to get wrong, and a test
    that asserts on `flush=True` in the source is a test of the spelling.

    The environment is pinned rather than inherited, and that is the whole
    second half of this test. `PYTHONUNBUFFERED=1` in the ambient environment
    flushes the child's every print whether `say` asks for it or not, so the
    row that removes `flush=True` scores SURVIVED — and the first thing a
    reader does when the verdicts interleave is re-run the harness under
    `PYTHONUNBUFFERED=1` to make the log legible, which is exactly the state in
    which this test stops testing anything. It scored SURVIVED for that reason
    before this line existed. [E7 pair review]
    """
    child = "import subprocess, sys; subprocess.run([sys.executable, '-c', \"print('child')\"])"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tests")}
    env.pop("PYTHONUNBUFFERED", None)

    def order(emit: str) -> list[str]:
        out = subprocess.run(
            [sys.executable, "-c", f"import mutate_index as m; {emit}; {child}"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        return out.stdout.split()

    # The floor, and it is the mutant: an unflushed print in this same
    # arrangement has to come out *behind* the child, or there is no buffering
    # here to get wrong and the assertion below passes on nothing.
    assert order("print('verdict')") == ["child", "verdict"]
    assert order("m.say('verdict')") == ["verdict", "child"]


def test_every_mutation_row_anchors_exactly_once():
    """Each of the five checks below reports *every* offender rather than
    parametrising, because the way these break is in batches — one refactor
    invalidated six rows at once — and a list is what you act on. It also keeps
    186 rows of a lint-shaped check out of the test count the README publishes.
    """
    bad = []
    for name, filename, find, _, _ in MUTANTS:
        path = _path(filename)
        if not path.exists():
            bad.append(f"{name}: {filename} does not exist")
            continue
        if (n := path.read_text().count(find)) != 1:
            bad.append(f"{name}: anchor appears {n}x in {filename}")
    assert not bad, "rows that would SKIP, holding nothing:\n  " + "\n  ".join(bad)


def test_every_mutation_row_produces_a_program():
    """[E7] A mutant that does not parse was never run, so it demonstrates
    nothing — and the BROKEN verdict, which exists to say exactly that, could
    not see it: `-x` reports a collection error as exit 1 or 2 depending on
    collection order, and the branch was only looking for 2.

    One row was in that state — the anchor deleted an `if`'s two body lines and
    left the `if` and its comment behind — and it scored CAUGHT. Both halves are
    repaired, but `verdict` is the last line of defence and this is the first:
    the whole class is a two-second `compile` over the index, against an hour of
    subprocess for a pass that will then misreport it anyway.

    `bash -n` for the three shim rows, which can break the same way.
    """
    bad = []
    for name, filename, find, replace, _ in MUTANTS:
        path = _path(filename)
        src = path.read_text()
        if src.count(find) != 1:
            continue  # the case above owns this; reporting it twice helps nobody
        mutated = src.replace(find, replace)
        if path.suffix == ".sh":
            shell = subprocess.run(
                ["bash", "-n", "-"], input=mutated, capture_output=True, text=True
            )
            if shell.returncode != 0:
                bad.append(f"{name}: {shell.stderr.strip().splitlines()[-1]}")
            continue
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as exc:
            bad.append(f"{name}: {exc.msg} (line {exc.lineno})")
    assert not bad, "rows whose mutant is not a program:\n  " + "\n  ".join(bad)


def test_every_mutation_row_actually_changes_the_file():
    """A mutant identical to the original reports on an unmodified tree. One row
    in this index already shipped as a semantic no-op (`setdefault` for
    `__setitem__`, where the dedup lives in the key); that one at least differed
    textually, and this catches only the degenerate case — which is why the row
    above it still needs a human to ask *what breaks*."""
    bad = [name for name, _, find, replace, _ in MUTANTS if find == replace]
    assert not bad, f"the mutant is the original: {bad}"


def test_every_mutation_row_names_a_test_that_exists():
    """The `NO TEST` verdict, an hour earlier. A renamed test leaves the row
    pointing at nothing and `-k` exits 5. Both `testpaths` are searched: the
    E3 rows name tests that live beside the harness in `bench/`.

    The field is a `-k` expression, not always a bare name: nine E7 rows each
    delete one detector out of a table and select the one parametrisation that
    covers it (`test_… and slack_token`), because otherwise all nine rows name
    the same function and a row that mutated the wrong line still reads CAUGHT.
    So every `test_`-shaped identifier in the expression has to exist, and the
    parameter halves are left alone — pytest matches those as substrings.

    A field may instead be a node id, `tests/test_pi.py::name`, for a name
    defined in more than one test module; `-k` would score those on the union
    of the files. The path half is a filename, not an identifier, so it is
    checked as a file and dropped before the identifier scan — without that,
    `test_pi` reads as a test that does not exist.

    Three separate claims, and the first version of this only checked the
    weakest of them. `defined` was a flat set, so a node id proved its name
    existed *somewhere* rather than in the file it names: `tests/test_pi.py::`
    plus a name that only `test_claude_code.py` defines passed here and then
    collected nothing, which is the `NO TEST` verdict this test is named for,
    reached through the feature added to prevent it. And a bare name that two
    modules define was never flagged at all — the ambiguity node ids exist for
    was only ever fixed by hand, one row at a time, for as many rows as
    somebody had noticed. Five names qualify today; the prose said three.
    [review: tests]
    """
    defined: dict[str, set[str]] = {}
    for d in ("tests", "bench"):
        for p in sorted((ROOT / d).glob("test_*.py")):
            rel = str(p.relative_to(ROOT))
            for found in re.findall(r"^def (test_\w+)\(", p.read_text(), re.M):
                defined.setdefault(found, set()).add(rel)

    missing_file = [
        f"{name}: {expr.split('::')[0]}"
        for name, _, _, _, expr in MUTANTS
        if "::" in expr and not (ROOT / expr.split("::")[0]).is_file()
    ]
    assert not missing_file, "rows naming a file that does not exist:\n  " + "\n  ".join(
        missing_file
    )
    bad = sorted(
        f"{name}: {ident}"
        for name, _, _, _, expr in MUTANTS
        for ident in re.findall(r"\btest_\w+", expr.split("::")[-1])
        if ident not in defined
    )
    assert not bad, "rows naming a test that does not exist:\n  " + "\n  ".join(bad)

    # A node id has to name the test in the file it names, not merely somewhere.
    elsewhere = sorted(
        f"{name}: {expr} (defined in {', '.join(sorted(defined[ident]))})"
        for name, _, _, _, expr in MUTANTS
        if "::" in expr
        for ident in [expr.split("::")[-1]]
        if ident in defined and expr.split("::")[0] not in defined[ident]
    )
    assert not elsewhere, "node ids naming a file the test is not in:\n  " + "\n  ".join(elsewhere)

    # And a bare name that two modules define is the ambiguity node ids exist
    # for: `-k` scores it on the union, so a mutant in one adapter reads CAUGHT
    # because the *other* adapter's test of the same name failed.
    ambiguous = sorted(
        f"{name}: {ident} (defined in {', '.join(sorted(defined[ident]))})"
        for name, _, _, _, expr in MUTANTS
        if "::" not in expr
        for ident in re.findall(r"\btest_\w+", expr)
        if len(defined.get(ident, ())) > 1
    )
    assert not ambiguous, (
        "bare names defined in more than one test file — use a node id:\n  "
        + "\n  ".join(ambiguous)
    )
    # A row whose expression has no `test_…` in it at all selects by parameter
    # alone, which is how a typo becomes a row that silently runs the suite.
    nameless = [n for n, _, _, _, expr in MUTANTS if not re.search(r"\btest_\w+", expr)]
    assert not nameless, f"rows selecting no test function: {nameless}"


def test_every_mutation_row_selects_with_an_expression_pytest_can_parse():
    """[E7 pair review] The case above checks the names in the expression. This
    checks that the expression is one.

    `test_… and segments as a dict` names a real test and a real parametrisation
    and is not a `-k` expression: the grammar is identifiers joined by
    and/or/not, with no string literals, so pytest collects the whole suite and
    exits 4 having run nothing. That row sat in the index for a full pass and
    came back BROKEN — a verdict about the mutant, for a defect in the row.

    Parsed with pytest's own parser rather than a grammar restated here, because
    a second-hand grammar is what goes stale when `-k` grows a token. Private
    import on purpose and not guarded: if it moves, this test says so, where a
    skip would leave the check quietly switched off.

    Two shapes, two checks. A node id is not a `-k` expression and the harness
    does not pass it as one — `_select` gives it as a positional argument — so
    running it through `Expression.compile` asked the wrong question of every
    row that has one. It happened to answer *yes*, because `tests/test_pi.py`
    lexes as the identifier `tests`, and so the check was vacuously green on
    exactly the rows added to fix an earlier defect. A slash in the wrong place
    or a missing `::` would have passed here and failed at collection.
    [review: tests]
    """
    from _pytest.mark.expression import Expression

    def parses(expr: str) -> bool:
        try:
            Expression.compile(expr)
        except SyntaxError:
            return False
        return True

    # The floor. A checker that accepts everything passes the loop below in
    # silence, and the bad shape is the one this test is named for.
    assert parses("test_a_manifest_the_gate_cannot_read and dict")
    assert not parses("test_a_manifest_the_gate_cannot_read and segments as a dict")

    bad = [
        f"{name}: -k {expr}"
        for name, _, _, _, expr in MUTANTS
        if "::" not in expr and not parses(expr)
    ]
    assert not bad, "rows whose -k pytest cannot parse:\n  " + "\n  ".join(bad)

    # A node id is `path::name`, one separator, and the name half is a bare
    # identifier — `path::name and other` is neither a node id nor a `-k`.
    malformed = [
        f"{name}: {expr}"
        for name, _, _, _, expr in MUTANTS
        if "::" in expr
        and not re.fullmatch(r"[\w./-]+\.py::[A-Za-z_]\w*(\[[^\[\]]*\])?", expr)
    ]
    assert not malformed, "rows whose node id is not one:\n  " + "\n  ".join(malformed)


def test_every_mutation_row_has_a_distinct_name():
    """The name is the filter (`mutate_index.py "some words"`) and the line in
    the report. Two rows sharing one means a verified row and an unverified row
    are indistinguishable in both places."""
    names = [m[0] for m in MUTANTS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicated: {dupes}"


def test_the_row_count_in_the_docstring_is_the_row_count():
    """`main`'s docstring justifies the name filter with the size of a full
    pass. That number has been wrong twice — it was written when the index held
    146 rows and stayed there through 276 more — and a stale one understates the
    cost of the thing it exists to excuse. [E7]"""
    from mutate_index import main

    claimed = re.search(r"a full pass is ([\d,]+) mutants", main.__doc__)
    assert claimed, "the docstring no longer states a row count"
    assert int(claimed.group(1).replace(",", "")) == len(MUTANTS)
