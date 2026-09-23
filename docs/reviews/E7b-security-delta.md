# E7b — the delta the RC1 security review did not read

The E7 security round ran at `8ce0cc3`. **4,966 lines landed after it**, 723 of
them in `src/` and `hook/`. The gate for this epoch is *no unresolved findings*,
which is a claim about the code being pushed and not about the code that was
reviewed, so this round is the difference. The brief the four lenses were given
is `docs/tasks/E7b-security-delta-brief.md`, committed alongside this file: it
fixes the threat model, so that "the derive command hangs" and "the agent
stalls" are not rated the same, and it forbids the reviewers the same things it
forbids the code.

Four lenses, each in its own worktree, none with a hand in what it read:

| Lens | Surface |
|---|---|
| L1 | input-dependent cost in the 31 compiled patterns of `derive.py` |
| L2 | the store, index and hook delta — `_GEN_RE`, `timed_out`, the shim |
| L3 | what happens to derived text downstream — graph, index, dashboard |
| L4 | the no-owner-data invariant, over the new bench and test code |

A fifth reading, Gemini's pair review, ran against the same range.

Every finding below was **re-measured here before it was accepted**, and four of
them changed shape under that: one severity was wrong, one count was wrong, one
was not a defect at all, and one came with arithmetic that contradicted itself.
Those are recorded as findings against the reports, not quietly corrected. A
fifth was withdrawn by the lens that raised it, which is recorded too.

## Status

**22 findings. No unresolved finding remains, which is the E7 ship gate.**

| Severity | Raised | Changed | Accepted as-is |
|---|---|---|---|
| High | 4 | 4 | — |
| Medium-High | 1 | 1 | — |
| Medium | 5 | 4 | 1 — L4-F2, not a defect |
| Medium-Low / Low-Medium | 3 | 3 | — |
| Low | 6 | 5 | 1 — Gemini-F1, measured at 1.4% |
| Informational | 3 | 2 | 1 — L2-F8, ceiling already pinned |
| **Total** | **22** | **19** | **3** |

*Changed* means something landed — code, comment, documentation or test.
*Accepted as-is* means nothing did, and each of the three says why where it
appears. By lens: L2 eight, L4 six, L3 four, L1 three, Gemini one. The four High
are `_MID`'s exponential (L1), the case-variant manifest (L2), the label that
scans one string and publishes another (L3), and the guard blind to Claude
Code's flattened path (L4) — **one per lens**, which is the argument for having
run four rather than one.

