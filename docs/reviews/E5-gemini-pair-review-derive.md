# E5 round 4 — Gemini pair review of the decision extractor

Standalone pair round on `src/gitmemory/derive.py`, `bench/gate.py`,
`bench/fixture.py`, `bench/probes.py`, `bench/test_probes.py`. The reviewer ran
in its own git worktree with `--yolo` confined to it, read nothing outside the
repository, and opened its report with that attestation — in particular nothing
under `~/.claude/` or `~/memory/`, which is the standing rule for every agent
on this project.

Report: `.claude/worktrees/gemini-derive/REVIEW-derive.md` (267 lines, five
findings plus two argued sections). Nothing in it was taken on its say-so.
**Every one of its seven example sentences reproduces**, and two of its
*explanations* do not survive the reproduction. That gap is the useful part of
the round and is written out below.

Funnel: **5 findings + 2 argued sections → 7 reproduced → 4 taken, 1 declined,
1 deferred, 1 taken-by-a-different-mechanism.** One further defect, larger than
any of them, was found while reproducing F3 and is the headline fix.

## The number that did not move, again

Dev gate before and after all five fixes: **precision 1.0000, recall 1.0000,
367/367/367, PASS**, per-group 180/101/86 all at 1.0000. The gate has now been
at 1.0000 through five rounds, each of which found real defects. It cannot see
them. The probes are the only score in this epoch that has ever moved, and both
of theirs held exactly at their pinned floors: **probe A class 26/32, probe B
class 27/32.**

Held, not improved — which is the point of the floor and also its limit. Both
probes have now informed a revision, so both are training data; their scores are
a floor, not evidence of generalisation. The reviewer argues the same thing from
the other side in its §4 and proposes a blind probe C. Agreed and owed — see
"Still owed" at the bottom.

## Taken

| # | Sev | Reproduced as | Mechanism of the fix | Test |
|---|---|---|---|---|
| — | high | `"We never had that problem."` → `directive` | `never` read against the tense of the clause it opens, with a present-tense copula as the one exception | `test_a_report_of_what_never_happened_is_not_a_rule` |
| F1 | high | `"For each client, same request, and never reuse the connection."` → `None` | `request` out of the `same <noun>` back-reference idioms — it is not elliptical for "as before" | `test_the_same_request_is_not_the_same_rule` |
| F4 | medium | `"In the first implementation of the module, never use unsafe code."` → `None` | the early-stage frame split out to `_RETRO_STAGE` and paired with a finite past verb | `test_an_early_stage_dates_a_report_and_scopes_a_rule` |
| F5a | high | `"Should we avoid raw SQL in the handlers?"` → `directive` | interrogative *shape* — wh-word or inverted auxiliary in front, mark at the back | `test_a_question_about_a_rule_is_not_the_rule` |
| F5b | high | `"If we never release the lock, the database hangs."` → `directive` | the protasis is cut out and the rules run on the apodosis | `test_a_condition_is_not_the_rule_it_carries` |

Ten mutation rows, **10/10 CAUGHT by their intended test**. Each fix has at
least one row per direction wherever the fix is a two-sided judgement: the
conjunct in F4 is pinned by both `and _PAST_FINITE` → gone and the whole test →
`False`, because a conjunct held from one side only is half held.

### The headline, which the reviewer saw only the shadow of

§3 of the report argues that declining bare `earlier` is wrong, on two
sentences: `"We implemented that earlier and never had any issues."` and
`"We avoided that earlier and never saw any failure."` Both reproduce.

But neither is about `earlier`. Strip it out and `"We never had that problem."`
— four words — is still scored `directive`. **Past-tense `never` is a report,
not a prohibition**, and the extractor had no tense cue at all. That is a
mechanism rather than a word, it subsumes both of the reviewer's sentences, and
it is why `earlier` stays declined: the word was never the defect.

Two things the fix had to get right, both of which cost a first attempt:

- `\w+ed` cannot stand for past tense. English spells `need, proceed, exceed,
  embed, feed, seed, speed, succeed, breed, heed, bleed, cede, indeed` with a
  final `-ed`, and `"never exceed 100 rows"` and `"never embed credentials"` are
  rules. They are excluded by name.
- A present-tense copula in front makes what follows a passive **rule**: `"raw
  SQL is never allowed here"`. The first version wrote this as a negative
  lookbehind, which is the opposite of the intent and silently dropped the
  passive rule; the hand table caught it, the gate did not. `was`/`were` are
  deliberately not in the exception — `"raw SQL was never allowed"` is the
  report.

