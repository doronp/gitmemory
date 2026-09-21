"""Retrieval index — SQLite FTS5 over the store, rebuilt from raw, never authoritative.

Three decisions, all of them reversible, none of them accidental:

**SQLite FTS5, not a vector store.** DESIGN.md §2.7 puts lexical BM25 first
because the queries an agent actually asks are full of identifiers — a function
name, a flag, an error string — and an embedding is worse than a token match at
exactly those. FTS5 ships inside CPython, so this rung of the ladder costs zero
dependencies. Dense and rerank arms are measured against it, not assumed
better; that is what `bench/` exists to settle.

**Four text columns, where the design named three.** The split exists because
tool output is the bulk of a transcript and the least of its signal. But a
`tool_use` block is the *arguments* — the command that was run, the file that
was edited — which is short and high-signal, and burying it with the output is
the mistake the split was invented to avoid. Weights are per-column and
configurable, so four columns is strictly more general than three: set
`tool_use == tool_result` and you are back to the design's shape.

**External-content FTS5.** The text lives once, in `blocks`; FTS5 keeps only
its inverted index and reads columns back from there. That halves the file and
keeps one copy of the truth.

The database is derived. It lives under `$GITMEMORY_HOME/index/`, which is
gitignored, and `build()` recreates it from the segments every time. Nothing in
here is ever the only copy of anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from . import store
from .adapters import get as get_adapter
from .records import Session, canonical_json

__all__ = [
    "DEFAULT_WEIGHTS",
    "Hit",
    "Hits",
    "Stats",
    "Weights",
    "build",
    "db_path",
    "match_expr",
    "open_db",
    "retriever",
    "search",
]

SCHEMA = 2
COLUMNS = ("prose", "tool_use", "tool_result", "paths")
# Serialises `build` per target directory. See `build`'s docstring for why the
# directory, and not `$GITMEMORY_HOME`, is the thing worth locking.
LOCK_NAME = ".gitmemory-build.lock"

# A token FTS5 will index, used to sanitise a user query into a MATCH
# expression. Anything outside this set is a separator to the `unicode61`
# tokenizer anyway, so dropping it loses nothing and closes the injection:
# a bare query string reaching MATCH lets `"` or `NEAR(` change the operator
# tree, and `*` turn a word into a prefix scan of the whole corpus.
_WORD = re.compile(r"\w+", re.UNICODE)
# How many query tokens reach MATCH. A pasted stack trace is a legitimate
# query and a thousand-term OR is a table scan; the cap is announced by
# `search`, never applied in silence.
MAX_TERMS = 64

# Path-shaped tokens: something with an interior separator. Deliberately not an
# extension allow-list — that is a table someone has to maintain and will get
# wrong for the next language. Bare filenames are recovered from tool arguments
# instead, where they are named explicitly.
#
# Each component is bounded and possessive, and both halves are load-bearing.
# Unbounded `+` made this quadratic: on a long run of word characters with no
# separator — base64, a JWT, a hex digest, exactly what lands in tool output —
# the engine rescans to the end from every start position. 32 KB cost 3.4 s and
# 200 KB hung the build for minutes. 255 is NAME_MAX, so nothing that is
# actually a path component is lost, and the scan is linear: 2 MB in 1.15 s.
# Possessive `+` alone was not enough (it only stops backtracking *within* a
# component); the bound is what caps the work per start position. [E3]
_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|[\\/])?(?:[\w.@%+-]{1,255}+[\\/])+[\w.@%+-]{1,255}+"
)
# Tool-argument keys that name a file. Claude Code, Hermes and opencode all use
# some casing of these; unknown keys simply contribute nothing.
_PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "notebookPath")

_DDL = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE blocks (
    rowid        INTEGER PRIMARY KEY,
    block_id     TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    agent        TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    turn_seq     INTEGER NOT NULL,
    block_seq    INTEGER NOT NULL,
    role         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    tool_name    TEXT,
    byte_offset  INTEGER NOT NULL,
    byte_len     INTEGER NOT NULL,
    ts           TEXT,
    prose        TEXT NOT NULL,
    tool_use     TEXT NOT NULL,
    tool_result  TEXT NOT NULL,
    paths        TEXT NOT NULL
);
CREATE INDEX blocks_turn ON blocks (turn_id);
CREATE INDEX blocks_offset ON blocks (session_key, byte_offset);
CREATE VIRTUAL TABLE fts USING fts5(
    {", ".join(COLUMNS)},
    content = 'blocks',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Retrieval needs blocks. Everything a person wants to *know about* the store
-- — how much of it there is, what it cost, whether it is whole — is a fact
-- about a turn or a generation, and neither had a table. Both are written from
-- the same parse `blocks` comes from, so they cost one extra insert per turn
-- and no extra read. [E6]
--
-- `turn_id` is deliberately not the primary key: it is content-derived, so a
-- turn replayed into a forked generation is the *same* id in two rows, and that
-- is the normal case rather than a corruption. Position is what is unique.
CREATE TABLE turns (
    turn_id      TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    agent        TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    model        TEXT,
    request_id   TEXT,
    is_sidechain INTEGER NOT NULL,
    agent_id     TEXT,
    byte_offset  INTEGER NOT NULL,
    byte_len     INTEGER NOT NULL,
    ts           TEXT,
    usage        TEXT NOT NULL,  -- JSON, exactly as the adapter scrubbed it
    PRIMARY KEY (session_key, seq)
);

-- A generation is what the store proves contiguous, so this is the table the
-- contiguity panel reads. `parsed = 0` rows are the ones `build` had to skip:
-- leaving them out would have made a store with an unparseable generation look
-- identical to a whole one, which is the single thing this panel is for.
CREATE TABLE generations (
    session_key TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    segments    INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    compactions INTEGER NOT NULL,  -- boundaries the manifest carries
    turns       INTEGER NOT NULL,
    blocks      INTEGER NOT NULL,
    parsed      INTEGER NOT NULL,
    skip_reason TEXT
);
"""

