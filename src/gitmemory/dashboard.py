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

# What each view is for, in the words a person reading the dashboard needs
# rather than the words the SQL uses. Keyed by view name; anything not listed
# simply gets no description, which is how a new view fails — quietly and
# harmlessly — instead of by breaking the page.
_TABLES = {
    "dash_corpus": (
        "How much store there is, per agent. <b>unparseable</b> is the count of "
        "generations the index could not read: if it is not zero, every other "
        "number on this page is a count over less than the whole store."
    ),
    "dash_growth": (
        "Turns and bytes per day. Days with no timestamp are absent rather than "
        "zero — an agent that does not stamp its turns is invisible here, not small."
    ),
    "dash_contiguity": (
        "One row per conversation. <b>generations</b> above one means the store "
        "forked it — usually a compaction. <b>compactions</b> counts boundaries "
        "the manifest carries, which is the thing this project exists to keep."
    ),
    "dash_requests": (
        "One row per billable request, deduplicated. Usage is repeated on every "
        "turn of a request and is cumulative, so a naive sum over turns "
        "over-counts by 2.79x on a real transcript."
    ),
    "dash_spend": (
        "Tokens by model. <b>cache_read_pct</b> is a saving against <i>sending "
        "the same prompt uncached</i> and against nothing else. It is not a "
        "saving against not having sent it. No dollar figure: a price table goes "
        "stale silently and a stale price is worse than no price."
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

_DESCRIPTION = """
<p>Built from a gitmemory store: agent transcripts, captured verbatim, committed
to git, indexed here. The database is opened <b>immutable</b> — nothing on this
page can change the store, and the store is not the only copy of anything.</p>
<p><b>Frugality, stated exactly.</b> Injection cost is a cost and is shown as
one. Cache-served share is a real saving, against the same prompt sent uncached
and against nothing else. There is no <i>tokens saved</i> tile, because the
comparison it would need — the same work without memory — has never been run.
See <b>dash_unmeasured</b>.</p>
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


def command(db: str, *, host: str = HOST, port: int = PORT, metadata_path: str) -> list[str]:
    """The argv Datasette is run with. A list, never a shell string.

    `uvx` when it is there, a `datasette` already on PATH otherwise. Nothing is
    installed behind the user's back: if neither exists, `serve` says so and
    prints the command to run, which is a better outcome than a silent
    forty-megabyte download during what looked like a read-only operation.
    """
    args = [
        "datasette",
        "--immutable",
        db,
        "--metadata",
        metadata_path,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if shutil.which("datasette"):
        return args
    if shutil.which("uvx"):
        return ["uvx", *args]
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
        print(f"http://{host}:{port}/  (ctrl-c to stop)")
        return subprocess.call(argv)
