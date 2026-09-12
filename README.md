# TRIAGE

**Reviewer attention is the scarce resource. Spend it on the lines that need it.**

Today's AI reviewers make the bottleneck worse: they add comments to a queue
that is already too long. TRIAGE does the opposite. It automatically proves
whatever it can about a diff and forwards only the **unproven remainder** to a
human.

> Think of airport security. A second AI reviewer is another officer commenting
> on every bag. TRIAGE is PreCheck: most bags clear by machine, and only the
> flagged ones get hand-searched.

```
PR diff -> hunks
   |
   |-- Covered?      do the tests actually execute these lines?
   |-- Constrained?  mutation testing on the diff only:
   |                 MS_D = killed mutants in D / viable mutants in D
   |-- Equivalent?   for hunks labelled "refactor": run old vs new on
   |                 generated inputs; every result must match
   |-- Effective?    for test code: revert the definitions this test
   |                 references; a test that still passes proves nothing
   |-- Crash?        run the new code on inputs its own signature admits;
   |                 an unasked-for exception is a defect, not a test gap
   v
Residual set R = hunks that fail *or cannot answer* any check
-> "review 16 of 34 changed lines"
```

## The one invariant

**A hunk is VERIFIED only if every applicable check actively passed.
An inconclusive check is never evidence of safety.**

`FAIL` and `ABSTAIN` both land in the residual set. Stamping "verified" on a
buggy hunk is worse than having no tool at all, so the system is built to be
wrong in the expensive direction (asking for review it did not need) rather
than the dangerous one. Every abstention carries its reason into the report.

## Install and run

```bash
pip install -e ".[dev]"

triage run --repo . --base main                 # terminal summary
triage run --repo . --base main --format md     # PR comment
triage run --repo . --base main --format json   # machine readable
triage run --base main --fail-on-residual       # CI gate (exit 1 if any)
```

See it work end to end on generated repos. Two scenarios, because one number in
isolation misleads:

```bash
bash scripts/demo.sh mature    # a disciplined change: 97% verified, 1 line to review
bash scripts/demo.sh mixed     # one of every hard case: 50% verified, 22 lines
bash scripts/demo.sh history   # plants two real bugs, then rediscovers them by blame
```

## What the output looks like

```
TRIAGE review 16 of 34 changed lines | 53% test-constrained
                                     | (a fact about this suite, not the change)
  [0.94] cart.py:9-9 (refactor)
        - equivalent: not equivalent: apply_discount(total=100.0, pct=100.0)
          returned 0.0 before and ValueError(percentage out of range) after
  [0.82] cart.py:22-25 (behavioral)
        - covered: 2/3 added lines never execute (lines 24, 25)
  [0.66] cart.py:14-19 (behavioral)
        - constrained: MS_delta=0.12 < 0.80; 7 mutations survive unnoticed:
          num-const @ cart.py:16 ('4.99' -> '5.99'), compare @ cart.py:16 ('<' -> '<=')
  [0.16] tests/test_cart.py:28-31 (test)
        - effective: test_subtotal_is_callable still passes with subtotal
          reverted to the pre-change implementation: it does not pin down any
          behaviour this PR introduced
```

The third one is 100% covered and worthless: the only test asserts
`isinstance(fee, float)`, so the price can be changed to anything and nothing
objects. Coverage says green; TRIAGE says *look here*.

**The percentage measures the test suite, not the change and not TRIAGE.** The
same tool reports 97% on the `mature` demo and 3% on its own greenfield first
commit. The product is the residual list; the number is context.

## The three checks

| check | question | verdict when it cannot answer |
|---|---|---|
| **covered** | do the tests execute these lines? | ABSTAIN (file unmeasured) |
| **constrained** | would any test notice if these lines were wrong? | ABSTAIN (too few mutants, budget spent, selected tests not green on their own) |
| **equivalent** | does a *declared refactor* actually preserve behaviour? | ABSTAIN (impure, unannotated, nondeterministic) |
| **effective** | does this test detect anything the change did? | ABSTAIN (edit to an existing test, or not inside a test function) |
| **crash** | does the code raise something unasked-for on an input its signature admits? | ABSTAIN (impure or unannotated) |

