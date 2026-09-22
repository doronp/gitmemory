> **Attestation.** Files read: exactly two — `README.md` and `docs/DESIGN.md`, each
> read once with the `Read` tool. Commands run: none — no shell at all, and in
> particular no `rg`, `grep`, `find`, `ls`, or `git` over this repository. Nothing
> under `src/`, `tests/`, or `docs/benchmarks/` was read, listed, or searched, so
> the extractor and the earlier probes are unseen. I read nothing outside this
> repository, in particular nothing under `~/.claude/` or `~/memory/`. Every
> utterance below is fiction I invented for this probe; none of it is anybody's
> real history.

# E5 probe D — items

Written blind by an agent that read only `README.md` and `docs/DESIGN.md`.
Domain: aviation line maintenance software. Frozen before scoring.

| # | Label | Utterance | What a correct extraction would say |
|---|---|---|---|
| 1 | NOT-A-DECISION | Does the scheduler already know which stations are ETOPS-capable? | — |
| 2 | REVERSAL | Actually, drop the ETOPS flag from the deferral form — put the check back on the task card the way it was before I asked. | The earlier instruction to move the ETOPS check onto the deferral form is revoked; the check returns to the task card. |
| 3 | DIRECTIVE | Never write a task card without a tail number. | A task card may not be created without a tail number. |
| 4 | NOT-A-DECISION | The old contractor never versioned the task cards — each revision just overwrote the last one. | — |
| 5 | DIRECTIVE | Deferral records stay in UTC. | Deferral records are stored in UTC. |
| 6 | REVERSAL | Forget the rule I gave you about rounding ground time to five minutes — store the raw block-in and block-out seconds. | The five-minute rounding rule for ground time is withdrawn; raw block-in and block-out seconds are stored. |
| 7 | NOT-A-DECISION | Add a column for the defect code to the snag list. | — |
| 8 | DIRECTIVE | Use the station's local stores API, not the central one. | Part lookups go to the station-local stores API rather than the central stores API. |
| 9 | REVERSAL | Scratch what I said about splitting deferrals by category — keep A through D in one table. | The earlier instruction to split deferrals into per-category tables is withdrawn; categories A to D stay in one table. |
| 10 | DIRECTIVE | Every task card carries the AMM reference it was raised against. | A task card always records the AMM reference it was raised against. |
| 11 | NOT-A-DECISION | EASA says the tech log entry has to be signed within 24 hours of the work. | — |
| 12 | REVERSAL | I was wrong to have you key the work package on the tail number — key it on the check package ID instead, the way it was originally. | The earlier instruction to key work packages on tail number is reversed; the key is the check package ID. |
| 13 | DIRECTIVE | A deferral never closes without a reference to the rectification work order. | Closing a deferral requires a reference to the rectification work order. |
| 14 | NOT-A-DECISION | We could maybe move deferrals to the new table once the C-check backlog clears. | — |
| 15 | REVERSAL | I told you earlier to block dispatch on an open category-A deferral; undo that — ops control wants a warning banner instead. | The earlier rule blocking dispatch on an open category-A deferral is revoked; show a warning banner instead. |
| 16 | DIRECTIVE | Dispatch checks run the MEL first and the CDL second, always in that order. | Dispatch checks evaluate the MEL before the CDL. |
| 17 | NOT-A-DECISION | Pull the last thirty deferrals for tail G-ABCD and print them with their expiry dates. | — |
| 18 | REVERSAL | Reverse the change I asked for on stores: line stations can raise a part request without a hangar approval step after all. | The earlier instruction requiring hangar approval for line-station part requests is reversed; no approval step. |
| 19 | DIRECTIVE | From here on, turnaround duration is measured block-in to block-out. | Turnaround duration is measured from block-in to block-out. |
| 20 | NOT-A-DECISION | Three rows in the deferral table still have a null station code. | — |
| 21 | REVERSAL | Change of mind on station codes: go back to the IATA three-letter ones after all, not the ICAO four I asked you for. | The earlier switch to four-letter ICAO station codes is reversed; three-letter IATA codes are used. |
| 22 | DIRECTIVE | No change to the tech log tables ships without a migration script in the same commit. | Tech log table changes must be accompanied by a migration script in the same commit. |
| 23 | NOT-A-DECISION | Where does the turnaround board get its block-in time from — the tech log or ops control's feed? | — |
| 24 | REVERSAL | Cancel my earlier instruction that every task card needs a certifying engineer's ID at creation; it can stay empty until sign-off. | The earlier requirement for a certifying engineer ID at task card creation is cancelled; it is required only at sign-off. |
| 25 | DIRECTIVE | Record releases to service on the EASA Form 1 fields, not the operator's internal release form. | Release-to-service records use EASA Form 1 fields rather than the operator's internal form. |
| 26 | REVERSAL | That instruction I gave you to log every MEL lookup to the tech log audit trail — withdraw it, it is drowning the trail. | The earlier instruction to log every MEL lookup to the tech log audit trail is withdrawn. |
| 27 | NOT-A-DECISION | It might be worth letting stores see the turnaround board, though I'm not sure it survives an access review. | — |
| 28 | DIRECTIVE | Part serial numbers keep their leading zeros — store them as text. | Part serial numbers are stored as text so leading zeros are preserved. |
| 29 | REVERSAL | Ignore what I said about hiding closed snags from the turnaround view — show them, greyed out. | The earlier instruction to hide closed snags from the turnaround view is revoked; they are shown greyed out. |
| 30 | DIRECTIVE | When a check package and a line task want the same ground time, the check package wins. | On a ground-time conflict, the check package takes precedence over the line task. |
| 31 | NOT-A-DECISION | Rename `task_card_no` to `task_card_ref` in the scheduler module. | — |
| 32 | REVERSAL | Earlier I said never auto-extend a deferral. Take that back — a category-B deferral may auto-extend once if maintenance control has signed the extension. | The earlier prohibition on auto-extending deferrals is retracted; a category-B deferral may auto-extend once with a signed extension. |
