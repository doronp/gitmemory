"""The localhost dashboard: Datasette over the index, and nothing of our own.

Datasette is Apache-2.0, reads SQLite, ships faceting, filtering, charts, CSV
and JSON export, and a stable URL for every query. Writing a web UI to show
eight tables next to that would be the single worst trade in this repository.
So this module is about forty lines: it points Datasette at the index, hands it
a metadata file that says what the numbers mean, and gets out of the way.

Three things it is careful about.

**`--immutable`.** The index is derived and rebuilt from raw, but a dashboard
that can write to it is a dashboard that can silently become the only copy of
something. Datasette opens it read-only and, with `--immutable`, promises SQLite
nothing else will write either.

**`127.0.0.1`.** A transcript store is the most sensitive file a developer owns.
The default bind is loopback and the flag to change it is `--host`, which is a
thing somebody has to type.

**The metadata is generated, not shipped.** Datasette keys table descriptions by
database *name*, and the name carries the schema version (`gitmemory-v2.db`), so
a checked-in YAML would silently stop matching on the next schema bump and the
descriptions would vanish with no error. Generating it from `SCHEMA` means the
one place that can be wrong is the one place that is already right. It is JSON,
which Datasette accepts and which is stdlib — no PyYAML for eighty lines of
prose.

See DESIGN.md §2.9 for the panels and, more importantly, for the refusals: the
`dash_unmeasured` view is not decoration, it is the part of this dashboard that
keeps the rest of it honest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import index

__all__ = ["command", "metadata", "serve"]

HOST = "127.0.0.1"
PORT = 8081  # not 8001: Datasette's default collides with half the world

# Every spelling of "this machine only". The CLI warns about a bind outside this
# set, so the set is the security boundary and not the default value.
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

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
Loopback is not authentication: anything running as you on this machine can read
this page.</p>
<p><b>Frugality, stated exactly.</b> Cache-served share is a real saving, against
the same prompt sent uncached and against nothing else. There is no <i>tokens
saved</i> tile, because the comparison it would need — the same work without
memory — has never been run, and no <i>injection cost</i> tile, because nothing
in this system injects anything yet. Both are named in
<b>dash_unmeasured</b>.</p>
"""


def metadata(db_name: str) -> dict:
    """Datasette metadata for one database, by name.

    Split out from `command` so a test can assert the descriptions land on the
    right database without starting a server.
    """
    return {
        "title": "gitmemory",
        "description_html": _DESCRIPTION.strip(),
        "license": "Apache-2.0",
        "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "databases": {
            db_name: {
                "tables": {name: {"description_html": html} for name, html in _TABLES.items()}
            }
        },
    }


UVX_SPEC = "datasette<2"  # a version range, so what runs is a reviewable line


def command(db: str, *, host: str = HOST, port: int = PORT, metadata_path: str) -> list[str]:
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
    """
    args = [
        "datasette",
        "--immutable",
        db,
        "--metadata",
        metadata_path,
        "--setting",
        "allow_download",
        "off",
        "--host",
        host,
        "--port",
        str(port),
    ]
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
        argv = command(path, host=host, port=port, metadata_path=meta)
        if argv[0] == "uvx":
            print(f"datasette is not on PATH; uvx may fetch `{UVX_SPEC}` from PyPI first")
        # Which build is on the page. Datasette holds the file open and
        # `--immutable` entitles SQLite to cache it, while `index.build` renames a
        # fresh file over the path — so a dashboard left running across a rebuild
        # serves the old inode forever, confidently, with no sign that it is
        # stale. Printing the digest does not fix that; it makes it checkable
        # against `gitmemory index`'s own output. [E6 review]
        print(f"http://{host}:{port}/  (ctrl-c to stop)")
        print(f"content {_digest(path)} — restart after a rebuild; the page will not notice one")
        return subprocess.call(argv)


def _digest(path: str) -> str:
    """The index's content digest, short, or `unknown` if it cannot be read."""
    try:
        db = index.open_db(path)
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'content_sha256'").fetchone()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - a start-up caption must not block the server
        return "unknown"
    return row["value"][:12] if row else "unknown"
