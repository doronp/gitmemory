# E8 review round — the comparison page

Two independent reviews of `d574462`, run after the commit landed: a standalone
code review with no prior context, and the standing Gemini 3.1 cross-review in
a detached worktree. They agreed on the two findings that mattered and
disagreed on the reason for one of them, which is the useful part.

Both are recorded because the headline finding is that the page **reinstated a
claim this repository had already retracted once**, and a review round that
only lists what got fixed loses that.

## The two blockers

### 1. The oracle is not a ceiling, and the page said it was

The committed page said "**0.9319 is the most any retriever can score here**"
and that Total Recall's 0.9773 therefore sat "above our apparatus ceiling" —
"a score we cannot reach with perfect retrieval is not a score we lost to".

That is false, and `docs/benchmarks/E3-longmemeval.md` §1 already said so in
plain text one commit series earlier: "nothing structurally forbids it: the
oracle retrieves by turn offset and a text retriever can return a session the
offsets cannot reach." `3b8aef1` exists solely to retract the same claim at the
`session_recall` level. The page reintroduced it one metric over, at `S-All`,
and promoted it to the README.

**Proof in our own table, not in argument.** The `live_context` arm scores turn
recall 0.0000 and S-Hit@10 0.8894: it retrieves no evidence turn at all and
still reaches 88.9% of answer sessions, through non-evidence turns the oracle
by construction never returns. Sessions are reachable off-evidence. Nothing
caps a retriever at the oracle's number.

Withdrawn, and the withdrawal is on the page rather than a silent deletion.

### 2. The 0.9319 is a dataset property, not a budget effect

The page attributed the oracle's shortfall to the ten-*turn* budget and used it
to price the turn-vs-session mismatch. Both reviewers rejected the conclusion;
the standalone reviewer got the reason right and it is checkable:

- The `apparatus` calibration gate requires `reference turn_recall == 1.0`
  **exactly**, and it passes on all four modes.
- The oracle retrieves `sorted(evidence)[:k]`, so its turn recall is
  `min(k, |evidence|) / |evidence|`.
- A mean of values each ≤ 1.0 equals 1.0 only if every value is 1.0. So
  `|evidence| ≤ 10` for all 470 instances, and the ten-turn budget **never
  truncates the oracle**.
- `438/470 = 0.9319`, and 32 is exactly the count of instances listing an
  answer session with no `has_answer` turn (45 of 890 answer sessions), from
  E3 §1.

So the oracle loses 6.8 points to the dataset and 0.0 to the budget. The budget
difference is real and is now stated as **unmeasured** rather than quantified.

**Gemini reached the same verdict by a different route, and its route is
wrong.** It argued the ceiling is "self-inflicted" because `sorted(evidence)[:k]`
truncates chronologically and a session-aware oracle would spread the budget
across sessions. That mechanism cannot fire here: with `|evidence| ≤ 10` the
slice returns every evidence turn, so there is nothing to spread. Recorded
because a right conclusion from a wrong mechanism is the kind of agreement that
looks like corroboration and is not.

## Confirmed and fixed

| # | Finding | Fix |
|---|---|---|
| 3 | The new test could not tell the two metrics apart. Every instance from `make_instance` has **one** answer session, and `found_sessions ⊆ answer_sessions` always, so `>=` collapses to `bool()` and both metrics were the same number on every path the test walked. On the corpus they differ by 13.6 points. | A two-answer-session case asserting the partial `(1.0, 0.0)`, plus the fractional `session_recall == 0.5` on the same retrieval. Four mutants pin it. |
| 4 | README called the session metrics `Hit@10` and `all-evidence@10` — names this repository already uses for the turn metrics, which read 0.8362 and 0.6617. Gemini rated this a BLOCKER independently. Precisely the confusion the commit existed to prevent. | `S-Hit@10` / `S-All@10`, with a sentence saying the turn-level columns are different quantities. |
| 5 | README named agentmemory's BM25-only run as "the one comparable baseline" and reported us ahead of it, omitting that the same source's hybrid run scores 0.9860 and beats all three of our arms. The full page disclosed it; the README did not. | README now states the split both ways. |
| 6 | `_metric_cells` and the column headers were two hand-maintained parallel lists reachable from no test. Transposing one adjacent pair relabels every number this project prints, suite green. | Headers extracted to `_metric_columns`, held against the cells index by index, one mutant. |
| 7 | `[E6]` was already the Dashboard epoch (`docs/reviews/E6-standalone-review.md`). | Renamed to E8 throughout. |
| 8 | `session_mrr` had no assertion anywhere; setting it to a constant survived the suite. Fractional `session_recall` was asserted only on the empty case. | Both now asserted, `session_mrr` with a mutant. |
| 9 | The page opened "Everything below is sourced" while Table 3 carried no source column. | Claim narrowed to Tables 1 and 2; Table 3's second-hand figures marked as such. |
| 10 | Stella V5 sat in Table 1's S-Hit column while annotated "M split, not S". | Italicised and marked not comparable. |
| 11 | Session metrics score a hit for *any* turn in the right session, so `live_context` reads 0.8894 S-Hit while holding none of the evidence. | Stated on the page, next to the argument that uses the same mechanism in the other direction. |

## Found while fixing

The README paragraph explaining why the two board counts do not sum read 1300,
1303 and 975 against a board already saying 984. Those three figures were the
only numbers on the page pinned by nothing, which is why they were the ones
that drifted — the same finding the two count tests were each written for,
reached a third time through the door neither covered.
`test_the_readme_arithmetic_paragraph_is_the_arithmetic` closes it. The word
"three" in that paragraph was right all along; everything around it was stale.

## Declined

- **"BM25 is deprecated too harshly"** (Gemini, OPINION). It is the weakest of
  the three arms we measured and that is a measurement. The resource-efficiency
  case for it is real and unmeasured; an unmeasured advantage does not soften a
  measured deficit on a page whose subject is measured comparison.
- **"Our run" table omits `Unmatched/query`** (Gemini, NIT). Deliberate
  subset — that column is a harness diagnostic, not a comparison axis.

## Open

- `answer_session_ids: []` is accepted by the loader and now contributes 0.0 to
  three means with no error and no gate, where the twin case on `evidence`
  raises. The asymmetry is unclosed.
- The corpus-shape figures (47.7 sessions and 493 turns per instance, the
  synth-transform percentages) are stated with no command that reproduces them.
- The page is not in the README epoch table, only in the prose section.
