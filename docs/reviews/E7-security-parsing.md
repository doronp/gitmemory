# E7 — security review: untrusted input and parsing

Standalone round, per the project's process: the reviewer is separate from the
author, it worked in its own worktree
(`.claude/worktrees/agent-a57ab2227389843e8`), and it edited nothing in the
tree. Scope: `src/gitmemory/jsonl.py`, `src/gitmemory/records.py`,
`src/gitmemory/adapters/claude_code.py`, `tests/test_claude_code.py`,
`tests/conformance.py`.

This is the surface where every byte is chosen by someone else. A transcript is
written by an agent, and the agent's transcript contains whatever the agent
read: a fetched HTTP body, a file it was pointed at, a tool result. There is no
trust boundary *inside* a session file — the whole file is the other side of
one.

**Funnel: 15 raw → 15 verified → 12 fixed, 3 accepted with reasoning, 0
refuted.** Every one of the fifteen reproduced. That is not a compliment to the
reviewer's aim so much as a statement about the surface: at this layer almost
anything you can think to try, you can make happen.

**Constraint check: clean.** The reviewer's attestation is reproduced in §5. It
read nothing under `~/.claude/` or `~/memory/`; every `find_session` call passed
an explicit `projects_root` under `/tmp`, so the `~/.claude/projects` default was
never taken; the daemon was never run. Every repro of mine used scratch homes
under `/tmp`. Nothing from this machine's own history entered the repository —
the standing hard constraint on this project, and it held on both sides.

## How a finding was adjudicated

The standing rule from `E3-findings.md` applies: **a finding is CONFIRMED only
with a reproduction I ran myself, by a route the reviewer did not use.** F2 was
reproduced end-to-end through `gitmemory capture` + `gitmemory index` and a
SQLite read, not by reading `jsonl.py`. F13's symlink loop was counted by
building one, not by reasoning about `**`.

And every fix landed with a test that fails when the fix is reverted, checked
mechanically rather than asserted: **32 new negative controls, 32/32 caught by
their intended test.** The suite went 1,032 → 1,063 tests, the control index
340 → 372.

---

## CONFIRMED — reproduced, fixed, and covered by a failing-on-revert test

| # | Sev | Site | Finding | Commit | Test that now fails without the fix |
|---|---|---|---|---|---|
| F1 | high | `records.py` | A transcript can hide a `tool_use` behind a look-alike text block: the turn digest omitted the block's kind and tool name, and the identity namespace was untagged, so a `uuid` of `"@211"` and an uuid-less line at byte 211 shared one slot | `5838515` | `test_a_line_that_ran_a_command_is_not_the_line_that_mentioned_it`, `test_the_digest_separates_a_tool_use_from_the_text_that_quotes_it`, `test_a_uuid_that_looks_like_an_offset_is_a_different_namespace` |
| F6 | low | `claude_code.py` | `billable_usage` merged three identifier namespaces into one key, so a later line could zero out an earlier request's bill | `5838515` | `test_a_uuid_that_spells_a_request_id_does_not_erase_that_request` |
| F3 | medium | `claude_code.py` | `message.role` overrode the line type, so a user line was billed as a model call | `c96ec21` | `test_the_line_type_decides_the_role_not_the_message` |
| F5 | medium | `claude_code.py` | The first line's `sessionId` is re-hashed once per turn — unbounded, so 2.6 MB of input cost 7 s of CPU | `8e514c7` | `test_a_long_session_id_is_bounded_before_it_reaches_every_turn`, `test_two_long_session_ids_stay_two_sessions` |
| F4 | medium | `jsonl.py` | No bound on a physical line. Measured in a fresh interpreter: a 33.6 MB line peaked at 266 MB RSS, a 134.2 MB line at 1,002 MB — 7.5–7.9× the line, because the line is copied by `strip`, decoded, parsed, re-sliced and re-encoded | `7e3c4e3` | `test_a_line_past_the_cap_is_refused_before_it_is_read` |
| F2 | medium | `jsonl.py` + `conformance.py` | One undecodable byte discards every remaining object on its line, counted once, and the accounting identity could not see it | `7f491ff` | `test_a_fused_line_that_stops_parsing_says_how_much_it_dropped` |
| F7 | low | `claude_code.py` | `estimate_cost` raised `OverflowError` on a large token count; a negative one billed a refund | `dfcb705` | `test_a_token_count_too_big_for_a_float_does_not_crash_the_cost_column` |
| F11 | low | `claude_code.py` | No length bound on `model`, `request_id`, `ts`, or on `usage` *keys* — each written once per turn into canonical JSON and once into the index | `dfcb705` | `test_the_other_identifiers_are_bounded_too_including_the_usage_keys` |
| F8 | low | `tests/` | Floats *do* reach canonical JSON; the docstring and the test said they could not | `5d15b0b` | `test_a_float_from_a_transcript_does_reach_canonical_output` |
| F13 | nit | `claude_code.py` | `session_files` followed symlink directory loops: one `agent-1.jsonl` under a self-linking `subagents/` yielded 16 extra paths, and `rollup_usage` parses every path returned — the same tokens billed 17 times | `5d15b0b` | `test_a_symlink_loop_bills_a_subagent_file_once` |
| F14 | nit | `claude_code.py` | `isSidechain` was truthiness, so the string `"false"` made a turn a subagent's | `5d15b0b` | `test_a_sidechain_flag_is_a_boolean_not_a_truthy_string` |
| F15 | nit | `jsonl.py` | A UTF-8 BOM made the whole first line undecodable, counted as a decode error | `5d15b0b` | `test_a_byte_order_mark_does_not_cost_the_first_line` |

