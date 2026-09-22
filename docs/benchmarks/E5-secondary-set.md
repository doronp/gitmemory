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
| Probe A / B | fiction, spent | 26/32, 27/32 |
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
