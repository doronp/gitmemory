# E4 cross-review

E4 was split in two. Gemini wrote **Half A** — the hook shim, its README, its
tests, and the latency tool. I wrote **Half B** — the git layer, the watcher
daemon, the CLI wiring, and their tests. Then each of us reviewed every line of
the other's half, by the E3 rules: BLOCKING / MAJOR / MINOR, a `file:line`, and
either a reproduction or the word PLAUSIBLE.

This file records my review of Half A. Gemini's review of Half B is in
`E4-gemini-review-of-claude.md`.

Everything below is fixed in the committed tree. The findings are kept because
three of them are about *tests that passed for the wrong reason*, and that
failure mode is the one this project is most exposed to.

---

## BLOCKING

### A1 — the README shipped the author's home directory

`hook/README.md`, two occurrences of `/Users/<owner>/work/gitmemory` in the
install example.

gitmemory is a generic memory system. The whole premise of E4's scoping rule is
that this machine's history is not this product's data; a path naming the
author's account in the install instructions is the same leak in a smaller form,
and it is the first thing a reader copies. Replaced with
`/absolute/path/to/gitmemory` and a line telling the reader to substitute it.

Reproduction: grepping the pre-fix file for an absolute macOS home path returned
two lines.

This one is now enforced rather than remembered. `tests/test_no_owner_data.py`
scans **every tracked file** for absolute home directories and for the author's
private memory tree, and carries its own negative control so a scanner that
matches nothing cannot pass silently. Its first run flagged nine more lines —
all of them false positives on `~/.claude/projects`, which is where Claude Code
puts transcripts and therefore something the adapter, the README, and every
brief saying "never read this" all have to be able to write down. The pattern
was narrowed to absolute paths only, which is the real leak: a path that works
on exactly one machine and names its owner.

### A2 — the documented Claude Code hook config was not valid

`hook/README.md`, the JSON block.

The example gave each event key a single object. Claude Code takes an **array of
matcher groups**, each with its own `hooks` array. A reader following the
document verbatim gets a hook that never fires, and — because the shim's entire
value proposition is "it is optional, the watcher is the guarantee" — nothing
anywhere reports the failure. It would look like it worked.

Verified against the published hook reference, not against memory. Fixed to the
matcher-group shape, with `args` carrying the event name because Claude Code
passes the payload on stdin and no arguments of its own.

### A3 — a relative `GITMEMORY_HOME` silently split the spool

`hook/gitmemory-hook.sh`, the home resolution.

The shim expanded `~` but accepted a relative path as given. The agent sets a
hook's working directory to whatever project the user is in, so
`GITMEMORY_HOME=.gitmemory` creates a *separate spool under every directory the
user visits* — and the watcher, resolving the same variable against *its* cwd,
reads none of them. Captures vanish with no error anywhere.

Reproduced live: ran the shim from three different directories with a relative
home and got three spools, none of them the watcher's.

Fixed by refusing a relative value: loud on stderr, `exit 0`. Never block the
session — a misconfigured hook must still not be able to wedge the agent.

The aggravating factor is A6 below: a test asserted this behaviour was correct.

---

## MAJOR

### A4 — a test drove the shim into obfuscating itself

`tests/test_hook.py`, `test_no_forbidden_commands_or_interpreters`.

The test scanned the shim's *source text* for the substring `git`, meaning to
prove the hook never shells out to git. But the shim's own default home is
`$HOME/.gitmemory`, which contains that substring. To get past its own test the
shim had been written as:

```sh
_g="gi"; _t="t"
H="${GITMEMORY_HOME:-$HOME/.${_g}${_t}memory}"
```

— splitting a literal path across two variables so a grep would not see it.
Gemini disclosed this in its handoff report rather than leaving it to be found,
which is the right call and worth recording.

The test was the defect, not the shim. A substring scan cannot distinguish
"invokes git" from "mentions git", and the cost of satisfying it was making the
single most safety-critical line in the program unreadable. Deleted; the
obfuscation is gone; the shim now says `.gitmemory` in plain text.

Replaced with something that checks a property rather than a spelling:

```python
@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "ksh", "zsh"])
def test_the_shim_parses_under_every_posix_shell_on_this_machine(shell):
    """`-n`: parse, do not run."""
```

