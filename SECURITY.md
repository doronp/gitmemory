# Security policy

gitmemory stores the most sensitive file a developer has: a full record of
their agent's terminal. This page covers how to report a vulnerability, what
is in scope, and what to do when a credential ends up in the store.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

- **Preferred:** report it privately through GitHub:
  [Security → Report a vulnerability](https://github.com/doronp/gitmemory/security/advisories/new).
- **Or email** the maintainer at the address on [their GitHub profile](https://github.com/doronp), with `gitmemory security` in the subject.

Include the version or commit, what you did, what happened, and a proof of
concept if you have one. Use a synthetic transcript: never send real session
history, whether yours or anybody else's.

| | Target |
|---|---|
| Acknowledge the report | 3 working days |
| First assessment and severity | 10 working days |
| Fix or public advisory | 90 days from the report, sooner for anything actively exploited |

We credit reporters in the advisory unless they ask us not to. The project is
pre-1.0, so security fixes land on `main` and in the next release. There are no
maintained back-branches.

## Supported versions

| Version | Supported |
|---|---|
| `main` | yes |
| 0.1.x | yes, once released |

## Scope

In scope: anything in this repository that runs on a user's machine. That
means capture, the store and its contiguity proof, `verify`, the redaction gate
behind `push`, the index, `derive`, the dashboard, the watcher and the hook shim.

Worth reporting, for example:

- a transcript (untrusted input) that makes capture, `verify`, `index` or
  `derive` crash, hang, write outside `$GITMEMORY_HOME`, or cost superlinear time
- a way to make `verify` accept a store whose segments do not tile, or whose
  bytes do not match the recorded digest
- a secret that the redaction gate lets through on `push`
- the dashboard being reachable without the sign-in URL, or writable

Out of scope: vulnerabilities in the third-party dependencies themselves
(report those upstream; we will bump the pin), and anything that requires an
attacker who already has your user account.

## How the code has been reviewed

Before this repository was published it went through two security rounds. E7
covered six surfaces and produced 68 findings; E7b reviewed the 4,966 lines that
landed after E7 and produced 22. Every finding is recorded with its fix or with
the measurement behind accepting it: [E7 round](docs/reviews/E7-security-round.md),
[E7 pair review](docs/reviews/E7-pair-review.md),
[E7b delta](docs/reviews/E7b-security-delta.md). No independent third-party
audit has been done.

## If a credential lands in the store

It will. A transcript is a recording of a terminal, and terminals print tokens.
The design answer is that `raw/` stays on your machine and the gate stands at
`push`; the operational answer is shorter:

1. **Rotate the credential.** Do this first and do not wait for anything below.
   The store is append-only and local, so the blob is in your history whatever
   you do next, and a rotated key is worth nothing to anyone holding it.
2. **`gitmemory push` will refuse, and that is working.** Note that `push` runs
   the redaction gate only and does not send yet. It scans the files git
   would ship, the segment seams, *and* the object graph a push transmits —
   deleting the file does not make the push clean, because `git push` does not
   send the working tree.
3. **If you must un-say it,** the store is an ordinary git repository, so the
   ordinary history-rewriting tools apply — `git filter-repo` is the usual one,
   and it is not vendored here. Run `gitmemory verify` afterwards: it will tell
   you whether the segments still tile. This is not automated and will not be —
   a memory system that silently edits its own history is not one you can quote
   from.

There is no override flag. A gate you can wave through on a deadline is a gate
that gets waved through on a deadline.
