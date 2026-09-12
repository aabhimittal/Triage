"""Running a function from a diff, in isolation, with a time limit.

Shared by the two checks that execute code rather than measure tests. Both need
the same three things: a module containing only the function and its pure
dependencies, a call that captures either a value or an exception, and a
guarantee that a runaway loop does not hang the run.

Building a *minimal* module matters. Executing the original file would run its
imports and any top-level statements, which is exactly the class of side effect
the purity gate exists to keep out.
"""

from __future__ import annotations

import ast
import signal
import threading
from dataclasses import dataclass
from typing import Any, Callable

from triage import purity


class Timeout(Exception):
    pass


@dataclass
class Outcome:
    kind: str          # "value" | "raise"
    payload: Any

    @property
    def raised(self) -> bool:
        return self.kind == "raise"

    def describe(self) -> str:
        if self.raised:
            return f"{type(self.payload).__name__}({self.payload})"
        return repr(self.payload)


def materialize(tree: ast.Module, func: ast.FunctionDef) -> tuple[Callable[..., Any], ast.Module]:
    """Compile `func` plus its pure module-level dependencies, nothing else."""
    report = purity.analyze(func, tree)
    needed = set(report.dependencies)
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in needed and node is not func:
            body.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = {
                t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(t, ast.Name)
            }
            if names & needed:
                body.append(node)
    body.append(func)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"__name__": "triage_sandbox"}
    exec(compile(module, "<triage-sandbox>", "exec"), namespace)  # noqa: S102
    return namespace[func.name], module


def invoke(fn: Callable[..., Any], kwargs: dict[str, Any], timeout: float) -> Outcome:
    with time_limit(timeout):
        try:
            return Outcome("value", fn(**kwargs))
        except Timeout as exc:
            return Outcome("raise", exc)
        except RecursionError as exc:
            return Outcome("raise", exc)
        except Exception as exc:  # noqa: BLE001 - exceptions are part of behaviour
            return Outcome("raise", exc)


class time_limit:
    """SIGALRM-based timeout; degrades to no timeout off the main thread."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.armed = (
            seconds > 0
            and threading.current_thread() is threading.main_thread()
            and hasattr(signal, "SIGALRM")
        )

    def __enter__(self):
        if self.armed:
            self.previous = signal.signal(signal.SIGALRM, self._fire)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, *exc):
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous)
        return False

    @staticmethod
    def _fire(signum, frame):
        raise Timeout("execution exceeded the time limit")


def same(a: Outcome, b: Outcome) -> bool:
    if a.kind != b.kind:
        return False
    if a.raised:
        return type(a.payload) is type(b.payload) and str(a.payload) == str(b.payload)
    return values_equal(a.payload, b.payload)


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            if a != a and b != b:  # NaN == NaN for our purposes
                return True
            return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError, OverflowError):
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(
            values_equal(x, y) for x, y in zip(a, b)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(values_equal(a[k], b[k]) for k in a)
    try:
        return type(a) is type(b) and bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def fmt_args(kwargs: dict[str, Any]) -> str:
    return "(" + ", ".join(f"{k}={v!r}" for k, v in kwargs.items()) + ")"


def declared_exceptions(module: ast.Module) -> set[str]:
    """Exception type names the code explicitly raises or asserts.

    Used to tell a deliberate rejection (`raise ValueError("negative amount")`)
    from a crash (`IndexError` out of an unguarded subscript). Collected across
    the whole materialised module, which over-approximates -- a helper's
    declared type excuses the same type escaping the caller. Over-approximating
    costs findings and buys precision, which is the right trade for a check
    whose output is a claim about the code rather than about the tests.
    """
    declared: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Assert):
            declared.add("AssertionError")
        elif isinstance(node, ast.Raise):
            exc = node.exc
            if exc is None:
                continue
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name):
                declared.add(exc.id)
            elif isinstance(exc, ast.Attribute):
                declared.add(exc.attr)
        elif isinstance(node, ast.ExceptHandler) and node.type is not None:
            # A caught type is handled deliberately; if it escapes anyway the
            # author is at least aware of it.
            for name in ast.walk(node.type):
                if isinstance(name, ast.Name):
                    declared.add(name.id)
    return declared