The "doesn't call git" property is now carried by the shim's header comment and
by its 60 lines being readable in one screen, which is the honest enforcement
mechanism for a file this size.

### A5 — the latency tool could not tell fast from broken

`tools/hook_latency.py`.

Three problems, one theme: the tool would happily publish a number that meant
nothing.

- It did not check exit status. **A broken shim exits fast, and a fast broken
  shim benchmarks beautifully.** Now the warm-up run and every timed run must
  exit 0 or the tool refuses to report.
- It had no control. What it timed was Python building an argv, forking,
  exec'ing, and draining two pipes — *and then* the shim. On this machine that
  harness overhead is 1.6 ms of an 8 ms measurement. Now it times spawning
  `true` on every iteration and reports both columns.
- `pct()` indexed `values[int(len(values) * q)]`, which walks off the end at
  `q=1.0` and for small `n`. Clamped.

The control immediately paid for itself — see A9.

### A6 — a test locked in the A3 bug

`tests/test_hook.py`, `test_spaces_and_relative_paths_work`.

One test asserting two unrelated properties, one of which was the hazard. Because
it passed, A3 read as intentional. Split into
`test_a_home_with_spaces_in_it_works`,
`test_a_tilde_in_the_variable_is_expanded`, and
`test_a_relative_home_is_refused_rather_than_resolved`.

A test named after two things is a test that cannot fail informatively.

---

## MINOR

### A7 — a fixed sleep where a condition was meant

`tests/test_hook.py`, the kill-9 test used `time.sleep(0.05)` to wait for the
shim to begin writing. That is a flake on a loaded machine and a silent pass if
the shim never starts at all. Replaced with a 10-second poll on the condition
plus an explicit `pytest.fail("the shim never began writing its temp file")`.

### A8 — the shim did not say what it was

`hook/gitmemory-hook.sh` had no header. For the one file in this project that
runs inside somebody else's process, the reader's first question is "what can
this do to me". Added four lines: it constructs no JSON, parses nothing, spawns
nothing, and the watcher re-derives every fact from the transcript — so the
worst a broken run costs is one immediate capture.

### A9 — the README's explanation of its own number was wrong

Not Gemini's line — mine, written over Gemini's table. I had written:

> Nearly all of that is process startup — fork, exec, and the shell reading the
> script — not work the script does.

The control added in A5 says otherwise: the shim's own share of p50 was 6.67 ms
against a 1.65 ms process-spawn baseline. "Nearly all" was off by a factor of
four. Running the decomposition:

| | p50 |
|---|---|
| `true` | 1.42 ms |
| `dash` running `exit 0` | 1.59 ms |
| `dash` + `mkdir`, `cat`, `date` | 6.01 ms |

The shell is nearly free. The cost is the external commands the script forks, at
roughly 1.5 ms each — and one of the three, `mkdir -p`, was running on every
fire to create a directory that exists on every fire but the first. Putting it
behind `[ -d ]` took p50 from **8.31 ms to 6.82 ms**, measured.

A one-line win that was invisible until the benchmark had a control. The README
now carries both columns and the decomposition.

---

## Score

Nine findings on roughly 300 lines of shell, tests, and tooling: three
BLOCKING, three MAJOR, three MINOR. All fixed, all with the fix verified by a
test that fails when the fix is reverted.

The pattern worth carrying into E5: **five of the nine were tests, not code.**
A4 and A6 were tests that actively made the product worse — one by forcing
obfuscation, one by certifying a bug. A5 and A9 were a measurement that would
have published a wrong number with a straight face. Test code in this project
gets reviewed as carefully as source, because a test that passes for the wrong
reason is worse than no test: it spends the reviewer's attention and returns
nothing.

---

# Addendum: Gemini's review of Half B, and what survived verification

Gemini reviewed the half I wrote — `gitrepo.py`, `daemon.py`, and their tests —
and returned 14 findings, every one labelled **PLAUSIBLE**. That label is the
honest one for a reviewer working from reading rather than running, and it is
also the reason none of them were applied as written. Each was reproduced, or
an attempt was made to reproduce it, before anything changed.

**Ten confirmed, four refuted.** The refutations matter as much as the fixes:
three of the four were the *blocking* findings, and all three were the same
mistake — a real git feature described accurately, attached to a code path this
project does not have.