# Views, not canned queries in the dashboard's YAML: Datasette lists a view as a
# table, so the panels are in the database and anything else that opens it —
# `sqlite3`, a notebook, the next dashboard — gets the same definitions. The
# YAML is left to say what the numbers *mean*. [E6]
_VIEWS = """
-- `stored_turns`, not `turns`, and the extra word is the whole point. A fork
-- replays the prefix of a conversation into the next generation, so the store
-- really does hold those turns twice and every column here is a per-generation
-- sum. Called `turns` beside a de-duplicated `sessions` count, one row read
-- "1 session, 3 turns" over a conversation with two. The name now carries the
-- caveat to wherever the number is quoted. [E6 review]
CREATE VIEW dash_corpus AS
SELECT agent,
       COUNT(DISTINCT session_id)  AS sessions,
       COUNT(*)                    AS generations,
       SUM(turns)                  AS stored_turns,
       SUM(blocks)                 AS stored_blocks,
       SUM(bytes)                  AS bytes,
       SUM(compactions)            AS compactions,
       SUM(1 - parsed)             AS unparseable
FROM generations GROUP BY agent;

-- `datetime(ts)`, not `ts IS NOT NULL`. A timestamp is a string the agent wrote
-- and an empty or malformed one is not null: `''` bucketed under the day `''`
-- and `not-a-date-at-all` under `not-a-date`, both sitting in the facet list as
-- peers of real dates, under a caption promising that unstamped turns are
-- absent. `datetime()` returns NULL for anything it cannot read, which is the
-- test the caption always meant, and it resolves a UTC offset rather than
-- slicing the local-clock date out of the front of the string. [E6 review]
CREATE VIEW dash_growth AS
SELECT substr(datetime(ts), 1, 10) AS day, agent,
       COUNT(*) AS stored_turns, SUM(byte_len) AS bytes
FROM turns WHERE datetime(ts) IS NOT NULL GROUP BY day, agent ORDER BY day;

-- Contiguity is per session, not per generation: a session with three
-- generations is one conversation the store forked, and "is it whole" is a
-- question about the conversation.
CREATE VIEW dash_contiguity AS
SELECT agent, session_id,
       COUNT(*)         AS generations,
       SUM(segments)    AS segments,
       SUM(compactions) AS compactions,
       SUM(bytes)       AS bytes,
       SUM(turns)       AS stored_turns,  -- per-generation sum; see dash_corpus
       SUM(1 - parsed)  AS unparseable,
       group_concat(skip_reason, ' | ') AS skipped
FROM generations GROUP BY agent, session_id;

-- One row per billable request. Usage is repeated on every turn of a request
-- and is *cumulative*, so summing turns over-counts -- 2.79x, measured on a
-- real transcript. The last write of a request wins.
--
-- `row_number()`, not `MAX(seq)` with bare columns, and the partition is
-- `(agent, request_id)` rather than anything per-file. Four separate defects
-- lived in the one line this replaces, all of them found by review and all of
-- them reproduced end-to-end before it was rewritten: [E6 review]
--
--  * `seq` restarts at 0 in every generation, so `MAX(seq)` did not mean "last
--    write", it meant "whichever generation was longer". A request caught
--    mid-stream at seq 3 of generation 0 and completed at seq 0 of the fork
--    reported the *truncated* number -- 10 where the request cost 30.
--  * A tie on `seq` -- the normal shape, since a fork diverges *at* a line --
--    left the answer to insert order. Flipping the order of two rows moved the
--    request from one model to the other. This is the defect `search()` already
--    names two hundred lines below; `(generation, seq)` is a total order within
--    a session and cannot tie.
--  * A `requestId` appears in both the main transcript and the subagent that
--    shares it, and those are separate *files*, so separate sessions. Grouping
--    by `session_id` billed both copies. One real session kept 78% of its
--    cache-read tokens in subagents (DESIGN 2.2), so this was most of that
--    column, doubled. A request id is unique per API call, so the agent is the
--    only scope it needs.
--  * `COALESCE(request_id, turn_id)` made every turn with no request id its own
--    request and added its cumulative usage in full -- the whole 2.79x
--    over-count, reinstated inside the view written to remove it. Those turns
--    are unattributable rather than free: they are excluded here and counted,
--    with their tokens, in `dash_unbilled`.
--
-- The `role`/`<synthetic>` filter matches `adapters.claude_code.billable_usage`,
-- which is the other implementation of this rule. Without it a user-role line
-- carrying a usage block was billed as a model: a probe read 99999 input tokens
-- against the adapter's 100.
--
-- ponytail: newest generation wins, mirroring `search()`. That is right because
-- a fork replays whole lines, so the only truncated copy of a request is the
-- live tail, which is the newest generation and has no older complete copy to
-- lose to. The ceiling is a shape where it does -- a completed request in an
-- older generation than a partial replay of it -- and the upgrade is to order
-- by the cumulative counter itself, which is monotonic per request.
CREATE VIEW dash_requests AS
SELECT agent, session_id, request_id AS request, ts,
       COALESCE(model, '(unknown)') AS model,
       COALESCE(json_extract(usage, '$.input_tokens'), 0)                 AS input_tokens,
       COALESCE(json_extract(usage, '$.output_tokens'), 0)                AS output_tokens,
       COALESCE(json_extract(usage, '$.cache_creation_input_tokens'), 0)  AS cache_write_tokens,
       COALESCE(json_extract(usage, '$.cache_read_input_tokens'), 0)      AS cache_read_tokens
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY agent, request_id ORDER BY generation DESC, seq DESC
    ) AS rn
    FROM turns
    WHERE usage <> '{}' AND request_id IS NOT NULL
      AND role = 'assistant' AND COALESCE(model, '') <> '<synthetic>'
) WHERE rn = 1;

-- The turns `dash_requests` refuses, and what they carry. Dropping them quietly
-- would make `dash_spend` disagree with `billable_usage` by an amount nobody
-- could see; this is that amount, with the reason next to it. A non-zero
-- `no request id` row means `dash_spend` is a floor rather than a total. [E6]
CREATE VIEW dash_unbilled (agent, reason, turns, input_tokens, output_tokens,
                           cache_write_tokens, cache_read_tokens) AS
SELECT agent,
       CASE WHEN role <> 'assistant'      THEN 'not an assistant turn'
            WHEN model = '<synthetic>'    THEN 'synthetic model'
            ELSE 'no request id' END,
       COUNT(*),
       SUM(COALESCE(json_extract(usage, '$.input_tokens'), 0)),
       SUM(COALESCE(json_extract(usage, '$.output_tokens'), 0)),
       SUM(COALESCE(json_extract(usage, '$.cache_creation_input_tokens'), 0)),
       SUM(COALESCE(json_extract(usage, '$.cache_read_input_tokens'), 0))
FROM turns
WHERE usage <> '{}'
  AND (request_id IS NULL OR role <> 'assistant' OR model = '<synthetic>')
GROUP BY 1, 2;

CREATE VIEW dash_spend AS
SELECT agent, model,
       COUNT(*)                 AS requests,
       SUM(input_tokens)        AS input_tokens,
       SUM(output_tokens)       AS output_tokens,
       SUM(cache_write_tokens)  AS cache_write_tokens,
       SUM(cache_read_tokens)   AS cache_read_tokens,
       -- Cache-served share of everything that went *in*. This is a saving
       -- against "the same prompt, uncached" and against nothing else; it is
       -- not a saving against not having sent the prompt.
       --
       -- No guard on the denominator. There was a `NULLIF(..., 0)` here and it
       -- was a no-op dressed as a safety measure: SQLite returns NULL for `x/0`
       -- rather than raising, so a model whose every input column is zero gets
       -- a NULL share either way. NULL is also the right answer -- a share of
       -- nothing is not 0% -- so the guard is gone and the NULL is asserted.
       -- [E6 review]
       ROUND(
           100.0 * SUM(cache_read_tokens)
           / SUM(input_tokens + cache_write_tokens + cache_read_tokens), 1
       ) AS cache_read_pct
FROM dash_requests GROUP BY agent, model;

-- The panel that says what this dashboard does not know. It is a view so that
-- it sits in the same list as the numbers and cannot be scrolled past: every
-- tile above is a count of something observed, and these are the questions a
-- count cannot answer. [E6, DESIGN 2.9]
CREATE VIEW dash_unmeasured (question, status, why) AS
SELECT 'net tokens saved vs no memory', 'UNMEASURED',
       'Needs the A/B harness. No "tokens saved" number ships before it runs, '
       || 'because the counterfactual has never been observed.'
UNION ALL SELECT 'did an injected memory change the answer', 'NOT OBSERVABLE',
       'Attention leaves no trace. The most that can be said is REFERENCED -- '
       || 'the text appeared in the reply -- and that is not the same claim.'
UNION ALL SELECT 'dead ends avoided', 'UNBOUNDED, UNMEASURED',
       'A search an agent did not have to run leaves nothing behind to count. '
       || 'Any single frugality number assumes this tail is zero.'
UNION ALL SELECT 'cost of a stale memory that misled', 'UNBOUNDED, UNMEASURED',
       'The other tail, and the one that argues against this project. Assumed '
       || 'zero by every number above, on no evidence.'
UNION ALL SELECT 'hook latency', 'NOT IN THIS DATABASE',
       'Measured by tools/hook_latency.py against a running hook, not by the '
       || 'index. Nothing here should be read as a p99.'
-- DESIGN 2.9 names injection cost as frugality item 1, "exact, and it is a
-- cost ... shown first", and the front page said it was shown. It is not: no
-- hook records injected tokens, nothing in this schema holds one, and a reader
-- who checked this panel and did not find it here would have concluded it was
-- among the measured numbers. The claim is gone from the page and the gap is
-- named where the other gaps are. [E6 review]
UNION ALL SELECT 'injection cost', 'NOT BUILT',
       'The one frugality number that would be exact -- tokens a memory system '
       || 'adds to a context is a count, not an estimate. Nothing injects yet, '
       || 'so nothing records it. When it exists it belongs above, not here.';
"""


