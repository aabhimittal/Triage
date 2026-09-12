"""Orchestration: PR diff in, residual set out."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from triage import gitutil
from triage.budget import Budget
from triage.checks import (
    coverage_check,
    equivalence_check,
    mutation_check,
    test_effect_check,
)
from triage.classify import classify, excluded, has_executable_change
from triage.config import Config
from triage.diffparse import parse_diff
from triage.model import CheckResult, Hunk, HunkVerdict, Label, Status, Verdict
from triage.risk import RiskModel, features
from triage.runner import CoverageIndex, collect_coverage
from triage.verdict import decide
from triage.workspace import scratch_copy


@dataclass
class Stats:
    changed_lines: int = 0
    executable_lines: int = 0
    verified_lines: int = 0
    residual_lines: int = 0
    exempt_lines: int = 0
    hunks: int = 0
    verified_hunks: int = 0
    mutants_evaluated: int = 0
    tests_in_suite: int = 0
    test_selections: list[int] = field(default_factory=list)
    wall_seconds: float = 0.0
    budget_spent: float = 0.0

    @property
    def verified_fraction(self) -> float:
        denom = self.verified_lines + self.residual_lines
        return self.verified_lines / denom if denom else 0.0

    @property
    def mean_tests_per_mutant(self) -> float:
        return (
            sum(self.test_selections) / len(self.test_selections)
            if self.test_selections else 0.0
        )

    @property
    def impact_selection_factor(self) -> float:
        """How much cheaper mutation runs got by selecting tests. Higher = better."""
        mean = self.mean_tests_per_mutant
        return self.tests_in_suite / mean if mean else 0.0


@dataclass
class TriageReport:
    repo: str
    base: str
    head: str
    verdicts: list[HunkVerdict]
    stats: Stats
    suite_green: bool = True
    suite_output: str = ""

    @property
    def residual(self) -> list[HunkVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.RESIDUAL]

    @property
    def verified(self) -> list[HunkVerdict]:
        return [v for v in self.verdicts if v.verdict is Verdict.VERIFIED]

    def to_dict(self) -> dict:
        s = self.stats
        return {
            "repo": self.repo,
            "base": self.base,
            "head": self.head,
            "suite_green": self.suite_green,
            "stats": {
                "changed_lines": s.changed_lines,
                "executable_lines": s.executable_lines,
                "verified_lines": s.verified_lines,
                "residual_lines": s.residual_lines,
                "exempt_lines": s.exempt_lines,
                "verified_fraction": round(s.verified_fraction, 4),
                "hunks": s.hunks,
                "verified_hunks": s.verified_hunks,
                "mutants_evaluated": s.mutants_evaluated,
                "tests_in_suite": s.tests_in_suite,
                "mean_tests_per_mutant": round(s.mean_tests_per_mutant, 2),
                "impact_selection_factor": round(s.impact_selection_factor, 1),
                "wall_seconds": round(s.wall_seconds, 1),
            },
            "hunks": [v.to_dict() for v in self.verdicts],
        }


def run(
    repo: Path,
    base: str,
    head: str = "HEAD",
    cfg: Config | None = None,
    diff_text: str | None = None,
) -> TriageReport:
    repo = Path(repo).resolve()
    cfg = cfg or Config.load(repo)
    started = time.time()

    diff_text = diff_text if diff_text is not None else gitutil.diff(repo, base, head)
    hunks = [h for h in parse_diff(diff_text) if not excluded(h.path, cfg.exclude_globs)]
    declared = _refactor_oracle(repo, base, head, diff_text)
    for hunk in hunks:
        hunk.label = classify(hunk, declared_refactor=declared)

    stats = Stats(hunks=len(hunks))
    stats.changed_lines = sum(len(h.added_lines) for h in hunks)

    with scratch_copy(repo) as workdir:
        index = collect_coverage(workdir, cfg)
        suite_green = bool(index.baseline and index.baseline.ok)
        stats.tests_in_suite = len(index.all_tests)
        budget = Budget(cfg.mutation_budget_seconds)
        model = RiskModel.load(cfg.risk_weights_path)

        base_sources = _base_sources(repo, base, hunks)
        verdicts: list[HunkVerdict] = []
        sanity_cache: dict[tuple[str, ...], bool] = {}
        for hunk in hunks:
            checks = _run_checks(
                hunk, workdir, repo, base, index, cfg, budget, suite_green,
                sanity_cache, base_sources,
            )
            hv = decide(hunk, checks, suite_green=suite_green)
            hv.risk_features = features(hunk, checks, churn_count=gitutil.churn(repo, hunk.path))
            hv.risk = model.score(hv.risk_features)
            verdicts.append(hv)
            _accumulate(stats, hv, index)

        stats.budget_spent = budget.spent

    stats.wall_seconds = time.time() - started
    verdicts.sort(key=lambda v: (v.verdict is not Verdict.RESIDUAL, -v.risk))
    return TriageReport(
        repo=str(repo), base=base, head=head, verdicts=verdicts, stats=stats,
        suite_green=suite_green,
        suite_output="" if suite_green else (index.baseline.output if index.baseline else ""),
    )


def _base_sources(repo: Path, base: str, hunks: list[Hunk]) -> dict[str, str | None]:
    """Pre-change content of every non-test Python file the diff touches.

    This is what the test-effectiveness check reverts to. Restricted to Python
    source: reverting a lockfile or CI config alongside it would change what is
    being asked from "does this test detect the code change" to something much
    vaguer.
    """
    try:
        anchor = gitutil.merge_base(repo, base)
    except gitutil.GitError:
        anchor = base
    out: dict[str, str | None] = {}
    for hunk in hunks:
        if hunk.label is Label.TEST or not hunk.path.endswith(".py"):
            continue
        if hunk.path not in out:
            out[hunk.path] = gitutil.show(repo, anchor, hunk.path)
    return out


def _refactor_oracle(repo: Path, base: str, head: str, diff_text: str | None):
    """A hunk is a declared refactor if its author said so in a commit subject."""
    try:
        positions = gitutil.refactor_lines(repo, base, head)
    except gitutil.GitError:
        positions = set()

    def declared(hunk: Hunk) -> bool:
        if not positions:
            return False
        lines = [(hunk.path, ln) for ln in hunk.added_linenos]
        hits = sum(1 for key in lines if key in positions)
        return bool(lines) and hits / len(lines) >= 0.5

    return declared


def _run_checks(
    hunk: Hunk,
    workdir: Path,
    repo: Path,
    base: str,
    index: CoverageIndex,
    cfg: Config,
    budget: Budget,
    suite_green: bool,
    sanity_cache: dict[tuple[str, ...], bool] | None = None,
    base_sources: dict[str, str | None] | None = None,
) -> list[CheckResult]:
    if hunk.label in (Label.DOCS, Label.CONFIG) or not has_executable_change(hunk):
        return []
    if not suite_green:
        return []  # every check downstream would be measuring a broken baseline

    if not hunk.path.endswith(".py"):
        # Said explicitly rather than left to fall out of "coverage found
        # nothing". Non-Python files are residual by design here, because the
        # analysers that could verify them do not exist, and a reader deserves
        # that reason instead of a misleading coverage message.
        suffix = Path(hunk.path).suffix or "(no extension)"
        return [CheckResult(
            "scope", Status.ABSTAIN,
            f"no analyser for {suffix} files: coverage, mutation and equivalence "
            "are Python-only here, so nothing about this change can be machine-checked",
            {"path": hunk.path},
        )]

    if hunk.label is Label.TEST:
        # Mutating test code is meaningless -- killing a mutant in an assertion
        # would require some *other* test to notice, which is not what the suite
        # is for. The one answerable question is whether the test detects this
        # PR's change at all.
        return [test_effect_check.check(
            hunk, workdir, base_sources or {},
            gitutil.show(repo, gitutil.merge_base(repo, base), hunk.path), cfg,
        )]

    checks = [coverage_check.check(hunk, index, cfg.coverage_threshold)]
    if checks[0].status is Status.PASS:
        checks.append(
            mutation_check.check(hunk, workdir, index, cfg, budget, sanity_cache)
        )
    else:
        # Skip the expensive check when the cheap gate already failed: an
        # uncovered line cannot be constrained, so the answer is known.
        checks.append(CheckResult(
            "constrained", Status.ABSTAIN,
            "not evaluated: lines are not covered, so no test could kill a mutant",
        ))

    if hunk.label is Label.REFACTOR:
        new_source = (workdir / hunk.path).read_text() if (workdir / hunk.path).exists() else ""
        old_source = gitutil.show(repo, gitutil.merge_base(repo, base), hunk.path)
        checks.append(equivalence_check.check(hunk, old_source, new_source, cfg))
    return checks


def _accumulate(stats: Stats, hv: HunkVerdict, index: CoverageIndex) -> None:
    n_exec = len(index.executable(hv.hunk.path, hv.hunk.added_linenos)) or 0
    n_added = len(hv.hunk.added_lines)
    stats.executable_lines += n_exec
    if hv.verdict is Verdict.VERIFIED:
        stats.verified_hunks += 1
        stats.verified_lines += n_added
    elif hv.verdict is Verdict.RESIDUAL:
        stats.residual_lines += n_added
    else:
        stats.exempt_lines += n_added
    mut = hv.check("constrained")
    if mut is not None:
        stats.mutants_evaluated += int(mut.evidence.get("mutants_evaluated", 0) or 0)
        stats.test_selections.extend(int(n) for n in mut.evidence.get("tests_selected", []))
