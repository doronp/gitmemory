# E3 — security review (independent agent)

Reviewer: Opus agent, adversarial brief, own synthetic fixtures under `/tmp`, repo untouched.
Every finding below came with a reproduction the reviewer ran. Adjudication and my own
re-verification are in [E3-findings.md](E3-findings.md); this file is the raw report, kept so
the fixer works from the reviewer's evidence rather than my summary of it.

All secrets in the reproductions are synthetic.

---

## 1. BLOCKING — the egress gate misses any credential spanning more than one segment cut

`src/gitmemory/redact.py:129` — `tail, prev = new_tail, path`

`scan_group` carries exactly **one file** of context. When a segment is shorter than the
credential the carry shrinks to that segment's whole length, so a secret crossing two cuts is
invisible in every window. No manifest tampering is needed — just a fast capture cadence,
which is what a per-turn hook produces.

```
token len 48 << SEAM 512
segments: [...000000000000-000000000051, ...051-067, ...067-083, ...083-099, ...099-103]
verify: []
gate clean (may these bytes leave)? True | findings: []
concatenation that would be pushed contains the token: True
scan of the true concatenation: [Finding(..., detector='openai_api_key', tier='high', ...)]
```

The caveat at `redact.py:29-32` — "a single secret longer than this AND cut by a segment
boundary is still missed" — understates it. The real condition is *any* secret spanning more
than one cut, at any length.

## 2. BLOCKING — `sessions()` trusts the manifest's segment order; `verify()` sorts

`store.py:508` and `:523` (no sort) against `store.py:605` (`sorted(..., key=start)`).
`Stored.segments` at `store.py:122` is documented "ascending by start offset". It is not.
Swapping two entries leaves every `start`/`end`/`sha256` correct, so the contiguity proof
passes.

**2a. Egress bypass.**

```
--- honest order --- rc=1
...000-073.jsonl + ...073-097.jsonl:53: high github_token ghp_…[40 bytes]
refusing to push: the redaction gate found credentials
--- REORDERED --- rc=1
gate passed for git@example.invalid:me/private.git; transport lands in E4
```

`store.verify()` on the reordered manifest: `[]`.

**2b. Provenance forgery in the index.** `index._parse` (`index.py:246-252`) concatenates in
the same untrusted order, so `Hit.byte_offset` — "the key every other layer agrees on" —
points at the wrong bytes:

```
honest  : turns 3 [(0,'FIRST turn alpha'), (91,'SECOND turn beta'), (182,'THIRD turn gamma')]
reversed: turns 3 [(0,'THIRD turn gamma'), (91,'SECOND turn beta'), (182,'FIRST turn alpha')]
verify: []
```

**Second trigger, same class.** One segment entry with an escaping path makes `sessions()`
drop the whole generation (`store.py:511`), so `segment_groups()` omits it and only per-file
scanning runs:

```
honest              : rc 1 | refusing to push: the redaction gate found credentials
one escaping entry  : rc 1 | gate passed for git@example.invalid:x/y.git
verify: ['... hole at byte 49 ...', "... segment path escapes the store: '../x'"]
```

The comment at `store.py:513-516` states this must not happen — "a manifest that opts out of
the scan by naming itself oddly would be a bypass, not a skip". The code four lines above
does exactly that. `push` never calls `verify`, so nothing catches it.

## 3. MAJOR — `verify()` builds `seg_dir` from unvalidated manifest `agent`/`session_id`

`store.py:640-642`. `capture()` runs both through `_safe()`; `_verify_manifest` reads them raw,
so an absolute value makes `os.path.join` discard the store root, and `../` works too.

Worse than the disclosure: it disables the stray-file check for the *real* generation
directory. With unattested bytes planted in `raw/claude-code/sess/g00/`:

```
honest manifest     -> ['... unrecorded file in the generation directory: planted.jsonl']
redirected manifest -> (only the victim dir's files)   planted file reported? False
seg_dir -> nonexistent: []                             planted reported? False

$ uv run python -m gitmemory --home /tmp/gmsec/home verify
0 problem(s)
exit=0
```

That is the [E2] defect the comment at `store.py:643-645` claims to have closed.

## 4. MAJOR — `_adopt_orphans` reads manifest segment paths with no `_inside()` guard

`store.py:302` — `open(os.path.join(home, seg["path"]), "rb")`. Every other reader
(`sessions`, `_verify_manifest`) goes through `_inside`; this one does not.

