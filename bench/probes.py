"""Two adversarial probes in vocabularies neither fixture split uses.

The gate fixture scores 1.0000/1.0000 and has done through four rounds of real
defects. That is not a contradiction: the generator writes in one register, and
every defect found since has been invisible to it. These probes are the other
measurement — hand-labelled by what each sentence *does*, in words the corpus
does not contain.

**Both are spent.** A held-out set is spent the moment it informs a revision,
and each of these set the brief for a round and then scored its result. The
numbers pinned in `bench/test_probes.py` are therefore a regression floor, not
evidence of generalisation, and optimising against them would be optimising
against training data. The next honest number needs a third probe nobody has
scored yet.

Scored by class and by ceiling separately. CEILING groups are shapes the
extractor declares out of reach — bare imperatives with no obligation frame,
mostly — so failing them confirms a known limit and passing one is a bonus.

Run: `python -m bench.probes`
"""

from __future__ import annotations

from gitmemory.derive import _decision_kind

# --------------------------------------------------------------------------- #
# Probe A
#
# An adversarial probe written by the reviewer, in a third vocabulary.

# Neither the dev dump nor the held-out pools were consulted for phrasing, and
# the extractor author's own probe and tests were deliberately not read before
# these sentences were written. Every item is hand-labelled by what the sentence
# *does*, not by what the extractor is likely to say about it.

# Groups marked CEILING are shapes the author declared out of reach in their own
# report. They are scored separately: failing them is a known ceiling being
# confirmed, passing them is a bonus. Everything in the CLASS groups is a shape
# the rules claim to cover, said in words neither split uses.
# --------------------------------------------------------------------------- #

# (group, role, text, expected)
CASES_A = [
    # --- CLASS: directives the user imposes, phrased freshly ------------------
    (
        "directives",
        "user",
        "Ship the parser with a hand-rolled tokenizer rather than leaning on a PEG library.",
        "directive",
    ),
    (
        "directives",
        "user",
        "Under no circumstances should the worker touch the production bucket.",
        "directive",
    ),
    ("directives", "user", "Keep credentials in the keychain, not in a dotfile.", "directive"),
    (
        "directives",
        "user",
        "Every migration has to be reversible; irreversible ones never land.",
        "directive",
    ),
    (
        "directives",
        "user",
        "We favour a flat schema over nested documents for anything the API returns.",
        "directive",
    ),
    (
        "directives",
        "user",
        "Route everything through the adapter in place of the raw client.",
        "directive",
    ),
    # --- CLASS: reversals the assistant announces -----------------------------
    (
        "reversals",
        "assistant",
        "Let me pull the caching out of the handler and put it "
        "behind the repository interface instead.",
        "reversal",
    ),
    ("reversals", "assistant", "We'll swap the regex validator for a real parser.", "reversal"),
    (
        "reversals",
        "assistant",
        "On reflection the queue is overkill — a plain table with a status column does it.",
        "reversal",
    ),
    (
        "reversals",
        "assistant",
        "Giving up on the in-process cache; I'll lean on the database's own buffer pool.",
        "reversal",
    ),
    ("reversals", "assistant", "Rolling back to the synchronous client for now.", "reversal"),
    # --- CLASS: restatement, which is not a new decision ----------------------
    (
        "guard: backref",
        "user",
        "Circling back to what I asked for: never log the raw request body.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "This is the third time I'm saying it — use the shared fixture, not a fresh one per test.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "Reiterating from the kickoff doc: no direct writes to the replica.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "You already have this one, but repeating for safety: "
        "config lives in env vars rather than files.",
        None,
    ),
    ("guard: backref", "user", "Quick recap of the rule: don't merge without a green build.", None),
    # --- CLASS: an open alternative set ---------------------------------------
    (
        "guard: deliberation",
        "user",
        "Postgres or SQLite — I genuinely don't know which fits here.",
        None,
    ),
    (
        "guard: deliberation",
        "user",
        "There's a case for dropping the cache entirely and a case for keeping it warm.",
        None,
    ),
    (
        "guard: deliberation",
        "user",
        "Still chewing on whether to use a queue instead of a cron.",
        None,
    ),
    # --- CLASS: a report about the past ---------------------------------------
    ("guard: retrospective", "user", "The first cut of this shipped with no retries at all.", None),
    (
        "guard: retrospective",
        "user",
        "Version 1 stored everything in a single table rather than splitting by tenant.",
        None,
    ),
    # --- CLASS: an opinion, which imposes nothing -----------------------------
    (
        "guard: opinion",
        "user",
        "I'm not sold on dropping the shim in favour of the native API.",
        None,
    ),
    ("guard: opinion", "user", "I've never seen that fail in practice.", None),
    (
        "guard: opinion",
        "user",
        "We don't especially mind which serialiser it uses, as long as it isn't pickle.",
        None,
    ),
    # --- CLASS: repairing a token, from either mouth --------------------------
    ("guard: repair", "assistant", "That should read `--no-verify`, not `--no-verfy`.", None),
    (
        "guard: repair",
        "user",
        "Small correction: the flag is spelled `--strict`, not `--stict`.",
        None,
    ),
    # --- CLASS: one-sided prose, the bulk of any transcript -------------------
    ("one-sided", "user", "Please add a test for the empty-input case.", None),
    ("one-sided", "user", "Is there a reason we're not using the stdlib here?", None),
    ("one-sided", "assistant", "Running the suite again now that the import is fixed.", None),
    ("one-sided", "assistant", "I'll start with the adapter and then wire the index.", None),
    ("one-sided", "assistant", "The build failed because the lockfile is stale.", None),
    (
        "one-sided",
        "assistant",
        "Both work; I'll pick whichever reads better once the tests are in.",
        None,
    ),
    # --- CEILING: shapes the author declared out of reach ---------------------
    (
        "ceiling",
        "user",
        "Stop writing new endpoints in Flask — the whole surface is FastAPI now.",
        "directive",
    ),
    (
        "ceiling",
        "assistant",
        "Scratch the cron approach; a systemd timer is the right tool here.",
        "reversal",
    ),
    (
        "ceiling",
        "assistant",
        "I had the retry loop wrapping the whole request; I'm "
        "moving it inside the session so only the socket call retries.",
        "reversal",
    ),
    ("ceiling", "user", "We ran it that way through most of the migration.", None),
    (
        "ceiling",
        "user",
        "It isn't obvious to me that a lock buys anything over an atomic write.",
        None,
    ),
]

