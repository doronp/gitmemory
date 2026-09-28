# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""The localhost dashboard: Datasette over the index, and nothing of our own.

Datasette is Apache-2.0, reads SQLite, ships faceting, filtering, charts, CSV
and JSON export, and a stable URL for every query. Writing a web UI to show
eight tables next to that would be the single worst trade in this repository.
So this module writes no UI: it points Datasette at the index, hands it
a metadata file that says what the numbers mean, and gets out of the way.

Three things it is careful about.

**`--immutable`.** The index is derived and rebuilt from raw, but a dashboard
that can write to it is a dashboard that can silently become the only copy of
something. Datasette opens it read-only and, with `--immutable`, promises SQLite
nothing else will write either.

**`127.0.0.1`, and a sign-in.** A transcript store is the most sensitive file a
developer owns. The default bind is loopback and the flag to change it is
`--host`, which is a thing somebody has to type — but loopback is not a
boundary. It has no uid check, so any process that can `connect()` reads the
store; and a web page the user visits can reach a loopback port by DNS
rebinding, because the origin it was served from never changes. So the
instance denies anonymous access (`allow: {id: root}`) and Datasette prints a
single-use sign-in URL at start-up. That cookie is scoped to the host in the
URL, which is what closes the rebinding route: a page at `attacker.example`
does not get it. [E7 dashboard-F1/F2]

A host, not a port: browsers send a `127.0.0.1` cookie to every port on
`127.0.0.1`, and Datasette sets it readable from script and with no expiry. So
`_PLUGIN` adds `HttpOnly`, which keeps a page served by some other local dev
server from reading it through `document.cookie`. It does not stop that other
server receiving the cookie with a request the browser makes to it; nothing
cookie-shaped can, and that server is a local process which could connect to
this one anyway. The cookie dies with the process: Datasette signs it with a
secret generated at start-up, so a restart revokes it. [SEC-2]

**The metadata is generated, not shipped.** Datasette keys table descriptions by
database *name*, and the name carries the schema version (`gitmemory-v2.db`), so
a checked-in YAML would silently stop matching on the next schema bump and the
descriptions would vanish with no error. Generating it from `SCHEMA` means the
one place that can be wrong is the one place that is already right. It is JSON,
which Datasette accepts and which is stdlib — no PyYAML for eighty lines of
prose.

**No CSP and no `nosniff`, deliberately.** Datasette 0.65.5 — what `uv.lock`
pins and what the `serve` extra resolves to — sends neither on any HTML page it
serves (measured in-process through `httpx.ASGITransport` against `/`, `/<db>`
and `/<db>/<table>`: both headers absent — **Grounded**) and has no setting
that would: the `Setting(...)` table in `app.py` is page size,
facets, SQL limits, `allow_download`, cache TTL, `base_url` and the debug
flags, and nothing about response headers. Adding them means either an
`asgi_wrapper` plugin — a dependency this project would then own and pin —
or serving Datasette behind our own ASGI app, which trades a module that
writes no UI for a web server we maintain. The exposure being bought is
small: every endpoint is 403 without the root token, and the one sink that
takes unescaped HTML (`description_html`) is fed module-level literals that a
test holds constant. Revisit if the dashboard ever renders anything a
transcript can reach.

"Sends neither" is about the HTML pages, not about Datasette entire:
`blob_renderer.py:44` does send `nosniff`, on `?_blob_column=` downloads. The
earlier version of this paragraph said 0.65.1 and said it three times, marked
**Grounded**, against a lock file that has pinned 0.65.5 throughout — a source
mark on a version nobody resolved. The conclusion held; the citation did not.
[review: opus 6]

See DESIGN.md §2.9 for the panels and, more importantly, for the refusals: the
`dash_unmeasured` view is not decoration, it is the part of this dashboard that
keeps the rest of it honest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import index

__all__ = ["command", "metadata", "serve"]

HOST = "127.0.0.1"
PORT = 8081  # not 8001: Datasette's default collides with half the world

