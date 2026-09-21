# E7 — security review of the dashboard surface

One of seven agents in the RC1 security round, each in its own git worktree,
none with a hand in the code it read. This one was given
`src/gitmemory/dashboard.py` and the `dashboard` subcommand in `__main__.py`:
what the served process exposes, to whom, over which socket, and what it fetches
from the network before it starts.

Every finding below is **re-derived independently before it is accepted**, by a
route the reviewer did not use, and every fix is pinned by a named test *and* a
negative control in `tests/mutate_index.py` — the module mutated to remove the
behaviour, the named test required to fail.

## Status

**Eight findings: six fixed, one declined with a reason, one informational.**

| | Finding | Outcome |
|---|---|---|
| F1 | A page the user visits reads the whole store by re-resolving its own name to loopback | **fixed** — the instance denies anonymous access; a single-use sign-in URL is the only way in |
| F2 | Any other process on this machine reads every transcript; `allow_download off` does not change that | **fixed** — same door, and the measurement of what `allow_download` actually covers |
| F3 | A non-loopback `--host` publishes the store, guarded by one line on stderr | **fixed** — a refusal, resolved not string-matched, with `--expose` as the way past |
| F4 | `uvx datasette<2` runs an unpinned datasette resolved at start-up | **fixed** — one range, declared once, and a test that the two agree |
| F5 | The URL is printed before the bind is known to have succeeded | **fixed** — no address of ours; datasette's own line, and a message when it exits non-zero |
| F6 | `description_html` is trusted HTML by contract, one interpolation from stored XSS | **fixed** — the contract is written down and a test holds the metadata invariant |
| F7 | The arbitrary-SQL console is on | **declined** — measured: `default_allow_sql off` blocks root too, and F1's door already gates every path SQL discloses |
| F8 | `--immutable`, stated precisely | informational — already covered by the start-up digest line |

---

## F1 and F2 — loopback is not a boundary, in two directions

The two findings are one door seen from two sides, so they were reproduced and
fixed together.

**Reproduced first, on a synthetic store.** 1500 blocks were planted, each
carrying a distinct `CANARY_SECRET_*` string, and served exactly as `gitmemory
dashboard` serves a real store. A single unauthenticated request —
`GET /gitmemory/blocks.csv?_stream=1`, with `Host: evil.example` — returned 200
and **1500 of 1500 canaries**. No cookie, no token, no same-origin check
anywhere in the path.

That is F2 directly: anything on the machine that can open a socket reads the
store, and the socket has no owner check — loopback carries no uid. It is also
F1, because the origin tuple never changes under DNS rebinding. A page at
`attacker.example` that re-resolves its own name to `127.0.0.1` is *same-origin
with itself*; the same-origin policy does not apply, CORS is not in the
conversation, and the server-side half is that Datasette validates `Host`
against nothing.

`--setting allow_download off` was measured and is not the fix. It blocks
exactly one route — downloading the `.db` file itself — and leaves every table
page, every `.csv?_stream=1`, and `/-/databases.json` open.

**The fix is Datasette's own machinery, and it was verified before it was
trusted.** `metadata["allow"] = {"id": "root"}` plus `--root` on the command
line. Measured on 0.65.5:

| Request | Before | After |
|---|---|---|
| `/` anonymous | 200 | 403 |
| `blocks.csv?_stream=1` anonymous | 200, 1500/1500 canaries | 403 |
| `blocks.json` anonymous | 200 | 403 |
| `/-/databases.json` anonymous | 200 | 403 |
| the same with `Host: evil.example` | 200 | 403 |
| `/gitmemory.db` | 200 | 403 |
| with the token URL | — | 200, canaries present |
| the token URL a second time | — | 403 |

Each half is inert alone, which is why both are in the fix and both have a
control row: `--root` on its own denies nobody, and `allow` on its own locks the
owner out of the instance with no way back in.

The way in is the `/-/auth-token?token=<64 hex>` URL Datasette prints on stdout
at start-up. It is **single-use** — the second request with the same token is
403 — and the `ds_actor` cookie it sets is scoped to the host in that URL. That
is what closes the rebinding route specifically: a page at `attacker.example`
never receives a cookie scoped to `127.0.0.1`, so it is back to being anonymous,
which is now 403.

One thing the test deliberately does *not* assert: an *authenticated* request
carrying `Host: evil.example` still returns 200 under `urllib`, because the
cookie jar keys on the real URL host and hands the cookie over regardless of
what we put in the header. That is an artefact of driving the client by hand,
not the rebinding model — a rebound page is the anonymous case. So the test
asserts the anonymous one.

## F3 — the string set was wrong in both directions

The control in front of publishing every transcript the developer owns to the
LAN was one line on stderr, while the URL went to stdout — separated in any pipe
or log — and the server came up with exit 0 regardless.

It was also wrong about what it was guarding. The old check compared `--host`
against a three-element string set, so `--host localhost` printed "serving the
store on localhost, which is not loopback", which is false, and a warning that
cries wolf is the one people learn to click past. `127.0.0.2` is an ordinary way
to give a local service its own address.

