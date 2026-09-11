"""Check 2 - Constrained? Would the tests notice if this code were wrong?

    MS_delta = killed mutants in the diff / viable mutants in the diff

Coverage asks "did this line run". Mutation asks "does anything *depend* on
this line being correct". The gap between those two questions is where most
escaped bugs live: a line executed by a test that asserts nothing about it is
coverage-green and completely unconstrained.

Cost control has three layers, because naive mutation testing is
mutants x full-suite-runtime and nobody can afford that:

  1. mutants are generated only from the diff (see triage.mutate);
  2. each mutant runs only the tests that the coverage context map says touch
     the mutated line -- typically a handful out of thousands;
  3. a shared wall-clock budget stops the run and abstains on the remainder.
"""

from __future__ import annotations

import time
from pathlib import Path

from triage.budget import Budget
from triage.config import Config
from triage.model import CheckResult, Hunk, Status
from triage.mutate import Mutant, generate_mutants
from triage.runner import CoverageIndex, run_tests
from triage.workspace import patched_file

NAME = "constrained"


def check(
    hunk: Hunk,
    workdir: Path,
    index: CoverageIndex,
    cfg: Config,
    budget: Budget,
) -> CheckResult:
    if not hunk.path.endswith(".py"):
        return CheckResult(NAME, Status.ABSTAIN, "mutation operators are Python-only", {})

    file_path = workdir / hunk.path
    if not file_path.exists():
        return CheckResult(NAME, Status.SKIP, "file does not exist at head", {})

    executable = index.executable(hunk.path, hunk.added_linenos)
    if not executable:
        return CheckResult(NAME, Status.SKIP, "no executable added lines", {})

    source = file_path.read_text()
    mutants = generate_mutants(hunk.path, source, set(executable), cfg.max_mutants_per_hunk)
    if len(mutants) < cfg.min_mutants:
        return CheckResult(
            NAME, Status.ABSTAIN,
            f"only {len(mutants)} viable mutant(s); too small a sample to trust",
            {"mutants": len(mutants)},
        )

    # Guard: the selected tests must pass *unmutated*. Without this, any
    # environment problem that makes the subset fail (an import error, an
    # order-dependent fixture, a flaky test) reads as "every mutant killed" and
    # the hunk is stamped VERIFIED on the strength of a broken test run.
    all_impacted = sorted(index.tests_for(hunk.path, executable))
    if all_impacted:
        sanity = run_tests(workdir, cfg, node_ids=all_impacted,
                           timeout=cfg.mutant_timeout_seconds * 2)
        budget.charge(sanity.duration)
        if not sanity.ok:
            return CheckResult(
                NAME, Status.ABSTAIN,
                "the tests covering these lines do not pass when run on their own "
                "(order-dependent, flaky, or an import problem), so a mutant kill "
                "would prove nothing",
                {"selected_tests": all_impacted[:10], "output": sanity.output[-800:]},
            )

    killed: list[str] = []
    survived: list[dict] = []
    tests_selected: list[int] = []
    unreached: list[str] = []
    skipped_for_budget = 0

    for mutant in mutants:
        if budget.exhausted:
            skipped_for_budget = len(mutants) - (len(killed) + len(survived) + len(unreached))
            break
        impacted = sorted(index.tests_for(hunk.path, [mutant.lineno]))
        if not impacted:
            # No test executes this line, so no test can kill the mutant. This
            # is a coverage hole, reported as such rather than as a kill.
            unreached.append(mutant.label)
            continue
        tests_selected.append(len(impacted))
        outcome, elapsed = _run_mutant(workdir, file_path, mutant, impacted, cfg)
        budget.charge(elapsed)
        if outcome:
            killed.append(mutant.label)
        else:
            survived.append(
                {"line": mutant.lineno, "mutation": mutant.label, "tests_run": len(impacted)}
            )

    evaluated = len(killed) + len(survived)
    evidence = {
        "mutants_generated": len(mutants),
        "mutants_evaluated": evaluated,
        "killed": len(killed),
        "survived": survived,
        "unreached": unreached,
        "skipped_for_budget": skipped_for_budget,
        "budget_remaining_s": round(budget.remaining, 1),
        "tests_selected": tests_selected,
    }

    if unreached:
        return CheckResult(
            NAME, Status.FAIL,
            f"{len(unreached)} mutant(s) sit on lines no test reaches",
            evidence,
        )
    if skipped_for_budget or evaluated < cfg.min_mutants:
        return CheckResult(
            NAME, Status.ABSTAIN,
            f"mutation budget exhausted after {evaluated}/{len(mutants)} mutants; "
            "score would be unrepresentative",
            evidence,
        )

    score = len(killed) / evaluated
    evidence["ms_delta"] = round(score, 4)
    if score + 1e-9 < cfg.mutation_threshold:
        worst = ", ".join(s["mutation"] for s in survived[:3])
        return CheckResult(
            NAME, Status.FAIL,
            f"MS_delta={score:.2f} < {cfg.mutation_threshold:.2f}; "
            f"{len(survived)} mutation(s) survive unnoticed: {worst}",
            evidence,
        )
    return CheckResult(
        NAME, Status.PASS,
        f"MS_delta={score:.2f} ({len(killed)}/{evaluated} mutants killed)",
        evidence,
    )


def _run_mutant(
    workdir: Path,
    file_path: Path,
    mutant: Mutant,
    impacted: list[str],
    cfg: Config,
) -> tuple[bool, float]:
    """Returns (killed, seconds). A timeout counts as a kill: an infinite loop
    is a detected behavioural change, not a passing test."""
    started = time.monotonic()
    with patched_file(file_path, mutant.source):
        result = run_tests(
            workdir, cfg, node_ids=impacted, timeout=cfg.mutant_timeout_seconds
        )
    elapsed = time.monotonic() - started
    if result.timed_out:
        return True, elapsed
    if result.no_tests_ran:
        return False, elapsed
    return (not result.ok), elapsed
