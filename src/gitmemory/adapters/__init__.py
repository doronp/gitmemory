"""Adapter registry. An agent is *supported* when it passes tests/conformance.py.

An adapter is exactly two callables and nothing else:

    parse(path: str) -> Session
    find_session(session_id: str, projects_root: str) -> str | None

Adapters may not touch git, the index, or derivation. Claude Code was first and
pi/oh-my-pi is second. The three this docstring used to name next — Hermes,
Kimi, opencode — are not candidates any more: two moved to SQLite and one is
archived. The README's "What this can be pointed at" table is the current list,
and it is a list of *formats*, not of products.
"""

from . import claude_code, pi

# "omp" is an alias, not a second adapter: oh-my-pi is a fork of pi and writes
# the same dialect, so `Session.agent` names the format ("pi") for both. A
# second key here costs nothing and means a user running oh-my-pi does not have
# to know it is a fork to point the daemon at it.
ADAPTERS = {claude_code.AGENT: claude_code, pi.AGENT: pi, "omp": pi}


def get(agent: str):
    try:
        return ADAPTERS[agent]
    except KeyError:
        raise ValueError(f"no adapter for {agent!r}; have {sorted(ADAPTERS)}") from None
