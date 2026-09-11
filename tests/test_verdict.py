"""The decision table. If any of these flip, the safety argument is gone."""

import pytest

from triage.model import CheckResult, Hunk, Label, Status, Verdict
from triage.verdict import decide


def hunk(label=Label.BEHAVIORAL, **kw) -> Hunk:
    return Hunk(
        path=kw.pop("path", "m.py"), header="", new_start=1, new_end=1,
        added_lines=kw.pop("added", {1: "    return 1"}), removed_lines={},
        label=label, **kw,
    )


def result(name, status):
    return CheckResult(name, status, "detail")


@pytest.mark.parametrize("status", [Status.FAIL, Status.ABSTAIN])
def test_a_failed_or_inconclusive_check_never_verifies(status):
    """ABSTAIN must behave exactly like FAIL. This is the whole design."""
    v = decide(hunk(), [result("covered", Status.PASS), result("constrained", status)])
    assert v.verdict is Verdict.RESIDUAL


def test_both_checks_passing_verifies():
    v = decide(hunk(), [result("covered", Status.PASS), result("constrained", Status.PASS)])
    assert v.verdict is Verdict.VERIFIED


def test_a_red_suite_makes_everything_residual():
    checks = [result("covered", Status.PASS), result("constrained", Status.PASS)]
    v = decide(hunk(), checks, suite_green=False)
    assert v.verdict is Verdict.RESIDUAL
    assert any("does not pass" in r for r in v.reasons)


def test_proved_refactor_verifies_without_a_coverage_argument():
    v = decide(
        hunk(Label.REFACTOR),
        [result("covered", Status.FAIL), result("constrained", Status.ABSTAIN),
         result("equivalent", Status.PASS)],
    )
    assert v.verdict is Verdict.VERIFIED


def test_disproved_refactor_is_residual_even_with_perfect_tests():
    v = decide(
        hunk(Label.REFACTOR),
        [result("covered", Status.PASS), result("constrained", Status.PASS),
         result("equivalent", Status.FAIL)],
    )
    assert v.verdict is Verdict.RESIDUAL


def test_test_code_is_never_self_verifying():
    v = decide(hunk(Label.TEST), [result("covered", Status.PASS),
                                  result("constrained", Status.PASS)])
    assert v.verdict is Verdict.RESIDUAL


def test_config_changes_are_residual_and_docs_are_exempt():
    assert decide(hunk(Label.CONFIG), []).verdict is Verdict.RESIDUAL
    assert decide(hunk(Label.DOCS), []).verdict is Verdict.EXEMPT


def test_deletions_are_residual():
    v = decide(hunk(added={}), [])
    assert v.verdict is Verdict.RESIDUAL


def test_no_checks_at_all_is_not_a_pass():
    assert decide(hunk(), []).verdict is Verdict.RESIDUAL
