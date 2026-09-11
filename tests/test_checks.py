from triage.checks import equivalence_check
from triage.checks.coverage_check import check as coverage
from triage.config import Config
from triage.model import Hunk, Status
from triage.runner import CoverageIndex, context_to_node_id

OLD = """def norm(values: list[float], scale: float) -> list[float]:
    out = []
    for value in values:
        out.append(value / scale if scale else 0.0)
    return out
"""


def hunk(path="m.py", added=None):
    added = added or {2: "    return 1"}
    return Hunk(path, "", min(added), max(added), added, {})


def index_with(covered, statements, tests=None):
    return CoverageIndex(
        covered={"m.py": set(covered)},
        statements={"m.py": set(statements)},
        tests_by_line={"m.py": tests or {}},
        all_tests={t for ts in (tests or {}).values() for t in ts},
    )


def test_coverage_fails_on_a_partially_covered_hunk():
    r = coverage(hunk(added={1: "a", 2: "b"}), index_with([1], [1, 2]))
    assert r.status is Status.FAIL and r.evidence["uncovered_lines"] == [2]


def test_coverage_abstains_on_an_unmeasured_file():
    r = coverage(hunk(path="other.py"), index_with([1], [1]))
    assert r.status is Status.ABSTAIN


def test_coverage_skips_when_nothing_executable_changed():
    assert coverage(hunk(added={9: "# comment"}), index_with([1], [1])).status is Status.SKIP


def test_equivalence_passes_on_a_real_refactor():
    new = OLD.replace(
        "    out = []\n    for value in values:\n        out.append(value / scale if scale else 0.0)\n    return out",
        "    return [value / scale if scale else 0.0 for value in values]",
    )
    r = equivalence_check.check(hunk(added={2: "x"}), OLD, new, Config())
    assert r.status is Status.PASS, r.detail


def test_equivalence_catches_a_boundary_only_difference():
    new = OLD.replace("if scale else 0.0", "if scale > 1 else 0.0")
    r = equivalence_check.check(hunk(added={4: "x"}), OLD, new, Config())
    assert r.status is Status.FAIL and "counterexample" in r.evidence


def test_equivalence_abstains_rather_than_guessing_on_impure_code():
    old = "def save(path: str) -> int:\n    with open(path) as fh:\n        return len(fh.read())\n"
    new = "def save(path: str) -> int:\n    with open(path) as fh:\n        return len(fh.readlines())\n"
    r = equivalence_check.check(hunk(added={3: "x"}), old, new, Config())
    assert r.status is Status.ABSTAIN and r.evidence["purity_reasons"]


def test_equivalence_flags_a_changed_signature_as_not_a_refactor():
    new = OLD.replace("(values: list[float], scale: float)", "(values: list[float], factor: float)")
    new = new.replace("scale", "factor").replace("def norm(values: list[float], factor: float)",
                                                 "def norm(values: list[float], factor: float)")
    r = equivalence_check.check(hunk(added={1: "x"}), OLD, new, Config())
    assert r.status is Status.FAIL and "signature" in r.detail


def test_context_names_resolve_to_runnable_node_ids():
    modules = {"tests.test_a": "tests/test_a.py", "test_a": "tests/test_a.py"}
    assert context_to_node_id("test_a.TestX.test_y", modules) == "tests/test_a.py::TestX::test_y"
    assert context_to_node_id("tests.test_a.test_y", modules) == "tests/test_a.py::test_y"
    assert context_to_node_id("", modules) is None


def test_mutation_check_respects_an_exhausted_budget_without_running_anything(tmp_path):
    """The budget must bind before any subprocess is spawned, not after."""
    from triage.budget import Budget
    from triage.checks.mutation_check import check as mutation

    spent = Budget(total_seconds=10.0, spent=10.0)
    result = mutation(hunk(added={1: "x = 1"}), tmp_path, index_with([1], [1]),
                      Config(), spent)
    assert result.status is Status.ABSTAIN
    assert "budget" in result.detail


def test_sanity_guard_result_is_reused_across_hunks(tmp_path, monkeypatch):
    from triage import checks
    from triage.budget import Budget
    from triage.checks import mutation_check

    calls = []

    def fake_run(workdir, cfg, node_ids=None, timeout=None, env=None):
        calls.append(tuple(node_ids or ()))
        from triage.runner import TestRunResult
        return TestRunResult(ok=False, returncode=1, duration=0.01)

    monkeypatch.setattr(mutation_check, "run_tests", fake_run)
    (tmp_path / "m.py").write_text("def f(a: int) -> int:\n    return a + 1\n")
    idx = index_with([1, 2], [1, 2], {2: {"tests/t.py::test_a"}})
    cache: dict = {}
    for _ in range(3):
        r = mutation_check.check(hunk(added={2: "    return a + 1"}), tmp_path, idx,
                                 Config(), Budget(60.0), cache)
        assert r.status is Status.ABSTAIN
    assert len(calls) == 1, "the sanity run should be memoised per test selection"