The first four are **credentialing** checks: they can grant verification, and
the invariant applies to them in full. `crash` is a **veto**: it can only take
verification away. A crash found on generated inputs is strong evidence a human
is needed; the absence of one over a few hundred inputs is weak evidence of
nothing, so a PASS there credentials nothing and an ABSTAIN costs nothing.
Letting weak evidence verify is the failure this tool exists to prevent.

The fourth one exists because test code cannot be verified by the suite that
contains it. The one question that *is* mechanically answerable: revert the
definitions the test references, and see whether it still passes. One that does
is either testing something that already worked or asserting too little.
Reverting happens one definition at a time, never one file at a time — reverting
whole files breaks the module's imports and makes every test look effective.

A hunk is labelled `refactor` only when the author declared it — a commit
subject starting with `refactor:`/`style:`, or a `# triage: refactor` marker —
or when the change is token-identical (pure reformatting). We then hold the
claim to account instead of taking it on trust.

## Cost

Naive mutation testing is `mutants x full-suite runtime`, which is why nobody
runs it. Three layers bring it down:

1. **Mutants come only from the diff.** The PR is responsible for the
   test-adequacy of the lines it added, not the whole repo.
2. **Test-impact selection.** One instrumented run with coverage *contexts*
   yields a reverse map `line -> tests that executed it`. Each mutant then runs
   only those tests. On the demo that is 1 test instead of 8; on a real repo it
   is single digits instead of thousands. The report prints the factor.
3. **A wall-clock budget.** When it runs out, the remaining hunks abstain and
   go to a human. Running out of time is a failure of *our evidence*, never a
   reason to pass something through.

## Evaluating the claim

```bash
triage eval --repo . --base main --bugs 50 --out curve.csv
```

Plant bugs in the diff. Discard the ones the suite catches — CI already had
those, they were never a review problem. Of the bugs that **survive the suite**,
measure how many land in a hunk TRIAGE called verified. Those are escapes, and
they are the only failure that matters.

```
bugs injected        44
  caught by CI       24 (the suite failed; never a review problem)
  survived the suite 20 (a human was the only defence left)
  ESCAPED TRIAGE     0

tau   review burden   escape rate
1.00           80%            0%     <- default: residual set only
0.41           97%            0%
```

Sweeping the risk threshold traces the whole trade-off curve between review
burden and escape rate; the product claim is one point on it. Raising
`mutation_threshold` to 1.0 pushes escapes toward zero and burden up.

## Catching real bugs, not just test gaps

Four of the five checks measure the *tests*. They can tell you nothing would
notice if the code were wrong; they cannot tell you it is wrong. Two things
address that directly.

**The crash check finds defects.** This is what it looks like when every
test-adequacy signal says a hunk is fine and the code is broken anyway:

```
[0.71] cart.py:35-40 (behavioral)   covered: PASS   constrained: PASS
   - crash: average_price(prices=[]) raises ZeroDivisionError: division by
     zero. The signature admits this input and the function never raises or
     catches that type deliberately
```

100% covered, every mutant killed. Every earlier version of TRIAGE marked that
hunk verified.

**`triage bugs` replays your repository's own history.** Mutants are a proxy for
bugs and a poor one. This uses real defects instead, via SZZ: find commits whose
message says they fix a bug, `git blame` the lines they changed back to the
commit that wrote them, replay that commit as a pull request, and ask whether
TRIAGE put the offending lines in front of a human.

```bash
triage bugs --repo . --scan 300 --max-bugs 5
```

```
bug-fix commits      2
bug-inducing commits 2 (blamed from the lines those fixes changed)
  CAUGHT             1  the buggy lines were routed to a human
  MISSED             1  the buggy lines sat in a verified hunk
  catch rate         50%
  review burden      53% of changed lines on those commits

MISSES (the only number that matters):
  59b6262432 feat: overtime pay
    bug at sched.py:21; the hunk holding the bug was marked verified
    later fixed by c18b13be70 fix: overtime was paid at double time, not
                              time and a half
```