# --------------------------------------------------------------------------- #
# Probe B
#
# Probe set B — the holdout for guard round 3.

# Written before round 3 was commissioned and not disclosed to the author, for
# the same reason a corpus seed is spent once it informs a revision: a probe you
# hand over stops measuring anything. Set A found the gaps; set B says whether
# the fix generalises or memorises.

# Register is deliberately different from both set A and the corpus: chat-shaped,
# multi-sentence blocks, trailing code fragments, sloppy punctuation. A block in
# a real transcript is rarely one clean sentence, and nothing has tested that.

# CEILING items are shapes the author declared out of reach. Scored separately.
# --------------------------------------------------------------------------- #

CASES_B = [
    # --- CLASS: directives, buried in chat-shaped blocks ----------------------
    (
        "directives",
        "user",
        "ok so I looked at the diff. The retry count belongs in config, not "
        "hardcoded at the call site.",
        "directive",
    ),
    (
        "directives",
        "user",
        "one thing before you carry on — timestamps go in UTC, never local.",
        "directive",
    ),
    (
        "directives",
        "user",
        "Use `pathlib` throughout instead of `os.path`. I know it's a bigger diff, do it anyway.",
        "directive",
    ),
    (
        "directives",
        "user",
        "no bare excepts anywhere in this module",
        "directive",
    ),
    (
        "directives",
        "user",
        "Rather than a background thread, drive it off the existing event loop.",
        "directive",
    ),
    # --- CLASS: reversals, chat-shaped ----------------------------------------
    (
        "reversals",
        "assistant",
        "Hmm, that's not going to work. Dropping the thread pool and doing it "
        "synchronously instead.",
        "reversal",
    ),
    (
        "reversals",
        "assistant",
        "I'm going to back out the subclass and use plain composition here.",
        "reversal",
    ),
    (
        "reversals",
        "assistant",
        "Replacing the hand-written loop with `itertools.groupby`.",
        "reversal",
    ),
    (
        "reversals",
        "assistant",
        "The mmap approach isn't paying for itself — switching to a plain buffered read.",
        "reversal",
    ),
    # --- CLASS: restatement. The frame is mid-block, which is where it lives
    # --- in real chat, and is the shape set A showed escapes an anchored match.
    (
        "guard: backref",
        "user",
        "I know we went over this, but saying it again: config lives in env vars, not on disk.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "Per my earlier message, the worker must never write to the replica.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "Not sure it landed the first time, so once more — no network calls in unit tests.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "To restate the constraint from the spec: every write goes through the "
        "queue rather than direct.",
        None,
    ),
    (
        "guard: backref",
        "user",
        "Same note as before: prefer the stdlib json over ujson.",
        None,
    ),
    # --- CLASS: an open alternative set ---------------------------------------
    (
        "guard: deliberation",
        "user",
        "Torn between a thread and a subprocess here, honestly.",
        None,
    ),
    (
        "guard: deliberation",
        "user",
        "Could go either way: bundle it, or ship it as a separate package.",
        None,
    ),
    (
        "guard: deliberation",
        "user",
        "I keep going back and forth on whether the cache should be per-request "
        "rather than global.",
        None,
    ),
    (
        "guard: deliberation",
        "assistant",
        "Two options and I don't have a strong view — a lock file, or an "
        "advisory lock in Postgres instead.",
        None,
    ),
    # --- CLASS: a report about the past ---------------------------------------
    (
        "guard: retrospective",
        "user",
        "Back in the 0.2 days we kept everything in one table rather than sharding it.",
        None,
    ),
    (
        "guard: retrospective",
        "user",
        "Historically this ran with no timeout at all, which is how we got here.",
        None,
    ),
    (
        "guard: retrospective",
        "assistant",
        "For context: the original implementation used polling instead of a "
        "watch, and it was fine at that scale.",
        None,
    ),
    # --- CLASS: an opinion, which imposes nothing -----------------------------
    (
        "guard: opinion",
        "user",
        "I honestly don't care whether it's tabs or spaces.",
        None,
    ),
    (
        "guard: opinion",
        "user",
        "My gut says a queue is nicer than a cron here, but I could be wrong.",
        None,
    ),
    (
        "guard: opinion",
        "assistant",
        "I really don't think the abstraction earns its keep, though that's a taste call.",
        None,
    ),
    # --- CLASS: repairing a token ---------------------------------------------
    (
        "guard: repair",
        "user",
        "typo above — I meant `--dry-run`, not `--dryrun`.",
        None,
    ),
    (
        "guard: repair",
        "assistant",
        "Correction: the module is `importlib.metadata`, not `importlib.metadeta`.",
        None,
    ),
    # --- CLASS: one-sided prose, the bulk of any transcript -------------------
    ("one-sided", "user", "can you add a docstring to that", None),
    (
        "one-sided",
        "user",
        "Why does this need a lock at all? Genuine question.",
        None,
    ),
    (
        "one-sided",
        "assistant",
        "Added the fixture and re-ran; 12 passed, 0 failed.",
        None,
    ),
    (
        "one-sided",
        "assistant",
        "Next I'll wire the CLI flag through, then update the help text.",
        None,
    ),
    (
        "one-sided",
        "assistant",
        "That traceback is a stale `.pyc` — clearing `__pycache__` and retrying.",
        None,
    ),
    (
        "one-sided",
        "user",
        "The docs say the default is 30s but the code says 60s.",
        None,
    ),
    # --- CEILING: declared out of reach ---------------------------------------
    (
        "ceiling",
        "user",
        "Kill the Flask endpoints. Everything is FastAPI from here.",
        "directive",
    ),
    (
        "ceiling",
        "assistant",
        "Forget the cron idea — a systemd timer is the right shape.",
        "reversal",
    ),
    (
        "ceiling",
        "user",
        "We shipped it without retries and lived with it for a year.",
        None,
    ),
]

PROBES = {"A": CASES_A, "B": CASES_B}


def score(cases) -> tuple[int, int, int, int, list]:
    """`(class_ok, class_n, ceiling_ok, ceiling_n, misses)`.

    The two are never added together. A ceiling miss is a limit behaving as
    documented; a class miss is a rule that claims the shape and does not
    deliver it, and only the second is a defect.
    """
    class_ok = class_n = ceil_ok = ceil_n = 0
    misses = []
    for group, role, text, expected in cases:
        got = _decision_kind(text, role)
        if group == "ceiling":
            ceil_n += 1
            ceil_ok += got == expected
        else:
            class_n += 1
            class_ok += got == expected
        if got != expected:
            misses.append((group, role, text, expected, got))
    return class_ok, class_n, ceil_ok, ceil_n, misses


def main() -> int:
    for name, cases in PROBES.items():
        class_ok, class_n, ceil_ok, ceil_n, misses = score(cases)
        print(f"probe {name}:  class {class_ok}/{class_n}   ceiling {ceil_ok}/{ceil_n}")
        for group, role, text, expected, got in misses:
            print(f"  [{group}] expected {expected}, got {got}\n    ({role}) {text}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
