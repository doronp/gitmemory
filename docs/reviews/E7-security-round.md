# E7 — the RC1 security round

The index over six reports. Each of them is the primary record for its own
surface; this page is what the round *was*, what it cost, and the handful of
things that are true across all of it.

## The shape

Seven agents, seven git worktrees, none with a hand in the code it read. Each
was given a surface rather than a file list, and each wrote its own report. A
seventh report does not exist as a document: that agent's findings, plus every
finding the others raised about code that was not theirs, were adjudicated
together in the carry-ins.

| Report | Surface |
|---|---|
| [parsing](E7-security-parsing.md) | `jsonl.py`, `records.py`, the Claude Code adapter — every byte chosen by someone else |
| [secrets](E7-security-secrets.md) | the redaction gate and `push`: what leaves the machine |
| [fs](E7-security-fs.md) | `store.py`, `gitrepo.py`, `daemon.py` — bytes on disk, the lock, the proof, the process environment |
| [index](E7-security-index.md) | `index.py`, `derive.py`, `graph.py` — SQL construction, resource exhaustion, the artifact write door |
| [dashboard](E7-security-dashboard.md) | `serve`: the one command that opens a socket |
| [carry-ins](E7-carry-ins.md) | the seventh agent, and every cross-surface finding |

## The standing rule

**A finding is accepted only when it has been re-derived by a route the reporter
did not use.** Not re-run — re-derived. Reading the reviewer's script and
watching it print the same thing checks the script.

This was not ceremony. It changed the outcome repeatedly, in both directions:

- **fs F1** — the round's HIGH — was found *unfixed* while writing the closing
  table, because nine rows had a commit in the Outcome column and the top one
  did not. A round that can lose its most severe finding between report and fix
  can lose the next one.
- **S6** was reported as a missing check. Running `verify()` end to end showed
  the other sweep does not cover for it, which is the difference between a
  duplicated message and a hole nobody reports.
- **F1 (carry-ins)** was reported as one shape and reproduced as three, two of
  which the reported fix did not touch — and the reporter's own demonstration
  did not reproduce, which corrected the finding rather than dismissing it.
- **S4** did not reproduce as stated: a FIFO loose in the store is reported, not
  opened. It has to be *named by a manifest*. The test had to be written to the
  narrower shape.
- **dashboard F7** was declined on a measurement, not an argument.

## The count

**68 findings across the six reports.**

| Disposition | | What it means |
|---|---:|---|
| fixed | 56 | code changed, pinned by a named test and a negative control |
| documented | 5 | real, not patchable — the claim or the limit is now written down |
| accepted with reasoning | 4 | the behaviour is deliberate; the report says why, where a user meets it |
| declined on a measurement | 2 | reproduced, and the measurement says it is not the finding |
| referred | 1 | a product decision, not a patch (`push` has no override flag) |

Per report: parsing 15, carry-ins 14, secrets 11, fs 10, index 10, dashboard 8.

## What it cost, and what pins it

The suite went **960 → 1121 tests** and the negative-control index went
**265 → 423 rows**: 161 tests and 158 controls, for 56 fixes.

One of each is this page's own doing: writing the paragraph above meant reading
`main`'s docstring in the harness, which justified its name filter with "146
mutants … about an hour". It had been 146 when that was written and had gained
276 rows since. The number is now `len(MUTANTS)` with a test and a mutation row
holding it there, which is the only reason to trust the two figures in the
sentence above rather than the two in the one before it.

That ratio is the round's method rather than an accident. Every fix lands with a
test that fails when the fix is reverted, and "fails when reverted" is checked
mechanically — `tests/mutate_index.py` holds the mutation, the harness applies
it, runs the suite and the named test, and scores the row. A fix with no row is
not finished.

The harness itself was a finding twice, both times from reading its output
rather than its exit code:

