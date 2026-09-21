"""The egress gate — the only place secrets are looked for.

Raw stays local and verbatim; that is the thesis, and scrubbing before hashing
would make the bytes we commit not the bytes the agent wrote (DESIGN.md §2.5a
records agentcairn losing exactly that way). So redaction lives at the two
boundaries where bytes leave: anything written to `derived/`, and anything
pushed to a remote.

Two tiers, because a one-tier gate is either useless or unpassable:

  HIGH     shapes that are a credential and nothing else. Blocks egress.
  SUSPECT  shapes that are usually a credential. Reported, does not block.

Findings never carry the matched value — a gate that prints the secret it found
has published it into a log. That includes the *path*: a session id becomes a
committed directory name, so `scan_path` masks any credential in the name it
labels findings with, not just in the bytes it read.

Known limit, stated here because a gate that hides its ceiling is worse than a
narrow one: every detector is a byte-literal regex, and nothing decodes. A
credential that arrives encoded — `\\u0073k-ant-…` in a JSON string, base64, a
token wrapped mid-value by an 80-column terminal — is not seen, and the consumer
that decodes it sees the credential. Normalising every encoding is not
achievable; under T1 the attacker picks the bytes and wins that race. What holds
is the shape of the deal: raw stays local, and the gate is a floor on egress,
not a proof of its absence. [E7]
"""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "Finding",
    "gate",
    "push_allowed",
    "safe_path",
    "scan_bytes",
    "scan_group",
    "scan_path",
]

# How far a credential may straddle a segment boundary and still be seen.
# ponytail: a fixed carry, not a full re-stream. A single secret longer than
# this AND cut by a segment boundary is still missed — uncut, `scan_path` has
# the whole file. Raise it if a detector ever needs more.
SEAM = 512

