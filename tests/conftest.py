import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A two-commit repo: one well-tested change, one untested one."""
    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "lib.py").write_text(
        "def double(n: int) -> int:\n"
        "    return n * 2\n"
    )
    (repo / "tests" / "test_lib.py").write_text(
        "from lib import double\n"
        "\n"
        "def test_double():\n"
        "    assert double(3) == 6\n"
        "    assert double(0) == 0\n"
        "    assert double(-2) == -4\n"
    )
    (repo / ".triage.toml").write_text(
        '[triage]\n'
        'test_command = ["pytest", "-q", "-p", "no:cacheprovider"]\n'
        'source_roots = ["."]\n'
        'test_paths = ["tests"]\n'
        'mutation_budget_seconds = 90.0\n'
        'max_mutants_per_hunk = 6\n'
    )
    git(repo.parent, "init", "-q", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "-M", "main")
    return repo
