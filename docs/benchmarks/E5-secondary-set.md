# E5 secondary set — the extractor on real text

**Precision 0.0000. Recall 0.0000.**

Not a typo and not a harness fault. On 140 real human turns taken from another
developer's published Claude Code sessions, the decision extractor produced 32
`directive` nodes and **not one of them was a directive**, while missing both of
the two that were there.

The same extractor reads **1.0000 / 1.0000** on its held-out synthetic split.

Its larger output on these sessions is the assistant-side `reversal` — 61
distinct blocks — and **9 of those 61** survived adjudication. Precision 0.15.

| Measurement | Text | Score |
|---|---|---|
| Gate, held-out split | synthetic, generated | precision 1.0000, recall 1.0000 |
| Probe A / B | fiction, spent | 26/32, 27/32 — **23/32, 25/32 after fix 2** |
| Probe C / D | fiction, blind, unspent | 14/32, 14/32 |
| **This set** | **real, third-party, census** | **precision 0.0000, recall 0.0000** |

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

**No transcript text is committed.** `docs/benchmarks/E5-secondary-manifest.json`
carries a sha256 per item and the label; `bench/secondary.py` reads the text back
out of the clone and refuses to score if a digest, a filename or the item count
has moved. Same arrangement as LongMemEval — the corpus is fetched, never
vendored — and here it also settles the question of republishing a stranger's
sessions.

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
`derive.decisions()` — over the same sessions emits **131 nodes from 1049 prose
blocks**:

| | nodes |
|---|---|
| `directive`, user role, machine-injected text | 36 |
| `directive`, user role, human text | 32 |
| `reversal`, assistant role | 63 |

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
   non-empty `text` block, and the user role is not the user. 39% of what it
   hands the extractor was written by the CLI. This is structural: no wording
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
   fourth time (C, D, and both directives here).

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
| assistant `reversal` | 9 of 61 | 9 of 61 |

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
  and this fix would have shrunk the labelled population from 140 to 85 and
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

`_REPAIR` (*"ignore/disregard/scratch/strike …"*) claims `scratch ` before
`_PIVOT`, which lists *"scratch that"* — the guards run before the rules, so
*"Scratch that, I'll use the cache"* is not a reversal and neither is probe A's
ceiling item *"Scratch the cron approach; a systemd timer is the right tool
here"*. Found while writing the negative test for the `scratch` branch, pinned
there as the behaviour it actually has, and left alone: this is a guard-ordering
question rather than a wording one, and it predates fix 2.

## What it does not measure

- **Recall rests on two items.** With two gold directives, recall is 0, 0.5 or
  1. The precision figure is the solid one: 138 negatives and 32 false
  positives.
- **The assistant side has no recall number.** Only what the extractor emitted
  was adjudicated, so a reversal it never flagged is invisible here.
- **Four projects, one developer, one working style.** Real, but not a sample of
  the population.
- **Labels are agent-applied.** Three independent annotators at 139/140
  agreement is a better instrument than one, and it is still not a human expert.
  The agreement rate is published so the reader can price that themselves.
