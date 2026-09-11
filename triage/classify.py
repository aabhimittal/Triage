"""Label hunks so the right checks get applied to the right change.

Labelling matters because the equivalence check is only sound for changes that
*claim* to preserve behaviour. We never infer "this is a refactor" from vibes:
a hunk earns the REFACTOR label only from an explicit author declaration or
from a token-identical rewrite, which is a mechanically checkable property.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from triage.model import Hunk, Label

_TEST_PATTERNS = ("tests/*", "test/*", "*/tests/*", "*_test.py", "test_*.py", "*/test_*.py")
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".json", ".lock"}
_REFACTOR_MARKER = re.compile(r"#\s*triage:\s*refactor", re.I)
_REFACTOR_SUBJECT = re.compile(r"^(refactor|style|chore\(refactor\))[(:]", re.I)


def classify(
    hunk: Hunk,
    declared_refactor: Callable[[Hunk], bool] | None = None,
) -> Label:
    path = hunk.path
    suffix = Path(path).suffix
    if any(fnmatch(path, p) for p in _TEST_PATTERNS):
        return Label.TEST
    if suffix in _DOC_SUFFIXES:
        return Label.DOCS
    if suffix in _CONFIG_SUFFIXES:
        return Label.CONFIG
    if suffix == ".py" and not has_executable_change(hunk):
        return Label.DOCS
    if _REFACTOR_MARKER.search("\n".join(hunk.added_lines.values())):
        return Label.REFACTOR
    if token_identical(hunk):
        return Label.REFACTOR
    if declared_refactor is not None and declared_refactor(hunk):
        return Label.REFACTOR
    return Label.BEHAVIORAL


def has_executable_change(hunk: Hunk) -> bool:
    """True if any added line contributes executable Python.

    Comment-only, blank-only and docstring-only edits are not verifiable by
    running tests, and pretending otherwise inflates the headline number.
    """
    return any(_is_executable_line(text) for text in hunk.added_lines.values())


def _is_executable_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return False
    # A line that is only a string literal is a docstring or a no-op expression.
    if re.fullmatch(r"""(['"]{1,3}).*""", stripped) and not re.search(r"[=(\[]", stripped):
        return False
    return True


def token_identical(hunk: Hunk) -> bool:
    """Removed and added lines tokenize to the same multiset (modulo layout).

    Catches reformatting and pure line-moves. Not a proof of equivalence — a
    reordering of two statements is token-identical and can change behaviour —
    so this only *proposes* the REFACTOR label, which then has to survive the
    equivalence check like any other refactor claim.
    """
    if not hunk.added_lines or not hunk.removed_lines:
        return False
    added = _token_multiset("\n".join(hunk.added_lines.values()))
    removed = _token_multiset("\n".join(hunk.removed_lines.values()))
    return added is not None and added == removed


def _token_multiset(src: str) -> tuple[tuple[str, str], ...] | None:
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        out = [
            (tokenize.tok_name[t.type], t.string)
            for t in toks
            if t.type
            not in (
                tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                tokenize.DEDENT, tokenize.COMMENT, tokenize.ENDMARKER,
            )
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return tuple(sorted(out))


def excluded(path: str, globs: list[str]) -> bool:
    return any(fnmatch(path, g) for g in globs)


def enclosing_definitions(source: str, linenos: list[int]) -> list[ast.AST]:
    """Innermost function/class defs containing any of the given lines."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = getattr(node, "end_lineno", start)
        if any(start <= ln <= end for ln in linenos):
            hits.append(node)
    # innermost first
    hits.sort(key=lambda n: getattr(n, "end_lineno", 0) - n.lineno)
    return hits
