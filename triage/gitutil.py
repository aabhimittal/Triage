"""Thin wrapper over the git CLI. No GitPython dependency."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def merge_base(repo: Path, base: str, head: str = "HEAD") -> str:
    return git(repo, "merge-base", base, head).strip()


def diff(repo: Path, base: str, head: str = "HEAD", context: int = 3) -> str:
    """Diff of base..head using the merge base, so we see only head's work."""
    try:
        anchor = merge_base(repo, base, head)
    except GitError:
        anchor = base
    return git(repo, "diff", f"-U{context}", "--no-color", f"{anchor}..{head}")


def show(repo: Path, rev: str, path: str) -> str | None:
    """File contents at a revision, or None if it did not exist there."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def churn(repo: Path, path: str, since_commits: int = 200) -> int:
    """How many of the last N commits touched this file. A decent risk prior."""
    out = git(
        repo, "log", f"-{since_commits}", "--format=%H", "--", path, check=False
    )
    return len([ln for ln in out.splitlines() if ln.strip()])


def head_sha(repo: Path, rev: str = "HEAD") -> str:
    return git(repo, "rev-parse", rev).strip()[:12]


_REFACTOR_SUBJECT = re.compile(r"^(refactor|style)\b", re.I)


def refactor_lines(repo: Path, base: str, head: str = "HEAD") -> set[tuple[str, int]]:
    """New-file (path, lineno) pairs introduced by commits declaring a refactor.

    This is how a hunk earns the REFACTOR label: the author said so, in the
    commit subject, before knowing we would check. We then hold them to it with
    the equivalence check rather than taking the claim on trust.
    """
    try:
        anchor = merge_base(repo, base, head)
    except GitError:
        anchor = base
    revs = git(repo, "log", "--format=%H%x00%s", f"{anchor}..{head}", check=False)
    out: set[tuple[str, int]] = set()
    for line in revs.splitlines():
        sha, _, subject = line.partition("\x00")
        if not sha or not _REFACTOR_SUBJECT.match(subject.strip()):
            continue
        patch = git(repo, "show", "--unified=0", "--format=", sha, check=False)
        out |= _added_positions(patch)
    return out


def _added_positions(patch: str) -> set[tuple[str, int]]:
    positions: set[tuple[str, int]] = set()
    path = ""
    lineno = 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = "" if path == "/dev/null" else path[2:] if path.startswith("b/") else path
        elif line.startswith("@@"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if path:
                positions.add((path, lineno))
            lineno += 1
        elif not line.startswith("-"):
            lineno += 1
    return positions
