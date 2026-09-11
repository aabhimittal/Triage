"""Diff-scoped mutation operators.

Two design choices worth defending:

1. **Source splicing, not ``ast.unparse``.** Every mutant is produced by
   replacing an exact character span in the original text. The file keeps its
   formatting, its comments and — critically — its *line numbers*, so the
   coverage-derived test-impact map stays valid for the mutant and a surviving
   mutant can be reported against the reviewer's actual line.

2. **Mutants are restricted to the diff.** Classic mutation testing mutates the
   whole codebase, which is why nobody runs it. Here the mutant set is
   generated only from lines the PR added, which is the only region whose
   test-adequacy the PR is responsible for.

Equivalent mutants (``x * 1`` -> ``x / 1``) are not detected. They depress
MS_delta and therefore push hunks *into* human review. That is the safe
direction to be wrong in, and it is why the threshold is 0.8 and not 1.0.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Iterator

_ARITH = {"+": "-", "-": "+", "*": "/", "/": "*", "//": "*", "%": "*", "**": "*"}
_COMPARE = {
    "<": "<=", "<=": "<", ">": ">=", ">=": ">",
    "==": "!=", "!=": "==",
    "is not": "is", "is": "is not",
    "not in": "in", "in": "not in",
}
_BOOL = {"and": "or", "or": "and"}


@dataclass(frozen=True)
class Mutant:
    path: str
    lineno: int
    operator: str
    before: str
    after: str
    source: str

    @property
    def label(self) -> str:
        return f"{self.operator} @ {self.path}:{self.lineno} ({self.before!r} -> {self.after!r})"


class _Splicer:
    def __init__(self, text: str):
        self.text = text
        self.offsets = [0]
        for line in text.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def pos(self, lineno: int, col: int) -> int:
        return self.offsets[lineno - 1] + col

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.pos(node.lineno, node.col_offset),
            self.pos(node.end_lineno, node.end_col_offset),
        )

    def replace(self, start: int, end: int, new: str) -> str:
        return self.text[:start] + new + self.text[end:]


def generate_mutants(
    path: str,
    source: str,
    target_lines: set[int],
    limit: int | None = None,
) -> list[Mutant]:
    """All viable mutants whose mutation point falls on a targeted line."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    splicer = _Splicer(source)
    seen: set[str] = set()
    out: list[Mutant] = []
    inert = _inert_strings(tree)
    for mutant in _candidates(path, tree, splicer, target_lines, inert):
        if mutant.source in seen or mutant.source == source:
            continue
        try:
            ast.parse(mutant.source)
        except SyntaxError:
            continue
        seen.add(mutant.source)
        out.append(mutant)
    out.sort(key=lambda m: (m.lineno, m.operator))
    if limit is not None and len(out) > limit:
        out = _spread(out, limit)
    return out


def _spread(mutants: list[Mutant], limit: int) -> list[Mutant]:
    """Sample mutants evenly across lines rather than truncating.

    Truncation would concentrate the whole budget on the first line of a hunk
    and leave the rest unprobed, which produces a mutation score that is
    unrepresentative in a way the reader cannot see.
    """
    by_line: dict[int, list[Mutant]] = {}
    for m in mutants:
        by_line.setdefault(m.lineno, []).append(m)
    picked: list[Mutant] = []
    round_no = 0
    while len(picked) < limit:
        added = False
        for lineno in sorted(by_line):
            bucket = by_line[lineno]
            if round_no < len(bucket) and len(picked) < limit:
                picked.append(bucket[round_no])
                added = True
        if not added:
            break
        round_no += 1
    return sorted(picked, key=lambda m: (m.lineno, m.operator))


_MESSAGE_SINKS = {"debug", "info", "warning", "warn", "error", "exception",
                  "critical", "log", "print", "format"}


