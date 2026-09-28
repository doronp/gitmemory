# E7 — security review of the redaction gate

One of seven agents in the RC1 security round, each in its own git worktree,
none with a hand in the code it read. This one was given the secrets surface:
`src/gitmemory/redact.py`, the `push` path in `src/gitmemory/__main__.py`, the
`derived/` write door in `derive._write`, and the label that reaches
`graph.extraction`.

Baseline before any edit: **964 passed, 1 deselected**. After: **981 passed, 1
deselected**.

Every finding below was **re-derived independently before it was accepted**, by
a route the reviewer did not use. Two of them changed shape under that:
reproducing F2 first turned up nothing at all because `gitmemory capture` does
not create a git repository — only `watch` does — so the first attempt was
running `git ls-files` against a directory git had never heard of. The finding
is real; the demonstration in the report was not the one that shows it.

## Status

**Ten findings: six fixed, three documented, one referred.** Each fix is pinned
by a named test and a negative control in `tests/mutate_index.py`. The row count
went 267 → 286, and the 19 rows this round added were run: **19/19 CAUGHT by
their intended test**, after one row was rewritten for being a crash rather than
a mutant (see "A mutant that is not a program" below).

| | Finding | Outcome |
|---|---|---|
| F1 | A credential deleted from the worktree is still in the push | fixed — the gate reads the object graph |
| F2 | `config.toml` holds the push credential and `git add --all` commits it | fixed — ignored, untracked, and refused at the source |
| F3 | A finding's label is the path, and the path can be the credential | fixed — `_safe_path` |
| F4a | The gate walks `os.walk` and refuses over gitignored bytes | fixed — `git ls-files` |
| F4b | No override flag | referred — product decision, not a patch |
| F5 | Nine credential shapes a transcript contains and no detector saw | fixed — nine detectors |
| F6 | No remediation path documented | documented — README, "If a credential lands in the store" |
| F7 | A manual `git push` is ungated | documented — DESIGN §2.4 |
| F8 | DESIGN §2.4 claims more than the code does | documented — claim withdrawn |
| F9 | The graph does not say how many labels it redacted | fixed — `labels_redacted` |
| F10 | Every detector is a byte regex; nothing decodes | documented — module docstring |

---

## F1 — `git push` ships the object graph, not the checkout

The gate walked the working tree. `git push` transmits the objects reachable
from the refs, so a credential committed and then deleted is **absent from the
walk and present in the push** — and deleting the file is the first thing a
person does on noticing a leak. The gate said clean about bytes it had never
read.

`gitrepo.pushable_objects` streams every reachable object through one
`git cat-file --batch` pipe and `gate` takes them as a third source. Trees and
commits come through too, not only blobs: a session id that is itself a
credential is a *name* inside a tree object. Unreachable objects are excluded
deliberately — `derived/` is rewritten on every rebuild and git prunes loose
objects on a delay, so blocking egress on garbage that will never be pushed is
the over-refusal F4a is about.

`gate` now has three sources and no one walk sees all three. The emptiness check
had to learn about the third: a store whose worktree was emptied still pushes
its history, and that is not nothing to attest to.

## F2 — the store authenticated with a secret it was about to push

`[remote.X] url` in `config.toml` is the only userspace HTTPS push form without
an ssh key. `redact.safe_url` goes to some trouble never to *print* that token.
`gitrepo.commit` runs `git add --all`, and `config.toml` sits at the root of the
store. The same argument, one boundary over, had never been made.

Three changes, because one is not enough:

- `/config.toml` in `GITIGNORE`, so new stores never track it.
- `git rm --cached --ignore-unmatch config.toml` in `init`, because **a
  `.gitignore` line does not untrack an already-tracked file** and `init` runs
  on every start precisely so an old store picks up a new rule. Without it the
  fix reads as applied while every store that predates it goes on committing.
- `push_allowed` now refuses a url with userinfo outright. The honest answer is
  that a url is the wrong place for a secret, and an ssh url has no such field.

The blob already in an old store's history stays there. That is F1's shape and
the answer to it is rotation, which is why the README says so first.

The refusal nearly shipped over-broad: the first draft tested `":" in
parts.netloc.rsplit("@", 1)[0]`, which for `https://host:8443/p.git` — no `@`
anywhere — yields `host:8443` and refuses a port. A store that cannot push is as
broken as one that pushes a token, so there is a mutation row for that direction
too.

## F3 — the gate published what it caught

`scan_path` scans the path as well as the contents, and it exists *because* the
path can be the credential: a Claude Code session directory is named after a
session id, and an id can be a token. Every finding it returned wore that path
as its label, `Finding.__str__` prints the label, and `_push` prints every
finding to stderr — into a terminal an agent transcribes into the transcript
this store then commits, verbatim and append-only. The gate that caught the key
filed a second permanent copy of it.

`_safe_path` masks the matched spans and leaves the rest readable, so the
finding still names the directory and the file. Spans are merged first: `sk-ant-…`
trips both `anthropic_api_key` and `openai_api_key` at the same offset, and
masking each in turn writes the name out twice with two disagreeing byte counts.

## F4 — refusing over bytes that were never being sent

`spool/` is the hook drop-box and `index/` is the SQLite database; both are
gitignored and both are full of exactly the shapes a detector fires on. The
walk scanned them, so a push could be refused over a file `git push` would never
have sent — and there is no override flag anywhere in the CLI (F4b).

`gitrepo.tracked` asks git which files are its own (`ls-files --cached --others
--exclude-standard`). Shorter than reproducing `.gitignore`, and right by
construction.