# There was a `LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})` here,
# with a comment calling it "the security boundary". It was neither: the CLI
# resolves `--host` and tests `ipaddress.ip_address(...).is_loopback`, which is
# the check that catches `127.0.0.2` and a name that resolves to 127.0.0.1, and
# it has not read this set since. A dead constant that claims to be a boundary
# is worse than no constant. [E9, review: 9]

# What each view is for, in the words a person reading the dashboard needs
# rather than the words the SQL uses. Keyed by view name; anything not listed
# simply gets no description, which is how a new view fails — quietly and
# harmlessly — instead of by breaking the page.
_TABLES = {
    "dash_corpus": (
        "How much store there is, per agent. <b>stored_turns</b> and "
        "<b>stored_blocks</b> are per-generation sums, so a conversation the "
        "store forked is counted once per generation — that is the store's size, "
        "not the conversation's length. <b>unparseable</b> is the count of "
        "generations the index could not read: if it is not zero, every other "
        "number on this page is a count over less than the whole store."
    ),
    "dash_growth": (
        "Stored turns and bytes per day, UTC. A turn whose timestamp is missing "
        "or unreadable is absent rather than zero — an agent that does not stamp "
        "its turns is invisible here, not small."
    ),
    "dash_contiguity": (
        "One row per conversation. <b>generations</b> above one means the store "
        "forked it — usually a compaction. <b>compactions</b> counts boundaries "
        "the manifest carries, which is the thing this project exists to keep. "
        "<b>stored_turns</b> counts turn rows, and a fork replays turns, so it "
        "exceeds the number of turns in the conversation."
    ),
    "dash_requests": (
        "One row per billable request, deduplicated across generations and "
        "across subagent files. Usage is repeated on every turn of a request and "
        "is cumulative, so a naive sum over turns over-counts by 2.79x on a real "
        "transcript."
    ),
    "dash_spend": (
        "Tokens by model. <b>cache_read_pct</b> is a saving against <i>sending "
        "the same prompt uncached</i> and against nothing else. It is not a "
        "saving against not having sent it. A NULL share means nothing went in "
        "to serve from cache. No dollar figure: a price table goes stale silently "
        "and a stale price is worse than no price. Read it with "
        "<b>dash_unbilled</b>: it is a floor, not a total."
    ),
    "dash_unbilled": (
        "Usage this dashboard will not bill, and why. A turn with no request id "
        "cannot be deduplicated — its cumulative usage would be counted in full, "
        "which is the 2.79x error — so its tokens are shown here instead of being "
        "added above or dropped in silence."
    ),
    "dash_unmeasured": (
        "<b>Read this one.</b> Every table above counts something that was "
        "observed. These are the questions a count cannot answer, and the ones a "
        "memory system is most tempted to answer anyway."
    ),
    "blocks": "The retrieval corpus — one row per content block. This is what BM25 searches.",
    "turns": "One row per message. <b>usage</b> is the raw JSON the agent reported.",
    "generations": "One row per generation the store holds, parseable or not.",
    "meta": "Schema version and the content digest this index was built from.",
    "fts": "FTS5's inverted index over <b>blocks</b>. Not meant to be read directly.",
}

# The front page. It said "Injection cost is a cost and is shown as one" and
# nothing on any page showed it: no hook records injected tokens and no column
# holds one. The sentence was worse than a missing panel — injection cost was
# also absent from `dash_unmeasured`, so a reader who checked the refusals would
# have concluded it was among the measured numbers. It is now a refusal, and the
# claim here is only about what is on the page. [E6 review]
_DESCRIPTION = """
<p>Built from a gitmemory store: agent transcripts, captured verbatim, committed
to git, indexed here. The database is opened <b>immutable</b> — nothing on this
page can change the store, and the store is not the only copy of anything.
You reached it through a one-time sign-in link, because loopback is not a
boundary: a socket on this machine has no owner check — anything that can
connect, not only what runs as you — and a web page you visit can reach a
loopback port by re-resolving its own name to 127.0.0.1.</p>
<p><b>Frugality, stated exactly.</b> Cache-served share is a real saving, against
the same prompt sent uncached and against nothing else. There is no <i>tokens
saved</i> tile, because the comparison it would need — the same work without
memory — has never been run, and no <i>injection cost</i> tile, because nothing
in this system injects anything yet. Both are named in
<b>dash_unmeasured</b>.</p>
"""


