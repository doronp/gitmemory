# E5 secondary set — the extractor on real text

**Precision 0.0000. Recall 0.0000.**

Not a typo and not a harness fault. On 140 real human turns taken from another
developer's published Claude Code sessions, the decision extractor produced 32
`directive` nodes and **not one of them was a directive**, while missing both of
the two that were there.

Seven fixes later it produces 7 and one of them is a directive, which is the
first non-zero numerator this page has had. The headline above is the number as
measured, and it stays the headline: fixes 1 to 7 were all tuned against this
corpus after it was scored, so 0.1250 is a regression floor and 0.0000 is the
measurement.

The same extractor read **1.0000 / 1.0000** on its held-out synthetic split.
After fix 2 it reads **1.0000 / 0.7428** there, and the loss is priced below.

Its larger output on these sessions is the assistant-side `reversal` — 61
distinct blocks — and **9 of those 61** survived adjudication. Precision 0.15.
Three fixes later it emits **7 of the 61 and all seven are reversals**, which is
a smaller claim than it sounds and is qualified where it is measured.

| Measurement | Text | Score |
|---|---|---|
| Gate, held-out split | synthetic, generated | 1.0000 / 1.0000 — **1.0000 / 0.7428 after fix 2** |
| Probe A / B | fiction, spent | 26/32, 27/32 — **23/32, 25/32 after fix 2** |
| Probe C / D | fiction, blind, unspent | 14/32, 14/32 — **16/32, 16/32 after fix 7** |
| **This set** | **real, third-party, census** | **precision 0.0000, recall 0.0000** — **0.1250 / 0.5000 after fix 7**, on 1 true positive |

`docs/DESIGN.md` §3 promised this set — "30 hand-labeled real-shaped sessions to
confirm the synthetic set isn't too easy". It did not confirm it.

## What the set is

