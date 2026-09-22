"""Probe E — the assistant-side boundary: genuine reversal vs. ordinary substitution narration.

Written blind. See section 4 below for the attestation.

------------------------------------------------------------------------------
1. DOMAIN
------------------------------------------------------------------------------

Chosen domain: **COBOL batch maintenance on an IBM mainframe shop.**

Everything here is phrased in the vocabulary of nightly batch: JCL steps and DD
statements, copybooks, record layouts, packed-decimal and display fields, sign
nibbles, 88-level condition names, OCCURS / OCCURS DEPENDING ON tables,
SEARCH ALL, PERFORM VARYING, sort control cards (OUTREC, SUM FIELDS), generation
data groups and DISP=MOD, tape mounts, abends, return codes and COND, checkpoint
restart, the linkage editor and static vs. dynamic CALL, region size on the JOB
card, run books, reject files, and operations' print output.

Why this cannot overlap a general web/Python/CLI corpus: the tuned corpus words
named as off-limits — parser, tokenizer, bucket, credential, worker, cache,
endpoint, migration, Docker, React, pytest — have no counterpart in this
register, and I have deliberately avoided their near synonyms too. Batch COBOL
does not have HTTP surfaces, containers, package managers, test runners, or
request handlers; it has job streams, datasets and abend codes. The nouns a
lexical model could have memorised from the tuning corpus simply do not appear,
so a classifier that is really keying on "endpoint"/"cache"/"migration" gets no
purchase here and has to work off sentence structure and stance instead. I did a
manual pass over every string below looking specifically for those eleven words
and their relatives (parse, token, caching, container, DB migration, API, test
suite) and found none. Generic English nouns that happen to be shared with
software in general — file, table, record, field, step, module, library, program
— are used in their mainframe senses and are not on the exclusion list.

------------------------------------------------------------------------------
2. LABELLING CONVENTIONS I COMMITTED TO
------------------------------------------------------------------------------

The whole probe turns on one question, applied to every assistant block:

    **Was there a position, held by the assistant, that this block puts down?**

Both halves have to be true. "Position" means a choice the assistant made and
stood behind — a design, an approach, a diagnosis it acted on, a plan it
announced as the way forward. "Puts down" means the block itself makes clear
that the choice is being abandoned, not merely advanced or refined.

Operationally I applied four tests, in order, to every borderline item:

  (a) **Ownership test.** Strip the sentence to "X is being replaced by Y". Ask:
      whose X was it? If X was a placeholder the assistant always intended to
      throw away, a pre-existing defect, code written by someone else long ago,
      or something the *user* asked for, then nothing of the assistant's is being
      withdrawn and the item is None — no matter how many substitution words are
      in the sentence. Nine of my twelve None items are built on exactly this:
      the substitution frame is present and load-bearing, and the ownership test
      still comes back negative.

  (b) **Direction test.** Does the block move *forward along* a plan, or *back
      out of* one? "Next I'll swap the hard-coded date for a PARM value" is the
      next step of an intact plan. "The intermediate work file I added is doing
      nothing but costing us a mount" is a retreat. Forward motion is narration
      even when it substitutes; retreat is reversal even when it substitutes
      nothing explicit.

  (c) **Self-sufficiency test.** The extractor sees one block with no history, so
      the block must carry the evidence of the abandoned position inside itself.
      Every item I labelled reversal names the thing being given up *and* marks
      it as the assistant's, usually with a first-person possessive ("the
      two-pass design I laid out", "the reject path I built", "the sequential
      read I argued for"). Where I could not put that marker in without making
      the sentence read like a fixture, I dropped the item rather than shipping
      an under-determined one.

  (d) **Marker independence.** I refused to let "I was wrong" / "you're right" /
      "on second thought" do the work. Exactly three reversals carry such a
      marker (REV-07, REV-08, REV-12, the cap allowed). The other nine signal
      retreat through stance only: resignation after repeated attempts, a
      constraint discovered that invalidates the earlier choice, a cost/benefit
      that has flipped, or a flat declaration of a new direction that is
      unintelligible except as a withdrawal from the old one. Conversely, none of
      the twelve None items were made None by the *absence* of a marker; each one
      fails the ownership or direction test on its merits.

Two further conventions:

  - **Only the user can issue a directive.** An assistant announcing a rule for
    its own future conduct ("static calls from here on") is not a directive; at
    most it is a reversal with a forward-looking tail. HARD-04 tests this.
  - **A directive must outlive the task.** A user saying "do it in the sort
    instead" is retargeting one piece of work, not legislating. It is None even
    though it changes direction, and even though it is the user speaking.
    HARD-01 and HARD-07 sit on either side of that line.

Register note: I wrote these to read like transcript turns — trailing thoughts,
mid-sentence self-interruption, an occasional aside about the shop — rather than
like minimal test strings, because an extractor that only survives on clean
fixtures is not the thing being measured.

------------------------------------------------------------------------------
3. ITEMS I EXPECT TO BE DISPUTED
------------------------------------------------------------------------------

These are the ones I argued with myself about and would defend rather than
concede. Named individually, with the case against.

**REV-09 ("Scratch the SEARCH ALL.")** — Against: "Scratch the X" is arguably a
self-correction marker in disguise, which would make it a fourth marked reversal
and stretch the cap. For: the cap was written around explicit *fault-admitting*
markers — "I was wrong", "you're right", "on second thought" — all of which
attribute error to the speaker. "Scratch" attributes nothing; it is the same word
you would use to cancel a lunch order, and a marker-hunting matcher trained on
apology phrasing will not have it. I kept it, but if the scorer counts it as
marked, the honest reading is that I shipped four marked reversals, not three.

**REV-06 (reject path sized for "a handful a night", forty thousand arrive)** —
Against: nothing is being replaced; the assistant is reporting a volume discovery
and proposing new work, which looks like analysis-plus-plan. For: the block says
outright that the design the assistant built the reject path around is no longer
a design, and names the successor shape. The abandoned thing is an assumption the
assistant acted on, and I hold that acting on an assumption makes it a position.
If the extractor requires the abandoned thing to be *code*, this will read None.

**REV-04 (intermediate work file)** — Against: one line of the block is a factual
complaint about a tape mount and the other is a statement of current behaviour;
neither says "I am changing my mind". For: "the intermediate work file I added"
is unambiguously the assistant's own construction, and "step two reads the master
directly now" is only meaningful as its removal. This is the purest test in the
probe of whether the extractor can read retreat without being told.

**REV-10 ("I don't think the single-program approach ... is going to hold")** —
Against: hedged. The assistant says "I don't think" and "the honest shape here",
which is closer to a recommendation than a decision, so a strict reading makes it
analysis. For: the thing being doubted is explicitly the approach the assistant
"has been building toward", and it lands on a replacement. I judged hedging to be
a tone, not a hedge on the decision itself. Reasonable people will split here.

**NONE-08 (edit mask Z(7)9.99-)** — Against: the assistant changed a decision
mid-flight and is flagging it, which has the shape of a reversal. For: the mask
being replaced is attributed to "the version the shop has been running since
1998" — the assistant never chose it, so the ownership test fails and it is a
bug repair reported in a status line. The whole item turns on that one clause; if
a reader skims past the attribution it flips, which is exactly why it is here.

**NONE-09 (two ways to do the totals)** — Against: it contains "rather than" and
weighs two designs, and an extractor that treats deliberation as decision will
call it a reversal. For: the block commits to nothing. It ends on an unresolved
trade-off. Deliberation is not a decision under any of my four tests.

**NONE-02 (switching the sort step per the user's note)** — Against: a switch is
a switch, the assistant is describing abandoning one step for another. For: the
first three words are "Per your note" — the change originates with the user, so
the assistant is executing, not retreating. If the extractor is insensitive to
attribution of origin this is a guaranteed false positive, which is the point.

**HARD-02 (abandoning an approach the *user* suggested and the assistant built
on)** — genuinely unsettled. Against reversal: the position originated with the
user, so the assistant is contradicting the user, not itself. For reversal: the
assistant adopted it and built on it, which makes it the assistant's position by
the time it is dropped. I labelled reversal because adoption transfers ownership,
but I would not argue hard, and this is the single item I most expect to be
scored against me.

**HARD-03 (scrap the approach, no replacement yet)** — the definition says the
assistant "puts down a position *and* changes course". Here only the first half
happens; the successor is explicitly unknown. I labelled reversal on the grounds
that abandonment is the decision and the replacement is future work, but a
literal reading of "and changing course" makes it None.

**HARD-08 (plan changed before any code was written)** — is an announced but
unbuilt plan a "position"? I said yes: the assistant said it would do the edits
in the second step and is now doing them in the first, which is a retraction of a
stated commitment. The counter-argument is that re-planning before execution is
just planning, and plans are listed under None.

**HARD-01 (user retracting their own earlier instruction)** — labelled None,
because only the assistant can reverse and because retargeting one piece of work
is not a standing rule. It is in the hard set rather than the scored None set
only because the twelve scored None items are all required to be assistant-role.

**HARD-07 ("after every step, actually, that should just be how we write
these")** — labelled directive. The block opens as a task ("put a check after
this step") and generalises mid-sentence into a rule. If the extractor keys on
the opening clause it will read task and return None. I think the generalisation
governs, because the last clause is explicitly about how "we write these" in
general, but the sentence is doing two jobs and that is why it is flagged.

------------------------------------------------------------------------------
4. ATTESTATION
------------------------------------------------------------------------------

I did not read the extractor, its source, its README, its documentation, its
benchmarks, its measurements, or any other probe. I did not list, search, grep or
open any path under the project directory. I did not search the web for this
project. I did not run the extractor, or anything else, against these sentences —
they are unscored by me and by any tool. The only file I created is this one.

------------------------------------------------------------------------------
COUNTS
------------------------------------------------------------------------------

Scored items: 32
  - reversal  : 12  (all role="assistant"; 3 with an explicit self-correction
                     marker — REV-07, REV-08, REV-12 — and 9 without)
  - None      : 12  (all role="assistant"; 10 of the 12 contain a substitution
                     frame: "instead of", "replace ... with", "switch to",
                     "rather than", "in place of")
  - directive :  8  (all role="user"; 4 positive standing rules, 3 prohibitions,
                     1 conditional rule)

Additional flagged items: 8  (group "hard"; 6 assistant, 2 user)

Total entries in CASES_E: 40

Tuple shape note: the brief's prose specifies a 4-tuple (group, role, text,
expected) while the inline annotation it showed had three slots. I followed the
prose, since it is the part that defines the fields; entries below are 4-tuples
and the annotation is widened to match.
"""