# Anchored on issuer-assigned prefixes, not entropy: a high-entropy heuristic
# fires on every sha256 in our own manifests.
HIGH: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("anthropic_api_key", re.compile(rb"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{80,}")),
    # `BLOCK` is optional because PGP puts it there — `-----BEGIN PGP PRIVATE
    # KEY BLOCK-----` sat between `KEY` and the dashes and walked straight past
    # the detector named after it. [E7]
    ("private_key_block", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}")),
    # `c` and `d` are the browser-session tokens a transcript picks up from a
    # copied `curl`; they are the same credential class as the rest. [E7]
    ("slack_token", re.compile(rb"\bxox[baprsecd]-[A-Za-z0-9-]{12,}")),
    ("slack_webhook", re.compile(rb"\bhttps://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{20,}")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    # The token a `gcloud`-using agent actually holds. `AIza` is the API key; a
    # transcript that ran `gcloud auth print-access-token` has this instead. [E7]
    ("google_oauth_token", re.compile(rb"\bya29\.[A-Za-z0-9_-]{20,}")),
    ("huggingface_token", re.compile(rb"\bhf_[A-Za-z0-9]{30,}\b")),
    ("stripe_secret", re.compile(rb"\b[sr]k_(?:live|test)_[A-Za-z0-9]{24,}\b")),
    ("openai_api_key", re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b")),
)

SUSPECT: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        # The quotes are optional on both sides, and a closing quote may sit
        # before the separator. `DB_PASSWORD=hunter2hunter2` in a printed `.env`
        # and `"api_key": "…"` in a serialised request body are the two forms a
        # transcript actually contains, and the quoted-only pattern saw neither.
        # `["']?[^\s"'\n]{12,}` still refuses `api_key = os.environ["X"]`: the
        # run stops at the quote after eleven characters. [E7, reopens E2:143]
        #
        # `\b` on both sides could not see `AWS_SECRET_ACCESS_KEY=…`, which is
        # what an `env` dump in a transcript actually looks like: `_` is a word
        # character, so there is no boundary either side of `SECRET`, and the
        # keyword is not adjacent to the `=` anyway. So the left edge is
        # `(?<![A-Za-z0-9])` — a separator or nothing, never mid-word, which
        # still refuses `mysecretvalue` — and the rest of the identifier is
        # consumed on the way to the assignment.
        "assigned_secret",
        re.compile(
            rb"""(?ix) (?<![A-Za-z0-9])
            (?: api[_-]?key | secret | passwd | password | auth[_-]?token
            | access[_-]?token | client[_-]?secret )
            [A-Za-z0-9_.-]{0,40} ["']? \s* [:=] \s*
            ["']? [^\s"'\n]{12,} ["']?"""
        ),
    ),
    (
        # `"authorization": "Bearer …"` is how a transcript records an HTTP
        # header — an MCP request log, a fetch tool's input — and the `"`
        # between the name and the value is not `\s`. `Basic` is the same
        # credential in base64. [E7]
        "bearer_header",
        re.compile(
            rb"""(?ix) \b authorization ["']? \s* [:=] \s* ["']?
            \s* (?: bearer | basic ) \s+ [A-Za-z0-9._~+/=-]{20,}"""
        ),
    ),
    (
        # Any scheme, not three. `mongodb+srv://` (the Atlas form) broke the old
        # alternation on the `+`, and `https://user:pw@` — the one form this
        # store's own `config.toml` uses — was not in it at all. [E7]
        "url_with_password",
        re.compile(rb"(?i)\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@"),
    ),
    # A JWT is a bearer credential wherever it appears, and it appears outside
    # an `Authorization:` header constantly. SUSPECT because an id token pasted
    # into a transcript to read its claims is not a secret. [E7]
    (
        "jwt",
        re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    offset: int
    detector: str
    tier: str  # "high" | "suspect"
    shape: str  # masked: enough to locate it, never enough to use it
    length: int = 0  # match length, so a seam finding can prove it straddles

    def __str__(self) -> str:
        return f"{self.path}:{self.offset}: {self.tier} {self.detector} {self.shape}"


def _mask(match: bytes) -> str:
    head = match[:4].decode("ascii", "replace")
    return f"{head}…[{len(match)} bytes]"


def scan_bytes(data: bytes, path: str = "-") -> list[Finding]:
    """Every match in `data`, ordered by offset. Binary-safe: patterns are bytes."""
    found = [
        Finding(path, m.start(), name, tier, _mask(m.group(0)), len(m.group(0)))
        for tier, table in (("high", HIGH), ("suspect", SUSPECT))
        for name, pattern in table
        for m in pattern.finditer(data)
    ]
    return sorted(found, key=lambda f: (f.offset, f.detector))


def safe_path(path: str) -> str:
    """`path` with any credential inside it masked, the rest left readable.

    `scan_path` exists precisely because the path can *be* the credential, and
    every finding it returns wears the path as its label. `Finding.__str__`
    prints that label and `__main__._push` prints every finding to stderr — so
    the gate that caught the key published it, into a terminal an agent
    transcribes into the transcript this store then commits verbatim and
    append-only. Masking the span rather than the whole string keeps the finding
    useful: it still names the directory and the file. [E7]
    """
    data = path.encode("utf-8", "surrogateescape")
    found = scan_bytes(data)
    if not found:
        return path
    out, cursor = b"", 0
    # Merged spans, because two detectors matching the same key (`sk-ant-…`
    # trips both `anthropic_api_key` and `openai_api_key`) would otherwise mask
    # it twice and mangle the rest of the path with it.
    for start, end in _merged(found):
        out += data[cursor:start] + _mask(data[start:end]).encode("utf-8")
        cursor = end
    return (out + data[cursor:]).decode("utf-8", "replace")


def _merged(findings: list[Finding]) -> list[tuple[int, int]]:
    """Non-overlapping `[start, end)` spans covering every match."""
    spans: list[tuple[int, int]] = []
    for start, end in sorted((f.offset, f.offset + f.length) for f in findings):
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def scan_path(path: str) -> list[Finding]:
    """Contents *and* the path itself.

    A session id becomes a committed directory name, and `_SAFE_RE` is happy to
    accept one that is a 103-character API key. Scanning only contents would
    push that key in the tree object names. [E2]
    """
    label = safe_path(path)
    found = scan_bytes(path.encode("utf-8", "surrogateescape"), label)
    with open(path, "rb") as fh:
        return found + scan_bytes(fh.read(), label)


def _edge(path: str, n: int) -> tuple[bytes, bytes]:
    with open(path, "rb") as fh:
        head = fh.read(n)
        fh.seek(max(0, os.path.getsize(path) - n))
        return head, fh.read(n)


def scan_group(paths: list[str]) -> list[Finding]:
    """Scan ordered files, plus the seams the concatenation reconstructs.

    The store cuts the source wherever EOF happened to be when the hook fired,
    so a credential can be split across two segments and match nothing in
    either. What leaves the machine is the concatenation, so the gate has to
    look at that too. Only matches that genuinely span a cut are reported here;
    the rest are already reported per file. [E2]
    """
    found: list[Finding] = []
    tail, first = b"", ""  # bytes carried across the cut, and the file they start in
    for path in paths:
        found += scan_path(path)
        head, new_tail = _edge(path, SEAM)
        if tail:
            # Both halves masked: a seam label names two paths, so it leaks a
            # credential-shaped filename twice over. [E7]
            seam = f"{safe_path(first)} + {safe_path(path)}"
            found += [
                Finding(seam, f.offset, f.detector, f.tier, f.shape, f.length)
                for f in scan_bytes(tail + head, seam)
                if f.offset < len(tail) < f.offset + f.length
            ]
        if len(new_tail) < SEAM:
            # A segment shorter than SEAM extends the carry instead of replacing
            # it: three ten-byte segments can hold a credential that no pair of
            # them shows, and `new_tail` alone would drop the oldest. `first`
            # stays put so the label names where the span began, in two names
            # rather than one per segment. [E3]
            tail, first = (tail + head)[-SEAM:], first or path
        else:
            tail, first = new_tail, path
    return found


def gate(
    paths: list[str],
    groups: list[list[str]] | None = None,
    *,
    objects: Iterable[tuple[str, bytes]] | None = None,
    allow_empty: bool = False,
) -> tuple[bool, list[Finding]]:
    """(may these bytes leave, everything found). False on any HIGH finding.

    Three sources, because a push has three halves and no one walk sees them
    all. `paths` are files on disk. `groups` are ordered segment runs, scanned
    with their seams — the only scan that sees a credential cut in two by a
    segment boundary. `objects` are `(label, bytes)` pairs from the repository's
    object graph, which is what `git push` actually transmits: a credential
    committed and then deleted is absent from the first two and present in the
    push. [E7]

    Raises when it would have nothing to attest to: `not any(...)` over an empty
    list is True, so a gate that inspected zero bytes used to report "these bytes
    may leave" — which is how a filter bug (`".git" in "~/.gitmemory"`) became a
    full bypass. A gate is the wrong place for an optimistic default; a caller
    that really expects an empty store has to say so. [E2]
    """
    findings = [f for p in paths for f in scan_path(p)]
    findings += [f for g in groups or [] for f in scan_group(g)]
    # Consumed here rather than counted by the caller, so the emptiness check
    # below sees the object graph too: a store whose worktree was emptied still
    # pushes its history, and that is not nothing to attest to.
    seen_objects = False
    for label, data in objects or ():
        seen_objects = True
        findings += scan_bytes(data, f"history:{safe_path(label)}")
    if not paths and not groups and not seen_objects and not allow_empty:
        raise ValueError("the redaction gate was given nothing to scan; refusing to attest")
    return not any(f.tier == "high" for f in findings), findings


def safe_url(url: str) -> str:
    """`https://user:pw@host/x` -> `https://host/x`.

    The only userspace HTTPS push form without an SSH key puts the credential in
    the URL, and this module's own docstring says a gate that prints the secret
    it found has published it. That applies to the secret it was handed. Printed
    output goes to a terminal an agent transcribes into the transcript this
    store then commits verbatim, append-only. [E2]
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url  # scp-like `git@host:path` carries a username, not a secret
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def push_allowed(home: str, remote: str) -> tuple[bool, str]:
    """Whether `config.toml` opts this remote in. Absent config means no.

    Default-deny, and the default config has no remotes at all — a store whose
    whole value is keeping raw bytes must not ship them anywhere because a
    field was left unset (DESIGN.md §2.4).

        [remote.origin]
        url = "git@github.com:me/private.git"
        allow_push = true
    """
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except FileNotFoundError:
        return False, f"no {path}; push is opt-in per remote"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return False, f"{path} is unreadable ({exc})"
    entry = (cfg.get("remote") or {}).get(remote)
    if not isinstance(entry, dict):
        return False, f"{path} has no [remote.{remote}]"
    if entry.get("allow_push") is not True:
        return False, f"[remote.{remote}] does not set allow_push = true"
    if not entry.get("url"):
        return False, f"[remote.{remote}] has no url"
    url = str(entry["url"])
    parts = urllib.parse.urlsplit(url)
    # Only the userinfo, and only when there is any: `https://host:8443/p.git`
    # is a port, and `ssh://git@host:22/p` is a port behind a username.
    userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""
    if ":" in userinfo:
        # `safe_url` goes to trouble never to *print* this credential, and the
        # same argument one boundary over was never made: `config.toml` sits at
        # the root of the store, `gitrepo.commit` runs `git add --all`, and the
        # store would authenticate to a remote with a token it is about to push
        # to that same remote. It is gitignored now, but a store initialised
        # before that is still tracking it, and the honest answer either way is
        # that the URL is the wrong place for a secret. [E7]
        return False, (
            f"[remote.{remote}] url embeds a credential; "
            "use an ssh url (git@host:path) — the inline form gets committed"
        )
    return True, safe_url(url)