`test_data/real_projects` in [claude-code-log](https://github.com/daaain/claude-code-log)
at pin `6ad029e` — the MIT corpus this repository already clones for parser
conformance. Four real projects, real sessions, somebody else's. Not one byte of
the owner's history, which is the rule every corpus here is held to.

**A census, not a sample.** Of the user-role blocks in those sessions, 1095 are
tool results and 20 are empty; the extractor reads none of them. The remaining
219 are prose, 140 of them distinct, and **all 140 are labelled**. There is no
sampling step to argue with.

**The corpus is not vendored.** `docs/benchmarks/E5-secondary-manifest.json`
carries a sha256 per item and the label; `bench/secondary.py` reads the text back
out of the clone and refuses to score if a digest, a filename or the item count
has moved. Same arrangement as LongMemEval — the corpus is fetched, never
vendored — and here it also settles the question of republishing a stranger's
sessions.

It does not settle the question of quoting one. This file quotes **8** runs of
40 characters or more out of those sessions verbatim, because an argument about
whether a particular sentence is a directive cannot be made without the
sentence. Eight sentences is a citation and 140 would be a republication; the
line is drawn here and `tests/test_docs.py` holds it. Until E7b the paragraph
above said "no transcript text is committed", in the file doing the quoting.
[E7b L4-F3]

**Three annotators, independently, blind to the extractor.** Each was given the
definitions and the 140 texts, and forbidden to open this repository. They
agreed on **139 of 140** labels and on **140 of 140** machine/human calls. The
single split was 2–1 on *"I have both Node and Python, but I don't want to make
it only work for me or make assumptions about people's env. Would there be a
cross-platform solution?"* — two read the constraint as standing, one read the
question as scoping it to the moment. Majority stands; the dissent is recorded
in `votes`.

## What real sessions actually contain

| | count | share |
|---|---|---|
| distinct user prose blocks | 140 | |
| … stating a standing rule (`directive`) | **2** | 1.4% |
| … revoking the user's own earlier instruction (`reversal-by-user`) | **3** | 2.1% |
| … neither (`none`) | 135 | 96.4% |
| … **not typed by a human at all** | **55** | **39.3%** |

Two facts here are worth more than the score.

**A standing rule is rare.** Two in 140 turns. Every probe in this repository is
a third or more decisions by construction, because an author asked to write
decisions writes decisions. A decision graph built from real sessions is a
sparse object, and an extractor tuned on a corpus where a third of turns are
rules is tuned for a world that does not exist.

**Two turns in five in the user role were not typed by a user.** The CLI injects
its own text there: `<ide_opened_file>`, `<ide_selection>`, `<command-name>`,
`<local-command-stdout>`, `<bash-input>`, `[Request interrupted by user]`. The
annotators identified all 55 unanimously, so the category is not a judgement
call. **No probe can contain this**, because every probe item is a sentence
somebody wrote on purpose.

## The 32 false positives

Nineteen of the thirty scored against `none` come from those machine blocks, and
the mechanism is one sentence the editor injects on every file you open:

> `<ide_opened_file>The user opened the file … in the IDE. This may or may not be related to the current task.</ide_opened_file>`

`_PROHIBIT` matches **`may not`**. That is the whole defect. Two more of the same
kind: `<local-command-stdout>(no content)</local-command-stdout>` matches
`no content`, and `No MCP servers configured` matches `No MCP`. A bare negated
noun phrase is read as a prohibition, and machine boilerplate is full of them.

The eleven human-text false positives are ordinary task talk with a negation in
it — *"…please go ahead with creating the branch and commits (but don't push)"*,
*"…I didn't find those cross-session navigation links helpful, can you please
replace them…"*, *"Wait, does it only run on main?"*. All three annotators read
all of these as task-scoped, and the last is a question.

The two misses are the two real directives: the cross-platform constraint quoted
above, and *"keep it simple and dependency free (except transformers.js) and
compilation free… the same 3 simple files"*. Both are plain positive standing
rules with no prohibition and no substitution frame — **exactly the shape probes
C and D said was missed, seven times each.** Three independent measurements, one
finding.

## The declared ceiling, priced

Three of the 140 turns revoke an instruction the user gave earlier. The
extractor is documented as not claiming that shape. What it does instead is
worse than nothing: **two of the three come back as `directive`.**

> *"Please have a look at this patch diff, I changed my mind a bit about it and
> would like to combine the logic: take the least nested path first…"*

The memory system records the revocation as a rule. Probe C predicted this in
the abstract and probe D measured it twice; this is the first time it has been
seen on text somebody actually typed, at a base rate of 2 in 140.

## The assistant side, which is larger

The labelled census covers the user role. Running the production path —
`derive.decisions()` — over the same sessions emitted **131 nodes from 1049
prose blocks** when this was written:

| | nodes |
|---|---|
| `directive`, user role, machine-injected text | 36 |
| `directive`, user role, human text | 32 |
| `reversal`, assistant role | 63 |

Where it stands after the three fixes below: **25 nodes from 862 prose blocks**
— 16 `directive` in the user role, 9 `reversal` in the assistant role, none of
them on a block `_injected` declines. The 1049 → 862 is that filter, and the
share it declines is larger than the annotated rate suggests: **121 of the 219
user-role text blocks, 55.3%**, against **51 of the 140 distinct ones, 36.4%**,
because a notice repeats and a sentence does not.

The 63 come from 61 distinct blocks, and they are the single largest output the
extractor produces on real sessions. Three adjudicators applied the reversal
definition to all 61 — not blind, since these *are* the extractor's output, so
this is an adjudication rather than a labelling round, and it measures precision
only.

**9 of 61. Precision 0.1475**, with 58 of 61 unanimous.

Six of every seven assistant-side nodes the extractor writes into a real
session's graph are not what they say they are. The substitution frame is where
it happens: `_CONTRAST` alone accounts for 31 of the 52 wrong calls, and the
sentences are ordinary forward narration that happens to name two things.

> *"I need to pass a Path object instead of a string. Let me fix this:"*
>
> *"Now I'll replace the complex cross-session navigation with a simple link to
> the combined transcript."*
>
> *"It seems like the code is still falling back to the regular `generate_html`
> function instead of using the combined link version."*

The first is a one-argument repair with the design unchanged. The second is the
next step of a plan the user asked for. The third is a bug being described. None
is the assistant putting down an approach it had taken up, and all three read
identically to a regex looking for *instead*.

`_SWITCH` accounts for 9 more alone and 9 jointly; `_ABANDON` + `_ADOPT`
assembles the last 3. On the right side the split is the same shape — 7 of the 9
true reversals come from `_CONTRAST` — so the frame is doing real work at a base
rate of 7 in 38.

The three split items are all ones where a plan is narrowed rather than
withdrawn, which is the boundary the definition draws and the hardest place to
draw it. Two went 2–1 to `reversal` and one to `none`; majority stands, and
`votes` records the dissent.

## Root causes, in the order they cost the most

1. **The prose filter admits machine-injected blocks.** `_prose` takes any
   non-empty `text` block, and the user role is not the user. 39% of the
   distinct user turns here were written by the CLI, and 55% of the blocks it
   hands the extractor, because notices repeat. This is structural: no wording
   change to the rules fixes it, and every downstream artifact — the index, the
   key ideas, the graph — reads the same blocks.
2. **`_PROHIBIT` treats bare negation as prohibition.** `may not`, `no content`,
   `No MCP`. It fires on machine boilerplate and on human sentences that are
   reporting rather than ruling.
3. **The assistant `reversal` branch fires on narration.** 52 of 61, and
   `_CONTRAST` is 31 of those on its own. The frame is two-sided by
   construction, which is why it is sufficient alone — but "pass a Path instead
   of a string" is two-sided and is not a course change.
4. **Positive standing rules are still missed**, confirmed for the third and
   fourth time (C, D, and both directives here). One corner of it is closed by
   fix 7 — the verbs that say *stays as it is* — which is where the set's first
   true positive comes from. The declarative rule with nothing lexical in it is
   still missed, twenty-odd times across the five probes.

## What was changed in response

Nothing, in the commit that recorded this. The measurement is against the code
exactly as it stood when the labels were written, because a number produced
after the fix is a number about the fix.

The set is pinned with `==` assertions in `bench/test_secondary.py`, not `>=`.
It is a record, and movement in either direction should stop a test run until
somebody writes down which way it went.

**This set is spent the moment anything is tuned against it.** Unlike a probe it
cannot be replaced by writing another one: there is one corpus of real
third-party sessions here, and after the first fix its score is a regression
floor and nothing more.

## Fix 1, and what it bought — the set is now spent

`derive._injected`: a block in a human's turn that a program put there is not
prose. Three whole-block shapes, structural rather than a list of tag names,
because the list belongs to the CLI and has changed before — markup that opens
and closes with a lowercase tag, a bracket notice, the local-command caveat —
plus the canonical JSON this adapter itself writes for a block kind it does not
recognise.

| | before | after |
|---|---|---|
| false positives | 30 | **12** |
| …of them machine-authored | 19 | **1** |
| true positives | 0 | 0 |
| misses | 2 | 2 |
| precision / recall | 0.0000 / 0.0000 | **0.0000 / 0.0000** |
| assistant `reversal` | 9 of 61 | 9 of 61 |  <!-- fix 3 takes this to 7 of 7 -->

**Precision did not move**, because it was 0/30 and is now 0/12: there is still
no true positive to divide by. That is the honest reading. What moved is that
five sixths of the machine-authored noise is gone from the decision graph, from
the key ideas, and from everything else downstream of `_prose` — and the
`<ide_opened_file>` sentence, which arrives on *every file opened*, no longer
writes a rule into anybody's memory.

The one machine false positive left is the `/init` expansion — *"Please analyze
this codebase and create a CLAUDE.md file…"* — which the CLI writes into the
user role as ordinary prose with no marker on it. Catching it needs the
preceding `<command-name>` block as context, which is a change to the adapter
rather than to a predicate over one block's text. `_injected` says so in its
docstring, with the subagent dispatch prompt beside it for the same reason.

Two things about the harness changed with it, and both were defects in the
measurement rather than in the product:

- **`bench.secondary._blocks` no longer goes through `derive._prose`.** It did,
  and this fix would have shrunk the labelled population from 140 to 89 and
  reported that as an improvement. The population is a property of the corpus.
- **`score()` reads `derive.decisions()`, not `_decision_kind()`.** The fix
  lives in *which blocks are read*, so a harness calling the rule directly on
  every item's text would have scored all 140 exactly as before and reported no
  change at all.

**The set is spent as a generalisation measure from here.** The fix is
structural and was not tuned against the miss list — it is a rule about who
wrote the text, taken from the annotators' machine/human column rather than
from the extractor's errors — but it was designed after reading this set, and
that is enough. What the numbers above are now is a floor.

## Fix 2 — a substitution is not a reversal

Root cause 3, the largest one: 52 of the 61 assistant-side `reversal` nodes were
narration, and the substitution frame alone was 31 of them.

The fix rests on a definition that had never been written down here. **A
reversal is the assistant putting down its own prior position** — not any
substitution in the artifact. In the *user* role a frame is enough, because
"use X instead of Y" governs later work however Y arrived. From the assistant it
is not, and the reason is specific to this corpus:

> Writing code is substitution all day long.

Passing a `Path` instead of a string, replacing one function with another,
swapping a loop for a library call — on a coding transcript the substitution
frame is the ordinary register of the work. Reading (B), *anything replaced*, is
recoverable from the diff. Reading (A), *a position abandoned*, is not, and that
is what a memory system is for.

So the assistant branch now needs the block to carry the withdrawal itself.
Three constructions count, and all three are **presupposition triggers** — the
presupposition is the whole point:

| trigger | what it presupposes |
|---|---|
| a concession — *"you're right"*, *"good catch"*, *"my mistake"* | a position that was contested, now granted |
| *"a different approach"* | a comparative, and a comparative presupposes a salient prior member |
| *"that's not going to work"* | the thing was being relied on to work |

None is sufficient alone; each only qualifies a frame. Two constructions still
stand on their own, because they lexicalise the withdrawal: the announced change
of course (`_PIVOT` — *"on second thought"*, *"changing my mind"*) and the
`_ABANDON` + `_ADOPT` pair, every member of `_ABANDON` presupposing that the
thing had been taken up.

**First-person ownership is deliberately not a trigger.** *"what I wrote"*,
*"my original code"*, *"here is a summary of what I did"* were tried and
dropped: they establish that a thing is the speaker's, which is the wrong
presupposition — the register of a *report* on prior work. Admitting the family
put two retrospective summaries back into the output to buy one extra true
reversal. Ownership is not withdrawal.

Two `_ABANDON` branches went at the same time, having fired eight times on real
sessions and never once on an abandonment. `no longer [\w-]+` is resultative —
it describes the state *after* a change (*"the TODO comment that's no longer
needed"*, *"since we're no longer using X"*), which is the retrospective
register this module already declines elsewhere. Bare `scratch` matched the
directory `scratch/` and the filename `main.py.oldscratch`; the verb takes an
object, so it needs one now.

| | before | after |
|---|---|---|
| assistant nodes still emitted | 61 | **9** |
| …adjudicated `reversal` | 9 | **7** |
| precision | 0.1475 | **0.7778** |
| *after fix 3 below* | | *7 emitted, 7 right* |
| user-side tp / fp / fn | 0 / 12 / 2 | 0 / 12 / 2 (unchanged) |
| probe A, class items | 26/32 | 23/32 |
| probe B, class items | 27/32 | 25/32 |
| probe C, assistant class items | 5/5 | 5/5 |

Precision here is `right / still_emitted` — 7/9 — so it rises when the extractor
*stops* emitting a wrong node, which is exactly what a precision fix does and
also exactly how a precision fix can be faked. A predicate that silenced all 61
blocks would read 1.0000. So `bench/test_secondary.py` pins all three numbers,
`(still_emitted, right, n) == (9, 7, 61)`: `n` is the adjudicated population and
cannot move, and pinning the other two means a withdrawal shows up as a
withdrawal rather than as an improvement.

### What it still gets wrong, and what it lost

*(Both false positives below are gone as of fix 3, which is the section after
this one. They are left here because fix 3 is what they caused.)*

Two false positives survive, and they are the same shape: a concession opens a
block that then *investigates* or *reports* rather than reverses.

> *"Oh no! You're absolutely right. When I modified the `generate_html`
> function… Let me check what happened to the session navigation."* — `fc2e108b`
>
> *"Perfect! You were absolutely right about the issue. The problem was that
> summary messages are generated asynchronously… ## What I Fixed"* — `b811f129`

The concession is real in both. What follows it is a diagnosis and a changelog.
Separating those needs more than one block's text, which is where this stops.

Two true positives were withdrawn with the 52:

> *"…This is much cleaner than duplicating all the message processing logic. Let
> me replace the complex `_generate_html_with_combined_link` function with a
> simple approach"* — `d439a8fe`, a 2-1 split with no explicit marker anywhere
> in the block.
>
> *"The `session_nav.html` component is… more comprehensive than what I wrote…
> Let me replace my duplicate code with the proper component usage"* —
> `e926d873`, caught only by the first-person family that was rejected above.

### The cost on the spent probes, priced

Five class items, all assistant `reversals`, and they split two ways:

- **Three are the trade.** *"We'll swap the regex validator for a real parser"*,
  *"Replacing the hand-written loop with `itertools.groupby`"*, *"Let me pull the
  caching out of the handler and put it behind the repository interface
  instead"*. These are a probe author writing a course change in the register of
  a diff, and they are word-for-word the shape three adjudicators called
  narration 31 times on real text. Recovering them means giving back the fix.
- **Two are collateral.** *"Rolling back to the synchronous client for now"* —
  `_ABANDON` fires, but the replacement arrives as a `to`-complement rather than
  the `_ADOPT` half the pair requires. *"The mmap approach isn't paying for
  itself — switching to a plain buffered read"* — a verdict `_RECANT` does not
  list. Both are genuine withdrawals whose evidence *is* in the block. They are
  the seed for the next round, not a reason to widen the frame.

The floors in `bench/test_probes.py` are re-pinned at 23 and 25 with that
reasoning written beside them. Lowering a floor should be hard; what makes it
legitimate here is that **A and B are spent** — their numbers are training data
and a regression floor, nothing more. The measurement that decides whether fix 2
was right is probe E: unwritten, blind, scored once. Until then the claim is
precision on real text against recall on fiction, stated as a trade.

### Recorded, not fixed

`_REPAIR` (*"ignore/disregard/scratch/strike that…"*) claims `scratch that`
before `_PIVOT`, which lists *"scratch that"* — the guards run before the rules,
so *"Scratch that, I'll use the cache"* is not a reversal. Found while writing
the negative test for the `scratch` branch, pinned there as the behaviour it
actually has, and left alone: this is a guard-ordering question rather than a
wording one, and it predates fix 2.

**Probe A's ceiling item was wrongly filed under the same heading**, here and in
the test, for three commits. *"Scratch the cron approach; a systemd timer is the
right tool here"* never reaches `_REPAIR` at all: that predicate's object list is
`that | this | my last | my previous | the last | the previous`, and *"the cron"*
is none of them. It fails a layer down — `_ABANDON` matches *"Scratch the"* and
neither `_ADOPT` nor `_COMMIT` fires, because *"is the right tool here"* names
the replacement with no adoption verb and no first-person commitment. The two
diagnoses imply different fixes: reordering the guards, which is what the
recorded one suggests, would not have moved this item by a single point. The
test now asserts which predicate is responsible instead of only asserting the
empty result, which held under either story. [E5 fix 2 review, F7]

## Fix 3 — a conjunction over a block is not a conjunction over an assertion

Found by a standalone review of fix 2, reproduced through `derive.decisions()`
before it was believed, and it is the only free correction in the round.

Fix 2 requires a substitution frame **and** a withdrawal marker. It required
them *somewhere in the same block*, and a block is a whole chat message. Both
survivors above are what that buys: a concession in the first line, then three
paragraphs of narration, then an `instead` that belongs to a description of the
bug or to a bullet in a changelog. Measured on the nine, the distance between
the two cues separates the classes cleanly — every true one is within 191
characters and the two false ones are 332 and 395 apart — but a threshold fitted
to nine points is not a finding. The unit is.

**The paragraph is the unit.** A concession licenses the substitution it is
*offered with*; at four hundred characters' distance it is licensing somebody
else's sentence. Scored on the nine that survived fix 2:

| scope | true kept | false kept |
|---|---|---|
| the block (fix 2) | 7 of 7 | 2 of 2 |
| **the paragraph** | **7 of 7** | **0 of 2** |
| the sentence | 1 of 7 | 0 of 2 |

The sentence is too small because a concession is its own sentence far more
often than not. Splitting on every newline instead of on a blank line scores
identically here, so the choice between those two is **not measured**; the
paragraph is the looser of them and a hard-wrapped line is a formatting artifact
rather than a boundary.

One thing had to be repaired for the scope to exist at all. `_without_opinion`
cuts attitude-bearing clauses out of a block and rejoins the rest with a space —
and `_CLAUSE` splits on the whitespace *after* a sentence end, which swallows
the blank line between two paragraphs. So the first version of this fix removed
one of the two false positives and not the other: the message it missed ends its
first paragraph in an opinion, and the cut reflowed three paragraphs into one
before the scope was applied. Cutting an attitude out of a message is not
licence to reflow it. It now cuts paragraph by paragraph and rejoins as
paragraphs.

| | before fix 3 | after |
|---|---|---|
| assistant nodes still emitted | 9 | **7** |
| …adjudicated `reversal` | 7 | **7** |
| user-side tp / fp / fn | 0 / 12 / 2 | 0 / 12 / 2 (unchanged) |
| gate, dev split | 1.0000 / 0.9319 | 1.0000 / 0.9319 (unchanged) |
| gate, held-out split | 1.0000 / 0.7428 | 1.0000 / 0.7428 (unchanged) |
| probes A / B / C / D | 23, 25, 14, 14 | 23, 25, 14, 14 (unchanged) |

**Seven of seven is not precision 1.0000 and must not be quoted as one.** The
denominator is the extractor's own output: it shrinks whenever the extractor
gets shyer, and a predicate that emitted nothing would read the same. That is
why `bench/test_secondary.py` pins `(still_emitted, right, n) == (7, 7, 61)` and
not the ratio. The claim that survives is narrow — of the seven assistant nodes
it still writes into the graph on these sessions, none is known to be wrong —
and it rests on seven items in four projects by one developer.

## Fix 4 — an object list that admitted an idiom and lost three objects

Not from this set. Fix 2's review read `_ABANDON` line by line and found that the
repair which gave the verb an object — added because bare `scratch` matched the
directory `scratch/` — took its object list from the one corpus it was written
against, and so got that corpus's objects.

| | before | after |
|---|---|---|
| *"this only scratches the surface — let's use a deeper scan"* | `reversal` | **nothing** |
| *"scratch those / them / these, I'll use a deque"* | nothing | **`reversal`** |
| *"scratch the cron job; I'll use a systemd timer"* | `reversal` | `reversal` |

The idiom is the costly half. *"Scratches the surface"* is a remark about how far
the work got, and `the` admitted it; joined to any first-person plan — which is
most of what an assistant says next — the stop/start pair fires and the block is
written into the graph as a change of course. `them`, `those` and `these` are the
quiet half: the same construction as `that`, and all three came back as nothing.

The exclusion is on the object, not on the inflections: *"he scratched the plan"*
is a real abandonment in the same tense, so dropping `-es/-ed/-ing` would have
cost more than it saved. It buys a ceiling, and the test asserts it rather than
leaving it to be discovered — `surface` is refused in the object position
whatever it heads, so a real abandonment of a thing called *"the surface probes"*
is declined along with the idiom. Telling those apart needs to know whether
`surface` is the head noun or a modifier, which is a parser and not a lookahead.

Free on every board that exists: dev 1.0000/0.9319, held-out 1.0000/0.7428,
probes 23/25/14/14, user side 0/12/2, assistant side 7 of 7 — all unchanged.
Two mutation rows, both CAUGHT. [E5 fix 2 review, F6]

## Fix 5 — eight alternatives nothing was holding, and a hole in the board

Also not from this set. Fix 2's review asked how much of `_RECANT` any test
actually holds, so the predicate was swept one alternative at a time — each
replaced by a token that cannot match, never deleted, because a deleted
alternative leaves an empty branch that matches everywhere and measures nothing.
**Nine of twelve survived the whole suite.** One of the nine was
`my (mistake|bad|error)`, which survived because it was dead; fix 3's companion
deleted it. That leaves eight.

The obvious response is eight tests. Worth one question first — are they
untested, or unused? Counted over every population this repository has:

| alternative | real | dev 42 | held-out 31337 | probes | held by a test |
|---|---|---|---|---|---|
| `you're right` | 14 | 0 | 0 | 0 | yes |
| `good catch` | 2 | 0 | 0 | 0 | — |
| `good point` | 2 | 0 | 0 | 0 | — |
| `good observation` | 3 | 0 | 0 | 0 | — |
| `i was wrong` | 0 | 0 | 0 | 0 († ) | — |
| `i apologi[sz]e` | 0 | 0 | 0 | 0 | — |
| `i'?m sorry` | 0 | 0 | 0 | 0 | — |
| `a different approach` | 4 | 0 | 0 | 0 | yes |
| `that's not going to work` | 0 | 0 | 0 | 1 | yes |
| `that won't work` | 0 | 0 | 0 | 0 | — |
| `that approach fails` | 0 | 0 | 0 | 0 | — |

"real" is 559 distinct assistant text blocks, sha256-deduped, `agent-*.jsonl`
and `subagents/` excluded. († ) `i was wrong` does occur in probe D, on a *user*
turn; `_RECANT` is read only in the assistant branch, so nothing reaches it.

**The synthetic corpus contains none of this vocabulary at all.** Not one
alternative fires on either split, including the three that tests hold. So every
"no change on the gate" line recorded for fix 2, fix 3 and fix 4 is true and
vacuous *for this branch* — the gate cannot see it. What has ever measured the
assistant substitution branch is the real-corpus adjudication (61 emitted, 7
kept) and probe B's single case. That is the whole of it, and it is the larger
of the two findings here.

Six alternatives fire nowhere. The tempting move is to delete the five of those
no test holds, the way `my (mistake|bad|error)` was deleted, and it is the wrong
move: that one was **dead** — a guard returned before it could be read — and
these are merely **unobserved**. A mutant of dead code survives because the code
cannot run; a mutant of unobserved code survives because nobody wrote the input.
Only the first is a fact about the program.

A cost was looked for and not found. The apology frames are the weakest members
— one can apologise for a delay without leaving any position, which fails the
class's own stated criterion — and they do turn *"I'm sorry, the build is slow
because of the cold cache rather than the linker"* into a reversal. But so does
*"You're right, …"* and so does *"Good catch, …"* on the same sentence: a
contrast between two **facts** read as a contrast between two **plans** is a
`_CONTRAST` limitation shared by every member of the class, including the one
that fires fourteen times. Recorded below, not fixed, and not evidence against
these five.

So all eight are pinned instead, one sentence each, taken from the class's
stated criterion rather than from the corpus that tuned the predicate. Eight
mutation rows; seven CAUGHT first time and one MISSED, which was the useful one:
the `good catch` body was *"Good catch. Dropping the retry wrapper and using the
built-in backoff instead"*, where `Dropping`/`using` is the stop/start pair and
reaches `reversal` without the alternative at all. A body that exercises a
branch and a body that depends on it are different things. Corrected, re-run,
CAUGHT. No behaviour changed, so every board is unchanged by construction.
[E5 fix 2 review, F4]

## Fix 6 — `don't` is how a person says something is broken

Root cause 2, which had sat open through five fixes: `_PROHIBIT` reads a bare
negation as a prohibition. Probe E put real instances on it, and the count is
what settles it. Of the 89 distinct user prose blocks here, the contracted
negated `do` occurs **21 times in 19 blocks**:

| | occurrences |
|---|---|
| a report — *"still doesn't work"*, *"they don't look that bad"*, *"the exported types don't accurately represent the request body"* | **18** |
| an imperative — *"Don't include generic development practices"*, *"(but don't push)"*, *"To be clear: don't use ClMail"* | **3** |

Not one of the eighteen forbids anything, and all three of the imperatives are
written as commands.

**They are still not standing rules, and this section said they were.** The
false-positive list near the top of this document has *"(but don't push)"* in
it, and all three annotators read all three of these as task-scoped and gold-
labelled the blocks `none`, unanimously. Both statements cannot hold; the
annotators are right, and the one this fix is entitled to is the weaker one —
the imperative separates a command from a report, not a policy from a one-off.
The three blocks emit `directive` for other reasons regardless, so the branch
costs nothing here and gains nothing here; it pays on the gate and the probes,
where a directive is written as one. [E5 fix7 review]
The uncontracted forms split the other way and stay untouched: `do not` and
`does not` are the register a rule gets written in, which is why three of the
four synthetic gate directive templates and probe E's plain directive
(*"we do not abend on bad input data"*) are all phrased that way.

So the contracted form is admitted in imperative position only — nothing in
front of the verb, at the head of a clause, allowing the one coordinator that
joins it to the last one. **`doesn't` gets no position at all.** It is
third-person singular present and English has no third-person imperative, so
there is no sentence in which it is a command; that is the call `_PROHIBIT`
already makes about bare `cannot`, where inability and prohibition share a word
and inability is the commoner one.

`never mind` went with it, one lookahead, for the same reason the determiner
`no` already excludes *"no rush"* and *"no worries"*: it is a conversational
formula for dropping a request, and it was scoring a standing rule on somebody
doing exactly that. The exclusion is the two-word formula, so a `never`
elsewhere in the same block is still a prohibition — suppressing the block
would be the `options` mistake, where a guard swallows a real rule beside it.

| | before | after |
|---|---|---|
| user-side false positives | 12 | **6** |
| …of them machine-authored | 1 | 1 |
| user-side tp / fn | 0 / 2 | 0 / 2 (unchanged) |
| `reversal-by-user` blocks labelled something | 2 of 3 | **0 of 3** |
| assistant `reversal` | 7 of 7 | 7 of 7 (unchanged) |
| gate, dev / held-out | 1.0000 / 0.9319, 1.0000 / 0.7428 | unchanged |
| probes A / B / D / E | 23, 25, 14, 20 | unchanged |
| **probe C** | 14 | **13** |

Precision still reads 0.0000 either way — there is no true positive to divide by
— so the honest statement is the count: half the false positives on the only
real user text in this repository, and the declared ceiling priced above at
*"two of the three come back as `directive`"* is now none of the three.

**And neither of those two was labelled for a reason to do with revocation.**
Worth writing down, because the ceiling was read as evidence about the
revocation frame and it was not. One is *"Never mind!"*, scored by `never`. The
other is *"I changed my mind a bit about it and would like to combine the
logic"*, followed by a pasted diff — and what scored it was on a **deleted line
inside that diff**, a code comment reading `# (shortest path that doesn't have
another working directory as its parent)`. A predicate about English commands
fired on a parenthetical in a Python comment in somebody else's file, three
screens below the sentence anybody read. That is the class root cause 2 names,
in its purest form, and no probe contains anything like it.

**Probe C lost an item, and it was right by accident.** *"Whatever you end up
doing about the moving-head timeout — and I know it's messy, the fixtures don't
even agree on what a timeout means — the house lights still have to be up within
two seconds of the panic button."* The directive there is *the house lights have
to be up*: a positive standing rule, which is root cause 4 and which nothing in
`derive.py` claims. What was scoring it was `don't` inside a parenthetical, in an
aside, about somebody else's software. Close root cause 4 and it comes back to 14
on the rule that should always have held it; the floor in `bench/test_probes.py`
is re-pinned at 13 with that written beside it, and re-pinning it up is for then.

*Then was the next fix.* `_PERSIST` holds this item on the positive rule, as
predicted, and C is re-pinned at 16 — see fix 7.

Four mutation rows, all CAUGHT by their intended test.

### What is left of root cause 2

Six user blocks still carry a `_PROHIBIT` hit that `_DELIBERATION` is masking,
and it matters *which* alternative of the guard is doing the masking, because
only one of them is the one under discussion:

| alternative | masked by | the text |
|---|---|---|
| `disallowed` ×2 | `Options` | *"was blocked because of a **disallowed** MIME type"* — a browser console error, the participle attributive inside a noun phrase |
| `no arguments` | `Options` | *"With **no arguments** / We will display the pull request of the branch you're currently on"* — a pasted `gh pr view` man page |
| `DO NOT` | `options`, `we could`, `we might` | the caveat banner, quoted inside a user turn that is itself deliberating |
| `avoid` | `options`, `vs` | *"the `--isolated` flag, which will **avoid** refreshing the cache"* — a report about what a flag does |
| `may not` | `which of` | *"This may or **may not** be related to the current task"* — the IDE notice, in the idiom that means uncertainty |

The first three rows — two alternatives, three blocks — are what blocks the
`options` narrowing, and measured on top of this fix that is exactly the
narrowing's cost: the board goes from 6 false positives to 9, and the three are
these. The bottom three keep a second `_DELIBERATION` hit each and would stay
masked, which is luck rather than design: `which of` and `vs` are no better a
reason to suppress a block than `options` was.

Each needs its own lookbehind, each fitted to one block, which is the shape this
class is built against — so they are recorded here rather than guessed at, and
the order of work is unchanged.

## Fix 7 — a rule can say that a thing stays as it is

Root cause 4, the largest one on the list and the one three probes report
independently: nothing in `derive.py` claims a **positive standing rule**. Every
user-side rule the module can see names something rejected — a substitution
frame (`X instead of Y`) or a prohibition (`never`, `do not`) — so a rule that
says what a thing *is* has no predicate at all. Collected across the five
probes, that is **24 missed user directives**, and both gold directives on this
page.

Most of it is out of reach of a regex. *"Every task card carries the AMM
reference"*, *"Dispatch checks run the MEL first"* — a subject noun phrase and a
simple-present verb, which is also the shape of *"the exported types don't
accurately represent the request body"*, a bug report from the corpus above. One
of those is a rule and one is a complaint and telling them apart wants a parse.

The **persistence verbs** are the corner of the class that carries the meaning in
the verb rather than in the syntax: `stays`, `remains`, `keeps`. Asserting that
something goes on being what it is *is* constraining future work on it, which
makes `_PERSIST` the positive dual of `_PROHIBIT` — one names what may not be
done, the other what may not be changed.

Three guards, and they are most of the rule:

| not admitted | why | where it comes from |
|---|---|---|
| *"**I keep** getting mysterious build errors"* | a subject in front makes the verb aspectual — it reports repetition, which is what you write when something is broken. The imperative, which is how half of these rules are written, has no subject. The guard is narrower than that sentence: six pronouns immediately in front and nothing else, so *"The build keeps failing"* and *"I still keep getting build errors"* are both missed | the real corpus |
| `keep-alive` | a header value; arrives five to a block in pasted HAR files | the real corpus |
| *"good to **keep as** milestone information"* | with no object between verb and `as`, the frame appraises the thing instead of constraining it. *"keep it as YAML"* has the object and still counts | the real corpus |

The first is the same discriminator fix 6 used on the contracted negation, and
for the same reason: **position, not vocabulary**. On the 89 distinct human prose
blocks here the verbs occur 10 times across 5 blocks — one gold directive, four
gold none — which is what set the guards.

| | before | after |
|---|---|---|
| user-side tp / fp / fn | 0 / 6 / 2 | **1 / 7 / 1** |
| …of the fp machine-authored | 1 | 1 |
| `reversal-by-user` blocks labelled something | 0 of 3 | 0 of 3 (unchanged) |
| assistant `reversal` | 7 of 7 of 61 | unchanged |
| gate, dev / held-out | 1.0000 / 0.9319, 1.0000 / 0.7428 | unchanged |
| probes A / B | 23 (aside 3/5), 25 (1/3) | unchanged |
| **probe C** | 13 | **16** |
| **probe D** | 14 | **16** |
| **probe E** | 20 | **21** |

**This is the first true positive the real-text set has ever produced.** Six
rounds of fixes moved false positives from 32 to 6 and never once moved the
numerator: *"I'd like to keep it the same 3 simple files"* is the first sentence
in 140 that the extractor and three annotators agree is a rule.

### Two defects it exposed on its way in, both independent of it

Neither is `_PERSIST`. Both were open holes that nothing could reach, because
until now no rule in this module read the word `keep`.

**1. `keep X in mind` is separable and `_BACKREF` only had the joined form.** The
restatement guard exists to stop a repeated instruction being recorded twice, and
it listed `keep in mind` and `bear in mind` as fixed strings. A reminder that
names what it is about — *"Please keep this instruction in mind."* — puts the
object in the middle and walked straight past a guard that claims exactly it.
Found by the held-out gate: the first `_PERSIST` dropped test precision to
**0.8595**, and every loss traced to one synthetic distractor header of that
shape. The repair takes no lookahead, unlike its neighbours — bare `remember` and
`recall` are ordinary words and need one; `in mind` is its own disambiguation.

**2. `_DELIBERATION` had `on the one hand … on the other` and not `a case for X
and a case for Y`.** The same frame in other words. Probe A files it under
`guard: deliberation`; it cost nothing while nothing read `keeping`, which is how
a hole stays open for six rounds.

### Three costs, and none of them is rounding

**The new false positive is a lexical twin of the new true positive.**

> tp — *"I'd like to **keep it the same** 3 simple files"*
> fp — *"**Keep it same** overall length as the pros or cons"*

Same verb, same frame, same register, adjacent turns of the same kind of work.
Three annotators split them; no rule in this module does, and no rule short of
knowing what a *file layout* is and what a *draft length* is would. The gain and
the loss here are one coin, and 1/7/1 should be read as such.

**Three user-reversal items move from a silent miss to `directive`** — one on
probe C, two on probe D. Probe C's author called this out when the probe was
written: recording the surviving rule and dropping the retraction is *worse than
a miss*, because the graph then asserts something the user took back. In fairness
to the extractor, in these three the actionable content genuinely is the `keep`
clause — *"…I changed my mind, keep the logic in one place"* — but the reader of
the graph is not told that anything was withdrawn. The declared ceiling is
unchanged and this is the cost of it, moved somewhere the census on this page
cannot see: the three real revocations in this corpus carry no persistence verb,
so the pin there stays 0 of 3.

**One of probe C's three gains is right for the wrong reason.**

> *"I'd rather eat the slower timecode sync than **keep** the one that drifts, so
> take the slow one."*

`_PERSIST` fires on the `keep` in the **rejected** alternative. The item is gold
`directive` and comes back `directive`, and the reason is a word inside the half
of the sentence the speaker is throwing away. This is the third time this
repository has had to write that sentence — probe C's buried-clause item in fix
6, the `pros or cons` guard idea declined below — and it is the reason a probe
score is a floor and not a claim.

### Declined: `pros or cons` in `_DELIBERATION`

It would take the false positives 7 back to 6 by suppressing the twin above. The
phrase in that block is a noun phrase naming a section of a prior document, not
the weighing idiom — so the guard would be right by accident, on an unrelated
span, which is precisely the `options` failure this page has spent two fixes
documenting. A green board bought that way is the worst kind. Not done.

Six mutation rows, all CAUGHT by their intended test. Root cause 4 stays open:
this is one corner of it, and the declarative standing rule — the other twenty or
so misses — still has no predicate.

## What it does not measure

- **Recall rests on two items.** With two gold directives, recall is 0, 0.5 or
  1. The precision figure is the solid one: 138 negatives, and 32 false
  positives when this was written — 7 after the fixes below, and all 7 of the
  nodes the extractor still emits here. **And one true positive, as of fix 7**,
  which is what makes 0.1250 a precision figure rather than a zero.
- **The assistant side has no recall number.** Only what the extractor emitted
  was adjudicated, so a reversal it never flagged is invisible here.
- **The gate is blind to the recant class.** Neither synthetic split contains a
  single one of `_RECANT`'s eleven alternatives, so a gate score is not evidence
  about fixes 2 to 5 in either direction. See fix 5 for the count.
- **A contrast between two facts reads as a contrast between two plans.** *"You're
  right, the build is slow because of the cold cache rather than the linker"* is
  a corrected claim with no decision in it, and it is emitted as a reversal. The
  frame is doing its job — the speaker really is withdrawing something — and
  `_CONTRAST` cannot tell a `rather than` between two causes from one between two
  courses of action. Affects every member of the class, including the ones with
  the most evidence behind them. Recorded, not fixed.
- **Four projects, one developer, one working style.** Real, but not a sample of
  the population.
- **Labels are agent-applied.** Three independent annotators at 139/140
  agreement is a better instrument than one, and it is still not a human expert.
  The agreement rate is published so the reader can price that themselves.