That miss is not a bug in TRIAGE. The overtime function was fully covered, every
mutant was killed, and *the tests asserted the wrong rule* — the author
misunderstood the requirement and encoded the misunderstanding in both places.
No test-adequacy signal can see that, and neither can a mutant. Reporting it as
a miss is the point: this is the honest denominator the mutant-based numbers
could never give.

Caveats that come with the method, not with this implementation: SZZ blame
fingers whoever last touched a line, so a reformat can be blamed instead of the
author; commit messages are a weak oracle (TRIAGE's own history matches *zero*
fix commits, since its authors wrote "Make the budget bind" rather than "fix");
the suite at an old commit may not run today, and those are skipped and counted;
and only bugs that were eventually found and fixed appear at all.

## Calibrating the ranking

The shipped risk weights are hand-set priors. To replace them with fitted ones:

```bash
triage eval --repo . --base main --dataset pr-1234.json   # repeat per PR
triage fit --dataset pr-*.json --out weights.json
```

`fit` refuses on samples too small to support an estimate (fewer than 40
labelled hunks, or fewer than 5 of either class) and reports the fitted ranking
AUC beside the priors' AUC, so a fit that helps nothing says so. Hunks no bug
was ever planted in are excluded rather than treated as negatives: a hunk we
never probed is not evidence that it is clean.

## Honest limitations

These are real, and none of them are fixed by trying harder:

- **False confidence is the killer failure mode.** Mitigated by the invariant
  above, by refusing to verify against a red suite, by re-running the selected
  tests unmutated before trusting any kill, and by never letting a skipped or
  budget-truncated check count as a pass. Mitigated, not eliminated.
- **Mutants are a proxy for bugs.** They over-represent local mechanical errors
  and entirely miss the ones that make review valuable: a wrong requirement, a
  missing case, a race. A green mutation score is necessary, not sufficient.
- **Equivalence checking has narrow reach.** Comparing return values says
  nothing about side effects, I/O, or nondeterminism — most production code.
  The purity gate refuses all three up front, so the check abstains far more
  often than it passes. That is the design working, not failing.
- **Verified does not mean correct.** It means "the tests constrain this". The
  code can still be the wrong feature with a bad interface. TRIAGE narrows the
  queue; it does not raise the ceiling.
- **A verified test can still be weakened later.** The effectiveness check
  certifies that a new test detects *this* change. Nothing stops someone
  deleting an assertion next week, and no test suite can notice its own
  degradation. `triage eval` counts test-code mutants separately for exactly
  this reason, and reports them rather than discounting them.
- **Edited tests are never verified**, only newly added ones: a test can be
  weakened and still fail against the old code, because the PR also changed the
  behaviour it covers.
- **The crash check has a real false-positive mode.** A function with an
  undocumented precondition ("callers always pass a non-empty list") gets
  flagged for the empty list its annotation admits. Python has no precondition
  language to read, so the choice is to flag those or to miss the real ones.
  It flags, with a counterexample a reviewer dismisses in seconds, and
  `# triage: allow-crash` silences it. It also refuses to blame a *proved*
  refactor for a crash, since an unchanged behaviour means the crash predates
  the PR.
- **Python only**, for everything except the coverage gate. Non-Python files are
  residual by design and say so — "no analyser for .sh files" — rather than
  arriving there through a misleading coverage message.

## Configuration

`.triage.toml` at the repo root:

```toml
[triage]
test_command = ["pytest", "-q", "-p", "no:cacheprovider"]
source_roots = ["mypackage"]
test_paths = ["tests"]

coverage_threshold = 1.0        # every added executable line must run
mutation_threshold = 0.8        # MS_delta required to call a hunk constrained
min_mutants = 2                 # below this the score is noise: abstain

mutation_budget_seconds = 240.0 # total; exhausting it abstains, never passes
max_mutants_per_hunk = 10
equivalence_cases = 64
```

`docs/DESIGN.md` covers the reasoning, the cost model, and what is deliberately
missing.

## Licence

MIT.
