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
has published it into a log.
"""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass

__all__ = ["Finding", "gate", "push_allowed", "scan_bytes", "scan_group", "scan_path"]

# How far a credential may straddle a segment boundary and still be seen.
# ponytail: a fixed carry, not a full re-stream. A single secret longer than
# this AND cut by a segment boundary is still missed; raise it if a detector
# ever needs more.
SEAM = 512

# Anchored on issuer-assigned prefixes, not entropy: a high-entropy heuristic
# fires on every sha256 in our own manifests.
HIGH: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("anthropic_api_key", re.compile(rb"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{80,}")),
    ("private_key_block", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}")),
    ("slack_token", re.compile(rb"\bxox[baprse]-[A-Za-z0-9-]{12,}")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_secret", re.compile(rb"\b[sr]k_(?:live|test)_[A-Za-z0-9]{24,}\b")),
    ("openai_api_key", re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b")),
)

SUSPECT: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "assigned_secret",
        re.compile(
            rb"""(?ix) \b (?: api[_-]?key | secret | passwd | password | auth[_-]?token
            | access[_-]?token | client[_-]?secret ) \b \s* [:=] \s* ["'][^"'\n]{12,}["']"""
        ),
    ),
    ("bearer_header", re.compile(rb"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("pg_url_with_password", re.compile(rb"(?i)\b(?:postgres(?:ql)?|mysql|mongodb)://[^\s:@/]+:[^\s@/]+@")),
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


def scan_path(path: str) -> list[Finding]:
    """Contents *and* the path itself.

    A session id becomes a committed directory name, and `_SAFE_RE` is happy to
    accept one that is a 103-character API key. Scanning only contents would
    push that key in the tree object names. [E2]
    """
    found = scan_bytes(path.encode("utf-8", "surrogateescape"), path)
    with open(path, "rb") as fh:
        return found + scan_bytes(fh.read(), path)


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
    tail, prev = b"", ""
    for path in paths:
        found += scan_path(path)
        head, new_tail = _edge(path, SEAM)
        if tail:
            found += [
                Finding(f"{prev} + {path}", f.offset, f.detector, f.tier, f.shape, f.length)
                for f in scan_bytes(tail + head, path)
                if f.offset < len(tail) < f.offset + f.length
            ]
        tail, prev = new_tail, path
    return found


def gate(
    paths: list[str], groups: list[list[str]] | None = None, *, allow_empty: bool = False
) -> tuple[bool, list[Finding]]:
    """(may these bytes leave, everything found). False on any HIGH finding.

    `groups` are ordered segment runs, scanned with their seams. Raises when it
    would have nothing to attest to: `not any(...)` over an empty list is True,
    so a gate that inspected zero bytes used to report "these bytes may leave" —
    which is how a filter bug (`".git" in "~/.gitmemory"`) became a full bypass.
    A gate is the wrong place for an optimistic default; a caller that really
    expects an empty store has to say so. [E2]
    """
    if not paths and not groups and not allow_empty:
        raise ValueError("the redaction gate was given nothing to scan; refusing to attest")
    findings = [f for p in paths for f in scan_path(p)]
    findings += [f for g in groups or [] for f in scan_group(g)]
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
    return True, safe_url(str(entry["url"]))
