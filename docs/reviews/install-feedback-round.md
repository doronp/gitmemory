# Install-feedback round: the Kimi K3 cross-review

The round answered ten items of feedback from a first install, on one branch
off `main`, one signed-off commit per item and then the 0.2.0 release commit.
Two channels reviewed it:

- **An independent Opus reviewer for each item commit**, with no part in
  writing it. Its findings were fixed in that item's commit before the item was
  integrated, so they are not repeated here.
- **Kimi K3, as a pair programmer, on each item commit.** One pack per item,
  `git diff -U8 main...<item>`, and one review per pack. The redaction item was
  split into a code pack and a docs pack to fit.

**Kimi saw each commit alone**, against `main` and before integration, and
some of its findings come from exactly that. It called the redaction docs
unbacked by code, and the code unbacked by docs, because each half of the
split pack held what the other said was missing. It read a fourteen-test jump
in `docs/REPRODUCE.md` as unexplained, when `main`'s copy was already ten
behind the README. It could not see `main()`'s exception handler, the
`## Echoes` heading or `tests/conftest.py`, because none of them was in the
hunk. Each is recorded below with what it turned out to be, not dropped.

Every finding was re-checked against the integrated branch at `7a10f22` before
anything changed. The lead refuted the first batch from a code read or a live
probe. A skeptic then took each of the rest and tried to break it, with a probe
wherever one could run. One finding, install-3, was on neither list, and was
checked while the fixes were applied. The fixes landed in one commit on top,
the one that adds this file.

## The ten items

| Item | What | Commit |
|---|---|---|
| 1 | Every install command names `--python 3.13` | `59865e1` |
| 7c | `gitmemory --version`; the recall skill recognises an older CLI | `56327cf` |
| 5 | latest-user stops taking a subagent's prompt for the user's words | `656136c` |
| 2 | `gitmemory init <agent>` writes the standard `[[watch]]` table | `beffcf2` |
| 3a | `gitmemory service install` writes a user unit or LaunchAgent | `b39449a` |
| 3b | `recall` says when hook records have waited an hour for a watcher | `5c6a874` |
| 4 | `gitmemory show` prints the turn at a recall address | `dba5db7` |
| 6 | `recall --redact` and `show --redact` | `b9a5f57` |
| 7a | Captured sizes for people, with the exact count kept | `3bb0aff` |
| 7b | How much disk the store takes, and why | `0434189` |
| | Release 0.2.0 | `7a10f22` |

## Status

**53 findings: 22 fixed, 6 declined, 25 refuted.** *Fixed* means something
landed: code, test or text. *Declined* means the claim is true and not worth
the change, with the reason written down. *Refuted* means the claim does not
hold on the integrated branch.

| Severity | Raised | Fixed | Declined | Refuted |
|---|---|---|---|---|
| High | 5 | — | — | 5 |
| Medium | 13 | 7 | — | 6 |
| Low | 35 | 15 | 6 | 14 |
| **Total** | **53** | **22** | **6** | **25** |