# Loaded by Datasette through `--plugins-dir`, so it runs inside Datasette's own
# environment (which may be a `uvx` one) and this package never imports
# datasette. Adds `HttpOnly` to the sign-in cookie; see the module docstring.
_PLUGIN = """\
from datasette import hookimpl


def _httponly(headers):
    for name, value in headers:
        if (
            bytes(name).lower() == b"set-cookie"
            and bytes(value).startswith(b"ds_actor=")
            and b"httponly" not in bytes(value).lower()
        ):
            value = bytes(value) + b"; HttpOnly"
        yield name, value


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        async def inner(scope, receive, send):
            async def patched(event):
                if event["type"] == "http.response.start":
                    event = dict(event, headers=list(_httponly(event.get("headers", []))))
                await send(event)

            await app(scope, receive, patched)

        return inner

    return wrap
"""


def metadata(db_name: str) -> dict:
    """Datasette metadata for one database, by name.

    Split out from `command` so a test can assert the descriptions land on the
    right database without starting a server.

    Two contracts here, both load-bearing.

    `allow` denies everyone who is not `root`, and `command` passes `--root` so
    that Datasette prints one sign-in URL. Measured before it was added, on a
    1500-block synthetic store: `curl -H 'Host: evil.example'
    '…/blocks.csv?_stream=1'` returned 200 and 1500 of 1500 canaries in a single
    request. With it, every endpoint is 403 until the token is used — including
    `/-/databases.json`, which publishes the index's absolute path.
    [E7 dashboard-F1/F2]

    `description_html` is inserted into the page *unescaped* — that is
    Datasette's contract for the key, not an oversight here. Nothing derived
    from a transcript may reach it. Every value is a module-level literal, and
    `test_the_metadata_is_the_same_whatever_the_store_holds` is what keeps it
    that way: the day a caption grows an f-string over `generations.agent`,
    that is stored XSS on the origin holding every secret the developer has
    typed at an agent. [E7 dashboard-F6]
    """
    return {
        "title": "gitmemory",
        "allow": {"id": "root"},
        "description_html": _DESCRIPTION.strip(),
        "license": "Apache-2.0",
        "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "databases": {
            db_name: {
                "tables": {name: {"description_html": html} for name, html in _TABLES.items()}
            }
        },
    }


# The same range `pyproject.toml` declares for the `serve` extra, and a test
# asserts they stay equal. They used to differ — `datasette<2` here against
# `datasette>=0.65,<1` there — which meant the version most users ran was the
# one nobody had tested: the day 1.0 ships final, `<2` starts resolving to a
# major version with a POST write API, `/-/create-token`, and permission
# defaults this review never looked at. A range, not a pin, so a patch release
# (which is where security fixes land) does not need an edit here.
# [E7 dashboard-F4]
UVX_SPEC = "datasette>=0.65,<1"


