"""End to end, against a real git repo and a real pytest run.

Slower than the unit tests (a few seconds) and worth every second: almost every
serious bug found while building this tool -- the test-impact map silently
returning nothing, mutants "dying" from an ImportError rather than from the
mutation -- was invisible to unit tests and obvious here.
"""

from pathlib import Path

import pytest

from tests.conftest import git
from triage.config import Config
from triage.model import Verdict
from triage.pipeline import run
from triage.report import to_markdown, to_text


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


@pytest.fixture
def triaged(tiny_repo: Path):
    git(tiny_repo, "checkout", "-qb", "feature")
    (tiny_repo / "lib.py").write_text(
        "def double(n: int) -> int:\n"
        "    return n * 2\n"
        "\n"
        "\n"
        "def triple(n: int) -> int:\n"
        "    return n * 3\n"
        "\n"
        "\n"
        "def quadruple(n: int) -> int:\n"
        "    return n * 4\n"
    )
    (tiny_repo / "tests" / "test_lib.py").write_text(
        "from lib import double, triple\n"
        "\n"
        "def test_double():\n"
        "    assert double(3) == 6\n"
        "    assert double(0) == 0\n"
        "    assert double(-2) == -4\n"
        "\n"
        "def test_triple():\n"
        "    assert triple(3) == 9\n"
        "    assert triple(0) == 0\n"
        "    assert triple(-2) == -6\n"
    )
    _commit(tiny_repo, "feat: triple and quadruple")
    return run(tiny_repo, "main", cfg=Config.load(tiny_repo))


def test_well_tested_code_is_verified_and_untested_code_is_not(triaged):
    assert triaged.suite_green
    by_line = {v.hunk.new_start: v for v in triaged.verdicts if v.hunk.path == "lib.py"}
    triple = by_line[5]
    quadruple = by_line[9]
    assert triple.verdict is Verdict.VERIFIED, triple.reasons
    assert quadruple.verdict is Verdict.RESIDUAL
    assert any("never execute" in r for r in quadruple.reasons)


def test_the_residual_set_is_smaller_than_the_diff(triaged):
    assert triaged.stats.verified_lines > 0
    assert 0 < triaged.stats.verified_fraction < 1


def test_test_impact_selection_runs_a_subset_not_the_suite(triaged):
    constrained = next(
        v.check("constrained") for v in triaged.verdicts
        if v.hunk.path == "lib.py" and v.hunk.new_start == 5
    )
    assert constrained.evidence["tests_selected"]
    assert max(constrained.evidence["tests_selected"]) < triaged.stats.tests_in_suite


def test_reports_render(triaged):
    md = to_markdown(triaged)
    assert "machine-verified" in md and "lib.py" in md
    assert "does not mean" in md  # the limitations block is not optional
    assert to_text(triaged).startswith("TRIAGE")


def test_a_red_suite_blocks_every_verification(tiny_repo: Path):
    git(tiny_repo, "checkout", "-qb", "broken")
    (tiny_repo / "lib.py").write_text("def double(n: int) -> int:\n    return n * 3\n")
    _commit(tiny_repo, "feat: break it")
    report = run(tiny_repo, "main", cfg=Config.load(tiny_repo))
    assert not report.suite_green
    assert report.verified == []
    assert "SUITE RED" in to_text(report)