@dataclass(frozen=True, slots=True)
class Weights:
    """Per-column BM25 weights. Higher means the column counts for more.

    These numbers are a starting point, not a result: they say prose beats a
    tool argument beats tool output, which is the ordering the column split was
    built to express. `bench/` is what replaces them with measured ones.
    """

    prose: float = 4.0
    tool_use: float = 2.0
    tool_result: float = 1.0
    paths: float = 3.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.prose, self.tool_use, self.tool_result, self.paths)


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True, slots=True)
class Hit:
    """One turn, ranked. `byte_offset` is the key every other layer agrees on.

    It is an offset into the *generation* — the concatenation of its segments —
    and not into a file, because that concatenation is what the parser read.
    The two coincide only while a generation is still one segment. To get the
    bytes, `store.span(stored, hit.byte_offset, hit.byte_len)`; `session_key`
    names which `store.sessions()` entry to pass. [E3]
    """

    byte_offset: int
    byte_len: int
    turn_id: str
    session_key: str
    agent: str
    session_id: str
    generation: int
    role: str
    kind: str
    score: float  # BM25; more negative is a better match, as FTS5 defines it
    text: str


@dataclass(frozen=True, slots=True)
class Stats:
    generations: int
    turns: int
    blocks: int
    skipped: tuple[str, ...]  # one line per generation that could not be parsed
    content_sha256: str  # over the indexed rows, so two builds can be compared