def command(
    db: str,
    *,
    host: str = HOST,
    port: int = PORT,
    metadata_path: str,
    plugins_dir: str | None = None,
) -> list[str]:
    """The argv Datasette is run with. A list, never a shell string.

    A `datasette` already on PATH first, `uvx` as a fallback, an error if
    neither. The docstring here used to say "nothing is installed behind the
    user's back" and the `uvx` branch did exactly that: on a machine with uvx and
    no datasette — which is this one — `uvx datasette` resolves against PyPI and
    downloads whatever version it serves that minute, during what the user ran as
    a read-only look at a local file. The branch stays, because it is genuinely
    the easy path, but it is pinned to a range and `serve` says out loud that it
    may fetch. Silence was the defect, not the convenience. [E6 review]

    `allow_download` off: `--immutable` constrains writes and says nothing about
    reads, and Datasette otherwise offers the whole `.db` — every transcript byte,
    unredacted, `redact` having never run on the index — as a single file link.
    It was also the only control this module had, and it is a small one: it
    removed the *tidiest* way to take the store, not the ability to take it.
    `blocks.csv?_stream=1` returned the whole table in one unauthenticated
    request. `--root` is the control; this is tidiness. [E7 dashboard-F1]

    Arbitrary SQL stays *on*. It is behind the sign-in now, and it is the thing
    a person opens a database for; switching it off would have cost the owner
    the console and bought nothing an attacker could have used, since every
    path it discloses is 403 without the token. [E7 dashboard-F7]
    """
    args = [
        "datasette",
        "--immutable",
        db,
        "--metadata",
        metadata_path,
        # Pairs with `metadata()["allow"]`, and is useless without it: `--root`
        # alone only *offers* a root sign-in, it denies nobody.
        "--root",
        "--setting",
        "allow_download",
        "off",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if plugins_dir:
        args += ["--plugins-dir", plugins_dir]
    if shutil.which("datasette"):
        return args
    if shutil.which("uvx"):
        return ["uvx", UVX_SPEC, *args[1:]]
    raise FileNotFoundError(
        "neither `datasette` nor `uvx` is on PATH; install one of them, then: " + " ".join(args)
    )


def serve(home: str | None = None, *, db: str | None = None, host: str = HOST, port: int = PORT):
    """Run Datasette in the foreground until interrupted. Returns its exit code."""
    path = os.path.abspath(db or index.db_path(home))
    if not os.path.exists(path):
        raise FileNotFoundError(f"no index at {path}; run `gitmemory index` first")
    name = os.path.splitext(os.path.basename(path))[0]
    # A real temp file rather than a pipe: Datasette wants a path, and the
    # directory goes away with the process even if Datasette is killed.
    with tempfile.TemporaryDirectory(prefix="gitmemory-dashboard-") as tmp:
        meta = os.path.join(tmp, "metadata.json")
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump(metadata(name), fh, indent=2)
        plugins = os.path.join(tmp, "plugins")
        os.mkdir(plugins)
        with open(os.path.join(plugins, "gitmemory_cookie.py"), "w", encoding="utf-8") as fh:
            fh.write(_PLUGIN)
        argv = command(path, host=host, port=port, metadata_path=meta, plugins_dir=plugins)
        if argv[0] == "uvx":
            print(f"datasette is not on PATH; uvx may fetch `{UVX_SPEC}` from PyPI first")
        # Which build is on the page. Datasette holds the file open and
        # `--immutable` entitles SQLite to cache it, while `index.build` renames a
        # fresh file over the path — so a dashboard left running across a rebuild
        # serves the old inode forever, confidently, with no sign that it is
        # stale. Printing the digest does not fix that; it makes it checkable
        # against `gitmemory index`'s own output. [E6 review]
        print(f"content {_digest(path)} — restart after a rebuild; the page will not notice one")
        print("open the one-time sign-in URL datasette prints below, not the bare address")
        print("the sign-in cookie reaches every port on this host; stop the server to revoke it")
        # No `http://host:port/` line of our own any more. It was printed
        # *before* `subprocess.call`, so a local process squatting the port —
        # 8081 is a published constant above 1024, so no scanning is needed —
        # got handed the user's browser on an origin it could then fill with
        # fabricated corpus and spend numbers. The bind failure arrived four
        # lines later, after three green `INFO` lines. Uvicorn prints
        # "Uvicorn running on http://…" when it has actually bound, which is
        # the same fact and is true when it says it. [E7 dashboard-F5]
        rc = subprocess.call(argv)
        if rc != 0:
            print(f"datasette exited {rc}; nothing is serving {host}:{port}", file=sys.stderr)
        return rc


def _digest(path: str) -> str:
    """The index's content digest, short, or `unknown` if it cannot be read.

    The `except` stays broad — a caption must not stop the server from
    starting — but it no longer swallows the reason. `unknown` was printed
    identically for "this index predates the key" and "this file is not a
    database", and the second is the one worth knowing about: it is the only
    signal the user gets that the thing Datasette is about to open read-only is
    not the thing they built. Saying why costs one line on stderr and nothing
    when the read succeeds. [L-1]
    """
    try:
        db = index.open_db(path)
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'content_sha256'").fetchone()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - a start-up caption must not block the server
        print(f"could not read the content digest from {path}: {exc}", file=sys.stderr)
        return "unknown"
    return row["value"][:12] if row else "unknown"
