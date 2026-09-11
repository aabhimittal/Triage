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
   v
Residual set R = hunks that fail *or cannot answer* any check
-> "81% machine-verified; review these 14 lines"
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

See it work end to end on a generated repo containing one of every interesting
case:

```bash
bash scripts/demo.sh
```

## What the output looks like

```
TRIAGE 20% verified  |  6 verified / 24 residual / 2 exempt lines  |  14.3s
  [0.94] cart.py:9-9 (refactor)
        - equivalent: not equivalent: apply_discount(total=100.0, pct=100.0)
          returned 0.0 before and ValueError(percentage out of range) after
  [0.82] cart.py:22-25 (behavioral)
        - covered: 2/3 added lines never execute (lines 24, 25)
  [0.66] cart.py:14-19 (behavioral)
        - constrained: MS_delta=0.12 < 0.80; 7 mutations survive unnoticed:
          num-const @ cart.py:16 ('4.99' -> '5.99'), compare @ cart.py:16 ('<' -> '<=')
```

That third one is the interesting case. It is 100% covered, and worthless: the
only test asserts `isinstance(fee, float)`, so the price can be changed to
anything and nothing objects. Coverage says green; TRIAGE says *look here*.

## The three checks

| check | question | verdict when it cannot answer |
|---|---|---|
| **covered** | do the tests execute these lines? | ABSTAIN (file unmeasured) |
| **constrained** | would any test notice if these lines were wrong? | ABSTAIN (too few mutants, budget spent, selected tests not green on their own) |
| **equivalent** | does a *declared refactor* actually preserve behaviour? | ABSTAIN (impure, unannotated, nondeterministic) |

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
- **Python only**, for the mutation and equivalence checks. Coverage-based
  triage generalises; the operators do not.

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
