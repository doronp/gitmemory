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
from dataclasses import dataclass

__all__ = ["Finding", "gate", "push_allowed", "scan_bytes", "scan_path"]

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

    def __str__(self) -> str:
        return f"{self.path}:{self.offset}: {self.tier} {self.detector} {self.shape}"


def _mask(match: bytes) -> str:
    head = match[:4].decode("ascii", "replace")
    return f"{head}…[{len(match)} bytes]"


def scan_bytes(data: bytes, path: str = "-") -> list[Finding]:
    """Every match in `data`, ordered by offset. Binary-safe: patterns are bytes."""
    found = [
        Finding(path, m.start(), name, tier, _mask(m.group(0)))
        for tier, table in (("high", HIGH), ("suspect", SUSPECT))
        for name, pattern in table
        for m in pattern.finditer(data)
    ]
    return sorted(found, key=lambda f: (f.offset, f.detector))


def scan_path(path: str) -> list[Finding]:
    with open(path, "rb") as fh:
        return scan_bytes(fh.read(), path)


def gate(paths: list[str]) -> tuple[bool, list[Finding]]:
    """(may these bytes leave, everything found). False on any HIGH finding."""
    findings = [f for p in paths for f in scan_path(p)]
    return not any(f.tier == "high" for f in findings), findings


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
    return True, str(entry["url"])
