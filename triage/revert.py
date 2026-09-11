"""Reverting individual definitions, rather than whole files.

Needed by the test-effectiveness check. The obvious implementation -- restore
every source file the PR touched, then run the new tests -- is wrong in a way
that quietly destroys the check's value. A test module almost always imports
several names at module level:

    from cart import apply_discount, shipping_fee, subtotal, tax

Revert the whole file and the two new functions vanish, so the module cannot be
imported, so *every* test in it fails, so every test looks like it detects the
change. The check degenerates into "yes" for any test file that imports
anything new -- which is most of them.

Reverting one definition at a time fixes it. For a given test we restore only
the definitions that test actually references; everything else stays at its new
state, so the module still imports and the failure (or absence of one) is
attributable to the behaviour under test.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    name: str
    start: int  # character offset, inclusive
    end: int    # character offset, exclusive
    text: str


def top_level_spans(source: str) -> dict[str, Span]:
    """Character spans of every top-level definition and constant assignment."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    offsets = _line_offsets(source)
    spans: dict[str, Span] = {}

    def record(name: str, first_line: int, last_line: int) -> None:
        start = offsets[first_line - 1]
        end = offsets[last_line] if last_line < len(offsets) else len(source)
        spans[name] = Span(name, start, end, source[start:end])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            record(node.name, first, getattr(node, "end_lineno", node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.lineno, getattr(node, "end_lineno", node.lineno))
    return spans


def changed_symbols(old_source: str | None, new_source: str) -> set[str]:
    """Top-level names this change added or rewrote."""
    new_spans = top_level_spans(new_source)
    if old_source is None:
        return set(new_spans)
    old_spans = top_level_spans(old_source)
    return {
        name
        for name, span in new_spans.items()
        if name not in old_spans or _normalise(old_spans[name].text) != _normalise(span.text)
    }


def revert_symbols(new_source: str, old_source: str | None, names: set[str]) -> str:
    """Restore the named definitions to their pre-change form.

    A name absent from the old source is deleted outright: before the change it
    did not exist, and a test that references it could not have run.
    """
    new_spans = top_level_spans(new_source)
    old_spans = top_level_spans(old_source) if old_source else {}
    edits = [
        (span.start, span.end, old_spans[name].text if name in old_spans else "")
        for name, span in new_spans.items()
        if name in names
    ]
    result = new_source
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def referenced_names(node: ast.AST) -> set[str]:
    """Every bare name a chunk of code reads, including attribute bases."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            base = child
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)
    return names


def _normalise(text: str) -> str:
    """Compare definitions ignoring pure layout, so reindentation is not a change."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets
