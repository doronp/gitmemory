## What and why

<!-- One change per PR. Link the issue: "Closes #123". -->

## Checklist

- [ ] `uv run pytest -q` and `uv run ruff check .` pass locally
- [ ] Each behavioural change has a test that fails without it; each fix has a row in `tests/mutate_index.py`
- [ ] Any pinned number in the docs that moved has been updated
- [ ] No real transcript, credential, or absolute home-directory path in the diff
- [ ] Every commit is signed off (`git commit -s`, [DCO](https://developercertificate.org/))