All five Highs are artifacts of the commit seen alone: four from the split
redaction pack, and one (the detector count, redact-docs-4) a correction of a
number `main`'s DESIGN.md had wrong. Fifteen of the 31 that changed nothing
rest on something outside the hunk Kimi was given. service-3 was refuted here
and is counted fixed: Kimi raised it again in its final pass, and the
[final gate](#final-gate) took the reload.

One change of behaviour came out of the round: two `gitmemory init`s run at
once could lose a table silently (init-1). It carries a test that failed
first and a new negative control, which brings the index to **658**. The rest
are one reworded message (stale-1), one help string (redact-code-3), tests,
wording and pinned numbers.

## Findings

| ID | Sev | Claim | Disposition | Evidence or reason |
|---|---|---|---|---|
| install-1 | med | The floor test skips `` `uv tool install` git+… ``, a command split across spans | fixed | Probed: such a line in hook/README.md passed. A span holding the tool alone now reads on when the next word is a source or a flag. Kimi's "rest of the line" rule would have failed three prose lines in hook/README.md |
| install-2 | low | A `#` ends a command, so a `#egg=` fragment hides a later `--python` | declined | Fails loud, with a message naming the line. Documented in the test. No scanned command has a fragment |
| install-3 | low | The `derive` install hint in derive.py has no mutation row | declined | The floor test scans every tracked file: with `--python 3.13` removed from the hint, it fails on `derive.py:142`. Rows are for fixes, and item 1 was docs |
| install-4 | low | A trailing `\` joins prose lines into one command | declined | Documented in both docstrings. Every Markdown line ending in `\` is a fenced shell continuation, and a join can only add a failure, never hide one |
| version-1 | med | The skill's "step 1" does not exist | refuted | The numbered setup list is in SKILL.md; step 1 is the install line (lead) |
| version-2 | med | Rerunning `uv tool install` does not move an install forward | refuted | Live probe, uv 0.11.3: rerunning the unpinned line replaced the install's commit with the remote's tip; `uv tool upgrade` said "Nothing to upgrade" (lead) |
| version-3 | low | A fix with no mutation row | refuted | A `feat:` adding one flag; rows are required for fixes. Deleting the flag fails two skill tests, measured |
| subagents-1 | med | A doc test writes into the real `~/.claude/projects` | refuted | conftest's autouse `_no_real_home` points HOME at `tmp_path` for every test (lead) |
| subagents-2 | low | REPRODUCE's counts jump by 14 for 4 tests | fixed | The jump was `main` already ten behind the README. The real gap is that nothing pinned the guide, so a test now holds it to the README |
| subagents-3 | low | The skill never says what to do when `subagent:` lines crowd the results | fixed | Probed: 20 subagents hid the user's rule at `-k 10` and `-k 30`, and it came first at `-k 50`. The skill now says to search again with `-k 50`. `main_only` is a user-written recipe, so the skill does not point at it |
| subagents-4 | low | A docstring claims "byte-for-byte" and the test checks one field | fixed | Probed: a patched address and text still passed. The docstring now says what is asserted |
| init-1 | med | Read, then rename: an edit between them is lost | fixed | The racer that hits it is a second `init`: 46 of 60 concurrent pairs lost a table, both saying "added". `init` now re-reads under a lock before the rename and refuses, exit 1, if the file moved. 0 losses in 60 since. An editor takes no lock and is not covered, as watching.md now says |
| init-2 | low | The temp-name test checks a typed name, not the writer's | fixed | Probed: it passed with the writer's temp renamed. The test now asks `git check-ignore` about the temp the writer really made, while it exists |
| init-3 | low | The invalid-TOML refusal prints the raw exception | refuted | `push` and the watcher print the same error the same way. tomllib quotes key names and single characters, never a value, and the credential is a value |
| init-4 | low | The refusal could print an empty reason | declined | Every skip a written table can reach logs with the prefix the filter wants. A fallback would only guard a future watcher bug |
| service-1 | med | Other control characters make an invalid plist and unpasteable commands | refuted | plistlib refuses them before anything is written (exit 2). The legal ones pass `plutil -lint`. The printed commands hold no store path |
| service-2 | low | USAGE says the unit keeps the watcher running across logins | fixed | Without linger, systemd stops user units at the last logout. Reworded to "at every login", with the `loginctl enable-linger` line for SSH-only machines |
| service-3 | low | The removal line lacks `daemon-reload` | fixed | On the second report (final gate, F2). First refuted from systemd's documentation, not a probe: `disable` reloads, and a stopped, disabled unit is garbage-collected. The reload is harmless and conventional, so the printed removal now ends with one |
| service-4 | low | `main()` may not map ValueError and RuntimeError to exit 2 | refuted | It does (lead) |
| stale-1 | med | "N records uncollected for <age>" gives the oldest's age to all N | fixed | Probed: a record from seconds earlier was reported 3 days old. Now `N hook record(s) uncollected, the oldest for <age>`, in the code, the skill, USAGE and watching.md |
| stale-2 | low | REPRODUCE's counts jump by 13 for 3 tests | fixed | As subagents-2. One fix serves both; see below |
| stale-3 | low | The skill test assumes `resolve_home` is the identity on a relative path | refuted | Every sibling message also goes through `resolve_home`, and pytest's `tmp_path` is already resolved |
| stale-4 | low | The `_ago` boundaries and the `>= STALE_RECORD` edge are unpinned | fixed | The 2-hour and 2-day edges are now pinned 30 s either side. Probed: four mutants of them that had survived are caught. The `>=` edge is left: it differs only at exactly 3600.0 s, and pinning it needs an injected clock |
| show-1 | med | Store-read errors escape `show`'s exit contract | refuted | `main()` turns OSError, ValueError, RuntimeError and sqlite3.Error into a masked `error:`, exit 2 (lead) |
| show-2 | low | mutate_index's docstrings keep stale counts | fixed | "the other 567", "955-test", "1280-test". The number is gone, and the timings are dated as taken when the suites were 955 and 1280 tests, not re-pinned to today's |
| show-3 | low | Role and kind are printed uncollapsed | refuted | Both adapters write only literal roles and kinds (lead) |
| show-4 | low | The skill's echo rule also skips a real turn that quotes an address | fixed | Probed: read literally, it skipped the user's newer reversal. Now the header's shape, marker then address then date, and a test pins the wording |
| show-5 | low | The `## Echoes` heading may not exist | refuted | It is on `main` (lead) |
| show-6 | low | `g5` and `@0007` are accepted | declined | Probed: same output as the canonical address, and the header prints the canonical one. The address never becomes a path |
| show-7 | low | The success path prints a credential-shaped session id unmasked | refuted | It does by design: masking is opt-in, and `--redact` masks it, except an upper-case shape the store has lowercased. This row first said that residue was documented at the call site and in USAGE. It was only at the call site; the final gate found the gap (A5, A6, A9), and USAGE's list of what masking does not guarantee now names it |
| redact-code-1 | high | No SKILL.md change ships | refuted | It was in the docs half of the split pack (lead) |
| redact-code-2 | med | The linearity test asserts on wall clock | fixed | The page of headers is now counted: one END search, against 2000 under the mutant. The never-closing END line is one `re` search, which cannot be counted, so it stays timed, as the suite's other regex-cost tests are |
| redact-code-3 | low | The `--redact` help says "set and not 0", but empty is off too | fixed | Now "set to anything but empty or 0" |
| redact-code-4 | low | `safe_path` now widens a PEM header to the end of a diagnostic | refuted | The owner's decision, pinned by a test and a mutation row. It only ever masks more |
| redact-code-5 | low | The flag is registered only for `recall` | refuted | Integrated, `show` registers it too, and those are the two commands that print transcript text |
| redact-code-6 | low | The docs for the env var are absent | refuted | They were in the docs half of the split pack (lead) |
| redact-docs-1 | high | No `--redact` in the CLI | refuted | In the code half (lead) |
| redact-docs-2 | high | No `mask()`, no `safe_path` delegation | refuted | In the code half (lead) |
| redact-docs-3 | high | Counts bumped with no tests or rows | refuted | The tests and rows were in the code half (lead) |
| redact-docs-4 | high | Detector counts edited to twelve and four with no code change | refuted | redact.py has 12 HIGH and 4 SUSPECT on `main` and here. `main`'s DESIGN.md said nine and three, which was stale |
| redact-docs-5 | med | The skill's env check may differ from the CLI's | refuted | Same predicate: unset, empty or `0` is off. Probed with `00`, `false` and padded values: both read them as on |
| redact-docs-6 | med | A masked address can be opened neither with `show` nor as a manifest | fixed | The first half was already fixed in integration. The skill now says the manifest cannot be found from one either, and not to list `<store>/sessions/`, whose names are the ids unmasked |
| redact-docs-7 | med | The PEM edges are untested | fixed | Three of the four were already pinned. The fourth, a second key inside the body, had no test, and `_merged`'s `max(` held it by accident: dropping it leaked the body's tail. It now has an assertion |
| redact-docs-8 | low | "Measured examples" should be pinned | refuted | Re-run: all ten examples still print unmasked. No rule says a measurement must be pinned, and drift could only understate coverage |
| redact-docs-9 | low | "Keeps its columns" overclaims | fixed | Alignment is not kept; the field count is. Now "stays one field of a `recall` line or a `show` header" |
| sizes-1 | low | mutate_index's docstring keeps "1280" and "955" | fixed | As show-2 |
| sizes-2 | low | `#set-it-up-once` may not exist | refuted | USAGE.md has `## Set it up once` (lead) |
| sizes-3 | low | The skill or hook README may show old `+NB` output | refuted | Neither has an output sample. The only `+NB` left is the commit subject, kept by decision |
| disk-1 | med | The test never reads which directory the section says to delete | fixed | Probed: "delete `sessions/`" passed. Every directory a sentence says to delete must now be one the store's `.gitignore` ignores |
| disk-2 | low | The CHANGELOG's link is outside the anchor loop | fixed | Added. A broken anchor there now fails |
| disk-3 | low | "About twice" sits beside a table of 2.3x and 3.4x | fixed | Bounded in watching.md, the README and DESIGN.md: "more if your transcripts compress poorly" |
| disk-4 | low | Three behavioural claims are unverified | refuted | Each is true and pinned: recall and dashboard refuse without an index, `show` does not need one, a rebuild renames into place, `gc --auto` runs |
| disk-5 | low | Only column-0 backtick fences are stripped | declined | No tracked Markdown has a `~~~` fence. An indented fence indents its `#` lines too, and the heading pattern reads column 0 only |

## Decided while applying

**One pin for two findings.** subagents-2 and stale-2 each proposed holding
`docs/REPRODUCE.md` to the counts: the first by adding assertions to the four
count tests, against the collected numbers, and the second with one new test
holding the guide to the README. The second is the one that landed. The README
is already held to the suite, so it needs no second collection, and it runs on
a machine without the corpora too. The first would have held two of the
guide's four numbers only where the corpora are cloned. The cost is one test.

**The re-read made an old control blind.** init-1's second read, under the
lock, raises on a file it cannot read by itself. So the row "init takes a
config.toml it cannot read for a missing one", which mutates the first read's
`except FileNotFoundError` to `except OSError`, survived: the second read
still refused. Both reads now go through one helper, the row's anchor moved
into it, and it is caught again.

## Two findings from outside Kimi's table

| ID | Claim | Disposition | Evidence or reason |
|---|---|---|---|
| opus-item1 | The README's `uv python install 3.13` advice is wrong where uv may not fetch Python | refuted | Item 1 had already limited it to `python-downloads = "manual"`, and says to put a 3.13 on `PATH` under `"never"`. Probed both, offline, uv 0.11.3 |
| daemon-tmp-x | A test on `main` uses `/tmp/x` as a watch root | fixed | Probed: a stray regular file there empties the result and fails the test. The root is now under `tmp_path`, never created, which also pins that a root that does not exist yet is kept |

## Verified

At the state this file describes:

| | |
|---|---|
| Full suite, corpora off | **1114 passed, 17 skipped**, 1 deselected: 1131, the README's number |
| Full suite, corpora on | **1455 passed, 1 skipped**, 1 deselected |
| Collected | 1131 off and 1456 on, 328 conformance cases: the README and §5b of REPRODUCE.md |
| `ruff check` | clean |
| Mutation rows | **658**. The 23 rows that name a test this commit changed or re-pinned, the new one included: **23 of 23 caught**. Three of them, the README-count rows, bite only with the corpora present, which is what they check; they were run with them |

## Final gate

Two more passes read the branch at `b7e3521`, and one commit on top answers
both, the one that adds this section.

- **Kimi K3, three packs:** the code half of the fix commit, its docs half,
  and SKILL.md as it now reads whole. On the code half its verdict was ship,
  with no findings.
- **An Opus gate of five lenses** over the whole branch: security, end to end,
  docs, CI from a fresh clone, and simplicity. Two skeptics took each finding
  and tried to refute it by default, with a probe wherever one could run. 18
  survived both, and 2 were refuted.

**33 findings: 18 fixed, 4 documented, 4 declined, 1 pre-existing and out of
scope, 6 refuted.** *Documented* means the behaviour stays and the docs now
say so. Four are changes of behaviour, A1, A4, A7 and F2, each with a test that
failed first and a new negative control.

### Kimi K3

| ID | Sev | Claim | Disposition | Evidence or reason |
|---|---|---|---|---|
| F1 | high | The fix commit re-ran only the rows whose test it changed, not the rows anchored in the hunks it changed | declined | Answered by a full pass over every row instead of a better selection. A missing anchor already fails: `test_every_mutation_row_anchors_exactly_once` on CI, and `SKIP` in a run. This commit also re-ran each row anchored in or beside a hunk it touched |
| F2 | med | service-3's refutation is wrong: after the `rm`, the unit stays loaded until a `daemon-reload` | fixed | service-3's second report, and its refutation rested on systemd's documentation, not a probe. The reload is harmless and conventional: the printed removal ends `; systemctl --user daemon-reload`, in the code, its test, a new row and watching.md |
| F3 | med | The CHANGELOG's list of what `service install` prints has no `loginctl enable-linger` line, which USAGE says it prints | fixed | It prints one on Linux, and the CHANGELOG now lists it |
| F4 | low | Pin redact-docs-8's measured examples | declined | Each is a shape `--redact` does not mask, so a pin would hold a weakness in place |
| F5 | low | A reflow left one overlong line in USAGE's `--redact` paragraph | fixed | Rewrapped, with A4's new order |
| S1 | med | Rerunning step 1 does not update the CLI | refuted | Live probe, uv 0.11.3, as version-2: rerunning the unpinned line replaced the install's commit with the remote's tip |
| S2 | med | A CLI from before `--version` does not print `the following arguments are required: cmd` | refuted | Measured on `c352a40`: it prints exactly that |
| S3 | med | A topic that starts with `-` is read as a flag, and the error reads as an older CLI | fixed | Probed: `recall "--bogus"` exits 2, `the following arguments are required: query`. The skill now puts `--` before such a topic, after any flags; `recall -k 30 --policy latest --redact -- "--bogus"` parses |
| S4 | low | Drop the `echo "${GITMEMORY_REDACT:-}"` check, since the CLI already defaults to masking | declined | The owner's decision: an explicit `--redact` makes a CLI too old for the flag fail loudly, where the default would print unmasked |
| S5 | low | `${CLAUDE_SESSION_ID}` may not exist | refuted | It is Claude Code's documented substitution in a skill, and the sentence predates this round |
| S6 | low | A masked address cannot occur | refuted | `capture --session-id` takes a credential-shaped id, and `recall --redact` masks it (A5) |
| S7 | low | Split the by-hand `raw/` lookup into numbered steps | declined | It is the fallback for a CLI older than `show`, and is left as it is |
| S8 | low | Rules are repeated: masking three times, the subagent cautions twice, and `-k 30` beside `-k 50` | fixed | Each is stated once: masking in the opening paragraph, what `subagent:` means in the field table, and one retry, `-k 50`, with its reason, that the order applies to the top `-k` matches only |

### The Opus gate

| ID | Lens | Sev | Claim | Disposition | Evidence or reason |
|---|---|---|---|---|---|
| A1 | security | med | `show --redact` stops, exit 2, partway through a turn that holds a lone surrogate | fixed | `mask` encoded with `surrogateescape`, which round-trips only U+DC80-U+DCFF; a JSON `\ud83d`, half an emoji, is any other. It encodes with `surrogatepass` now, as `records` hashes. Tested at the address and in a `-C` neighbour |
| A2 | security | med | The push gate misses a `\b`-anchored key right after a JSON `\n` or `\t` escape | pre-existing, out of scope | Reproduced. The HIGH table is `main`'s, and `push` does not send yet. Left for its own issue; the owner's unpushed egress branch adds a scan of the decoded text that targets this class |
| A3 | security | low | `show --redact` prints a private key's body when its BEGIN line is in an earlier block of the turn | documented | No span is carried across blocks. USAGE's list says a key split across two blocks is masked to the end of the first |
| A4 | security | low | `recall --redact` prints a SUSPECT value with a Unicode space in it, which `show --redact` and the gate flag | fixed | `str.split()` broke the value at a no-break space before the mask saw it. recall now masks as `show` does, with the mask's own collapse, then flattens and cuts |
| A5 | security | low | The address's case-fold residue is not in USAGE, so show-7's evidence is wrong | documented | A bullet in USAGE's list; show-7 corrected above |
| A6 | e2e | low | An AWS or Google key used as the session id prints unmasked, though USAGE, DESIGN and the CHANGELOG say the address is masked | documented | As A5. USAGE and the CHANGELOG now make the exception for a session id, and the DESIGN amendment no longer lists what is masked |
| A7 | e2e | low | `show`'s refusal of a masked address says `as recall prints it` about what recall printed | fixed | A one-line refusal of its own, `a masked address: ...`, still exit 2 |
| A8 | e2e | low | USAGE's `--redact` sample cannot come from the transcript every sample on the page comes from | fixed | It comes from a second transcript now, with its own session, and says so |
| A9 | docs | med | `--redact` leaves a credential-shaped session id unmasked, because ids are stored lowercased; show-7 cites USAGE text that does not exist | documented | As A5 and A6. The ruling was to document it and leave the code |
| A10 | docs | low | While masking, the skill still reads a hit's manifest, whose `source_path` is unmasked and in its original case | fixed | While masking, the skill reads nothing under the store by hand and goes by the address alone |
| A11 | docs | low | USAGE's `--redact` sample puts a different turn at another sample's address | fixed | As A8 |
| A12 | docs | low | The CHANGELOG and SECURITY say the skill passes the flag when the variable is "set and not `0`"; empty is off too | fixed | "set to anything but empty or `0`" in both, as redact-code-3 did for the help |
| A13 | docs | low | The CHANGELOG misses the reworded skip message and keeps the unbounded "about twice" | fixed | Both added |
| A14 | docs | low | The `--redact` caveats and the `settings.json` snippet are kept twice, and the copies differ; some passages run long | fixed | The caveat list and the snippet are in USAGE only; SECURITY and hook/README link them. watching.md's init paragraph and the DESIGN amendment are cut to what a reader acts on |
| A15 | docs | low | The docs index describes watching.md as configuration and flags only | fixed | The row names the service and the disk section |
| A16 | ci | low | REPRODUCE §5b's count is two low on an account named `ubuntu`, `runner` or `root` | fixed | Its commands set `HOME` as the suite's conftest does, and a paragraph says why |
| A17 | simplicity | low | init's join check cannot fire once the appended config parses | fixed | It refuses in the `except`; `have` is gone, and the new table's number is the length of the parsed `watch` array |
| A18 | simplicity | low | `uncollected`'s injected clock has no caller | fixed | Dropped. The `waiting and` the finding called optional is left: it changes nothing, and two rows anchor on it |
| R1 | docs | — | "Nothing leaves the machine" and "the only way anything leaves is `push`" contradict text this branch added | refuted | The tension was on `main` already, where SECURITY.md said the skill's quotes reach the provider unscanned; the added text puts that egress in the agent's path, not in anything gitmemory sends |
| R2 | simplicity | — | `test_recall_redact_keeps_a_subagent_turn_marked` repeats `test_recall_marks_a_subagent_turn` | refuted | By mutation: dropping the mark under `--redact` only fails this test and no other |

### Decided while applying

**A4 moves where recall masks.** The owner's decision put the mask after the
whitespace collapse, so that it would see a PEM header a line break split. But
`str.split()` also breaks at a no-break space, and that collapse was what cut
the value. recall now does as `show` does: the mask's own collapse, of ASCII
whitespace, finds the split header, and recall flattens and cuts after it. The
decision's purpose holds, and recall without `--redact` prints what it did.

**What A1 costs.** In a block that also holds a mask, a lone surrogate prints
as replacement characters, where the same block unmasked prints `?`.

**Rows.** One new row for each change of behaviour brings the index to
**662**. A4 moved three rows' anchors: the two on recall's head line, and
`show`'s collapse row, whose anchor recall's new line repeated. Every row
anchored in or beside a hunk this commit touched was run, the new ones
included: **45 of 45 caught** by their own test.

| | |
|---|---|
| Full suite, corpora off | **1117 passed, 17 skipped**, 1 deselected: 1134, the README's number |
| Full suite, corpora on | **1458 passed, 1 skipped**, 1 deselected |
| Collected | 1134 off and 1459 on, 328 conformance cases: the README and §5b of REPRODUCE.md |
| `ruff check` | clean |

### The full mutation pass

F1 asked for every row, not a better choice of rows, so every row was run:
twice over, on two tips, each split six ways across fresh clones, with no
corpora.

| Tip | Rows | Caught by their own test | Not |
|---|---|---|---|
| `b7e3521`, the Kimi fix commit | 658 | 642 | 16 MISSED, 0 SURVIVED, 0 SKIP |
| this commit | 662 | 646 | 15 of the same 16, MISSED or SURVIVED; 0 SKIP |

None of the sixteen is this branch's. Each was run again on `main`, under the
same two setups the passes used, and each gave the verdict it gave here, row
for row. They are rows whose test cannot run offline here: the README and
conformance counts need the corpora, the dashboard's need the `serve` extra,
and the rest fail the same way on `main`. The sixteenth, the suite-wide home
isolation, is caught on this tip with the real `HOME`: it misses only when the
pass itself overrides `HOME`, so that verdict was the harness's, not the row's.
Every row this branch added or moved is caught by its own test.

The sixteen are a finding about `main`, and they are left for a round of
their own.
