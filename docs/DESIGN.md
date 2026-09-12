# TRIAGE: design notes

## 1. The problem is a scheduler problem

Code review is a queue with one server (a human) and an arrival rate set by
everyone else on the team. AI review assistants have, almost universally,
increased the arrival rate: they emit comments, and comments are work. The
queue gets longer, and because reviewer attention decays down a page, the
*marginal* comment is read with less care than the one above it. Adding a
reviewer that never gets tired does not help if the bottleneck is the reader.

TRIAGE treats attention the way an OS treats CPU: a fixed budget to be
allocated, not a resource to be spent uniformly. The tool's output is not
comments. It is a **partition** of the diff:

- **verified** — evidence exists that the tests constrain this code;
- **residual** — no such evidence; a human is the remaining defence;
- **exempt** — nothing executable here (docs, comments, blank lines).

The product is the size and the ordering of the residual set.

## 2. Why three checks, in this order

Each check answers a strictly stronger question than the last, and costs
strictly more to answer.

**Covered?** Did the tests execute these lines? Cheap: one instrumented run for
the whole PR. Nearly worthless as evidence on its own — executing a line proves
only that control reached it — but decisive as a *gate*. An uncovered line
cannot be constrained by any test, so failing here lets us skip the expensive
check with the answer already known. This is the branch-prediction of the
pipeline: the cheap test that makes the expensive one rare.

**Constrained?** Would any test notice if this code were wrong?

```
MS_D = killed mutants in D / viable mutants in D
```

This is the check that carries the weight. The gap between "covered" and
"constrained" is exactly where escaped bugs live: a line executed by a test
that asserts nothing about it is green on every coverage dashboard and
completely unprotected. In the demo, a 100%-covered `shipping_fee` scores
MS_D = 0.12, because the only test asserts `isinstance(fee, float)` — every
price in the function can be changed freely and the suite stays green.

**Equivalent?** For a hunk whose author *declared* a refactor: do the old and
new implementations agree on generated inputs? Narrow reach, sharp teeth. It is
one of two checks that can verify a hunk on its own, because behavioural
equivalence makes the coverage question moot: if nothing observable changed,
there is nothing for a reviewer to find.

**Effective?** For test code, where the other three are meaningless. Mutating a
test is incoherent — killing a mutant in an assertion would require some *other*
test to object — and coverage of a test file measures nothing. The answerable
question is the one a careful reviewer asks anyway: *does this test fail for the
right reason?* Revert the definitions the test references, run it, and a test
that still passes has told you nothing about the change.

The implementation detail that makes or breaks this check is granularity.
Reverting whole files is the obvious approach and it is useless: a test module
imports several names at once, so removing the file's new definitions breaks the
import and *every* test in it fails, which reads as "every test is effective".
Reverting one definition at a time — and only the ones the test under
examination actually references — keeps the module importable, so the result is
attributable to the behaviour under test. See `triage/revert.py`.

The check is conclusive only for *newly added* tests. An edit to an existing
test can weaken it while still failing against old code, because the PR changed
the behaviour it covers; those stay residual.

**Crash?** The only check that is about the code. Run the changed function on
inputs drawn from its own annotations and report any exception the author never
asked for. This is the first thing in TRIAGE that can find a defect rather than
diagnose a missing test, and the demo case is the one that justifies it:
`average_price(prices)` is 100% covered with every mutant killed, and dies on an
empty cart. All three test-adequacy checks certify it. Only running it does not.

## 2b. Credentials and vetoes

Adding a check that reports on code rather than tests forces a refinement of the
invariant, and it is worth stating precisely because a sloppy version of it
would undo the whole safety argument.

*Credentialing* checks -- covered, constrained, equivalent, effective -- can
grant verification. The invariant applies to them unchanged: every applicable
one must actively PASS, and ABSTAIN disqualifies exactly like FAIL.

*Veto* checks -- crash -- can only take verification away. The asymmetry follows
from what the evidence is worth in each direction. A crash on a generated input
is a demonstrated fact about the code, with a counterexample attached: strong
evidence a human is needed. No crash across two hundred inputs is weak evidence
of nothing much, since the input space is unbounded and the generator is naive.
If a PASS there could help verify a hunk, weak evidence would be laundered into
a safety claim -- the precise failure this tool exists to prevent.

