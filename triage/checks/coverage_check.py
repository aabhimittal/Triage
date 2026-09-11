"""Check 1 - Covered? Do the tests actually execute these lines?

The weakest of the three checks, and the cheapest. Its real job is to be a
*gate*: an uncovered line cannot be constrained by mutation testing either
(nothing observes the mutant), so failing here short-circuits the expensive
work. Coverage alone is never sufficient for a VERIFIED verdict -- executing a
line proves only that it ran, not that anything asserted on the result.
"""

from __future__ import annotations

from triage.model import CheckResult, Hunk, Status
from triage.runner import CoverageIndex

NAME = "covered"


def check(hunk: Hunk, index: CoverageIndex, threshold: float = 1.0) -> CheckResult:
    linenos = hunk.added_linenos
    if not index.is_measured(hunk.path):
        return CheckResult(
            NAME, Status.ABSTAIN,
            "file is outside the measured source set; coverage says nothing",
            {"path": hunk.path},
        )
    executable = index.executable(hunk.path, linenos)
    if not executable:
        return CheckResult(NAME, Status.SKIP, "no executable added lines", {})

    uncovered = index.uncovered(hunk.path, linenos)
    ratio = 1.0 - (len(uncovered) / len(executable))
    tests = index.tests_for(hunk.path, executable)
    evidence = {
        "executable_lines": len(executable),
        "uncovered_lines": uncovered,
        "ratio": round(ratio, 4),
        "covering_tests": sorted(tests)[:20],
        "covering_test_count": len(tests),
    }
    if ratio + 1e-9 < threshold:
        return CheckResult(
            NAME, Status.FAIL,
            f"{len(uncovered)}/{len(executable)} added lines never execute "
            f"(lines {_fmt(uncovered)})",
            evidence,
        )
    return CheckResult(
        NAME, Status.PASS,
        f"all {len(executable)} executable lines run under {len(tests)} test(s)",
        evidence,
    )


def _fmt(lines: list[int], limit: int = 8) -> str:
    shown = ", ".join(str(n) for n in lines[:limit])
    return shown + (f", +{len(lines) - limit} more" if len(lines) > limit else "")
