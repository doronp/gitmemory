# E3 — Gemini's review of Claude's half

Reviewed: `src/gitmemory/index.py` (whole file), `src/gitmemory/store.py` (lines 110–150 and
485–555), `src/gitmemory/__main__.py` (`index` and `recall` subcommands), `tests/test_index.py`
(whole file), `tests/mutate_index.py` (mutation set), and `docs/DESIGN.md` §2.7 and §3.

---

### Finding C1 (LOW): Help message discrepancy in `gitmemory index`
The CLI argument parser for the `index` subcommand in `src/gitmemory/__main__.py` specifies:
```python
idx.add_argument("--db", default=None, help="database path (default $GITMEMORY_HOME/index/)")
```
However, the actual default database path returned by `index.db_path(home)` is:
```python
def db_path(home: str | None = None) -> str:
    return os.path.join(store.resolve_home(home), "raw", f"gitmemory-v{SCHEMA}.db")
```
This means the database is built in the `raw/` subdirectory as `gitmemory-v1.db`, which contradicts
the CLI help text stating it defaults to the `index/` directory.

---

### Finding C2 (MEDIUM): Unsafe dictionary access on `block.native` in `_paths`
In `src/gitmemory/index.py` (§2.8, line 285), `_paths` accesses `block.native` assuming it is
always a dictionary or `None`:
```python
value = (block.native.get("input") or {}).get(key) if block.native else None
```
Under untrusted input, schema drift, or corrupted sessions, if `block.native` is present but is of
a non-dictionary type (such as a string or list), calling `.get()` will raise an uncaught
`AttributeError`. Adding an explicit type check is necessary:
```python
is_dict = isinstance(block.native, dict)
value = (block.native.get("input") or {}).get(key) if (block.native and is_dict) else None
```

---

### Finding C3 (LOW): File re-opening race condition/caching in `_parse`
In `src/gitmemory/index.py` (line 235), `_parse` handles multi-segment stored runs by unrolling
them into a temporary file:
```python
fd, tmp = tempfile.mkstemp(prefix="gitmemory-concat-", suffix=".jsonl")
try:
    with os.fdopen(fd, "wb") as out:
        for seg in stored.segments:
            with open(seg, "rb") as fh:
                shutil.copyfileobj(fh, out)
    return adapter.parse(tmp)
```
Writing to a temp path via `os.fdopen(fd)`, closing it at the end of the `with` block, and then
re-opening it immediately by path in `adapter.parse(tmp)` can trigger occasional metadata/locking
delays on network-attached filesystems (NFS) or high-concurrency environments. Passing an open
file object or a file-like stream directly to the adapter's parse function would make this cleaner.

---

### Finding C4 (LOW): Implicit SQLite transaction overhead in `build`
During a full rebuild in `index.build`, blocks are inserted sequentially via `_fill`:
```python
db.execute(
    f"INSERT INTO blocks ({','.join(row)}) VALUES ({','.join(':' + k for k in row)})",
    row,
)
```
While Python's `sqlite3` connection manages implicit transactions, doing bulk inserts using separate
individual `execute` statements on the raw connection relies heavily on implicit driver transaction
demarcations. It is cleaner and more performant to wrap the insertion loop in an explicit
`with db:` block or utilize `db.executemany` to minimize write locks and optimize commit grouping.