One consequence worth noting: a *proved* refactor outranks the veto. If the new
code is observably identical to the old, any crash it has is one the codebase
already had, and blaming this PR for it would flag every refactor that brushes
against long-standing fragile code.

## 3. The safety argument

Everything rests on one asymmetry. Two ways to be wrong:

| error | cost |
|---|---|
| residual when it could have been verified | a human reads a few lines they did not need to |
| verified when it should have been residual | a bug ships, *and* the tool taught the reviewer to trust it |

The second error also compounds: each undetected escape raises the trust the
next escape will exploit. So every ambiguity resolves toward residual:

1. `ABSTAIN` is a distinct status from `FAIL`, and both route to a human.
   "We could not tell" is never laundered into "safe".
2. A red baseline suite voids the entire run. Test-based evidence against a
   broken suite is not weak evidence, it is no evidence.
3. Before any mutation kill is believed, the selected tests are re-run
   *unmutated*. Without this guard, an import error in the mutant subprocess
   kills every mutant and every hunk looks perfect — a bug this repo actually
   had, and the reason the check exists.
4. A budget-truncated mutation run abstains. It never reports the partial score
   as if it were the whole picture.
5. Statement-level coverage falls back to *abstain*, never to "assume the
   covered lines are all the lines". A silent fallback of that shape was the
   second real false-PASS found while building this.
6. `verdict.decide()` is a pure function with no I/O, so the whole safety
   argument is one readable file with a parametrised decision table beside it.

## 4. The cost model

Naive mutation testing costs `M x T` where `M` is mutants and `T` is full-suite
runtime. For any real repo that product is measured in days. Three reductions:

**Scope.** `M` is generated only from lines the diff added. A PR is responsible
for the test-adequacy of what it changed.

**Test-impact selection.** Running coverage with `dynamic_context = test_function`
yields, per source line, the set of tests that executed it. The mutation loop
then runs `tests(line(m))` instead of the suite:

```
cost = M x T_impacted   where T_impacted << T
```

`T_impacted` is usually single digits. The reported `impact_selection_factor`
is `|suite| / mean(|selected|)`. This is the single biggest lever in the system
and the reason the whole thing fits in a CI job.

**Budget.** A shared wall clock. Exhaustion abstains.

One subtlety worth naming: the impact map is derived from *unmutated* coverage.
A mutant could in principle cause a previously-unrelated test to fail, and that
kill would be missed. This biases MS_D **down**, which pushes hunks into review.
Wrong in the safe direction.

## 5. Input generation, and why random fuzzing was not enough

The first working version of the equivalence check passed a deliberately broken
refactor: `pct > 100.0` changed to `pct >= 100.0`. The two functions differ on
exactly one input out of a continuum, and uniform sampling finds it with
probability ~0.

The fix is standard in fuzzing and easy to forget: **mine the literals out of
the code under test** and use them as seeds, expanded to their immediate
neighbourhood (`n-1, n, n+1`). Boundaries are written in the source. They do not
need to be searched for, they need to be read. With `100.0` in the pool, the
counterexample appears in the first few cases:

```
not equivalent: apply_discount(total=100.0, pct=100.0)
returned 0.0 before and ValueError(percentage out of range) after
```

## 6. Equivalent mutants

`x * 1` -> `x / 1` is a mutation that changes nothing. Such mutants cannot be
killed and depress MS_D, pushing good hunks into review — safe, but it costs
reach, and the literature's usual answer (detecting equivalence) is
undecidable in general.

One class was worth excluding by hand: **string literals used as messages**
(exception text, log lines, assertion messages). Mutating them tests nothing
anybody asserts on, and the evaluation harness showed why it matters — the
tool's single "escaped bug" in the first eval run was a mutated `ValueError`
message, which is not a bug. Leaving it in would have made the escape rate
report a defect that does not exist. The exclusion narrows what MS_D claims to
cover, and says so.

## 7. Ranking the residual set

Shrinking the queue is half the job; ordering it is the other half, because
attention decays down the page. The score is a logistic over features in three
groups:

- **evidence**: uncovered fraction, mutant survival, disproved refactor claim,
  fraction of checks that abstained;
- **structure**: hunk size, branch density, new file, public API surface, churn;
- **domain**: names matching auth/crypto/payment/SQL patterns, error handling,
  concurrency.