### Notes on four of them

**F1 is the finding of the round**, and it is the one no fuzzer was going to
reach: the forgery requires `uuid` to equal `"@" + str(the next line's byte
offset)`. Structure-aware reasoning found it; 7,013 random and mutated cases did
not. The fix tags the identity namespace and puts `kind` and `tool_name` inside
the digest, so a text block and a tool call can no longer hash to one id.

**F2 had two sides, and only fixing one would have been worse than fixing
neither.** A parse failure part-way along a physical line abandons the rest of
that line; that is correct — concatenated JSON has no resynchronisation point
that isn't a guess — and it is not the finding. The finding is that the
abandonment left no trace. Reproduced through the CLI: a 844-byte transcript,
one line holding five well-formed objects and one truncated fragment, produced
one indexed turn, `records_seen=2`, a balanced accounting identity, and every
conformance check green. 685 bytes — 81% of the file — vanished silently.

`LineTruncated` names the skip, but only when the line had already yielded an
object: a line that fails at offset zero dropped nothing beyond itself, and
naming every malformed line "truncated" would make the counter say nothing. The
second side is `conformance.py`. `_input_is_recounted_independently` was the
check that made the accounting identity non-vacuous — and it calls
`iter_records`, so it agrees with the reader by construction and catches only
drops the *adapter* makes. A reader that loses input balanced against itself.
`_every_line_produced_a_record` shares nothing with the reader. It is a floor,
not an equality, deliberately: one line can hold several objects, so an exact
independent count means reimplementing the fused-line reader, and a recount that
is a second copy of its subject is two bugs waiting to agree. Its independence
is tested rather than claimed — the floor runs with `jsonl.iter_records`
replaced by something that raises.

**F5 and F11 are one fix wearing two numbers**, and the bound was measured
before it was chosen. Over claude-code-log's 162 fixtures (4,571 records) the
longest `model` is 26 characters, `requestId` 28, `timestamp` 27, `usage` key
27, `uuid` 36. `_MAX_ID = 128` is 3.5× the longest real value of any of them,
and is the same 128 the path rule already used, so the two bounds cannot drift
apart. One constant, not five, for the same reason.

The over-long value is **truncated with a digest, never elided to a marker**.
Two long values have to stay two values: a `model` collapsed to a constant
merges two models' spend, and a rejected session id falls back to the filename
stem, which two transcripts can share. The same reasoning extends the bound to
dict *keys* in `_scrub`, which was the one string here nothing bounded — a
100,000-character `usage` key reached the record verbatim.

**F7's fix is a clamp, not a rejection.** An absurd token count reading as
`$0.00` would say "free". Clamping into `[0, 2**53]` — where float stops
counting integers exactly — keeps the estimate monotonic: nonsense input
produces a visibly enormous number rather than a quietly wrong small one.

---

## ACCEPTED — reproduced, ruled not a defect, pinned so the ruling cannot rot

These three are recorded rather than dropped, because "we looked and decided
not to" and "nobody noticed" are indistinguishable a year later unless the
first one is written down.

### F9 — a raw invalid byte and its `\udcXX` escape hash identically

Reproduced: the two files produce the same `turn_id`.

**Not a defect.** `turn_id` hashes parsed *content*, so two spellings of one
value colliding is the contract, exactly as a literal `x` and its `x`
escape collide — and nobody would file that. The layer that must be injective
over bytes is the store, and it is: the two files get different `file_sha256`,
different segment digests, and different session keys. The manifest is a
commitment to the raw bytes; the turn id is not, and is not supposed to be.

Both halves are pinned by
`test_an_invalid_byte_and_its_escape_are_one_record_and_two_files`, so a future
change that makes the store's side collide too will fail rather than pass
quietly.

### F10 — `find_session` on APFS: case-insensitive match, non-`realpath` return

Reproduced. On a case-insensitive filesystem the containment check is a string
comparison against a case-insensitively-resolved namespace, so a legitimate hit
whose casing differs from the caller's root is refused.

**Document-only, no code change.** `realpath` is applied to both sides and the
failure direction is closed: an escape is *rejected*, never admitted. What is
left is a false negative on a legitimate path with mismatched case. A fix here
means either case-folding the comparison — which weakens a containment check on
the one filesystem where containment is hardest to reason about — or walking the
directory to recover the on-disk casing, which introduces a TOCTOU window where
there is currently none. Both candidate fixes are worse than the bug. Revisit
only if a real caller hits the false negative.

### F12 — the `_scrub` elision marker is forgeable and not injective