def db_path(home: str | None = None) -> str:
    return os.path.join(store.resolve_home(home), "index", f"gitmemory-v{SCHEMA}.db")


def open_db(path: str, *, write: bool = False) -> sqlite3.Connection:
    """Open an index. Read-only unless `build` says otherwise.

    `sqlite3.connect(path)` opens for writing and **creates the file if it is
    not there**, so `gitmemory recall --db typo.db` left a 0-byte database at
    the typo and every reader could scribble on an index it only meant to
    query. Read-only mode is both fixes at once: an absent path is refused
    where it is named, instead of becoming a file that answers nothing.

    Not a defence against a *hostile* index — an attacker who can write the
    file chooses what `recall` prints, and no open mode changes that. What it
    removes is the accident. [E7]
    """
    if write:
        db = sqlite3.connect(path)
    else:
        # `quote`, because `?` and `#` are URI syntax and a path may hold them;
        # `abspath`, so the URI is never parsed as a relative one.
        db = sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


@contextlib.contextmanager
def _step_budget(db: sqlite3.Connection, ticks: int = 1000):
    """Abort a query on this connection after roughly a million VM steps.

    `meta` is a table in every index this code writes — but in a *file* it did
    not write, `meta` can be a view, and a view is arbitrary SQL that runs
    inside the innocuous `SELECT` below. A view over a recursive CTE counting
    to 4x10^8 held `_check_schema` for **29.7 seconds** and could not be
    interrupted: SQLite is executing in C, so `signal.alarm` does not fire and
    neither does Ctrl-C. Scale the constant in the file and it is unbounded.

    The real query is an index seek on a two-row table — a handful of VM steps
    against a budget of a million — so the headroom is about six orders of
    magnitude. Hitting it raises `OperationalError`, which `_check_schema`
    already turns into "not a gitmemory index". [E7]
    """
    left = [ticks]

    def tick() -> int:
        left[0] -= 1
        return left[0] <= 0

    db.set_progress_handler(tick, 1000)
    try:
        yield
    finally:
        db.set_progress_handler(None, 0)


_SUPERSEDED_RE = re.compile(r"\Agitmemory-v(\d+)\.db\Z")
# What `build`'s `mkstemp` actually leaves, plus the journals SQLite hangs off
# it. The prefix alone is not the name of anything we wrote: `--db` points the
# sweep at a directory the user chose, and `.building-manifest.yaml` sitting in
# it was deleted on sight. Ours always end `.db`, because `mkstemp` is called
# with `suffix=".db"` eight lines from here. [E7]
_PARTIAL_RE = re.compile(r"\A\.building-.+\.db(-journal|-wal|-shm)?\Z")