The weights ship as hand-set priors, stated as data in `triage/risk.py`.
`triage fit` replaces them with values learned from `triage eval` datasets:
each probed hunk contributes its features and a label saying whether a bug
planted there survived the suite.

Two properties of the fitter matter more than the arithmetic:

*It refuses.* Logistic regression returns coefficients from nine rows and one
positive just as readily as from ten thousand, and the output looks equally
authoritative either way. A tool premised on never claiming more confidence
than the evidence supports cannot then ship a model fitted on noise, so `fit`
raises rather than guessing below 40 labelled hunks or 5 per class. It also
prints the fitted ranking AUC next to the priors' AUC, so a fit that improves
nothing announces it.

*Unprobed hunks are not negatives.* Only hunks a bug was actually planted in
are labelled. Treating the rest as "no bug here" would train the model to trust
precisely the code the evaluation failed to examine — the same absence-of-
evidence error the ABSTAIN status exists to prevent, smuggled in as training
data.

**Do not read the absolute numbers as probabilities.** The ordering is the
product.

## 8. Evaluation, part one: mutants

The experiment in one line: *only bugs that survive CI are review problems, so
measure what fraction of those TRIAGE routes to a human.*

```
for each candidate bug planted in the diff:
    run the FULL suite
    suite fails -> CI caught it; discard, it was never a review problem
    suite passes -> a human was the last defence. Did it land in the
                    residual set (caught) or a verified hunk (ESCAPED)?
```

Product code and test code are counted separately, and both are reported. A
mutant that deletes an assertion from a test survives the suite *by
construction* — no suite can notice its own degradation — so pooling those with
product-code bugs yields an "escape rate" measuring an impossibility. They are
not discounted either: the underlying risk is real, and the effectiveness check
certifies that a new test detects *this* change, not that nobody weakens it
later.

Sweeping the risk threshold `tau` — below which even verified hunks are added
back to the review queue — traces the curve between `review burden` and
`escape rate`. It has the shape of a precision/recall curve, and any product
claim ("review 19% of the lines") is a single point on it, which is the honest
way to state such a claim.

The caveat is structural and cannot be engineered away: **mutants are not
bugs**. They over-represent local mechanical errors and entirely miss wrong
requirements, missing cases, and races — the defects that make human review
worth having. A good escape rate is necessary for the claim and nowhere near
sufficient.

## 9. The headline number is a property of the repository

`97% machine-verified` is not a claim about TRIAGE and not a claim about the
diff. It is a measurement of the *test suite* the diff landed in. The same
binary reports 97% on the `mature` demo, 65% on the `mixed` one, and 3% on
TRIAGE's own first commit, where nothing had tests yet — and all three are
correct.

This matters because the number is the part people quote. The reports therefore
lead with the actionable quantity (*review 12 of 34 changed lines*) and carry
the percentage as context with that caveat attached, rather than the reverse.
A tool that sells itself on a number it does not control will be caught out by
the first reviewer who runs it on a legacy module.

## 10. What is deliberately missing

- **Languages other than Python.** The coverage-and-impact layer generalises;
  the mutation operators, the purity analyser and the definition-level reverter
  do not. Non-Python hunks are residual with an explicit "no analyser for this
  file type" reason, which is honest but is not analysis.
- **Cross-hunk reasoning.** Each hunk is judged alone. A change that is
  individually verified in two places can still be wrong in combination, and
  nothing here will notice.
- **Incremental caching.** Every run re-derives coverage from scratch. Caching
  by content hash is straightforward and not implemented.
- **A fitted model in the box.** The fitter exists and refuses small samples,
  which means shipping fitted weights requires evaluation runs across many real
  PRs. That data does not exist yet, so the priors are still what ships.
- **Stated invariants.** The crash check asks only "does it blow up". A
  `# triage: invariant: result >= 0` annotation would let the same generator
  check properties the author cares about, which is the natural next step and
  the one that would catch wrong-requirement bugs like the `overtime_pay` miss
  -- but only if someone writes the invariant down, which is the whole
  difficulty.
- **SZZ refinements.** Ignoring cosmetic-only commits when blaming, and
  weighting by how many independent fixes implicate the same commit, are both
  cheap and both unimplemented.
