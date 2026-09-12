"""Combining check results into a single verdict per hunk.

This is the whole safety argument of the system, so it is deliberately a small
pure function with no I/O: it is the thing you should read first and test
hardest.

    VERIFIED  every applicable check actively PASSED
    RESIDUAL  anything else -- a FAIL, an ABSTAIN, a red suite, a label we
              cannot verify by running code
    EXEMPT    no executable content at all

The asymmetry is the point. FAIL and ABSTAIN land in the same bucket because
"the tests do not constrain this" and "we could not tell whether the tests
constrain this" have identical consequences for a reviewer, even though only
one of them is a defect signal.
"""

from __future__ import annotations

from triage.model import CheckResult, Hunk, HunkVerdict, Label, Status, Verdict

_SUITE_RED = (
    "the test suite does not pass at head, so no test-based evidence means "
    "anything; every hunk is residual until it is green"
)


def decide(
    hunk: Hunk,
    checks: list[CheckResult],
    suite_green: bool = True,
) -> HunkVerdict:
    if not suite_green:
        return HunkVerdict(
            hunk,
            Verdict.RESIDUAL,
            checks + [CheckResult("suite", Status.FAIL, _SUITE_RED)],
        )

    if hunk.label is Label.DOCS:
        return HunkVerdict(hunk, Verdict.EXEMPT, checks)

    if hunk.label is Label.CONFIG:
        return HunkVerdict(
            hunk, Verdict.RESIDUAL,
            checks + [CheckResult(
                "scope", Status.ABSTAIN,
                "configuration change: it has no executable semantics here, so "
                "no check applies and it needs eyes",
            )],
        )

    if hunk.label is Label.TEST:
        # A test cannot be verified by the suite that contains it, but it can be
        # run against the pre-change implementation. A new test that fails there
        # demonstrably constrains what the PR did; anything less stays residual.
        effective = _get(checks, "effective")
        if effective is not None and effective.status is Status.PASS:
            return HunkVerdict(hunk, Verdict.VERIFIED, checks)
        if effective is not None and effective.status is Status.FAIL:
            return HunkVerdict(hunk, Verdict.RESIDUAL, checks)
        return HunkVerdict(
            hunk, Verdict.RESIDUAL,
            checks + [CheckResult(
                "scope", Status.ABSTAIN,
                "test code cannot verify itself: a weakened assertion still "
                "passes the suite that contains it",
            )],
        )

    if hunk.is_deleted_file or not hunk.added_lines:
        return HunkVerdict(
            hunk, Verdict.RESIDUAL,
            checks + [CheckResult(
                "scope", Status.ABSTAIN,
                "deletion: nothing was added to exercise, and the tests cannot "
                "tell you whether the removed code had callers you missed",
            )],
        )

    covered = _get(checks, "covered")
    constrained = _get(checks, "constrained")
    equivalent = _get(checks, "equivalent")

    if _all_skipped(checks):
        return HunkVerdict(hunk, Verdict.EXEMPT, checks)

    # A refactor proved equivalent on generated inputs needs no coverage
    # argument: if the observable behaviour is unchanged, there is nothing for
    # a reviewer to find. This is the one route to VERIFIED that a single check
    # can open on its own, and it is available only to hunks whose author
    # claimed behaviour preservation in the first place.
    if hunk.label is Label.REFACTOR and equivalent is not None:
        if equivalent.status is Status.PASS:
            return HunkVerdict(hunk, Verdict.VERIFIED, checks)
        if equivalent.status is Status.FAIL:
            return HunkVerdict(hunk, Verdict.RESIDUAL, checks)

    required = [c for c in (covered, constrained) if c is not None and c.status is not Status.SKIP]
    if not required:
        return HunkVerdict(hunk, Verdict.RESIDUAL, checks)
    if all(c.status is Status.PASS for c in required):
        return HunkVerdict(hunk, Verdict.VERIFIED, checks)
    return HunkVerdict(hunk, Verdict.RESIDUAL, checks)


def _get(checks: list[CheckResult], name: str) -> CheckResult | None:
    for c in checks:
        if c.name == name:
            return c
    return None


def _all_skipped(checks: list[CheckResult]) -> bool:
    return bool(checks) and all(c.status is Status.SKIP for c in checks)