def _sweep_partials(parent: str, keep: str = "") -> None:
    """Delete `.building-*.db` left by a killed build, and superseded indexes.

    `build`'s `except BaseException` covers a crash; it cannot cover SIGKILL or
    a lost power rail, and three killed builds leave three partial indexes plus
    their journals sitting next to the real one forever. The store already does
    this for `raw/` in `_adopt_orphans`; `index/` had no equivalent. Safe to do
    unconditionally: the name is ours, the file is derived, and a concurrent
    build holds its own `mkstemp` name that this pass has not seen yet. [E3]

    The schema version is in the filename, so a bump does not collide — it
    *orphans*. E6 took the schema to 2 and left every `gitmemory-v1.db` on disk:
    a full second copy of the transcript text, in a gitignored directory nobody
    opens, recurring at every future bump. Rebuild-from-raw is the migration
    story and the old file is garbage the moment the new one lands; the E3
    argument above applies to it verbatim. `keep` is the build's own target,
    because `--db` can point at a name this pattern matches. [E6 review]
    """
    with contextlib.suppress(OSError), os.scandir(parent) as it:
        for entry in it:
            superseded = _SUPERSEDED_RE.match(entry.name)
            # `<`, not `!=`. A version this code has never heard of is not a
            # leftover, it is an index written by a *newer* gitmemory, and `!=`
            # had the two versions deleting each other's index on alternate
            # runs — each rebuilding from raw, each destroying the other's work,
            # neither reporting anything. Forward is not the same direction as
            # stale. [E7]
            stale = superseded and int(superseded.group(1)) < SCHEMA
            if (_PARTIAL_RE.match(entry.name) or stale) and entry.is_file():
                if os.path.abspath(entry.path) == keep:
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)


def _check_schema(db: sqlite3.Connection) -> None:
    """Refuse a database this code cannot read.

    `db_path` puts the version in the filename, so the default path can never
    collide — but `--db` bypasses that, and a stale database answered queries
    from the old schema in silence. Silence is the whole problem: a wrong answer
    from a retrieval index looks exactly like a right one. [E3]
    """
    try:
        with _step_budget(db):
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"not a gitmemory index: {exc}") from exc
    found = row["value"] if row else None
    if found != str(SCHEMA):
        raise ValueError(
            f"index is schema {found or 'unknown'}, this is gitmemory schema {SCHEMA}; "
            f"rebuild it with `gitmemory index`"
        )


def build(home: str | None = None, *, path: str | None = None) -> Stats:
    """Rebuild the index from the store. Replaces any existing database.

    Full rebuild only. Incremental indexing is a cache-invalidation problem,
    and the thing being cached is a few seconds of parsing over bytes we
    already own; add it when a measurement says the rebuild is the bottleneck.

    **One build at a time per directory.** The sweep cannot tell a partial index
    left by a kill from one a *live* build is still writing — both are
    `.building-*.db`, and the name is the only evidence there is. Two builds in
    the same directory therefore deleted each other's temp and journal
    mid-transaction, and the loser died on `sqlite3.OperationalError: disk I/O
    error`: an error that names a failing disk when the disk is fine. Two
    `gitmemory index` runs in two terminals is the ordinary way to get there.

    The lock sits in the target's own directory rather than under `<home>`,
    because the directory is the thing being contended: `--db` puts the sweep
    wherever the caller points it, and a home-keyed lock would not serialise two
    homes writing into one directory. [E7]
    """
    home = store.resolve_home(home)
    # abspath, because `--db out.db` has no dirname and `makedirs("")` raises
    # ENOENT on the most obvious value for a flag documented as "database path".
    target = os.path.abspath(path or db_path(home))
    parent = os.path.dirname(target)
    # `_mkdir`, not `os.makedirs`: the index is a second full copy of the
    # transcript text, and `makedirs` left it in a 0755 directory while `raw/`
    # and `sessions/` next to it were 0700. Measured on this tree before the
    # change: `index/` 0755, and `--db a/b/out.db` created both `a` and `b` at
    # 0755. The contents were never exposed — the db and its journal are 0600 —
    # so what leaked was the directory listing: that a store exists here, how
    # big its index is, when it last built. [E7 index-F9]
    #
    # `realpath` on a caller-supplied parent, because `_mkdir` refuses symlinks
    # and the caller's are theirs to follow: `/tmp` is a symlink on macOS, so
    # `--db /tmp/x.db` — the most ordinary scratch invocation there is — was
    # being refused, by `_lockfile`'s own `_mkdir` one line below, ever since
    # index-F1 landed. The leaf name is kept as given, so the rename at the end
    # of a build still replaces a symlink sitting at the target rather than
    # writing through it. `<home>/index` gets no such courtesy: nothing but
    # gitmemory ever creates that name, which is exactly what makes it
    # plantable.
    if path:
        parent = os.path.realpath(parent)
        target = os.path.join(parent, os.path.basename(target))
    store._mkdir(parent)
    with store._lockfile(os.path.join(parent, LOCK_NAME)):
        _sweep_partials(parent, keep=target)

        # Build into a temp database and rename. A half-built index that answers
        # queries is worse than no index, and a crash mid-build is the normal way
        # to get one.
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".building-", suffix=".db")
        os.close(fd)
        try:
            db = open_db(tmp, write=True)
            try:
                db.executescript(_DDL + _VIEWS)
                stats = _fill(db, home)
                db.executescript("INSERT INTO fts(fts) VALUES('rebuild'); ANALYZE;")
                db.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("schema", str(SCHEMA)), ("content_sha256", stats.content_sha256)],
                )
                db.commit()
            finally:
                db.close()
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return stats