`avoided` went with the same finding: `"we avoided threads"` is the same report
in a different verb. The cost is the passive `"raw SQL is avoided here"`, which
is a weak rule and rare beside the report.

### F4, and a fix that was refused

The obvious repair — delete `the first` from
`in (older|earlier|…|the first|the early) <artefact>` — was tried and **refused
on evidence**: it makes `"In the first version we had no CI at all."` a
directive, because `no CI` is a prohibition shape. It trades one false positive
for another.

The frame genuinely points both ways, so it cannot decide alone. What decides is
the tense of the clause, which is the same mechanism as the `never` fix and a
generalisation of the determiner rule already in `_RETROSPECTIVE`
(`the (old|previous|original|former|first) <noun> (was|were|had|did|used)`).
`_PAST_FINITE` deliberately drops `_PAST`'s `\w+ed` branch: across a whole block
that branch matches participial adjectives, and `"never use deprecated APIs"` is
a rule with no past clause in it.

Ceiling, written at the site: `initially`, `at first` and `early on` have the
same two readings and stay unconditional, because the tense test costs a
suppression on every verbless fragment (`"back then, no CI"`) and they are
commoner in that shape.

### F5, as two fixes

The reviewer grouped questions and conditionals together. They are different
defects with opposite repairs:

- **Questions** are suppressed, but on interrogative shape rather than on the
  mark, because a directive can carry a mark: `"Use Parquet instead of CSV,
  ok?"` is a tag question and `"Do not use pip here — clear?"` is an imperative.
  `do not` is excluded from the openers for exactly that sentence.
- **Conditionals** are *not* suppressed. Blanket `if`-suppression is wrong:
  `"If the build fails, never retry more than twice."` is a directive and the
  reviewer's own §5 diff would have lost it. The protasis is cut out instead and
  the rules run on what remains, which works wherever the rule sits — after the
  clause, before it, or wrapped around it. All three are controls in the test.

## Declined

**F3 — `"We chose to go over the design."` and `"We recommend running the tests
over the network."` → `directive`.** Both reproduce. Declined, and this is a
re-decline: the spatial/scopal reading of `over` is a documented ceiling on
`_PREFER` from an earlier round.

Two reasons beyond that. The report contradicts itself — its own §3 confirms the
decline of spatial `over` while F3 asks for it to be reversed. And both proposed
repairs are blocklists of particular verbs and nouns, which is the failure mode
this extractor is built against: a list that admits the two sentences someone
wrote down and nothing else. The upgrade is a complement test on `over`, which
needs a parse.

What the round *did* change is the evidence at the site: both sentences are now
named in the ceiling comment, so the next reader argues with examples rather
than with the abstraction.

## Deferred

**F2 — `"We still don't want to use pickle, so never import it."` → `None`,
attributed to `still`.** The sentence reproduces; **the attribution is wrong.**
Remove `still` and it still returns `None`. The suppressor is
`don't want`, an `_OPINION` match, and `still` does nothing.

Deferred rather than taken because the fix the report implies — narrowing
`_OPINION` so a want-clause does not suppress a following imperative — flips
`"I don't think we need a rule that we never commit generated files."` into a
directive, which is the sentence `_OPINION` exists for. Doing it properly means
scoping the suppressor to its own clause, which is the same clause-boundary work
F5b started and is the natural next round.

## Rejected, on the reviewer's own diffs

§5 offers five diffs. Not applied, and worth recording why, because a pair review
that silently drops half a report is not a pair review:

- **Diff 3 does not compile** — `[[^.;:!?]]` is a character class containing
  `[`, `^`, `.`, `;`, `:`, `!`, `?`, `]`, not a negated one.
- **Diff 2 hardcodes what `_ADV` already derives**, which is the duplication the
  `_NOT` class was written to remove.
- **Diff 1 over-narrows `per <noun>`** to a list of nouns, losing `per the ADR`
  and every other citation by reference.

The findings behind diffs 1 and 2 were taken anyway; the diffs were not.

## Still owed

1. **A third, unspent probe.** Both current probes have informed revisions and
   are now training data. The reviewer's §4 makes this argument independently
   and it is correct: a `>=` floor on a spent probe pressures overfitting. Its
   proposal — promote the spent probes to `==` unit tests and keep one blind —
   is the right shape.
2. **The hand-labelled secondary set** named in `docs/DESIGN.md`. Still the
   binding measurement gap: every number in this epoch is either a synthetic
   fixture or a probe written by a reviewer.
3. **Clause-scoped suppressors** (F2, above). `_PROTASIS` is the first cut at
   clause boundaries; the suppressors still scan the whole block.