Arbitrary read, folded into the committed proof:

```
poisoned segment path: ../outside-the-store.txt
manifest file_sha256      : 0e9abb94b197bedf...
sha256(OUTSIDE-FILE+BBBBB): 0e9abb94b197bedf...
out-of-store bytes were read and hashed into the proof: True
```

Unbounded hang — point it at a FIFO and `capture` blocks forever *while holding the
per-session flock*, wedging the hook and every later capture for that session:

```
Timeout (0:00:06)!
  File ".../store.py", line 302 in _adopt_orphans
  File ".../store.py", line 351 in _capture
  File ".../store.py", line 340 in capture
```

## 5. MAJOR — terminal-escape injection from untrusted transcripts and manifests

Recall output is explicitly "shown to a user or fed to an agent"; `str.split()` at
`__main__.py:114` treats ESC as printable.

**a. `recall` body** — `__main__.py:114-115`, from attacker-controlled file contents or tool output:

```
b'  -0.000  claude-code/s-bd8dcc50/g00@0  user/text  needle
   \x1b]52;c;SFlKQUNLRUQtU1lOVEhFVElD\x07\x1b[2K \x1b[32m0 problems, store is clean\x1b[0m\n'
OSC-52 clipboard sequence intact: True
```

OSC 52 writes attacker data into the user's clipboard on iTerm2/kitty/xterm/foot/Windows
Terminal; `\x1b[2K` erases the line the tool just printed.

**b. `recall` provenance label** — `store.py:129` / `__main__.py:115`. `Stored.key` is
documented "Not a path — `agent` is untrusted" but is never charset-checked, and manifest
`session_id` is unvalidated (`store.py:522`):

```
b'  -0.000  claude-code/\x1b[2K\rVERIFIED-SIGNED-STORE\x1b[1;32m ✓\x1b[0m/g00@0  user/text  needle here\n'
```

Every hit can be relabelled as coming from a different, trustworthy-looking session.

**c. `verify`** — `store.py:622`, `:626`, `:629` interpolate `seg["path"]` raw, unlike `:615`
which uses `!r`:

```
b"sessions/claude-code/s/g00.json: segment raw/\x1b[2K\rSPOOFED: 0 problem(s)\x1b[1;32m ok\x1b[0m/x unreadable (...)\n1 problem(s)\n"
```

**d. `capture`** — `store.py:370-373` echoes the manifest's `source_path` verbatim:

```
error: session 's' was captured from \x1b]0;pwned-title\x07\x1b[2K\rcaptured from /safe/path\x1b[0m, not /private/tmp/...
```

## 6. MAJOR — quadratic `_PATH` regex: 80 KB of attacker text is 20 s of CPU in `index build`

`index.py:76`, applied per block at `index.py:288`.
`(?:[\w.@%+-]+[\\/])+[\w.@%+-]+` on a long run of word characters with no separator backtracks
at every start offset. `_ELIDE_OVER` does not apply to `text`/`tool_result` payloads, so block
text is unbounded.

```
payload=  20000 chars  transcript=   20113 B  index build=   1.30s
payload=  40000 chars  transcript=   40113 B  index build=   5.18s
payload=  80000 chars  transcript=   80113 B  index build=  20.43s
# isolated: 160 000 chars -> 82.1 s
```

Clean 4× per doubling. ~1 h for a 1 MB blob, ~18 h for 4 MB. Reachable with one `cat` of a hex
dump, a long identifier, or base64url output into a tool result. (`a/a/a/...` is fast; the
pathological input is the *absence* of separators.)

## 7. MAJOR — untyped manifest fields crash `capture` permanently and dump a traceback

`store.py:374` (`_prefix_hash(fh, prev["size"])`), and the same for `prev["segments"]`,
`prev["compact_boundaries"]`, `prev["file_sha256"]` at `:377-400`. `_verify_manifest` and
`sessions()` both type-check these; `_capture` does not, and `__main__.main:155` catches
neither `TypeError` nor `KeyError`.

```
$ ... capture ... --session-id s     # manifest has "size": "10"
rc 1
Traceback (most recent call last):
  File ".../store.py", line 374, in _capture
    whole = _prefix_hash(fh, prev["size"])
  File ".../store.py", line 162, in _prefix_hash
    chunk = fh.read(min(CHUNK, left))
TypeError: '<' not supported between instances of 'str' and 'int'
```