def _fill(db: sqlite3.Connection, home: str) -> Stats:
    digest = hashlib.sha256()
    skipped: list[str] = []
    turns = blocks = generations = 0
    # One transaction around the whole fill, explicitly, so the per-generation
    # `SAVEPOINT`s below are always *nested* ones. Releasing an outermost
    # savepoint commits, which would publish each generation as it went and
    # leave `build`'s `db.commit()` describing nothing. [E7]
    db.execute("BEGIN")
    for stored in store.sessions(home):
        # The guard covers parsing, row construction **and the writes**. It used
        # to wrap only the parse, so a malformed tool call or a lone surrogate —
        # both of which the store preserves on purpose — escaped from the row
        # loop and cost the index every other generation as well, which is the
        # exact failure this guard says it prevents. The writes were still
        # outside it: `prose`, `tool_result` and `paths` are unbounded, SQLite
        # refuses a value over SQLITE_LIMIT_LENGTH, and one transcript block
        # over that limit — an agent that `cat`'d a big file, which is the
        # reason this project keeps raw bytes at all — took the whole build
        # down at bind time with a `DataError`, for every session in the store,
        # on every run until someone deleted the generation by hand.
        #
        # The savepoint is what makes the wider guard honest: a generation that
        # fails halfway through its inserts leaves nothing behind, and its
        # contribution to the digest is held in `chunk` until it is known to
        # have landed. All-or-nothing per generation, in the database and in
        # the hash. [E3, widened E7]
        db.execute("SAVEPOINT generation")
        chunk: list[bytes] = []
        try:
            session = parse_generation(stored)
            rows = [_row(stored, turn, block) for turn in session.turns for block in turn.blocks]
            for turn in session.turns:
                # The turn rows are in the digest too, and they have to be: every
                # number the dashboard shows is read off `turns` and `generations`,
                # and the digest existed to answer "are these two builds over the
                # same content". A turn that produced no blocks — an assistant turn
                # with empty content and a usage block is the ordinary case — fed
                # *nothing* into the digest, so two stores whose spend differed by
                # any amount hashed identically. Same for the generation row, where
                # a differently segmented capture of byte-identical content compared
                # equal while `dash_contiguity` showed a different shape. This is the
                # same argument the skips below were added under. [E6 review]
                chunk.append(_insert(db, "turns", _turn_row(stored, turn)))
            for row in rows:
                chunk.append(canonical_json(row))
                db.execute(
                    f"INSERT INTO blocks ({','.join(row)}) "
                    f"VALUES ({','.join(':' + k for k in row)})",
                    row,
                )
            chunk.append(
                _generation_row(db, stored, turns=len(session.turns), blocks=len(rows), reason=None)
            )
        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data
            # One bad generation must not cost the index every other one; it is
            # reported, not swallowed. Same rule as `verify`. [E2]
            db.execute("ROLLBACK TO generation")
            db.execute("RELEASE generation")
            skipped.append(f"{stored.key}: {exc!r}")
            # A generation the index could not take is still a generation the
            # store holds, and a dashboard that silently omits it reports a
            # whole store. It goes in with `parsed = 0` and the reason. [E6]
            digest.update(_generation_row(db, stored, turns=0, blocks=0, reason=repr(exc)))
            continue
        db.execute("RELEASE generation")
        generations += 1
        turns += len(session.turns)
        blocks += len(rows)
        for part in chunk:
            digest.update(part)
    # The skips are part of what this index *is*. Left out, two builds over
    # different stores — one whole, one with a generation that would not parse —
    # compared equal, which is the one question this digest exists to answer.
    for line in skipped:
        digest.update(canonical_json({"skipped": line}))
    return Stats(generations, turns, blocks, tuple(skipped), digest.hexdigest())


def parse_generation(stored: store.Stored) -> Session:
    """Parse a generation's bytes as one transcript.

    Public because `derive` needs exactly this and a second copy of it would be
    a second place for the concatenation rule below to be got wrong. It lives
    here rather than in `store` because the store does not parse — it moves
    bytes and proves they tile — and that separation is worth an odd import.

    The concatenation is the transcript — a segment is a copy window, not a
    unit of meaning, and a record can straddle the cut. Parsing segments
    separately would drop exactly the lines a compaction boundary sits next to.
    """
    adapter = get_adapter(stored.agent)
    if len(stored.segments) == 1:
        return adapter.parse(stored.segments[0])
    fd, tmp = tempfile.mkstemp(prefix="gitmemory-concat-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "wb") as out:
            for seg in stored.segments:
                with open(seg, "rb") as fh:
                    shutil.copyfileobj(fh, out)
        return adapter.parse(tmp)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _encodable(value):
    """Make a value safe for sqlite3, which encodes strict UTF-8.

    The layers below deliberately do not: `jsonl` decodes with `surrogateescape`
    and `records` hashes with `surrogatepass`, so a transcript that caught a
    binary `cat` in its tool output keeps those bytes byte-for-byte. sqlite3
    raises on a lone surrogate, and it raised from inside the row loop, so one
    bad byte anywhere cost the whole store its index. Replacing here loses a
    character from the *derived* copy only — raw still has it, and raw is what
    the offsets point at. [E3]
    """
    if not isinstance(value, str):
        return value
    return value.encode("utf-8", "replace").decode("utf-8")


def _insert(db: sqlite3.Connection, table: str, row: dict) -> bytes:
    """Named-parameter insert. The column list comes from the row, not a
    literal, so adding a field to a `_*_row` function cannot get out of step
    with the statement that writes it.

    Returns the row's canonical bytes so the caller can feed the digest from the
    same value it wrote, rather than from a second rendering of it."""
    db.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':' + k for k in row)})", row
    )
    return canonical_json(row)