`_is_loopback` now resolves through `getaddrinfo` and requires *every* returned
address to be loopback. Twelve spellings are in the test:

| Treated as here (exit 0) | Treated as away (exit 2) |
|---|---|
| `127.0.0.1`, `localhost`, `::1`, `[::1]`, `127.0.0.2`, `127.1`, `::ffff:127.0.0.1` | `0.0.0.0`, `::`, `""`, `192.168.1.50`, `example.invalid` |

`""` and an unresolvable name both come back False, so the refusal is the
default for everything unrecognised rather than a hole. The consequence changed
too: exit 2 with a message naming the flag, not a warning beside a working
server. `--expose` is the way past, and it still says on stderr what it is
doing.

## F4 — one range, declared once

`uvx datasette<2` resolved whatever the environment's index served at that
moment, which is not the range `pyproject.toml` declares for the `serve` extra.
`UVX_SPEC = "datasette>=0.65,<1"` is now the single spelling, and
`test_uvx_runs_the_range_the_project_declares` parses `pyproject.toml` with
`tomllib` and asserts the declared datasette spec list is exactly
`[dashboard.UVX_SPEC]`. Verified that `uvx` accepts a PEP 508 range:
`uvx 'datasette>=0.65,<1' --version` → 0.65.5.

This bounds which datasette runs. It does not authenticate the index it comes
from; that is a property of the environment's package index, and the fallback
exists precisely so a machine without datasette installed can still serve.

## F5 — no address before there is a bind

`serve()` printed `http://127.0.0.1:8081/` and then called datasette. If another
process already held the port, the user had a URL in their terminal that pointed
at that process. Our line is gone: the address the user follows is now the
sign-in URL datasette itself prints, after it has the socket. A non-zero exit
prints `datasette exited <rc>; nothing is serving <host>:<port>` on stderr
rather than leaving uvicorn's traceback to speak for us.

## F6 — the contract, and a test that it holds

`description_html` is unescaped by contract — that is what the key is for, and
the front page uses it. Nothing in the metadata is derived from the store today,
so there is nothing to inject, but nothing said so and nothing tested it.

Both contracts are now in `metadata()`'s docstring, and
`test_the_metadata_is_the_same_whatever_the_store_holds` runs `serve()` against
two different stores with a fake `subprocess.call` that captures the metadata
file Datasette would read, and asserts the two are byte-identical. The first
attempt at this test was vacuous — `metadata(name) == metadata(name)` cannot
fail, and `metadata` never saw a store — which is why it goes through `serve()`.

## F7 — declined, with the measurement

`--setting default_allow_sql off` was tried and **blocks root as well as
everyone else**, so it would cost the owner the SQL console outright. What the
console discloses over the table pages is the on-disk path, the schema, and a
cheap DoS — and after F1, every one of those paths is behind the sign-in, so an
attacker who can run SQL is an attacker who already has the token. The setting
costs the owner a real feature and buys nothing an attacker could have used. The
reasoning is recorded in `command()`'s docstring so the next reader does not
re-litigate it from scratch.

## F8 — informational

`--immutable` is a promise to SQLite that no other process will write the file,
not a lock. `index.build` renames a fresh file over the path, so a dashboard
left running across a rebuild serves the old inode. That is already the subject
of the start-up digest line and its test; nothing changed here.

## Negative controls added this round

Nine rows, all run, **9/9 CAUGHT by their intended test**.

| Mutant | Verdict |
|---|---|
| the instance allows anyone again | CAUGHT |
| no sign-in url is offered, so nobody can get in | CAUGHT |
| a bind outside loopback warns instead of refusing | CAUGHT |
| loopback is a string set again, not a resolution | CAUGHT |
| `--expose` stops being the way past the refusal | CAUGHT |
| uvx resolves a wider range than the project declares | CAUGHT |
| the address is printed before datasette has it | CAUGHT |
| a failed bind is left to uvicorn to explain | CAUGHT |
| something derived from the store reaches the metadata | CAUGHT |

Three existing rows were re-pointed at rewritten lines and re-run: "the uvx
fallback runs an unpinned datasette", now anchored on `UVX_SPEC`; and the two
F3 rows, which changed meaning as well as anchor — "a loopback alias is
**warned about** as if it were public" became "a loopback alias is **refused**
as if it were public", and "a public bind is not warned about at all" became
"a public bind is not noticed at all". All three still CAUGHT.

One row was written and dropped: it anchored on a line in
`tests/test_dashboard.py` while declaring `dashboard.py`, so it could only ever
score SKIP. Mutating a test is not a control, and the first two rows already
cover the `allow`/`--root` pair from both sides.

**One harness fix came out of this round.** The `--root`-removed mutation made
`_serving` hang rather than fail: datasette prints nothing to stdout when there
is no token to print, and `proc.stdout.readline()` on a pipe blocks forever. A
test that hangs under the mutation it most needs to catch is a WEDGED verdict,
not a catch. `_serving` now redirects stdout to a file and polls it, so the
mutation FAILs cleanly.
