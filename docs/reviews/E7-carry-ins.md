# E7 — carry-ins

The RC1 security round ran seven agents in seven worktrees. Six of them reported
inside their own surface; the seventh's report, and a handful of findings the
others raised about code that was not theirs, land here. Same discipline as the
rest of the round: **every finding is re-derived independently, by a route the
reporter did not use, before it is accepted.**

## Status

| | Finding | Outcome |
|---|---|---|
| S1 | A manifest the gate cannot read opts its generation out of the seam scan | fixed — `UnreadableManifest`, `sessions(strict=)` |
| S1b | The refusal, and every other exception, prints an unmasked path | fixed — `_safe_exc` |

---

## S1 — a `continue` in the gate's feed is not a skip, it is a bypass

`store.sessions` skips a manifest it cannot read. That skip is right, and there
is an E2 regression test for it: a reader that raised would make one
half-written manifest — what a full disk leaves behind — un-indexable for the
whole store.

`segment_groups` is not a reader. It is the gate's feed, and the *only* thing
that produces the ordered segment runs `redact.scan_group` joins across. A
credential cut in half by a segment boundary is invisible to every per-file
scan; the seam join is what sees it. So a skip there does not lose a row, it
loses exactly the class of secret the group scan exists for — while `push` goes
on printing `gate passed`.

One byte does it, by anything that can write inside the store:

```
honest                       groups=1 per-file-high=[]        seam-high=['openai_api_key']
start is a string            groups=0 per-file-high=[]        seam-high=[]
segments is a dict           groups=0 per-file-high=[]        seam-high=[]
manifest not json            groups=0 per-file-high=[]        seam-high=[]
```

Reproduced through `segment_groups`/`scan_group` directly rather than the
reporter's route through the CLI and a `config.toml`, so that the demonstration
does not depend on the push path being set up at all. `verify` reports the same
manifest as broken; `_push` does not call `verify`.

The same round's `pushable_objects` (F1) does **not** close it. That change scans
the object graph, and blobs are scanned individually — the straddling secret is
still in two halves.

**The fix is the strict/lenient split, not a third code path.** `sessions` grew
`strict: bool = False`; `segment_groups` is the only caller that passes `True`.
A reader wants as much of the store as it can get and the gate wants all of it
or nothing, and those are the two settings.

Two details the tests pin:

- `_Skip`, a private exception, so the two hand-written rejections (`segments`
  is not a list; a segment entry names no usable path) leave by the same door as
  a `json.JSONDecodeError`. Two ways out of one loop body is how the `strict`
  check gets added to one of them and forgotten on the other — there is a
  mutation row that makes one of them a bare `continue` again.
- A file matching `g*.json` that is not a generation name is still skipped under
  `strict`. `gfoo.json` claims no segments; refusing over it would be F4a's
  over-refusal one directory down. `verify` reports it and the file walk scans
  it.

`_push` catches `UnreadableManifest` next to `EscapingSegment` and prints the
refusal with the reason and the command that explains it. Uncaught it would
reach the top-level handler and print `error:` with a traceback's vocabulary —
the store still does not push, but "error" and "refusing to push" are different
sentences to the person reading them at 3am.

## S1b — the message carries the path, and the path can be the credential

Found by S1's own mutation run rather than by a reviewer. The last mutant's
captured stderr read, in full:

```
error: /…/sessions/claude-code/sess/g00.json: Expecting property name … line 1 column 2
```

The path, unmasked, out of the top-level handler. F3 established that the gate
must not publish what it caught, and fixed the channel F3 was looking at. This
is the same shape on a channel nobody had looked at: a generation is named after
a session id, a session id is chosen by whoever calls `capture`, and an `OSError`
carries whatever path it failed on. Reproduced with a missing source file named
`ghp_AAAA…`, which printed it twice — once from the parse-failure warning, once
from the handler.

`_safe_exc` masks it at all three sites. The path stays readable either side of
the mask, which matters here more than anywhere: the refusal's whole job is to
send you to `verify` knowing which file to look for. A
`json.JSONDecodeError` says "line 1 column 2" and names nothing, so the strict
raise always prefixes the path — that prefix is what there was to mask.

**Deliberately not in the `print` shim**, whose docstring otherwise makes exactly
the centralising argument ("the one that gets forgotten is the vulnerability").
Escaping is lossless; masking is not. `recall` prints transcript content because
that is what it is for, and a store on the owner's own disk is not an egress —
masking there would make the tool withhold the user's own bytes from them.
Egress is `push` and `serve`. An exception is neither: it is the third case,
nobody asked to see it, and it is the one that leaks by accident.

The `{home}` and `{path}` in `not a store:`, `no index at`, and `is not a git
repository` are left alone. Those echo an argument the invoking command line
already recorded, so masking them removes nothing from the transcript.

## Negative controls

Ten rows, 296 total. **10/10 CAUGHT by their intended test.**

| Row | Test |
|---|---|
| an unreadable manifest goes back to being a skip | `…refuses_the_push_instead_of_passing_it` |
| the gate reads the store as leniently as a reader does | `…refuses_the_push_instead_of_passing_it` |
| a non-list segments field skips without telling the gate | `… and segments as a dict` |
| the readers become as strict as the gate | `test_the_readers_still_skip_what_the_gate_refuses` |
| the refusal stops naming which manifest it cannot read | `test_the_readers_still_skip_what_the_gate_refuses` |
| an unreadable manifest leaves by the traceback handler | `test_push_names_the_unreadable_manifest…` |
| the refusal prints a session id that is itself a credential | `test_the_refusal_masks_a_session_id…` |
| the top-level handler prints an exception's credential | `test_a_credential_in_an_exception_message_is_masked…` |
| exception text stops being masked at all | `test_a_credential_in_an_exception_message_is_masked…` |
| the parse-failure warning prints the path it could not read | `test_a_credential_in_an_exception_message_is_masked…` |
