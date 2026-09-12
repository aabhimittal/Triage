"""The one check that reports a property of the code rather than of the tests."""

from triage.checks.crash_check import check
from triage.config import Config
from triage.model import CheckResult, Hunk, Label, Status, Verdict
from triage.verdict import decide


def hunk_for(line: int, path: str = "m.py") -> Hunk:
    return Hunk(path, "", line, line, {line: "x"}, {})


SRC = '''def divide(total: int, parts: int) -> int:
    return total // parts


def guarded(total: int, parts: int) -> int:
    if parts == 0:
        raise ValueError("parts must be non-zero")
    return total // parts


def first(xs: list[int]) -> int:
    return xs[0]


def annotated_only_partially(xs, n: int) -> int:
    return n


def writes_a_file(path: str) -> int:
    with open(path) as fh:
        return len(fh.read())


def allowed(xs: list[int]) -> int:  # triage: allow-crash
    return xs[0]
'''


def test_finds_a_real_crash_with_a_counterexample():
    result = check(hunk_for(2), SRC, Config())
    assert result.status is Status.FAIL
    assert result.evidence["exception"] == "ZeroDivisionError"
    assert "parts=0" in result.evidence["counterexample"]


def test_a_deliberate_rejection_is_not_a_crash():
    assert check(hunk_for(8), SRC, Config()).status is Status.PASS


def test_finds_an_unguarded_subscript():
    result = check(hunk_for(12), SRC, Config())
    assert result.status is Status.FAIL and result.evidence["exception"] == "IndexError"


def test_abstains_rather_than_guessing_without_annotations():
    assert check(hunk_for(16), SRC, Config()).status is Status.ABSTAIN


def test_abstains_on_code_it_must_not_execute():
    result = check(hunk_for(20), SRC, Config())
    assert result.status is Status.ABSTAIN


def test_the_suppression_marker_is_honoured():
    assert check(hunk_for(25), SRC, Config()).status is Status.ABSTAIN


def test_a_correct_function_is_not_blamed_for_a_huge_input():
    """Allocating proportionally to your input is arithmetic, not a defect."""
    src = (
        "def spread(total: int, ways: int) -> list[int]:\n"
        "    if ways <= 0:\n"
        "        raise ValueError('ways must be positive')\n"
        "    return [total // ways for _ in range(ways)]\n"
    )
    assert check(hunk_for(4), src, Config()).status is Status.PASS


def _credentials(status: Status = Status.PASS) -> list[CheckResult]:
    return [CheckResult("covered", status, ""), CheckResult("constrained", status, "")]


def test_a_crash_demotes_an_otherwise_verified_hunk():
    checks = _credentials() + [CheckResult("crash", Status.FAIL, "boom", {})]
    assert decide(hunk_for(1), checks).verdict is Verdict.RESIDUAL


def test_a_clean_crash_run_cannot_verify_on_its_own():
    """PASS here is weak evidence and must never substitute for a credential."""
    checks = [CheckResult("covered", Status.FAIL, ""),
              CheckResult("constrained", Status.ABSTAIN, ""),
              CheckResult("crash", Status.PASS, "")]
    assert decide(hunk_for(1), checks).verdict is Verdict.RESIDUAL


def test_abstaining_on_the_veto_costs_nothing():
    checks = _credentials() + [CheckResult("crash", Status.ABSTAIN, "impure", {})]
    assert decide(hunk_for(1), checks).verdict is Verdict.VERIFIED


def test_a_proved_refactor_is_not_blamed_for_a_pre_existing_crash():
    hunk = hunk_for(1)
    hunk.label = Label.REFACTOR
    checks = [
        CheckResult("covered", Status.PASS, ""),
        CheckResult("constrained", Status.ABSTAIN, ""),
        CheckResult("equivalent", Status.PASS, "agrees on 64 inputs"),
        CheckResult("crash", Status.FAIL, "IndexError", {}),
    ]
    assert decide(hunk, checks).verdict is Verdict.VERIFIED