| # | Sev claimed | Verdict | Outcome |
|---|---|---|---|
| 01 `GIT_COMMON_DIR` | BLOCKING | **REFUTED** | Real variable, but it needs a `GIT_DIR` to be common *to*; with `-C <home>` and no `GIT_DIR` it does not move the repository. Closed anyway by 04's blanket scrub. |
| 02 `GIT_EXTERNAL_DIFF` | BLOCKING | **REFUTED** | Real execution primitive, but gitmemory's only `diff` is `--cached --quiet`, which takes the no-external-diff path. Not reachable. Closed by 04. |
| 03 `GIT_EXEC_PATH` | BLOCKING | **REFUTED** | Redirects git *subcommand* lookup; every command gitmemory runs is a builtin, so nothing is looked up there. Closed by 04. |
| 04 author/committer spoofing | MAJOR | **CONFIRMED** | The one that was real. `GIT_AUTHOR_NAME`/`_EMAIL` outrank repo config, so the environment could sign a capture as anyone. Fixed by dropping **every** `GIT_*` and setting back `GIT_TERMINAL_PROMPT`. |
| 05 `--no-verify` ≠ hook lock | MAJOR | **CONFIRMED** | Ran it: a planted `post-commit` fires through `--no-verify`. `core.hooksPath` moved from a non-existent directory to `/dev/null`, which cannot acquire children. |
| 06 `store.sessions()` escapes `tick` | MAJOR | **CONFIRMED** | One corrupt manifest killed the watcher, every restart, for ever. Now reported, pass abandoned. |
| 07 negative epoch shifts fields | MAJOR | **CONFIRMED** | A pre-1970 clock makes `date +%s` negative, so the name leads with `-` and `fields[2]` is the pid. Fixed by validating against `EVENTS`, not by counting fields. |
| 08 case-insensitive roots | MAJOR | **CONFIRMED** | APFS. `realpath` does not correct case, so a root typed in the wrong case dropped every doorbell. Now compared by device+inode. |
| 09 `init` not re-applied per pass | MINOR | **CONFIRMED (docs)** | The code is right; the docstring described code that does not exist. Per-start is the correct frequency. Sentence fixed, no behaviour change, no test — the defect was prose. |
| 10 a watch root that is a file | MINOR | **CONFIRMED, narrowed** | Silently and totally dead: no glob match, and `_covers` excludes the root itself. Dropped at load — but only if it *exists* and is not a directory. A root that does not exist yet is the normal pre-first-run state and must survive. |
| 11 `--initial-branch=main` | MINOR | **CONFIRMED** | Passed because this machine's git already defaults to `main`. Test now plants `init.defaultBranch = theirs` in a fake `$HOME`. |
| 12 `gc` no-op mutation | MINOR | **CONFIRMED** | `gc --auto` usually does nothing, so a `gc()` that did nothing looked identical. Test now asserts git was invoked. |
| 13 `is_repo` mutated to `False` | MINOR | **REFUTED** | Not a test hole: `git init` is idempotent by design and `init()` is documented as idempotent, so a second `init` is correct behaviour, not a masked bug. |
| 14 `GIT_OBJECT_DIRECTORY` untested | MINOR | **CONFIRMED** | Now covered by a test asserting *no* `GIT_*` variable reaches the subprocess, which is the property, rather than one more name on a list. |

## What this round actually taught

**All 673 tests passed before the six production fixes landed.** Not one
existing test caught any of them. That is the finding behind the findings: the
suite was testing the behaviour the code had, which is the failure mode a suite
written alongside its subject drifts into. Eighteen tests were added, seven
negative controls written, and all seven catch their mutant.

**Three of three BLOCKING findings were refuted, and the one real security bug
was filed as MAJOR.** Severity assigned from a variable's documented power,
without checking whether the code reaches it, inverts the ranking. The fix for
the three refuted findings was adopted regardless — not because they were
right, but because the shape of the defence was wrong. A denylist of four names
was being asked to grow by three more; an allowlist ends the argument, and it
retroactively closes `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_GRAFT_FILE`, and
whatever the next git release adds. **A refuted finding can still be a correct
diagnosis of the wrong thing.**

**Five of the fourteen were about tests.** Combined with the five of nine in my
review of Half A, that is ten of twenty-three findings across both directions
landing on test code. The reviews keep finding the same class of defect in the
same place, which says the tests deserve the same scrutiny as the source and
were not getting it.
