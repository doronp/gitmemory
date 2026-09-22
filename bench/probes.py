"""Four adversarial probes in vocabularies no fixture split uses.

The gate fixture scores 1.0000/1.0000 and has done through five rounds of real
defects. That is not a contradiction: the generator writes in one register, and
every defect found since has been invisible to it. These probes are the other
measurement — hand-labelled by what each sentence *does*, in words the corpus
does not contain.

**A and B are spent.** A held-out set is spent the moment it informs a revision,
and each of those set the brief for a round and then scored its result. The
numbers pinned in `bench/test_probes.py` are therefore a regression floor, not
evidence of generalisation, and optimising against them would be optimising
against training data.

**C and D are the generalisation numbers**, each scored once — C on 2026-09-21,
D on 2026-09-22 — and both read **14/32** against A's 26 and B's 27. The gap is
the finding, not a defect in the probes: A and B were written to attack
precision, C and D to attack recall, and the extractor turns out to be badly
lopsided. Both score 10/10 on the items that are not decisions and 4/22 on the
ones that are.

D exists because one number from one domain cannot tell you whether the domain
was the problem. It was written blind, in aviation line maintenance, by an
author who had not seen C or its theatrical show control, and it replicates C
exactly — same total, same split, same four constructions hit. See
`docs/benchmarks/E5-probe-C.md` and `docs/benchmarks/E5-probe-D.md`. Each is
spent the moment anyone tunes against it.

Scored by class and aside separately — see `ASIDE`.

Run: `python -m bench.probes`
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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

# --------------------------------------------------------------------------- #
# Probe C
#
# Written blind on 2026-09-21 by an agent that read only README.md and
# docs/DESIGN.md — not derive.py, not this file, not bench/, not the gate
# reports. It never ran the extractor. Unlike A and B it did not set the brief
# for a round: it was frozen unscored and scored once, after the fact, and the
# number below is therefore the first honest generalisation figure this project
# has. A and B are training data now; C is spent the moment anyone tunes
# against it, so do not.
#
# Domain: theatrical show control — cue stacks, DMX universes, sACN/ArtNet,
# fade curves, grandmaster, followspots, house lights. Chosen so lexical
# overlap with the corpus (parsers, tokenizers, buckets, credentials, workers)
# is impossible by construction.
#
# Conventions the author committed to, stated before scoring:
#   1. A one-off work request ("run the validator") is None — it asks for
#      labour, it does not bind how later work is done.
#   2. Reversal is scored on effect, never on cue words: no item in the
#      reversal groups contains "actually" or "instead".
#   3. `directive` is read as human-imposed, so no assistant turn in the 32 is
#      a directive. The assistant self-commitment case is in `hard`.
#
# The 32 split 11 directive / 11 reversal / 10 None. The 8 `hard` items are
# the ones the author could not settle — an assistant imposing a rule on
# itself, a correction of a belief rather than of a decision, a rule proposed
# in interrogative mood, a third party's rule relayed. They are scored and
# reported apart from the 32 for the same reason the CEILING groups are: an
# item two careful readers would split on measures the label, not the code.
# --------------------------------------------------------------------------- #

# (group, role, text, expected)
CASES_C = [
    (
        "standing-rule-declarative",
        "user",
        "Fade times live in the cue stack as integer milliseconds, never seconds.",
        "directive",
    ),
    (
        "standing-rule-declarative",
        "user",
        "Everything that speaks to the desk goes out through the sACN sender; nothing opens its "
        "own socket.",
        "directive",
    ),
    (
        "standing-rule-declarative",
        "user",
        "The grandmaster belongs to the console — our code reads it and does not set it.",
        "directive",
    ),
    (
        "directive-terse",
        "user",
        "One universe per output thread. No sharing.",
        "directive",
    ),
    (
        "directive-terse",
        "user",
        "Patch file stays YAML.",
        "directive",
    ),
    (
        "directive-buried-clause",
        "user",
        "I honestly don't care how the fade curve gets computed, as long as the interpolation "
        "happens on the show thread and not inside the renderer.",
        "directive",
    ),
    (
        "directive-buried-clause",
        "user",
        "While you're in the cue loader anyway, index zero stays reserved for blackout.",
        "directive",
    ),
    (
        "directive-buried-clause",
        "user",
        "Whatever you end up doing about the moving-head timeout — and I know it's messy, the "
        "fixtures don't even agree on what a timeout means — the house lights still have to be up "
        "within two seconds of the panic button.",
        "directive",
    ),
    (
        "directive-selection-or-hedge",
        "user",
        "Between ArtNet and sACN for the studio rig, we're going with sACN.",
        "directive",
    ),
    (
        "directive-selection-or-hedge",
        "user",
        "I'd rather eat the slower timecode sync than keep the one that drifts, so take the slow "
        "one.",
        "directive",
    ),
    (
        "directive-selection-or-hedge",
        "user",
        "Let's not grow a second dimmer curve table; fold the extra points into the one we "
        "already have.",
        "directive",
    ),
    (
        "reversal-plain",
        "user",
        "The millisecond rule is off for the legacy patch importer — that file keeps whatever "
        "units it was written with.",
        "reversal",
    ),
    (
        "reversal-plain",
        "user",
        "Drop the sACN decision. The studio rig turned out to be ArtNet and nobody is rewiring it "
        "this season.",
        "reversal",
    ),
    (
        "reversal-plain",
        "user",
        "Cue index zero is no longer reserved. Numbering is free again.",
        "reversal",
    ),
    (
        "reversal-partial-scope",
        "user",
        "One universe per thread still holds on the main rig, but it stops applying to the "
        "rehearsal room.",
        "reversal",
    ),
    (
        "reversal-partial-scope",
        "user",
        "We are not holding the two-second house-lights target anymore; best effort is fine.",
        "reversal",
    ),
    (
        "reversal-partial-scope",
        "user",
        "Stop treating the rehearsal desk as read-only — that constraint came out of a "
        "misunderstanding on my side.",
        "reversal",
    ),
    (
        "reversal-reinstate-prior",
        "user",
        "Back to the YAML patch file. Moving it to JSON was a mistake and I'd like it undone.",
        "reversal",
    ),
    (
        "reversal-reinstate-prior",
        "user",
        "The followspot goes back to manual. The automatic tracking we agreed on last week isn't "
        "happening.",
        "reversal",
    ),
    (
        "reversal-abandon-plan",
        "user",
        "Scratch precomputing every fade ramp at load time.",
        "reversal",
    ),
    (
        "reversal-abandon-plan",
        "user",
        "That rule about never setting the grandmaster is lifted; the panic path needs it.",
        "reversal",
    ),
    (
        "reversal-abandon-plan",
        "user",
        "We said the cue stack would be append-only. It won't be — editors have to reorder.",
        "reversal",
    ),
    (
        "none-one-off-request",
        "user",
        "Can you re-render the cue list preview and paste me the first ten rows?",
        None,
    ),
    (
        "none-one-off-request",
        "user",
        "Run the patch validator over the touring rig file when you get a minute.",
        None,
    ),
    (
        "none-one-off-request",
        "user",
        "Where does the followspot timeout actually get set?",
        None,
    ),
    (
        "none-external-rule",
        "user",
        "The sACN spec says a source must stop transmitting after three seconds of inactivity.",
        None,
    ),
    (
        "none-external-rule",
        "user",
        "The venue's contract requires a dark break every ninety minutes, which is why the house "
        "file has those gaps in it.",
        None,
    ),
    (
        "none-assistant-reports",
        "assistant",
        "Following your call earlier, fade times stay in milliseconds everywhere in the cue "
        "loader.",
        None,
    ),
    (
        "none-assistant-reports",
        "assistant",
        "Now that the cue stack is no longer append-only, the reorder path doesn't need the "
        "shadow copy.",
        None,
    ),
    (
        "none-assistant-reports",
        "assistant",
        "I've moved the studio rig over to sACN as you asked and the desk is answering on "
        "universe one.",
        None,
    ),
    (
        "none-chatter-speculation",
        "user",
        "Nice — the crossfade looks a lot smoother than it did this morning.",
        None,
    ),
    (
        "none-chatter-speculation",
        "assistant",
        "If timecode ever drifts more than a frame we'd probably want a resync, though I haven't "
        "seen it drift on this rig yet.",
        None,
    ),
    (
        "hard",
        "assistant",
        "From here on I'll keep every cue-stack edit behind the undo journal.",
        None,
    ),
    (
        "hard",
        "user",
        "If the touring rig ever gets a second universe, that one runs on its own thread too.",
        "directive",
    ),
    (
        "hard",
        "user",
        "Half of what I told you about the fade engine this morning was wrong — the ramp is "
        "computed on the desk, not by us.",
        None,
    ),
    (
        "hard",
        "user",
        "Could we make it a rule that no cue writes to two universes in the same frame?",
        "directive",
    ),
    (
        "hard",
        "user",
        "Rig's back up, desk is patched, and the followspot stays on DMX after all rather than "
        "going over the network — the latency was awful.",
        "reversal",
    ),
    (
        "hard",
        "user",
        "The lighting director's note says every cue under three seconds has to be marked as a "
        "snap.",
        None,
    ),
    (
        "hard",
        "user",
        "There is no reason for the renderer to ever touch the patch table.",
        "directive",
    ),
    (
        "hard",
        "user",
        "We've flipped back on the YAML question one more time — JSON, and that's final.",
        "reversal",
    ),
]


# --------------------------------------------------------------------------- #
# Probe D
#
# Written blind by an agent that read only `README.md` and `docs/DESIGN.md` —
# never this file, never the extractor, never A, B or C — in aviation line
# maintenance vocabulary, and committed unscored. Composition matches C's so the
# two numbers compare: 11 reversals, 11 directives, 10 non-decisions.
#
# Read out of the frozen markdown rather than transcribed into this file, which
# is the opposite of what A, B and C do. Transcribing 32 sentences by hand is 32
# chances to change one, and a probe whose items drifted is worth nothing at all;
# the digest below is what makes the file the original rather than a copy of it.
#
# The eleven reversals are all the *user* retracting their own earlier
# instruction — the shape `_decision_kind` declares out of reach in a source
# comment. They are scored in the class anyway, exactly as C scored its eleven,
# because C's 14/32 is the number this probe exists to be compared against.
# --------------------------------------------------------------------------- #

PROBE_D_ITEMS = Path(__file__).resolve().parent.parent / "docs/benchmarks/E5-probe-D-items.md"

# `shasum -a 256 docs/benchmarks/E5-probe-D-items.md` at the commit that froze it.
PROBE_D_SHA256 = "8f53260b00ffcb2dd91d49c3c35eb6dce2ba5d013cb55ed54239628a91659031"

_EXPECTED = {"DIRECTIVE": "directive", "REVERSAL": "reversal", "NOT-A-DECISION": None}
_ROW = re.compile(r"\|\s*(\d+)\s*\|\s*([A-Z-]+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def _probe_d() -> list[tuple[str, str, str, str | None]]:
    """Probe D's 32 items, read out of the frozen file and checked against it.

    The digest is the point. Everything else here is a markdown table parser,
    which is not interesting; a probe that can be edited after it is scored is.
    """
    raw = PROBE_D_ITEMS.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PROBE_D_SHA256:
        raise RuntimeError(f"probe D has been edited since it was scored: {digest}")

    cases = []
    for line in raw.decode().splitlines():
        if row := _ROW.match(line):
            _, label, text, _gold = row.groups()
            if label not in _EXPECTED:
                continue
            group = "none" if label == "NOT-A-DECISION" else label.lower()
            cases.append((group, "user", text, _EXPECTED[label]))
    if len(cases) != 32:
        raise RuntimeError(f"probe D is 32 items; parsed {len(cases)}")
    return cases


CASES_D = _probe_d()

PROBES = {"A": CASES_A, "B": CASES_B, "C": CASES_C, "D": CASES_D}


# Groups held out of the class score. Two reasons, one rule: neither measures a
# shape the rules claim to cover. `ceiling` is a limit the extractor's author
# declared out of reach; `hard` is an item probe C's author could not settle,
# where a miss may be the label's fault rather than the code's. Folding either
# into the 32 would move the denominator every probe is compared against.
ASIDE = frozenset({"ceiling", "hard"})


def score(cases) -> tuple[int, int, int, int, list]:
    """`(class_ok, class_n, aside_ok, aside_n, misses)`.

    The two are never added together. An aside miss is a documented limit or a
    contested label; a class miss is a rule that claims the shape and does not
    deliver it, and only the second is a defect.
    """
    class_ok = class_n = ceil_ok = ceil_n = 0
    misses = []
    for group, role, text, expected in cases:
        got = _decision_kind(text, role)
        if group in ASIDE:
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
        print(f"probe {name}:  class {class_ok}/{class_n}   aside {ceil_ok}/{ceil_n}")
        for group, role, text, expected, got in misses:
            print(f"  [{group}] expected {expected}, got {got}\n    ({role}) {text}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
