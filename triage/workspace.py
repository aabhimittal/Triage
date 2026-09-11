"""An isolated copy of the repo that mutation runs are allowed to damage.

Mutation testing edits source in place. Doing that in the user's working tree
is how you lose someone's uncommitted work, so every destructive operation
happens in a throwaway copy instead.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".tox", "build", "dist", ".triage-cache",
)


@contextmanager
def scratch_copy(repo: Path, prefix: str = "triage-") -> Iterator[Path]:
    repo = Path(repo).resolve()
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    dest = tmp / (repo.name or "repo")
    try:
        shutil.copytree(repo, dest, ignore=_IGNORE, symlinks=True)
        yield dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def patched_file(path: Path, new_text: str) -> Iterator[None]:
    """Swap a file's contents, guaranteeing restoration."""
    original = path.read_bytes()
    try:
        path.write_text(new_text)
        yield
    finally:
        path.write_bytes(original)
