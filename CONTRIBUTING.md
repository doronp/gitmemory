# Contributing to gitmemory

Thanks for your interest. This guide covers setup, testing, the project's
rules, and how a change gets merged. Everyone taking part agrees to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Quick path

```sh
git clone https://github.com/doronp/gitmemory && cd gitmemory
uv sync --extra dev --extra derive     # Python 3.13+, https://docs.astral.sh/uv/
uv run pytest -q                       # ~1.5 min, no network, no downloads
uv run ruff check .
```

Before opening a pull request, make sure all three pass. CI runs the same
commands.

### Optional: the larger suites

| What | Setup | Run |
|---|---|---|
| 328 conformance cases against three third-party MIT corpora | `tests/fetch_fixtures.sh`, then export the two variables it prints | `uv run pytest -q` |
| LongMemEval corpus tests | `bench/fetch_longmemeval.sh` | `uv run pytest -q -m corpus` |
| Mutation and attribution check (slow, by hand) | none | `uv run python tests/mutate_index.py` |
| Benchmarks | see [docs/RESULTS.md](docs/RESULTS.md) and `bench/` | `uv run python -m bench --help` |

## Sign your commits (DCO)

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/).
Add a `Signed-off-by` line to every commit to certify that you wrote the
change, or otherwise have the right to submit it under Apache-2.0:

```sh
git commit -s -m "store: refuse a FIFO as a capture source"
```

We do not ask for a CLA. Contributions come in under Apache-2.0, the same
licence the project goes out under (inbound = outbound).

## The rules this project holds itself to

These rules are why the published numbers can be trusted. Most of them are
enforced by tests, so a PR that breaks one goes red.

1. **Every behavioural change comes with a test that fails without it.** Write
   the test first and watch it fail. For a fix, also add one row to
   `tests/mutate_index.py`: the row reverts the fix and names the test that must
   then fail. If the test does not fail, it is decorative.
2. **Numbers in the docs are pinned.** `tests/test_docs.py` checks the README's
   test count, its negative-control count, its conformance count and the hook
   latency against reality. If your change moves one, update the doc in the
   same PR.
3. **Nobody's personal data, ever.** Never commit a real transcript, and never
   commit an absolute path under a home directory. Test data is synthetic or
   comes from a public corpus. Secrets used in tests are synthetic and live in
   `tmp_path`. `tests/test_no_owner_data.py` scans every tracked file; do not
   add entries to its allowlist to make it pass.
4. **No LLM and no network at runtime.** The product is deterministic. A model
   may appear only in `bench/`, to evaluate the product.
5. **The core has zero dependencies,** and it stays that way. An optional extra
   has to earn its place with a measurement; `pyproject.toml` records why each
   one is there.
6. **Report the losing arm.** A benchmark result is published with its
   baselines, its caveats and every configuration that lost. Choices are made
   on a dev split and reported on the held-out split.
7. **Credit prior art.** Vendored or adapted code goes into
   [THIRD_PARTY.md](THIRD_PARTY.md), its licence into `licenses/`, and its
   notice into [NOTICE](NOTICE).
8. **New source files carry an SPDX header:**
   `# SPDX-License-Identifier: Apache-2.0`.

## Adding an adapter

An agent can be adapted when its history meets the four properties in
[docs/agents.md](docs/agents.md). An adapter is a module in
`src/gitmemory/adapters/` exposing `AGENT` and `parse(path) -> Session`. It
is supported when `tests/conformance.py::check_adapter` passes on a public
corpus of that agent's transcripts. Say in the PR where the corpus comes from
and what its licence is.

## Pull requests

- Keep each PR to one change. Explain the *why* in the description; the diff
  shows the what.
- Link the issue it closes. For anything larger than a fix, open an issue
  first so the design can be discussed before the code is written.
- Merging needs green CI, DCO sign-off on every commit, and approval from a
  maintainer (see [MAINTAINERS.md](MAINTAINERS.md)).
- If the change touches a locked decision in [docs/DESIGN.md](docs/DESIGN.md),
  update the design document in the same PR.

## How this project reviews itself

Every module gets a standalone review round by an agent that did not write it,
in its own worktree, and every finding is re-derived by a route the reviewer did
not use before it is accepted — because a reviewer who is right for the wrong
reason is still being graded. Seven of the last round's tests were refuted that
way and rewritten: three the reviewer caught, four found while writing the tests
that answer them.

`docs/DESIGN.md` holds the locked decisions. `docs/reviews/` holds the record,
including the findings that were disputed and why, and the ones whose
demonstration was wrong while their conclusion held.
