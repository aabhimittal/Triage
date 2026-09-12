"""Mining real bugs out of git history."""

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import git
from triage.config import Config
from triage.history import (
    FIX_PATTERN,
    find_fix_commits,
    fixed_lines,
    inducing_commits,
    study,
)


def test_fix_pattern_matches_the_usual_conventions():
    for subject in ["fix: off by one", "Fixes #12", "hotfix for crash",
                    "bug in parser", "regression from #4"]:
        assert FIX_PATTERN.search(subject), subject
    for subject in ["feat: add slots", "refactor: tidy", "docs: readme"]:
        assert not FIX_PATTERN.search(subject), subject


def test_fixed_lines_takes_what_the_fix_removed_not_what_it_added(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "m.py").write_text("a = 1\nb = 2\nc = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    (repo / "m.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix: b was wrong")

    sha = git(repo, "rev-parse", "HEAD").strip()
    # Line 2 was replaced; line 4 was added and so has no buggy ancestor.
    assert fixed_lines(repo, sha) == {"m.py": {2}}


def test_blame_finds_the_commit_that_introduced_the_bug(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    (repo / "m.py").write_text("def f():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    (repo / "m.py").write_text("def f():\n    return 1\n\n\ndef g(n):\n    return n + 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feat: add g")
    guilty = git(repo, "rev-parse", "HEAD").strip()
    (repo / "m.py").write_text("def f():\n    return 1\n\n\ndef g(n):\n    return n + 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix: g was off by one")
    fix = git(repo, "rev-parse", "HEAD").strip()

    (induced,) = inducing_commits(repo, fix, "fix: g was off by one")
    assert induced.sha == guilty
    assert induced.lines == {"m.py": {6}}
    assert induced.subject == "feat: add g"


def test_find_fix_commits_skips_merges(tmp_path, tiny_repo):
    git(tiny_repo, "checkout", "-qb", "side")
    (tiny_repo / "lib.py").write_text("def double(n: int) -> int:\n    return n * 2\n\n")
    git(tiny_repo, "commit", "-qam", "fix: trailing newline")
    git(tiny_repo, "checkout", "-q", "main")
    git(tiny_repo, "merge", "-q", "--no-ff", "-m", "Merge fix branch", "side")

    subjects = [s for _, s in find_fix_commits(tiny_repo, 50)]
    assert "fix: trailing newline" in subjects
    assert "Merge fix branch" not in subjects


@pytest.mark.slow
def test_study_replays_a_real_bug_and_reports_the_verdict(tiny_repo):
    """A bug introduced untested, then fixed, must come back as CAUGHT."""
    (tiny_repo / "lib.py").write_text(
        "def double(n: int) -> int:\n"
        "    return n * 2\n"
        "\n"
        "\n"
        "def slots(items: int, per: int) -> int:\n"
        "    return items // per + 1\n"
    )
    git(tiny_repo, "add", "-A")
    git(tiny_repo, "commit", "-qm", "feat: slots")
    guilty = git(tiny_repo, "rev-parse", "HEAD").strip()

    (tiny_repo / "lib.py").write_text(
        "def double(n: int) -> int:\n"
        "    return n * 2\n"
        "\n"
        "\n"
        "def slots(items: int, per: int) -> int:\n"
        "    return -(-items // per)\n"
    )
    git(tiny_repo, "add", "-A")
    git(tiny_repo, "commit", "-qm", "fix: slots was off by one")

    report = study(tiny_repo, Config.load(tiny_repo), scan=50, max_bugs=2)
    assert report.fix_commits == 1
    assert report.candidates == 1
    outcome = report.outcomes[0]
    assert outcome.inducing.sha == guilty
    assert outcome.caught is True, outcome.reason
