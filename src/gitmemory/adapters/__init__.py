"""Adapter registry. An agent is *supported* when it passes tests/conformance.py.

An adapter is exactly two callables and nothing else:

    parse(path: str) -> Session
    find_session(session_id: str, projects_root: str | None = None) -> str | None

Adapters may not touch git, the index, or derivation. Claude Code is first;
Hermes, Kimi and opencode follow the same contract.
"""

from . import claude_code

ADAPTERS = {claude_code.AGENT: claude_code}


def get(agent: str):
    try:
        return ADAPTERS[agent]
    except KeyError:
        raise ValueError(f"no adapter for {agent!r}; have {sorted(ADAPTERS)}") from None