Every subsequent capture dies the same way: a one-byte manifest edit permanently stops capture
for that session, and the traceback discloses absolute install paths.

## 8. MINOR — raw transcripts and manifests are world-readable

`store.py:180` (`_write_atomic`), `:410` (segment temp), `:177`/`:405` (`os.makedirs`, default
0777 & umask).

```
-rw-------  .locks/claude-code/sess.lock          <- explicitly 0o600 (store.py:223)
-rw-------  index/gitmemory-v1.db                 <- 0600 via mkstemp
-rw-r--r--  raw/claude-code/sess/g00/*.jsonl      <- verbatim unredacted transcript
-rw-r--r--  sessions/claude-code/sess/g00.json
drwxr-xr-x  raw/claude-code/sess/g00
```

The derived index is 0600; the unredacted source it came from is 0644.

## 9. MINOR — quadratic prefix re-encode in the JSONL reader

`jsonl.py:74` — `len(text[:pos].encode(...))` re-encodes the whole line prefix per object.

```
objects=  8000  bytes=  416001  parse= 0.06s
objects= 16000  bytes=  832001  parse= 0.21s
objects= 32000  bytes= 1664001  parse= 0.80s
```

Needs raw byte control (multiple top-level objects on one physical line — a shape the module
docstring says occurs in real transcripts). Also: a 64 MiB single line parses at 7.7× peak RSS
(492 MiB).

## Minor, fail-closed

- `store.py:508` + `redact.py:98` — a manifest naming a directory as a segment raises
  `IsADirectoryError` out of `redact.gate`; `main` catches `OSError` → rc 2. Push is blocked,
  not bypassed, but it is an unhandled shape in the gate.
- `index.py:352` — `search(k=-1)` becomes `LIMIT -1`, which is unbounded in SQLite.
  Verified `k=1 -> 1`, `k=-1 -> 3`. CLI flag only, not attacker-reachable.

---

## Clean

- **SQL / FTS5 injection.** `match_expr` (`index.py:301-303`) reduces the query to `\w+` tokens
  and quotes each. 19 probes — `AND/OR/NOT`, `NEAR(a b, 2)`, `prose:secret`,
  `{prose tool_use}:secret`, `sec*`, `^hello`, phrase quotes, parens, `a" OR "b`,
  `x") OR fts MATCH ("y`, combining marks, ZWSP, fullwidth — nothing reaches the FTS5 parser as
  syntax and nothing errors. The `blocks` INSERT interpolates only fixed literal key names;
  `bm25` weights and `k` are bound parameters.
- **`_inside()`.** Rejects `/etc/hosts`, `../../etc/hosts`, prefix-confusion
  `../sl-home-evil/x`, a symlink to an outside file, and a symlink to an outside directory. It
  is the callers that skip it (3, 4) that are the problem.
- **Zip-slip / symlink following in the write path.** `capture` never writes outside `home`;
  `_safe()` (`store.py:63-75`) is sound and refuses to clobber an existing segment (`:446`).
- **Deeply nested JSON.** 100 000-deep array → `{'json_decode_error': 1}`, no crash;
  `_MAX_DEPTH=32` bounds `_scrub`/`_flatten`.
- **Owner-path references.** None in `src/`, `tests/`, or `bench/`. `claude_code.find_session`
  defaults to `~/.claude/projects` as the Claude Code product location, charset-checks the id,
  `glob.escape`s it, and confirms every hit resolves inside the root — correct, not a leak.
- **Findings never carry the secret.** `_mask` (`redact.py:74-76`) is 4 bytes plus a length;
  `safe_url` strips userinfo.
- **Temp files.** `index._parse`'s concat file and `build`'s `.building-*.db` are both `mkstemp`
  0600.

## Unverified suspicions

- **TOCTOU between `_inside()` and the later `open()`.** `sessions()`/`_verify_manifest`
  realpath first and read later (`index.py:250`, `store.py:619`, `redact.py:98`); swapping an
  intermediate directory component for a symlink in that window would redirect the read.
  Requires a local race the reviewer did not build.
- **`.git` is excluded from the gate** (`__main__.py:70`), but `git push` ships the object
  store, not the working tree. A credential committed and later removed from the worktree would
  be pushed unscanned. Not reproducible today — the transport is stubbed.
- **`SEAM = 512` vs long secrets.** A PEM body or a >512-byte token straddling one cut is missed
  by design; only the multi-cut case (finding 1) was reproduced.