CASES_E: list[tuple[str, str, str, str | None]] = [
    # ------------------------------------------------------------------
    # REVERSALS — assistant abandoning its own position. 12 items.
    # Nine carry no self-correction marker.
    # ------------------------------------------------------------------
    (
        "reversal-unannounced",
        "assistant",
        "The two-pass design I laid out — build the summary in the first pass, "
        "apply it in the second — doesn't survive the fact that the detail file "
        "comes off tape and operations will only give us one mount. It all has "
        "to happen in a single pass, with the totals accumulated in a table and "
        "written at end of job.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "I've spent the whole morning trying to get the OCCURS DEPENDING ON "
        "table to behave under the compiler options this shop still uses, and it "
        "mislays every odd-length record no matter what I do with the count "
        "field. I'm dropping the variable-length layout. Fixed 250 entries and "
        "an explicit occurrence counter.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "Keeping the reformat inside the program made sense while I believed the "
        "sort card couldn't express the packed-decimal rounding. It can — OUTREC "
        "handles the whole thing including the sign — so the program comes out "
        "of the job stream and the sort step does the work on its own.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "That intermediate work file I put between the two steps is doing nothing "
        "for us except costing a second mount and forty minutes of elapsed time. "
        "Step two reads the master directly now.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "The scheduler here can't see a return code across a job boundary, which "
        "I didn't know when I proposed splitting this into two jobs — the second "
        "one would happily run on garbage every time the first one failed. It "
        "stays a single job with COND set on the later steps.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "I built the whole reject path around the assumption that bad records "
        "would be a handful a night. The January file has just over forty "
        "thousand of them. A reject file plus a manual re-key isn't a design any "
        "more; the job has to correct the common cases in line and only reject "
        "what it genuinely can't resolve.",
        "reversal",
    ),
    (
        "reversal-marked",
        "assistant",
        "You're right that the restart logic has no business living inside the "
        "program. I'm pulling the checkpoint calls out of the procedure division "
        "and letting the utility take it at step level, the way the other "
        "nightly jobs do it.",
        "reversal",
    ),
    (
        "reversal-marked",
        "assistant",
        "I was wrong about the copybook being safe to extend at the end. Three "
        "other programs copy the same book with the shorter layout and they'll "
        "all start reading past their record area. The new fields go into a "
        "separate redefined region instead.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "Scratch the SEARCH ALL. The table I'm loading isn't guaranteed ascending "
        "once the regional codes get mixed in with the branch codes, and sorting "
        "it at load time costs more than the straight PERFORM VARYING ever did.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "I don't think the single-program approach I've been building toward is "
        "going to hold. The edit rules and the posting rules change on completely "
        "different schedules and two different groups own them. Two programs with "
        "a work file between them is the honest shape for this.",
        "reversal",
    ),
    (
        "reversal-unannounced",
        "assistant",
        "Fourth attempt at getting the dynamic CALL to resolve at run time, and "
        "the linkage editor still won't see the module in the private library no "
        "matter where I put the concatenation. Static CALL it is. I'll write the "
        "recompile requirement into the run book so nobody is surprised later.",
        "reversal",
    ),
    (
        "reversal-marked",
        "assistant",
        "On second thought the 88-levels I added to the copybook are the wrong "
        "home for this. Condition names get compiled into every program that "
        "copies the book, and these codes change twice a year — it needs to be a "
        "small file the job reads into a table at startup.",
        "reversal",
    ),

    # ------------------------------------------------------------------
    # NONE — ordinary assistant narration. 12 items.
    # Ten contain a substitution frame and are the intended traps.
    # ------------------------------------------------------------------
    (
        "narration-substitution",
        "assistant",
        "The paragraph that returns the branch name is still the stub I left in "
        "on Tuesday — it just moves spaces. Now that the layout is confirmed I'm "
        "replacing it with the real read against the branch file.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "Per your note on the JCL I've switched the second step from the plain "
        "sort to the one with the SUM FIELDS card, and taken the counters back "
        "out of the program. Recompiling the caller now.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "The abend traces back to the MOVE at line 412 — a signed packed field "
        "going into an unsigned display field, so instead of the sign nibble you "
        "get an F sitting in the low-order byte and every compare after it "
        "quietly fails.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "The 1997 version walks the entire table with a PERFORM VARYING rather "
        "than doing a binary search, which is why the lookup eats eleven minutes "
        "of CPU on a full extract. Nobody noticed while the table was four "
        "hundred entries.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "Next I'll replace the hard-coded cutoff date in the selection paragraph "
        "with a value taken off the PARM field, then rerun against the small file "
        "and compare the control totals line for line.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "In place of appending with DISP=MOD, the new step writes a fresh "
        "generation and lets the base roll overnight. That's how every other "
        "nightly job in this shop is set up, so the housekeeping rules already "
        "cover the retention without anybody changing them.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "Do you want the reconciliation totals going to the print file rather "
        "than a separate flat file? The run book reads as though operations "
        "expects to see them on paper at handover, but the run book is four years "
        "old.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "Compile is clean and the small run reconciles to the penny. One change "
        "worth flagging: the edit mask the version this shop has been running "
        "since 1998 used truncated the sign on negative adjustments, so I put "
        "Z(7)9.99- in place of it.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "There are two honest ways to do the totals. Accumulate them in the "
        "program, or let the sort do it with a SUM card rather than carrying the "
        "counters ourselves. The sort is faster but we lose the per-branch "
        "breakdown unless we add a second pass to get it back.",
        None,
    ),
    (
        "narration-substitution",
        "assistant",
        "I folded the three duplicated edit paragraphs into one and had the "
        "callers pass a flag, instead of the same forty lines sitting in the "
        "source three times. No behaviour change — the small run comes out "
        "byte-identical to the baseline.",
        None,
    ),
    (
        "narration-plain",
        "assistant",
        "Sorry, the run I showed you earlier was against last month's generation, "
        "which is why the branch totals looked so flat. I'm resubmitting it "
        "against the current one now and I'll send the new listing when it comes "
        "back.",
        None,
    ),
    (
        "narration-plain",
        "assistant",
        "The link step is failing because the modules are listed in the wrong "
        "order in the control cards — the called routine has to come after the "
        "caller for the linkage editor to resolve it. Reordering and "
        "resubmitting.",
        None,
    ),

    # ------------------------------------------------------------------
    # DIRECTIVES — user imposing standing rules. 8 items.
    # ------------------------------------------------------------------
    (
        "directive-plain",
        "user",
        "Every step we add to this job from here on gets a checkpoint taken at "
        "the top, no exceptions. I am tired of restarting nightly runs from the "
        "beginning because somebody decided one step was too short to bother.",
        "directive",
    ),
    (
        "directive-prohibition",
        "user",
        "Never code a dataset name as a literal inside a program in this shop. It "
        "goes on the DD statement or it comes in on the PARM, and that's the end "
        "of it.",
        "directive",
    ),
    (
        "directive-plain",
        "user",
        "From now on any copybook you touch gets a change line at the top with "
        "the ticket number and the date. The auditors ask for it every quarter "
        "and I'd rather it were already there.",
        "directive",
    ),
    (
        "directive-plain",
        "user",
        "As a rule here: we do not abend on bad input data. The record goes to "
        "the reject file with a reason code and the run carries on to the end.",
        "directive",
    ),
    (
        "directive-conditional",
        "user",
        "If a job looks like it will run past the window for its class, split it. "
        "Don't come back to me asking for a bigger class — that conversation ends "
        "the same way every time.",
        "directive",
    ),
    (
        "directive-plain",
        "user",
        "Put the region on the JOB card once and keep it off the individual "
        "steps. Same on this one and on anything else you write for us.",
        "directive",
    ),
    (
        "directive-prohibition",
        "user",
        "No program goes into production here without a restart section in the "
        "run book. If you can't write down how an operator restarts it at three "
        "in the morning, it isn't finished.",
        "directive",
    ),
    (
        "directive-prohibition",
        "user",
        "Treat every numeric field coming off that tape as unsigned display "
        "unless the record layout explicitly says packed. I don't want another "
        "sign surprise turning up six months after we've gone live.",
        "directive",
    ),

    # ------------------------------------------------------------------
    # HARD — flagged, labelled as best I can. 8 items.
    # ------------------------------------------------------------------
    (
        "hard",
        "user",
        "Ignore what I said earlier about keeping the totals in the program — do "
        "them in the sort. It was my mistake, the sort was always the right place "
        "for it.",
        None,
    ),
    (
        "hard",
        "assistant",
        "The record-at-a-time read you suggested, which I've been building on all "
        "week, can't hold a running balance across a checkpoint restart — the "
        "position is lost and there's nowhere to put it. I'm loading the whole "
        "day into a table at startup and working from that.",
        "reversal",
    ),
    (
        "hard",
        "assistant",
        "Scrap the entire approach I described for the reconciliation step. I "
        "don't yet know what replaces it — give me an hour with the record counts "
        "from the last four cycles and I'll come back with something.",
        "reversal",
    ),
    (
        "hard",
        "assistant",
        "I'm not using dynamic calls anywhere else in this system after what the "
        "linkage editor did to us here. Static from here on, and the run book "
        "carries the recompile list.",
        "reversal",
    ),
    (
        "hard",
        "assistant",
        "Backed out the table-driven edits and put the in-line IF statements back "
        "the way they were, as you asked. The small run matches the old output "
        "again, down to the trailing blanks.",
        None,
    ),
    (
        "hard",
        "assistant",
        "Compile is clean, the small run matches and the totals reconcile. The "
        "one thing that changed since yesterday: the straight sequential read of "
        "the master that I argued for won't hold up at full volume, so the "
        "program now reads it keyed off the transaction file.",
        "reversal",
    ),
    (
        "hard",
        "user",
        "Put a return-code check after this step — after every step, actually, "
        "that should just be how we write these.",
        "directive",
    ),
    (
        "hard",
        "assistant",
        "I said I'd do the edits in the second step. Having read the run book "
        "properly, they belong in the first one, before the sort ever sees the "
        "data — otherwise we're sorting records we're about to throw away.",
        "reversal",
    ),
]