- It **scored a mutant that does not parse as CAUGHT.** The mutant was never a
  program. `verdict` read only the `-x` suite run, whose exit code for a
  collection error is 1 or 2 depending on collection order. Three repairs: the
  verdict reads both runs, the row was rewritten to be a program, and every row
  is compiled (or `bash -n`'d) by a test that runs on every commit.
- It **wedged** for 33 minutes on a test that proved "this does not block" by
  blocking. Two repairs: a `setitimer` deadline with its own positive control,
  and a `WEDGED` verdict that names which of the two runs hung.

A meta-test named for the first of those passed throughout, because it asserted
on an exit-code pair the harness never produces. A check that passes for a
reason unrelated to the behaviour it is named for, in the file whose whole job
is to refuse exactly that.

## The three things that were true on every surface

**1. The gate and the reader want different things, and the difference is the
vulnerability.** A reader should get as much of a damaged store as it can; a
gate should get all of it or refuse. Where one function served both — `sessions`
for the seam scan (S1), `_verify_one`'s early returns for the stray-file sweep
(S6), `os.walk` for the push gate (secrets F4a) — the lenient behaviour won and
the strict caller silently lost its coverage while printing success.

**2. What git stores is not what the filesystem shows.** Three findings, three
surfaces, one root: `git push` sends the object graph and not the checkout
(secrets F1, and the owner-data test's S2); git stores a symlink as its *text*
and never follows it, so scanning through one reads bytes that do not ship and
misses the bytes that do (S12a, S12b). The fix converged on one function,
`gitrepo.pushable_objects`, now feeding both the egress gate and the owner-data
scanner — one definition of "what would leave this machine".

**3. A mode set once is a mode set never.** `os.makedirs(mode=)` and `mkdir -p`
apply a mode only when they create. Four findings (fs F2, fs F9, S9, index F9)
are all that sentence, on four directories, each holding a complete copy of
captured transcript bytes. They are re-applied on every start now, which is why
`init` is idempotent rather than first-run-only.

## What did not get fixed, said plainly

- **`push` has no override flag** (secrets F4b), and will not get one. A gate
  you can wave through on a deadline is a gate that gets waved through on a
  deadline. The remediation path is in the README instead: rotate first, the
  gate refusing is the gate working, and history rewriting is an ordinary git
  operation this project will not automate.
- **A manual `git push` from the store is ungated** (secrets F7). The store is
  an ordinary git repository and that is a feature; `gitmemory push` is the
  gated door, not the only one. Documented in `DESIGN.md` §2.4, whose earlier
  claim to the contrary was withdrawn.
- **Every detector is a byte regex; nothing decodes** (secrets F10). A
  base64-wrapped credential is not caught. Stated in the module docstring, where
  someone adding a detector reads it.
- **The hook shim is silent on every write failure** (S10). Deliberate: a
  refusal is a configuration you fix once, a full disk would speak at every
  compaction until you cleared it. `hook/README.md` says so, and says what a
  broken run costs beyond the one capture.
- **`git` is resolved through the inherited `PATH`** (fs F8), on the reviewer's
  own argument: a `PATH` an attacker controls is a machine already lost.
- **The arbitrary-SQL console stays on** (dashboard F7), because
  `default_allow_sql off` blocks the owner too and the sign-in door already
  gates every path SQL discloses.

## The constraint that binds the reviewers too

This repository must not contain a byte of its author's machine. Every agent in
this round opened its report with an attestation naming the files it read, the
commands it ran, and the sentence that it read nothing under the user's
`~/.claude/` or `~/memory/`. Every reproduction ran against synthetic stores
under `/tmp`, and every credential in a test or a repro is a synthetic string of
the right shape.

`tests/test_no_owner_data.py` is the mechanical half, and it was itself three
findings this round (S2, S12b, S13) — it looked in the wrong place, read the
wrong bytes, and its patterns missed 23 of 27 evasions. It now reads the object
graph, including commit messages. The first thing it caught after the fix was a
commit message of mine.
