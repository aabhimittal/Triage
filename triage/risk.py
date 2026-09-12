"""Ranking the residual set.

TRIAGE's first job is to shrink the review queue. Its second is to *order* what
is left, because a reviewer's attention decays down the page. The score below
estimates "probability this hunk hides a defect a reviewer could catch".

The weights are hand-set priors, not learned parameters, and they are written
here as data rather than buried in code so that `triage eval` can replace them
with fitted values (see docs/DESIGN.md, "Calibration"). Treat the absolute
numbers as meaningless and the *ordering* as the product.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from triage.model import CheckResult, Hunk, Label, Status

SENSITIVE = re.compile(
    r"auth|login|passwd|password|secret|token|credential|crypt|hash|permission|"
    r"privile|admin|sql|query|exec|eval|subprocess|pickle|deserial|session|csrf|"
    r"sanitiz|escape|validate|payment|billing|charge|refund|balance",
    re.I,
)
ERROR_HANDLING = re.compile(r"\b(except|raise|finally|assert|retry|rollback|timeout)\b")
CONCURRENCY = re.compile(r"\b(thread|lock|async|await|mutex|atomic|concurren|queue)\b", re.I)

DEFAULT_WEIGHTS: dict[str, float] = {
    "bias": -2.2,
    # Direct evidence of missing tests. The two strongest signals by design.
    "uncovered_frac": 2.4,
    "mutant_survival": 2.0,
    # A refactor that provably is not one. Rare, and nearly always a real bug.
    "broken_refactor_claim": 3.0,
    # A reproducible crash on an input the signature admits. The only feature
    # here that is evidence about the code rather than about its tests, which
    # is why it outranks everything except a disproved refactor claim.
    "crashes": 2.6,
    # A test demonstrated to detect nothing the change did. Not a bug in the
    # product, but a hole in the evidence the PR claims to be adding, and the
    # one finding a reviewer can act on immediately.
    "ineffective_test": 1.6,
    # We could not run a check at all: absence of evidence, weighted as mild risk.
    "unknown_frac": 0.8,
    # Structural priors.
    "size": 0.9,
    "branchiness": 1.1,
    "new_file": 0.3,
    "public_api": 0.5,
    "churn": 0.6,
    # Domain priors: where a missed bug costs the most.
    "sensitive": 1.3,
    "error_handling": 0.7,
    "concurrency": 1.0,
    # Test-only edits are lower stakes for correctness, but not zero: a
    # weakened assertion is invisible to the suite that runs it.
    "test_code": -0.8,
}


@dataclass
class RiskModel:
    weights: dict[str, float]

    @classmethod
    def load(cls, path: str | None) -> "RiskModel":
        if path and Path(path).exists():
            fitted = json.loads(Path(path).read_text())
            return cls({**DEFAULT_WEIGHTS, **fitted})
        return cls(dict(DEFAULT_WEIGHTS))

    def score(self, features: dict[str, float]) -> float:
        z = self.weights.get("bias", 0.0)
        for name, value in features.items():
            z += self.weights.get(name, 0.0) * value
        return 1.0 / (1.0 + math.exp(-z))


def features(
    hunk: Hunk,
    checks: list[CheckResult],
    churn_count: int = 0,
    churn_scale: int = 20,
) -> dict[str, float]:
    text = "\n".join(hunk.added_lines.values())
    by_name = {c.name: c for c in checks}

    cov = by_name.get("covered")
    uncovered_frac = 0.0
    if cov is not None and cov.status is Status.FAIL:
        uncovered_frac = 1.0 - float(cov.evidence.get("ratio", 0.0))
    elif cov is not None and cov.status is Status.ABSTAIN:
        uncovered_frac = 0.5

    mut = by_name.get("constrained")
    if mut is not None and "ms_delta" in mut.evidence:
        survival = 1.0 - float(mut.evidence["ms_delta"])
    elif mut is not None and mut.status is Status.FAIL:
        survival = 1.0
    elif mut is not None and mut.status is Status.ABSTAIN:
        survival = 0.5
    else:
        survival = 0.0

    equiv = by_name.get("equivalent")
    broken_refactor = 1.0 if (equiv is not None and equiv.status is Status.FAIL) else 0.0

    effective = by_name.get("effective")
    ineffective = 1.0 if (effective is not None and effective.status is Status.FAIL) else 0.0

    crash = by_name.get("crash")
    crashes = 1.0 if (crash is not None and crash.status is Status.FAIL) else 0.0

    inconclusive = [c for c in checks if c.status is Status.ABSTAIN]
    applicable = [c for c in checks if c.status is not Status.SKIP] or [None]
    unknown_frac = len(inconclusive) / len(applicable)

    return {
        "uncovered_frac": uncovered_frac,
        "mutant_survival": survival,
        "broken_refactor_claim": broken_refactor,
        "ineffective_test": ineffective,
        "crashes": crashes,
        "unknown_frac": unknown_frac,
        "size": min(1.0, hunk.size / 50.0),
        "branchiness": _branchiness(text),
        "new_file": 1.0 if hunk.is_new_file else 0.0,
        "public_api": _public_api(text),
        "churn": min(1.0, churn_count / churn_scale),
        "sensitive": 1.0 if SENSITIVE.search(text + " " + hunk.path) else 0.0,
        "error_handling": 1.0 if ERROR_HANDLING.search(text) else 0.0,
        "concurrency": 1.0 if CONCURRENCY.search(text) else 0.0,
        "test_code": 1.0 if hunk.label is Label.TEST else 0.0,
    }


def _branchiness(text: str) -> float:
    """Decision points per added line, capped. A proxy for cyclomatic delta."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    try:
        tree = ast.parse("\n".join(ln.strip() for ln in lines))
        branches = sum(
            isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp,
                           ast.IfExp, ast.ExceptHandler, ast.Match))
            for n in ast.walk(tree)
        )
    except SyntaxError:
        branches = len(re.findall(r"\b(if|for|while|try|except|and|or|elif)\b", text))
    return min(1.0, branches / max(1, len(lines)))


def _public_api(text: str) -> float:
    for match in re.finditer(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", text, re.M):
        if not match.group(1).startswith("_"):
            return 1.0
    return 0.0
