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
the only check that can verify a hunk on its own, because behavioural
equivalence makes the coverage question moot: if nothing observable changed,
there is nothing for a reviewer to find.

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

The weights are hand-set priors, stated as data in `triage/risk.py` and
overridable from JSON. **Do not read the absolute numbers as probabilities.**
The ordering is the product. `triage eval` produces the labelled data
(`escaped` per injected bug) needed to fit them properly; that fit is not done
here, and claiming otherwise would be the kind of unearned precision this tool
exists to avoid.

## 8. Evaluation

The experiment in one line: *only bugs that survive CI are review problems, so
measure what fraction of those TRIAGE routes to a human.*

```
for each candidate bug planted in the diff:
    run the FULL suite
    suite fails -> CI caught it; discard, it was never a review problem
    suite passes -> a human was the last defence. Did it land in the
                    residual set (caught) or a verified hunk (ESCAPED)?
```

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

## 9. What is deliberately missing

- **Test-code verification.** A weakened assertion still passes the suite that
  contains it, so test edits are always residual. A real check exists — run the
  new test against the *old* implementation and require it to fail — and would
  be the highest-value next addition.
- **Fitted risk weights.** The harness produces the labels; the fit is not done.
- **Languages other than Python.** The coverage-and-impact layer generalises;
  the mutation operators and the purity analyser do not.
- **Cross-hunk reasoning.** Each hunk is judged alone. A change that is
  individually verified in two places can still be wrong in combination, and
  nothing here will notice.
- **Incremental caching.** Every run re-derives coverage from scratch. Caching
  by content hash is straightforward and not implemented.