Three commits carry the earlier half: `810b9e3` (L2 F1–F5, L4 F1 and F6),
`2bd6392` (L3-F1, L3-F4), `cf5320c` (L4 F3–F5). The rest landed with this
document in `3a3b144`: L2-F6 (the shim's tracing guard) and L2-F7
(`hook/README.md`), L3-F2 (`safe_text` on the index path), L3-F3 (`MAX_CHARS`),
and all three of L1 — F1's two-token deletion in `_MID`, and F2 and F3 as
comments.

Every source fix has a negative control. Six mutation rows were added or
re-anchored this round and a targeted pass caught **6 of 6** by the intended
test; the index stood at **517** rows. The two exceptions are stated where they
occur and neither is silent: L4-F3's control is the doc test itself, run by hand
because the test is corpus-gated and skips on a fresh checkout, and the two
documentation-only dispositions (L1-F2, L1-F3) have nothing to mutate.

Verified at the state this document describes:

| | |
|---|---|
| Full suite, corpus present | **1231 passed**, 1 deselected |
| Fresh checkout, corpus off | **896 passed, 13 skipped** — 909, the README's number |
| `ruff check` | clean |
| New/re-anchored mutation rows | 6/6 caught by their intended test |

Two things are recorded here rather than buried in their sections, because a
reader deciding how much of this document to trust should not have to find them.
**L1 withdrew a finding of its own** before it reached this file — a measured
claim that `derive.py`'s documented 9.1 s ceiling was a 2–4× understatement,
which turned out to be `tracemalloc` inflating wall time about 11×; re-measured
with tracing off, 8.940 s, and the docstring is honest. And **one instruction I
sent a lens mid-round was wrong** — I told L1 to move scratch files it had not
written; it checked, declined, and said so. Both are written up in the L1
section.

---

## The bug this round kept finding

Five of the accepted findings are the same shape, and it is worth naming because
it is cheaper to look for than to trip over: **a check and an enumeration that
ask different questions.**

| | The check | The enumeration | The gap |
|---|---|---|---|
| L2-F1 | `os.path.exists`, case-insensitive on APFS | `glob`, case-sensitive | a case-variant manifest |
| L2-F4 | `$` in a Python regex | `fnmatch` | a trailing newline |
| L3-F1 | the secret scan, on the block | the label, published after collapsing | a key the collapse assembles |
| L3-F4 | `records.UNSAFE` | `derive._INVISIBLE` | four characters |
| L4-F1 | `FORBIDDEN`, three patterns all requiring `/` | Claude Code's flattened `-Users-…` | the account name |

None of them is a subtle bug. Each is two lines of code that a reader would tell
you mean the same thing, written far enough apart that nobody put them side by
side. The pattern to take away is not "audit your regexes"; it is that wherever
one piece of code decides a thing and another piece of code *acts* on the same
decision, the two spellings of it are a defect waiting for an input.

---

## L2 — the store, index and hook delta

Eight findings: five fixed in `810b9e3`, two fixed here, one recorded.

### F1 — a case-variant manifest name (High, fixed)

The attested-generation gate calls `os.path.exists` on the manifest path and the
sweep enumerates with `glob`. On APFS the first is case-insensitive and the
second is not, so `G00.json` beside `raw/…/g00/` attests a directory the sweep
never names: unattested transcript bytes, spoken for by nobody. Reproduced and
fixed in `810b9e3`.

### F2 — `_abandoned` discards `timed_out` (Medium-High, fixed)

A slow capture made `verify` accuse a sound store, and said nothing about the
lock it had failed to take. Fixed in `810b9e3`.

### F3 — the lock was taken per file (Medium, fixed)

One `flock` held by any same-uid process cost `verify` 5 s per manifest and per
stray candidate. Measured before: **15.01 s at n=2, 25.05 s at n=4**. After:
**5.02 s and 5.01 s** — flat, which is the property, not the number. Fixed in
`810b9e3`.

### F4 — `$` matches before a trailing newline (Medium, fixed)

`_GEN_RE`'s `$` accepted `g00.json\n`; `fnmatch` in the sweep did not. `\Z`.
Fixed in `810b9e3`.

### F5 — every lock error read as "not timed out" (Medium-Low, fixed)

`verify` ran fully lockless and reported nothing. The errnos are now
distinguished and `ELOOP` is named. Fixed in `810b9e3`.

### F6 — the verbose guard's position (Low, fixed here)

`set +v` narrows the `SHELLOPTS=verbose` door rather than closing it: `verbose`
echoes input as the shell *reads* it, so everything above the guard is already
on stderr — which in this file meant the 25-line comment explaining the guard.
1,723 bytes of the shim's own prose into the agent's stderr at every compaction.

Nothing is disclosed (verbose echoes source text, so
`H="${GITMEMORY_HOME:-$HOME/.gitmemory}"` prints unexpanded), which is why it is
Low. It is the noise the shim's opening paragraph promises not to make, and the
report's sharpest observation is that **the residual is proportional to the
comment above the guard** — the E7 fix made its own leftover bigger.

The guard is now line 2 and the explanation is below it. Re-measured against the
file as it now stands: **90 bytes** with the guard on line 2, **9,491** with it
deleted. `test_under_verbose_the_shim_echoes_two_lines_because_the_guard_is_the_second`
pins the line *count* rather than a byte budget, because one more comment line
above the guard is one more line on the agent's stderr, and that is the failure
mode.

### F7 — `SHELLOPTS=noexec` disables the shim silently (Low, documented)

0 bytes of output, 0 spool files, against 1 spool file on a clean run. No line
in the script can defend against it: `-n` is read before anything executes and
POSIX gives `set +n` no effect in a non-interactive shell. Documented in
`hook/README.md` under "What a broken run can cost", with the matrix
re-measured against the edited shim — `errexit` and `nounset` still write the
record, `xtrace` and `verbose` are handled, `noexec` is the one that is not.
Anything that can set `SHELLOPTS` for your agent can set `BASH_ENV`, which is
arbitrary code execution, so this costs you the memory system and not more.

### F8 — the timeout early-return reopens the skip asymmetry (Informational)

`_verify_unattested` attests a directory on manifest existence while
`_verify_one` declines to enumerate it under a timed-out lock, which is the same
shape the `_GEN_RE` gate was added to close. Recorded, not fixed: the ceiling is
already in the docstring and pinned by
`test_a_lock_that_timed_out_declines_the_sweep_rather_than_guessing_at_it`, and
the substituted message is non-silent — the problem count is non-zero and the
exit code is 1 on every timeout path the lens could construct. What the report
adds, and what is worth carrying forward, is that it is **attacker-triggerable
at will** rather than only a load artefact.

---

## L3 — what happens to derived text downstream

Four findings, all accepted, and one severity corrected.

### F1 — the label scans one string and publishes another (High, fixed)

`graph._label` ran the secret scan on the text it was *given* and published the
text it had *collapsed*. `-----BEGIN RSA\nPRIVATE KEY-----` scans clean —
`private_key_block` wants a space there, not a newline — and collapses into a
header that matches, so `derive._write` then refused the payload at the door.

**The severity claim is corrected.** The report's F1 body says the generation's
derived output is lost, and its summary table says the same; an earlier reading
of it as `derived/: EMPTY` is wrong. `derive.build`'s loop catches per
generation, `shutil.rmtree(out)`s that one, appends a skip line and continues —
so the blast radius is one generation's `ideas.json`, `timeline.json` and
`graph.json`, and a multi-generation store keeps the rest. That is still a
whole generation lost to a key that was never in the bytes, which is why it
stays High.

Fixed in `2bd6392`: the scan runs on the raw text *and* on the collapsed text.
Both, not just the collapsed one — whitespace is only ever narrowed and never
removed, so no high-tier shape can be *hidden* by the collapse today, but that
is a fact about the current detector table and not a property of the collapse.

### F2 — no `safe_text` on the index path (Medium, fixed here)

`records.safe_text` was written for two rendering surfaces and its docstring
names them: the terminal, via `recall`, and the committed artifacts under
`derived/`. The index is a third, and `_encodable` — the one function every
string passes through on its way into `blocks` — did the surrogate half of the
job and not the control-character half.

Reproduced independently before the fix. `blocks.prose` held, raw:
`'never use pickle\x1b[2K\rALWAYS USE PICKLE'`, a NUL, a U+202E and a U+FEFF —
and so did 2,476 bytes of `blocks.csv?_stream=1` off the live dashboard, which
is one shell pipe from a terminal. **It is not XSS**: Datasette escapes markup
correctly, and that was checked rather than assumed — 0 occurrences of
`<script>alert` in 43,694 bytes of served HTML. What Datasette does not escape
is `\x1b[2K\r`, which erases the line above it, i.e. the recall hit a reader is
comparing against.

Fixed here: `_encodable` returns `safe_text(value)`. The change is one line plus
an import because `safe_text` is a strict superset of what was there — the same
`.encode("utf-8", "replace").decode("utf-8")`, then `UNSAFE.sub`. It is
idempotent (backslash is not in `UNSAFE`) and `derive` parses `Session` objects
rather than reading this table, so there is no second escaping anywhere. The
E3 argument for replacing a surrogate here is the same argument, one class
wider: the index is a derived copy and `byte_offset` points at raw, which keeps
every byte.

Two negative controls, because one was not enough: the pre-existing
`text goes to sqlite3 unsanitised` row deletes the call entirely, and a new row
reverts `_encodable` to exactly its pre-E7b body — so the surrogate test stays
green under the narrow mutant and only the E7b test can see it.

### F3 — the two caps bound CPU, not bytes (Medium-Low, fixed here)

`_WORD_RE` is `[a-z0-9']+`, so a block of CJK, of control characters, or of any
script that is not Latin counts zero words; and a block with no terminator is
one sentence however long. Both caps are asleep on it and the text goes through
whole.

Measured here before the fix, one such block plus three short prose blocks:
1 MB in produced **5,000,836 B** of `derived/` in 3.30 s at 270 MB peak RSS,
2 MB → **10,000,836 B** in 5.27 s, 4 MB → **20,000,836 B** in 10.50 s at
656 MB. A clean 5.0× and linear: `safe_text` spells `\x01` as six characters and
`canonical_json` spells a CJK character as a six-character `\uXXXX`. `derived/`
is not in `gitrepo.GITIGNORE` and the daemon commits with `git add --all`, so
the amplification lands in history, where deleting the file does not get the
bytes back. That is what makes it a cap rather than a `# ponytail: fine`.

Fixed here: `MAX_CHARS = 400_000`, checked **before** `_leaks` rather than after
it — a sentence the cap rejects is never published, and scanning four megabytes
of it for a secret that cannot escape is 16 `finditer` passes the cap exists to
refuse. After: 883 B of `derived/` from all three inputs, flat, in 0.47 / 0.50 /
0.99 s, with the three real sentences beside the pathological one still ranked
(`sentences_seen` 4, `sentences_ranked` 3, `chars_ranked` 81).

**The number is calibrated so that it is a backstop and not a second truncation
point.** Over the 92 parseable transcripts of the conformance corpus the largest
generation ranks 56,986 characters against 8,220 words — 6.93 characters per
ranked word, spaces included — which puts `MAX_WORDS` at about 347,000
characters of English, and the longest single sentence anywhere in that corpus
at 29,821. A test asserts the ratio rather than the constant, so lowering
`MAX_CHARS` under the word cap fails rather than silently truncating sessions.

### F4 — two gates disagreeing about "invisible" (Low, fixed)

`records.UNSAFE` did not hold U+00AD, U+180E, U+2060 or U+FEFF, which
`derive._INVISIBLE` has enumerated since E5 for exactly the reason `safe_text`
exists. They agree now. Fixed in `2bd6392`.

---

## L4 — the no-owner-data invariant

Six findings: four fixed, one dispositioned as not a defect, one informational
and already implemented.

### F1 — the guard is blind to the flattened path (High, fixed)

Claude Code names a project directory by flattening the absolute path, so
`/Users/<owner>/work/x` becomes `-Users-<owner>-work-x`, and not one of the
three `FORBIDDEN` patterns matches that — all three require a literal `/`. It is
not an exotic encoding; it is the name of every directory under
`~/.claude/projects`, which is to say the single most likely spelling for an
owner path to arrive here by accident.

Fixed in `810b9e3` by asking the question directly instead of through a spelling
of it: `_account()` reads the account name off `~` at run time and is never
written down, `GENERIC_ACCOUNTS` turns the scan off on a CI runner rather than
letting it fire on prose, and `test_the_account_name_scan_is_on_or_says_why_it_is_not`
makes an off scan say so. The report's other half — a generic `-Users-<any>-`
pattern — was **rejected**: this repository legitimately contains 173 of those,
because the corpus manifest keys each item by its path inside the pinned
third-party clone, and a pattern that cannot tell a public fixture locator from
an account leak would be answered with an allowlist over the file with the most
to hide.

### F2 — 5,798 paths behind the one filter the scanner honours (Medium, not a defect)

Measured here: **0 in-scope hits.** All 80 files are under gitignored paths —
`.venv/`, `.conformance/`, `graphify-out/` — which `git ls-files` does not
report and `pushable_objects` cannot reach, because git will not push them.
Widening the scan to ignored paths is how a guard gets switched off: it would
report the third-party clone's own directory names on every run, and the first
response to a guard that cries wolf is an allowlist over the file that has the
most to hide. Accepted as designed, with the measurement recorded here so the
next reader does not have to take it on trust.

### F3 — a claim in the wrong file (Medium, fixed)

`bench/secondary.py`'s docstring said "no transcript text is committed" and
`docs/benchmarks/E5-secondary-set.md` said "the corpus is not vendored", while
the write-up quotes verbatim runs out of those sessions. The quoting is right —
an argument about whether a particular sentence is a directive cannot be made
without the sentence — and the claim was wrong, so the claim was corrected and
the quotes kept. Count pinned at **8** runs of 40 characters or more; the report
said 9, and the difference is whitespace normalisation, which is why the test
that holds the line normalises before it counts.

`test_the_secondary_write_up_quotes_what_it_says_it_quotes` parses the number
out of the prose and matches the quotes against the real clone, so the sentence
and the count cannot drift apart. It is corpus-gated and skips when the clone is
absent, so it has **no mutation row**; the negative control was run by hand, by
flipping the number in the doc and watching it fail. Fixed in `cf5320c`.

### F4 — the corpus walk follows a symlink out of the corpus (Low-Medium, fixed)

A corpus is somebody else's directory. `store._open_source` opens a transcript
with `O_NOFOLLOW`; the two corpus walks did not, so a `*.jsonl` symlink planted
in the corpus is read through — and the one thing this suite must never read is
this machine's own history. Python 3.13's `rglob` does not descend symlinked
*directories* by default, so the leaf link is the whole hole.

Both walks now resolve and bound: `path.resolve().is_relative_to(base)`. The
three identical loops in `bench/secondary.py` became one `transcripts()` helper,
which is also where the subagent-transcript rule now lives instead of in
triplicate. Fixed in `cf5320c`.

### F5 — an exported-but-empty corpus variable (Low, fixed)

`os.environ.get(k, default)` returns `""` for `export FOO=` — the default is for
*absent*, not for *empty*, and `export GITMEMORY_CC_FIXTURES=` is what a shell
script whose variable did not expand looks like. Measured: **410 collected cases
became 88**, the same silent disappearance the comment above `CC_FIXTURES`
already warns about, reached through a different door. `or`, not a `get`
default. Fixed in `cf5320c`.

### F6 — the account name is already in the object graph (Informational, implemented)

The report's practical half — that the account name is already known to this
repository 140 times over, so a fourth pattern built from it would be free — is
what F1's fix does. `_IDENTITY` excludes the `author `/`committer ` lines of a
commit object, which are where that authorship legitimately lives, so the
conventional 140 are not findings and a blob carrying the same name is.

---

## Gemini's pair review

Gemini 3 read the same range across all four lenses in its own worktree, wrote
`REVIEW-e7b.md` there, and opened with the attestation the brief requires. It
returned **one finding, Low**, and passed the other three lenses as sound. The
finding is not fixed, and the *reason* it found only one is the more useful half
of this section.

### Finding 1 — redundant work past `MAX_SENTENCES` (Low, not fixed)

Correct as a description: once `len(owners) + len(kept)` reaches
`MAX_SENTENCES`, every remaining sentence in the transcript still pays
`_leaks` and `to_words` before being discarded. Not fixed, for two reasons.

The remedy Gemini proposes — break out of the loop, or hoist the cap above the
scan — is wrong on the larger half. `_leaks` past the cap is **not** redundant:
it is what makes `sentences_redacted` a count of what leaked rather than a count
of what leaked in the first 2,000 sentences. Deleting it changes a reported
number. Measured here, that half is 88% of the cost.

What is genuinely redundant is `tok.to_words(text)` on line 476, computed one
line before the cap check that throws it away. Measured on this machine,
per skipped sentence: `_leaks` **7.2 µs**, `to_words` **0.96 µs** — both flat
across 20k / 40k / 80k sentences. End to end, `ideas()` on Gemini's own two
cases:

| | sentences seen | ranked | `ideas()` |
|---|---|---|---|
| Case A | 2,000 | 2,000 | 2.973 s |
| Case B | 52,000 | 2,000 | 3.425 s |

The 50,000 skipped sentences cost **0.452 s**, which matches 50,000 × 9.0 µs of
loop, and the redundant part of it — `to_words` alone — is **48 ms**, or 1.4% of
the run. That is a one-line move for 1.4%, in a function whose documented
ceiling is 9.1 s, and it is not worth the mutation row it would need to stay
honest. Recorded rather than fixed.

The asymmetry with L3-F3 is deliberate and worth stating, because the two caps
sit four lines apart and are ordered against `_leaks` in opposite directions.
`MAX_CHARS` goes **before** the scan because the sentence it rejects can be four
megabytes on its own, and 16 `finditer` passes over four megabytes is the cost
the cap exists to refuse. `MAX_SENTENCES` goes **after** it because the sentence
it rejects is an ordinary one, the scan of it costs 7 µs, and the count stays
true. Cheap per item and the answer is used → scan. Unbounded per item and the
answer is discarded → do not.

Gemini's own numbers were self-refuting and are worth recording as such: it
reported Case A at 1.838 s and Case B at 1.700 s — 26× the input, *less* time —
and then subtracted the two to claim "~1.64 seconds wasted", a figure derived
from a hypothetical 0.06 s baseline rather than measured. It ran concurrently
with four other lenses on a loaded machine. The finding survives that; the
arithmetic does not.

### Where it agreed, and the two places that is informative

Lenses 2 and 4 it passed, and L2 and L4 independently found eight and three
things in them. Lens 3 it passed on the specific ground that *"every string
written to `derived/` is fully sanitized by `records.safe_text`"* — which is
true, and was true, and is the wrong writer. L3-F2 is the **index** writer,
`_encodable`, which did not call `safe_text`; Gemini checked the sanitised path,
confirmed it, and did not ask whether there was a second one. Its dashboard
reading is the same shape and lands the other way: it verified that
`metadata()`'s captions are module literals, which is exactly the right question
and the right answer.

The L1 pass is the sharpest case, because it tested the *right pattern* and
reached the opposite conclusion. From its report:

> `_BACKREF` performance with extremely long chains of `_MID` elements followed
> by failure showed strictly linear cost: `n = 1,000` units: 0.000811s,
> `2n = 2,000` units: 0.001612s.

Reproduced here to three significant figures with `have` as the repeated token —
0.786 ms and 1.478 ms — so that is the shape it built. `have` is in `_MID`'s
literal list and matches nothing else. The exponential needs a token matched
**twice**: `only`, which is in the list *and* is matched by `_ADV`'s `\w+ly`.
Pre-fix, twenty-one of those cost 1.955 s where two thousand `have` cost 1.5 ms.

The lesson is not that Gemini was careless — it chose the construct L1 chose,
ran n and 2n as the brief asks, and got a clean linear answer. It is that for
ambiguity-driven backtracking **the adversarial input is not a long input, it is
an input the pattern can match two ways**, and scaling n on the wrong token
proves linearity forever. L1 found it by cross-checking `_MID`'s members against
`_ADV` rather than by timing, which is also why the test that pins it is
structural.

---

## L1 — input-dependent cost in the extractor

Three findings: one fixed here and it is the most severe thing this round found,
two documented rather than coded. The lens measured every compiled pattern in
`derive.py` and `index.py` at n and 2n on an adversarial shape chosen per
construct — about 90 measurements, ~75 of them tabulated — which is why the two
that are *not* defects are worth as much as the one that is.

### F1 — `_MID` backtracks exponentially (High, fixed here)

`_ADV` leads with `\w+ly`. `_MID` listed `only` and `previously` as literals
beside it. Two ways to match the same span, inside a group under `*`, is 2ⁿ
paths through the group.

Re-measured here before accepting it, `_BACKREF.search(". as" + " only" * n + " x")`:

| n | bytes | before | after |
|---|---|---|---|
| 14 | 76 | 16.5 ms | 0.019 ms |
| 16 | 86 | 62.8 ms | 0.016 ms |
| 18 | 96 | 251.8 ms | 0.017 ms |
| 20 | 106 | 1.006 s | 0.018 ms |
| 21 | 111 | 1.955 s | 0.018 ms |
| 2000 | 10,006 | — | 1.530 ms |

×1.95 per *added token*, across every point and across L1's sixteen; it reached
8.23 s at n=23 and extrapolates to roughly nine hours at 181 bytes. The payload
is single-spaced ASCII, so `_flatten` is the identity on it and the `_BLANK_RUN`
remedy does not apply. It is reachable through the real entry point —
`derive.build` → `graph.extraction` → `decisions()` → `_decision_kind` →
`_flatten` → `_BACKREF.search` — at 1.040 s on a 104-byte block.

**High, on an availability-only threat model, for three reasons that are about
the store and not the pattern.** `derive.build` rebuilds every generation on
every run over an append-only store, so the poisoned block is re-processed
forever. Its per-generation `try/except` cannot catch this: a `re` call that
returns in nine hours is not an exception, and there is no timeout or size guard
anywhere on the path. And the failure is silent — an unattended batch command
that hangs has nobody watching it, and the next run hangs at the same byte. Two
hundred bytes permanently disables `gitmemory derive` for that store. It cannot
touch the interactive agent, which is what holds it below critical.

Fixed by deleting the two literals, which is L1's own preferred remedy over
making the group possessive: `\w+ly` already matches both at the same span, so
**no accepted language changes** — verified on five probes including
`as only previously kept noted`, all matching the identical span before and
after, and by the full suite.

The test is **structural, not by-example**: no member of `_MID`'s literal list
may be matched by `_ADV`. An example test would pin `only` and say nothing about
the next `-ly` word someone adds, which is exactly how these two arrived. A
second test times the real entry point at n=24 with a 1.0 s budget against a
0.15 ms actual, to catch an ambiguity introduced from the other direction — a
word added to `_ADV` that the literal list already holds — which the structural
test cannot see. The mutation row restores `previously` and is killed by the
structural test without timing anything, because a row that only a stopwatch can
catch is a flaky row.

### F2 — the caps bound time, not memory (Low, documented)

Both caps are consulted only after `to_sentences` has materialised the whole
block, so peak memory is bounded by the block and not by them: a flat **21×**
amplification, linear on both axes, measured to 4.8 MB in. A 48 MB block — the
size `derive.py`'s own comment says one transcript can reach — peaks near 1 GB
while still ranking exactly 2000 sentences. `decisions()` is 15×, `index._paths`
6.7–7.2×.

Not fixed, and L1's own recommendation is not to: it is a constant factor rather
than a complexity defect, the consequence is a `MemoryError` or a swap storm
rather than a hang — noisy and recoverable, unlike F1 — and the fix would be to
stream the split, which is a structural change to buy a constant. What it was
was a **documentation defect**: the cap comment introduced the two caps as
bounding "the two costs" and the reader had no way to know memory was not one of
them. The comment now says which axis is not bounded and what bounds it.

### F3 — `_MARKUP` is quadratic under `search` (Informational, documented)

`_MARKUP` ends in `.*?</\1>`, so on unclosed tags every `<a_b>` is a start
position that scans the rest of the block: ×3.99 and ×4.00 per doubling,
5.7 s on `("<a_b>" + "x" * 40) * 8000`. Its only caller uses
`_MARKUP.match(text, pos)`, which is anchored — one start position per
iteration, ×1.98 on the same inputs. **No live defect**, and the docstring's
linearity claim is true.

Recorded because the safety property lives in the call site while the comment
above the pattern documents a different hazard, and reaching for `finditer` to
find markup anywhere in a block is the obvious next feature. `_all_markup`'s
docstring now names the anchoring as load-bearing and carries the number.

### The retraction, recorded here because it is the reason to trust the rest

L1 reports that an earlier round measured `ideas()` at a flat ~18 s and
"2000 × 25 identical words 38.6 s", which would have made the 9.1 s ceiling in
`derive.py`'s cap comment a 2–4× understatement — a finding. It was an artefact:
`tracemalloc` was active and inflates wall time on that workload by about 11×.
Re-measured with tracing off, 8.940 s against a documented 9.1 s. **The ceiling
is honest**, and the lens says so having tried to break it. It also withdrew an
overclaim in its own summary and two mis-attributed symbols during a final prose
pass. None of that is in this document because it was retracted before it
reached it; it is recorded because a reader who saw only the surviving numbers
would have no way to know they had been checked.

### One correction against this document, not against the lens

Mid-round I sent L1 a message telling it to move scratch files it had supposedly
written into the repository root. **That was wrong** — the files were another
agent's, `git status` was clean throughout, and L1 correctly declined to act on
it and said why. Recorded because an unverified instruction to a reviewer is
exactly the kind of thing the brief forbids the reviewers from accepting, and it
came from the wrong direction.

---

## What this round did not cover

- **The E5 extractor backlog is not a security matter and is untouched here.**
  The `_PROHIBIT` residue, `_DELIBERATION` on bare set-nouns, the declarative
  standing rule, the `_REPAIR`/`_PIVOT` deictic ordering and the rest are recall
  and precision, and both held-out seeds are spent.
- **No second reviewer read the fixes.** Each is pinned by a named test and,
  where the harness can express it, by a negative control in
  `tests/mutate_index.py`; that is a weaker claim than the round that produced
  the findings, and it is the claim being made.