Reproduced: a line that writes the elision marker itself is indistinguishable
from a line that was elided.

**Accepted.** No in-band marker is unforgeable by an in-band forger; this is a
property of in-band signalling, not a bug in this marker. The mitigation is
structural and already present: `native` holds the verbatim text, so the
forgery is visible to anything that looks at the source rather than the
projection. Previously that was an assumption; it is now asserted by
`test_an_elision_marker_is_forgeable_and_native_is_the_answer`.

---

## What the reviewer tried and could not break

Reproduced here in condensed form because a negative result is evidence and is
worth as much as the findings — these are the attacks that are now known not to
work, so the next round need not re-run them:

- **ReDoS.** All three regexes in scope are linear — one anchored character
  class with bounded repetition, no nested quantifier, no overlapping
  alternation. 200,001-char adversarial input: 0.7–1.7 µs each.
- **Deep nesting.** Bounded at both layers. `_scrub`/`_flatten` cap at
  `_MAX_DEPTH = 32`; `json`'s `RecursionError` and bare `ValueError` are caught
  in the reader. Depth 5,000 → `[nesting too deep]`, transcript intact; depth
  200,000 → one counted skip and the following line survives.
- **NaN / Infinity.** Converted to text by `_scrub`; `canonical_json(allow_nan=False)`
  never sees one. Every `to_canonical` field traced.
- **Encoding.** Lone high and low surrogates, raw invalid UTF-8, NUL in block
  text *and* in `role`: all parse, all hash, all survive `to_canonical()` as
  pure ASCII that `json.loads` reads back. Byte offsets stay true across
  multi-byte content and across a second object on the same line.
- **No transcript field reaches a filesystem path.** Stated positively because
  it is the question this round most expected to answer badly. Three
  independent places get it right: `store.session_id_for` derives from the
  *source path*, `store._safe` charset-checks and lowercases every component,
  `derive.derived_dir` rebuilds from the manifest's already-vetted location, and
  `index._turn_row` writes `stored.session_id`, not `turn.session_id`.
  Traversal, absolute paths, NUL, `..`, `PATH_MAX` and macOS case folding are
  therefore unreachable *from a transcript field*.
- **Type confusion.** 13 hand-built hostile shapes, 4,000 generated JSON
  structures over the adapter's real key names, 3,000 byte-level mutations of a
  valid transcript. 7,013 cases, zero uncaught exceptions, zero conformance
  violations.
- **Skip-reason cardinality.** `_safe_type` caps the key shape at 40 chars and
  `_MAX_REASONS` caps the count; worst case 52 chars and 65 keys, both inside
  the contract. Held under fuzz.
- **Determinism.** Every probe re-parsed byte-identically, and the project's own
  gate is stronger than anything the round added:
  `test_canonical_output_is_identical_in_a_fresh_interpreter` runs the whole
  corpus in two subprocesses under two `PYTHONHASHSEED`s.

---

## Carried forward

Named here so they are visibly open rather than quietly unexamined:

- **`redact.py` and the secret-survival question** — another round's. This round
  established the parsing-side input to it: `usage` keys, `usage` values under
  1,024 chars, `compactMetadata`, `role`, `model`, `request_id` and `ts` all
  pass from a transcript into derived output. Whatever the gate covers, it has
  to cover those and not just block text.
- **SQLite/FTS5 on NUL and lone surrogates.** `index._encodable` re-encodes with
  `errors="replace"` before insert; the consequences of that lossy re-encode
  belong to the index round.
- **`find_session`'s `~/.claude/projects` default** has no production caller and
  no test pins that. Make `projects_root` required and add an autouse fixture
  that scrubs `HOME`/`GITMEMORY_HOME` — carried into the carry-ins round as S11.
- **A long fuzz campaign.** 7,013 cases over two generators is a coverage
  argument, not an exhaustive one. F1 is the standing evidence for why that
  matters: no fuzzer was going to guess the `uuid` forgery.
- **Whether the MIT corpus itself contains hostile shapes.** It runs green; its
  162 fixtures were not audited for adversarial content.

---

## 5. Reviewer attestation, reproduced

> I read nothing outside the repository. In particular I read nothing under
> `~/.claude/` and nothing under `~/memory/`. `claude_code.find_session()`
> defaults its root to `~/.claude/projects`; every call I made passed an
> explicit `projects_root` under `/tmp/gmsec/`, so that default was never taken.
> I did not run the daemon or `gitmemory watch`. The only path outside the
> worktree I executed is the interpreter `.venv/bin/python`, as directed. No
> files were added to the repository. No `sudo`, no `crontab`.
>
> No text in the repository reads as an instruction addressed to a reviewer. A
> targeted grep for injection phrasings (`ignore all previous`,
> `disregard … instructions`, `you are now`, `mark this as safe`, `<!-- ai …`)
> returned nothing. The docs directory contains ordinary review prose that
> mentions "reviewer"; that is documentation, not an instruction.

Checked independently: no file the reviewer touched is in the repository, and
`tests/test_no_owner_data.py` — which scans tracked *and* untracked files for
this machine's own paths, with a non-vacuity floor and a positive control — is
green.