def _turn_row(stored: store.Stored, turn) -> dict:
    fields = {
        "turn_id": turn.turn_id,
        "session_key": stored.key,
        "agent": stored.agent,
        "session_id": stored.session_id,
        "generation": stored.generation,
        "seq": turn.seq,
        "role": turn.role,
        "model": turn.model,
        "request_id": turn.request_id,
        "is_sidechain": int(turn.is_sidechain),
        "agent_id": turn.agent_id,
        "byte_offset": turn.byte_offset,
        "byte_len": turn.byte_len,
        "ts": turn.ts,
        # `canonical_json` rather than `json.dumps`: sorted keys and pure ASCII,
        # so `json_extract` in the spend views reads the same bytes every build
        # and a surrogate from a mangled transcript cannot make the column
        # unparseable. Its output is ASCII by construction, so decoding is safe.
        "usage": canonical_json(turn.usage).decode(),
    }
    return {k: _encodable(v) for k, v in fields.items()}


def _generation_row(
    db: sqlite3.Connection, stored: store.Stored, *, turns: int, blocks: int, reason: str | None
) -> bytes:
    """Write the generation row. Returns its canonical bytes, for the digest."""
    return _insert(
        db,
        "generations",
        {
            "session_key": stored.key,
            "agent": stored.agent,
            "session_id": stored.session_id,
            "generation": stored.generation,
            "segments": len(stored.segments),
            "bytes": stored.size,
            "compactions": len(stored.boundaries),
            "turns": turns,
            "blocks": blocks,
            "parsed": int(reason is None),
            "skip_reason": _encodable(reason),
        },
    )


def _row(stored: store.Stored, turn, block) -> dict:
    column = {"tool_use": "tool_use", "tool_result": "tool_result"}.get(block.kind, "prose")
    text = {"prose": "", "tool_use": "", "tool_result": ""} | {column: block.text}
    fields = {
        "block_id": block.block_id,
        "turn_id": block.turn_id,
        "session_key": stored.key,
        "agent": stored.agent,
        "session_id": stored.session_id,
        "generation": stored.generation,
        "turn_seq": turn.seq,
        "block_seq": block.seq,
        "role": turn.role,
        "kind": block.kind,
        "tool_name": block.tool_name,
        "byte_offset": turn.byte_offset,
        "byte_len": turn.byte_len,
        "ts": turn.ts,
        **text,
        "paths": _paths(block),
    }
    return {k: _encodable(v) for k, v in fields.items()}


def _paths(block) -> str:
    """Path-shaped strings in a block, deduped, in order of appearance."""
    found: dict[str, None] = {}
    # `native["input"]` is a tool call the model wrote, so its shape is a claim,
    # not a fact: a string or a list where an object was expected is ordinary
    # malformed output. It used to raise `AttributeError` from inside the row
    # loop and take down the build for every *other* generation too. [E3]
    native = block.native if isinstance(block.native, dict) else {}
    args = native.get("input")
    if isinstance(args, dict):
        for key in _PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                found[value] = None
    for match in _PATH.finditer(block.text):
        found[match.group(0)] = None
    return "\n".join(found)


def match_expr(query: str) -> tuple[str, int]:
    """A safe FTS5 MATCH expression, and how many query terms were dropped.

    Each term is quoted, so FTS5 reads it as a bare string and no operator in
    the user's text can reach the parser. Terms are OR'd because FTS5's default
    is AND, and AND is not BM25: a five-word question that misses on one word
    should rank lower, not vanish.

    The query is normalised to NFC first because the two tokenizers disagree
    about combining marks. `unicode61 remove_diacritics 2` folds a mark into its
    base letter, so a diaeresis-bearing word indexes as the folded form in
    either normal form — but a combining mark is Unicode category Mn, which
    Python's `\\w` does not match, so a *decomposed* query term is shredded into
    fragments before FTS5 ever sees it. NFD came out as `"nai" OR "ve"`, which
    misses the word and spuriously hits any document containing "nai". macOS
    hands out NFD paths as a matter of course, so this is the common case, not
    the exotic one. [E3, Codex]

    Underscore and hyphen deliberately differ. `_` is a `\\w` character, so
    `parse_manifest` survives as one term and quotes to a *phrase* — FTS5 splits
    it on the underscore and requires the two tokens adjacent, which finds the
    identifier and little else. `parse-manifest` splits into
    `"parse" OR "manifest"`. Identifiers want precision, hyphenated words want
    recall, and that is the right way round for a coding agent's transcript.

    The `""` escape is unreachable — `\\w+` never yields a token containing a
    quote — and is kept as the thing that stays correct if `_WORD` is ever
    widened. No test can cover it; that is the point of writing it down here.
    """
    terms = [m.group(0) for m in _WORD.finditer(unicodedata.normalize("NFC", query))]
    kept = terms[:MAX_TERMS]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in kept), len(terms) - len(kept)


