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
import warnings
from dataclasses import dataclass

from . import store
from .adapters import get as get_adapter
from .records import Session, canonical_json

__all__ = [
    "DEFAULT_WEIGHTS",
    "Hit",
    "Stats",
    "Weights",
    "build",
    "db_path",
    "match_expr",
    "open_db",
    "retriever",
    "search",
]

SCHEMA = 1
COLUMNS = ("prose", "tool_use", "tool_result", "paths")

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


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _sweep_partials(parent: str) -> None:
    """Delete `.building-*.db` left by a killed build.

    `build`'s `except BaseException` covers a crash; it cannot cover SIGKILL or
    a lost power rail, and three killed builds leave three partial indexes plus
    their journals sitting next to the real one forever. The store already does
    this for `raw/` in `_adopt_orphans`; `index/` had no equivalent. Safe to do
    unconditionally: the name is ours, the file is derived, and a concurrent
    build holds its own `mkstemp` name that this pass has not seen yet. [E3]
    """
    with contextlib.suppress(OSError), os.scandir(parent) as it:
        for entry in it:
            if entry.name.startswith(".building-") and entry.is_file():
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
    """
    home = store.resolve_home(home)
    # abspath, because `--db out.db` has no dirname and `makedirs("")` raises
    # ENOENT on the most obvious value for a flag documented as "database path".
    target = os.path.abspath(path or db_path(home))
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    _sweep_partials(parent)

    # Build into a temp database and rename. A half-built index that answers
    # queries is worse than no index, and a crash mid-build is the normal way
    # to get one.
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".building-", suffix=".db")
    os.close(fd)
    try:
        db = open_db(tmp)
        try:
            db.executescript(_DDL)
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
    for stored in store.sessions(home):
        # Parse *and* build every row before writing any of them. The guard used
        # to wrap only the parse, so a malformed tool call or a lone surrogate —
        # both of which the store preserves on purpose — escaped from the row
        # loop and cost the index every other generation as well, which is the
        # exact failure this guard says it prevents. All-or-nothing per
        # generation also keeps a half-written generation out of the digest. [E3]
        try:
            session = parse_generation(stored)
            rows = [_row(stored, turn, block) for turn in session.turns for block in turn.blocks]
        except Exception as exc:  # noqa: BLE001 - a segment run is untrusted data
            # One unparseable generation must not cost the index every other
            # one; it is reported, not swallowed. Same rule as `verify`. [E2]
            skipped.append(f"{stored.key}: {exc!r}")
            continue
        generations += 1
        turns += len(session.turns)
        for row in rows:
            digest.update(canonical_json(row))
            db.execute(
                f"INSERT INTO blocks ({','.join(row)}) VALUES ({','.join(':' + k for k in row)})",
                row,
            )
            blocks += 1
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


def search(
    db: sqlite3.Connection,
    query: str,
    *,
    k: int = 10,
    weights: Weights = DEFAULT_WEIGHTS,
) -> list[Hit]:
    """Top `k` turns for `query`, best first. A turn scores as its best block.

    Turn-level, not block-level: two matching blocks in one turn are one piece
    of evidence, and returning both would spend the budget on a single place in
    the transcript.
    """
    _check_schema(db)
    expr, dropped = match_expr(query)
    if not expr:
        return []
    if dropped:
        # Announced, never silent: a truncated query returns a real ranking
        # over fewer terms, which reads exactly like a complete one.
        warnings.warn(
            f"query truncated to {MAX_TERMS} terms; {dropped} dropped",
            stacklevel=2,
        )
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
    return [
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
    ]


def retriever(db: sqlite3.Connection, *, weights: Weights = DEFAULT_WEIGHTS):
    """The seam `bench/score.py` scores: `retrieve(query, k) -> [byte offset]`."""

    def retrieve(query: str, k: int) -> list[int]:
        return [h.byte_offset for h in search(db, query, k=k, weights=weights)]

    return retrieve