**F4b is referred, not fixed.** An override is a product decision: a gate that
can be waved through on a deadline is a gate that gets waved through on a
deadline, and the right answer may be a narrower scope rather than a flag. It is
recorded in the README as a deliberate absence.

## F5 — nine shapes, nine detectors

Confirmed misses, each reproduced against the live table before the line was
written. Every one is an alternation on an already-anchored pattern, not an
entropy heuristic: the reason this gate is prefix-anchored is that a
high-entropy rule fires on every sha256 in our own manifests, and
`test_our_own_manifests_do_not_trip_the_gate` is what holds that line.

HIGH: `private_key_block` widened for PGP's `KEY BLOCK` — the detector named
after the thing walked straight past it; `slack_token` widened to `xoxc`/`xoxd`,
the browser-session tokens a copied `curl` carries; and four new —
`slack_webhook`, `google_oauth_token` (`ya29.…`, what a `gcloud`-using agent
actually holds, as opposed to the `AIza` API key), `huggingface_token`.

SUSPECT: `bearer_header` now sees `"authorization": "Bearer …"`, which is how a
transcript records a header — the `"` between the name and the value is not
`\s` — and `Basic` as well; `url_with_password` takes any scheme rather than
three, because `mongodb+srv://` broke the old alternation on the `+` and
`https://user:pw@` — the form this store's own `config.toml` uses — was not in
it at all; and `jwt` is new.

`assigned_secret` is the one that reopens an E2 ruling. E2 declined to widen it
on false-positive grounds. Two separate things were wrong with the narrow form:
the value had to be quoted, so `DB_PASSWORD=hunter2hunter2` in a printed `.env`
was invisible; and `\b` on both sides of the keyword cannot see
`AWS_SECRET_ACCESS_KEY=…`, because `_` is a word character and there is no
boundary either side of `SECRET`. The left edge is now `(?<![A-Za-z0-9])` — a
separator or nothing, never mid-word, so `mysecretvalue` is still refused — and
the rest of the identifier is consumed on the way to the assignment. E2's bound
is kept: `["']?[^\s"'\n]{12,}` stops at a quote, so `api_key =
os.environ["ANTHROPIC_API_KEY"]` is a lookup and not a value, and
`test_widening_assigned_secret_did_not_make_it_match_a_lookup` holds that.

## F9 — a redaction you can only discover

`ideas()` reports `sentences_redacted`. `graph.extraction` reported nothing. One
false positive here is a block that merely *names* a PEM header, so a store
where the gate misfires on every block looked exactly like a store full of
secrets — and both looked like a store with none. `labels_redacted` is one
`sum`. graphify's own validator was checked against the extra key rather than
assumed to tolerate it.

## F6, F7, F8, F10 — the four that are documentation

Not because they are small. F8 is the largest finding in the report: DESIGN
§2.4 said redaction applies to "anything that leaves the machine", and the code
gates one command. The store is an ordinary git repository in a directory its
owner can read; a hand-run `git push` is ungated (F7), and so is `scp`, a backup
agent, or a second tool pointed at the same path. `core.hooksPath` is
`/dev/null`, so there is not even a `pre-push` hook to hang one on — and
installing one *before* F4b's override exists would make stores unpushable by
hand as well, which is a worse product than an honest sentence. The claim is
withdrawn and replaced with what the gate actually is: a floor on the one egress
this product performs.

F10 is the same discipline one level down. Every detector is a byte-literal
regex and nothing decodes, so a credential that arrives as `sk-ant-…` in a
JSON string, or base64, or wrapped mid-value by an 80-column terminal, is not
seen — and the consumer that decodes it sees the credential. Normalising every
encoding is not achievable; under a threat model where the attacker picks the
bytes, that race is lost. The ceiling is in the module docstring, because a gate
that hides its ceiling is worse than a narrow one.

F6 is the README's new section. It leads with *rotate the credential*, because
the store is append-only and the blob is in the history whatever happens next.

---

## A mutant that is not a program

Nine of the nineteen new rows delete one detector out of a table. Eight of the
entries are one line; `jwt` is a four-line tuple, and deleting only its
`re.compile` leaves `("jwt",)` — which raises `ValueError: not enough values to
unpack` inside `scan_bytes` and takes down every test that scans anything. The
harness reported CAUGHT, and it was wrong to: the intended test went red on an
unpacking error, not on `jwt would have been pushed`. **A mutant that crashes is
credited with being caught by whatever test ran into the crash first, which is
attribution on no evidence** — the same failure `verdict()`'s `BROKEN` branch
exists to catch, one level below where that branch can see it. The row now
deletes the whole tuple and the failure message names the shape.

`test_every_mutation_row_names_a_test_that_exists` also had to learn that the
field is a `-k` expression: those nine rows select a single parametrisation
(`test_… and slack_token`), because otherwise all nine name the same function
and a row that mutated the wrong line still reads CAUGHT. The check now requires
every `test_`-shaped identifier in the expression to exist, and refuses a row
that names no test function at all.

## Carried in from the same round

One claim in the report did not survive reproduction and is recorded here rather
than silently dropped: the newline-split `Authorization:\nBearer` case was
reported as disagreeing between the raw and canonical scans in `derive._write`.
Reproduced independently, the two scans **agree**. The `ensure_ascii` hole next
to it is real and was fixed separately (`4036a94`): `\uXXXX` ends in a hex
digit, so a non-ASCII character immediately before a token annihilates the
leading `\b` the rule anchors on, and the door now scans both the canonical and
the raw UTF-8 form.