class Hits(list):
    """`list[Hit]`, carrying how many query terms `search` had to drop.

    A `list` subclass rather than a `(hits, dropped)` tuple because the count
    is a property of the search and not a second result, and because every
    caller that does not care keeps working unchanged.

    The channel this replaces was `warnings.warn`, which is once per call site
    per process. Measured before the change: three over-long queries in one
    interpreter, one warning — and all three returned *nothing*, because the
    only term that would have matched was the one past the cap. A long-lived
    reader (the bench harness, a server) therefore got a real-looking empty
    ranking with no explanation for every query after the first. [E7 index-F10]
    """

    dropped: int = 0


def search(
    db: sqlite3.Connection,
    query: str,
    *,
    k: int = 10,
    weights: Weights = DEFAULT_WEIGHTS,
) -> Hits:
    """Top `k` turns for `query`, best first. A turn scores as its best block.

    Turn-level, not block-level: two matching blocks in one turn are one piece
    of evidence, and returning both would spend the budget on a single place in
    the transcript.

    `.dropped` on the result is the number of terms past `MAX_TERMS`; a caller
    that renders hits to a person is expected to say so. A truncated query
    returns a real ranking over fewer terms, which reads exactly like a
    complete one.
    """
    _check_schema(db)
    expr, dropped = match_expr(query)
    if not expr:
        return Hits()
    rows = db.execute(
        f"""
        -- bm25() is an FTS5 auxiliary function: it exists only in a query whose
        -- direct source is the fts table — not inside an aggregate, not across
        -- a join. MATERIALIZED is load-bearing, not a hint: without it SQLite
        -- flattens this CTE into the join below and bm25 vanishes from the
        -- context it needs, with "unable to use function bm25" at runtime.
        WITH scored AS MATERIALIZED (
            SELECT rowid AS rid, bm25(fts, {", ".join("?" * len(COLUMNS))}) AS score
            FROM fts WHERE fts MATCH ?
        ),
        -- row_number(), not MIN() with bare columns. The bare-column rule does
        -- pick the winning row, but it does not say *which* winning row when
        -- two blocks of a turn score equally — and they tie whenever both
        -- contain the query term. The representative followed insert order, so
        -- the same content indexed in a different order answered differently.
        -- This ordering is total, so it cannot. [E3, Codex]
        ranked AS (
            SELECT b.byte_offset, b.byte_len, b.turn_id, b.session_key, b.agent,
                   b.session_id, b.generation, b.role, b.kind, b.block_seq,
                   b.prose || b.tool_use || b.tool_result AS text, s.score AS score,
                   -- Partitioned by turn *and session*, not by turn alone.
                   -- `turn_id` is content-derived over the record's sessionId,
                   -- which the Claude Code adapter warns is reused across a
                   -- fork; grouping on it alone collapsed one turn in session A
                   -- with a verbatim-identical turn in session B and returned
                   -- only the first, silently under-delivering `k`. Two
                   -- sessions are two places to go look. [E3]
                   --
                   -- Generations of *one* session are not: a pruner rewrite
                   -- copies a turn forward unchanged, and returning it once per
                   -- generation spends the budget on the same text. The newest
                   -- generation wins, because its offsets are the ones that
                   -- still address the live file.
                   row_number() OVER (
                       PARTITION BY b.turn_id, b.agent, b.session_id
                       ORDER BY s.score, b.generation DESC, b.block_seq
                   ) AS rn
            FROM scored s JOIN blocks b ON b.rowid = s.rid
        )
        SELECT byte_offset, byte_len, turn_id, session_key, agent,
               session_id, generation, role, kind, text, score
        FROM ranked WHERE rn = 1
        -- Ties are broken by position, never by rowid: the answer must not
        -- depend on the order generations happened to be indexed in.
        ORDER BY score, session_key, byte_offset, block_seq
        LIMIT ?
        """,
        (*weights.as_tuple(), expr, max(k, 0)),
    ).fetchall()
    hits = Hits(
        Hit(
            byte_offset=r["byte_offset"],
            byte_len=r["byte_len"],
            turn_id=r["turn_id"],
            session_key=r["session_key"],
            agent=r["agent"],
            session_id=r["session_id"],
            generation=r["generation"],
            role=r["role"],
            kind=r["kind"],
            score=r["score"],
            text=r["text"],
        )
        for r in rows
    )
    hits.dropped = dropped
    return hits


def retriever(db: sqlite3.Connection, *, weights: Weights = DEFAULT_WEIGHTS):
    """The seam `bench/score.py` scores: `retrieve(query, k) -> [byte offset]`."""

    def retrieve(query: str, k: int) -> list[int]:
        return [h.byte_offset for h in search(db, query, k=k, weights=weights)]

    return retrieve