def _inert_strings(tree: ast.AST) -> set[int]:
    """String literals whose value no reasonable test asserts on.

    Exception messages and log lines are the classic equivalent-mutant factory:
    flipping ``raise ValueError("too big")`` to ``raise ValueError("")`` changes
    no behaviour anyone tests, yet it counts as a surviving mutant. Left in, it
    depresses MS_delta on well-tested code (pushing good hunks into review) and,
    worse, shows up in evaluation as a bug that "escaped" -- making the tool
    look unsafe for a defect that is not one. Excluding them narrows what the
    mutation score claims to cover, which is the honest trade.
    """
    inert: set[int] = set()

    def mark(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                inert.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            mark(node.exc)
        elif isinstance(node, ast.Assert) and node.msg is not None:
            mark(node.msg)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if name in _MESSAGE_SINKS:
                for arg in node.args:
                    mark(arg)
    return inert


def _candidates(
    path: str, tree: ast.AST, sp: _Splicer, targets: set[int], inert: set[int]
) -> Iterator[Mutant]:
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue

        if isinstance(node, ast.BinOp):
            yield from _binop(path, node, sp, targets)
        elif isinstance(node, ast.AugAssign):
            yield from _augassign(path, node, sp, targets)
        elif isinstance(node, ast.Compare):
            yield from _compare(path, node, sp, targets)
        elif isinstance(node, ast.BoolOp):
            yield from _boolop(path, node, sp, targets)
        elif isinstance(node, ast.Constant):
            if id(node) not in inert:
                yield from _constant(path, node, sp, targets)
        elif isinstance(node, (ast.If, ast.While)):
            yield from _negate_test(path, node, sp, targets)
        elif isinstance(node, ast.Return):
            yield from _return_none(path, node, sp, targets)
        elif isinstance(node, (ast.Break, ast.Continue)):
            yield from _loop_control(path, node, sp, targets)
        elif isinstance(node, ast.stmt):
            yield from _stmt_delete(path, node, sp, targets)


def _find_op(sp: _Splicer, start: int, end: int, wanted: set[str]) -> tuple[int, int, str] | None:
    """Locate an operator token in the gap between two operands."""
    segment = sp.text[start:end]
    best: tuple[int, int, str] | None = None
    for token in sorted(wanted, key=len, reverse=True):
        pattern = (
            rf"(?<![\w.]){re.escape(token)}(?![\w.])"
            if token[0].isalpha()
            else re.escape(token)
        )
        for m in re.finditer(pattern, segment):
            # Ignore operators inside comments or strings in the gap.
            prefix = segment[: m.start()]
            if "#" in prefix or prefix.count("'") % 2 or prefix.count('"') % 2:
                continue
            cand = (start + m.start(), start + m.end(), token)
            if best is None or cand[0] < best[0]:
                best = cand
            break
    return best


def _line_of(sp: _Splicer, offset: int) -> int:
    lo, hi = 0, len(sp.offsets) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if sp.offsets[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid
    return max(lo, 1)


def _binop(path: str, node: ast.BinOp, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    left_end = sp.span(node.left)[1]
    right_start = sp.span(node.right)[0]
    found = _find_op(sp, left_end, right_start, set(_ARITH))
    if not found:
        return
    start, end, token = found
    lineno = _line_of(sp, start)
    if lineno not in targets:
        return
    yield Mutant(path, lineno, "arith", token, _ARITH[token],
                 sp.replace(start, end, _ARITH[token]))


def _augassign(path: str, node: ast.AugAssign, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    target_end = sp.span(node.target)[1]
    value_start = sp.span(node.value)[0]
    found = _find_op(sp, target_end, value_start, {f"{k}=" for k in _ARITH})
    if not found:
        return
    start, end, token = found
    lineno = _line_of(sp, start)
    if lineno not in targets:
        return
    repl = _ARITH[token[:-1]] + "="
    yield Mutant(path, lineno, "aug-arith", token, repl, sp.replace(start, end, repl))


def _compare(path: str, node: ast.Compare, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    left = node.left
    for op, right in zip(node.ops, node.comparators):
        gap = (sp.span(left)[1], sp.span(right)[0])
        found = _find_op(sp, gap[0], gap[1], set(_COMPARE))
        left = right
        if not found:
            continue
        start, end, token = found
        lineno = _line_of(sp, start)
        if lineno not in targets:
            continue
        yield Mutant(path, lineno, "compare", token, _COMPARE[token],
                     sp.replace(start, end, _COMPARE[token]))


def _boolop(path: str, node: ast.BoolOp, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    for a, b in zip(node.values, node.values[1:]):
        found = _find_op(sp, sp.span(a)[1], sp.span(b)[0], set(_BOOL))
        if not found:
            continue
        start, end, token = found
        lineno = _line_of(sp, start)
        if lineno not in targets:
            continue
        yield Mutant(path, lineno, "boolop", token, _BOOL[token],
                     sp.replace(start, end, _BOOL[token]))


def _constant(path: str, node: ast.Constant, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    if node.lineno not in targets:
        return
    start, end = sp.span(node)
    value = node.value
    if isinstance(value, bool):
        repl = repr(not value)
        op = "bool-const"
    elif isinstance(value, int):
        repl = repr(value + 1)
        op = "num-const"
    elif isinstance(value, float):
        repl = repr(value + 1.0)
        op = "num-const"
    elif isinstance(value, str):
        if len(value) > 60 or "\n" in value:
            return
        repl = '""' if value else '"triage"'
        op = "str-const"
    else:
        return
    yield Mutant(path, node.lineno, op, sp.text[start:end], repl, sp.replace(start, end, repl))


def _negate_test(path: str, node: ast.If | ast.While, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    test = node.test
    if getattr(test, "lineno", 0) not in targets:
        return
    start, end = sp.span(test)
    original = sp.text[start:end]
    if original.startswith("not "):
        repl = original[4:]
    else:
        repl = f"not ({original})"
    yield Mutant(path, test.lineno, "negate-cond", original[:40], repl[:40],
                 sp.replace(start, end, repl))


def _return_none(path: str, node: ast.Return, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    if node.value is None or node.lineno not in targets:
        return
    start, end = sp.span(node.value)
    if sp.text[start:end].strip() == "None":
        return
    yield Mutant(path, node.lineno, "return-none", sp.text[start:end][:40], "None",
                 sp.replace(start, end, "None"))


def _loop_control(path: str, node: ast.Break | ast.Continue, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    if node.lineno not in targets:
        return
    start, end = sp.span(node)
    token = sp.text[start:end]
    repl = "continue" if token == "break" else "break"
    yield Mutant(path, node.lineno, "loop-control", token, repl, sp.replace(start, end, repl))


_DELETABLE = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Raise, ast.Assert)


def _stmt_delete(path: str, node: ast.stmt, sp: _Splicer, targets: set[int]) -> Iterator[Mutant]:
    """Statement deletion (SDL).

    This is the universal operator, and the reason every executable line gets
    at least one probe. Plenty of real lines -- ``self.cache[key] = value``,
    ``logger.warning(...)``, a bare call for its side effect -- contain no
    operator and no literal to flip. Without SDL those lines would yield zero
    mutants and the mutation check would have to abstain on them, which is the
    difference between a tool that covers a diff and one that covers the fun
    parts of a diff.
    """
    if not isinstance(node, _DELETABLE):
        return
    start_line = node.lineno
    end_line = getattr(node, "end_lineno", start_line)
    if not all(ln in targets for ln in range(start_line, end_line + 1)):
        return
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return  # docstring / bare literal: deleting it is a no-op by construction
    start, end = sp.span(node)
    line_start = sp.offsets[start_line - 1]
    if sp.text[line_start:start].strip():
        return  # statement shares a line (``if x: y = 1``); splicing is unsafe
    yield Mutant(path, start_line, "stmt-delete", sp.text[start:end][:40], "pass",
                 sp.replace(start, end, "pass"))
