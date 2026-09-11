"""Check 3 - Equivalent? For hunks that claim "refactor", is the claim true?

Run the pre-change and post-change implementations side by side on generated
inputs and require identical observable results, including identical raised
exceptions.

This check has the narrowest reach of the three and the sharpest teeth. Its
reach is bounded by three things we cannot work around, only detect:

  * **side effects** - comparing return values says nothing about what a
    function wrote to a database;
  * **I/O** - we will not open sockets or files to test a refactor;
  * **nondeterminism** - a function of the clock or the RNG disagrees with
    itself, so disagreement carries no signal.

triage.purity refuses all three up front, and a determinism self-check catches
what slips past it. When the gate refuses, the result is ABSTAIN with the
reason attached, never PASS. A refactor we could not verify is simply a
refactor a human still has to read.
"""

from __future__ import annotations

import ast
import random
import signal
import threading
from dataclasses import dataclass
from typing import Any, Callable

from triage import inputs, purity
from triage.classify import enclosing_definitions
from triage.config import Config
from triage.model import CheckResult, Hunk, Status

NAME = "equivalent"


class _Timeout(Exception):
    pass


@dataclass
class _Outcome:
    kind: str          # "value" | "raise"
    payload: Any

    def describe(self) -> str:
        if self.kind == "raise":
            return f"{type(self.payload).__name__}({self.payload})"
        return repr(self.payload)


def check(hunk: Hunk, old_source: str | None, new_source: str, cfg: Config) -> CheckResult:
    if old_source is None:
        return CheckResult(NAME, Status.SKIP, "no pre-change version to compare against", {})
    try:
        new_tree = ast.parse(new_source)
        old_tree = ast.parse(old_source)
    except SyntaxError as exc:
        return CheckResult(NAME, Status.ABSTAIN, f"unparsable source: {exc}", {})

    targets = [
        n for n in enclosing_definitions(new_source, hunk.added_linenos)
        if isinstance(n, ast.FunctionDef)
    ]
    if not targets:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "change is not inside a function; nothing callable to compare", {},
        )
    func_new = targets[0]
    func_old = _find_function(old_tree, func_new.name)
    if func_old is None:
        return CheckResult(
            NAME, Status.SKIP, f"'{func_new.name}' is new; no old behaviour exists", {},
        )

    if _signature(func_old) != _signature(func_new):
        return CheckResult(
            NAME, Status.FAIL,
            f"signature of '{func_new.name}' changed: "
            f"{_signature(func_old)} -> {_signature(func_new)}; "
            "callers observe this, so it is not a pure refactor",
            {"function": func_new.name},
        )

    for tree, func, side in ((old_tree, func_old, "old"), (new_tree, func_new, "new")):
        report = purity.analyze(func, tree)
        if not report.pure:
            return CheckResult(
                NAME, Status.ABSTAIN,
                f"cannot compare by execution ({side} side): {report.reasons[0]}",
                {"function": func_new.name, "purity_reasons": report.reasons},
            )

    try:
        old_fn = _materialize(old_tree, func_old)
        new_fn = _materialize(new_tree, func_new)
    except Exception as exc:  # noqa: BLE001 - any failure here means "we can't tell"
        return CheckResult(NAME, Status.ABSTAIN, f"could not load function: {exc!r}", {})

    seeds = _mine_literals(func_old) + _mine_literals(func_new)
    try:
        cases = _build_cases(func_new, cfg.equivalence_cases, tuple(seeds))
    except inputs.Unsupported as exc:
        return CheckResult(NAME, Status.ABSTAIN, str(exc), {"function": func_new.name})

    checked = 0
    for args in cases:
        first = _invoke(new_fn, args, cfg.equivalence_timeout_seconds)
        second = _invoke(new_fn, args, cfg.equivalence_timeout_seconds)
        if not _same(first, second):
            return CheckResult(
                NAME, Status.ABSTAIN,
                f"'{func_new.name}' is nondeterministic: two calls on "
                f"{_fmt_args(args)} gave {first.describe()} and {second.describe()}",
                {"function": func_new.name},
            )
        old_out = _invoke(old_fn, args, cfg.equivalence_timeout_seconds)
        if isinstance(old_out.payload, _Timeout) or isinstance(first.payload, _Timeout):
            return CheckResult(
                NAME, Status.ABSTAIN,
                f"execution timed out on {_fmt_args(args)}", {"function": func_new.name},
            )
        checked += 1
        if not _same(old_out, first):
            return CheckResult(
                NAME, Status.FAIL,
                f"not equivalent: {func_new.name}{_fmt_args(args)} returned "
                f"{old_out.describe()} before and {first.describe()} after",
                {
                    "function": func_new.name,
                    "counterexample": _fmt_args(args),
                    "before": old_out.describe(),
                    "after": first.describe(),
                    "cases_checked": checked,
                },
            )
    return CheckResult(
        NAME, Status.PASS,
        f"'{func_new.name}' agrees with its pre-change version on {checked} generated inputs",
        {"function": func_new.name, "cases_checked": checked},
    )


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _signature(func: ast.FunctionDef) -> str:
    a = func.args
    parts = [arg.arg for arg in [*a.posonlyargs, *a.args]]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    parts += [arg.arg for arg in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return f"({', '.join(parts)})"


def _mine_literals(func: ast.FunctionDef) -> list[Any]:
    """Every literal the function mentions is a candidate boundary value."""
    out: list[Any] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
            if isinstance(node.value, str) and len(node.value) > 40:
                continue
            out.append(node.value)
    return out[:20]


def _build_cases(
    func: ast.FunctionDef, count: int, seeds: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    rng = random.Random(0x7213)
    columns: dict[str, list[Any]] = {}
    a = func.args
    positional = [*a.posonlyargs, *a.args, *a.kwonlyargs]
    if a.vararg or a.kwarg:
        raise inputs.Unsupported("*args/**kwargs cannot be generated from annotations")
    for arg in positional:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        columns[arg.arg] = inputs.generate(annotation, rng, count, seeds)
    if not columns:
        return [{}]
    return [
        {name: values[i] for name, values in columns.items()}
        for i in range(count)
    ]


def _materialize(tree: ast.Module, func: ast.FunctionDef) -> Callable[..., Any]:
    """Build a minimal module containing only the function and its pure deps.

    Executing the *whole* original module would run its imports and any
    top-level statements, which is exactly the kind of side effect this check
    exists to avoid.
    """
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
    namespace: dict[str, Any] = {"__name__": "triage_equiv"}
    exec(compile(module, "<triage-equiv>", "exec"), namespace)  # noqa: S102
    return namespace[func.name]


def _invoke(fn: Callable[..., Any], kwargs: dict[str, Any], timeout: float) -> _Outcome:
    with _time_limit(timeout):
        try:
            return _Outcome("value", fn(**kwargs))
        except _Timeout as exc:
            return _Outcome("raise", exc)
        except RecursionError as exc:
            return _Outcome("raise", exc)
        except Exception as exc:  # noqa: BLE001 - exceptions are part of behaviour
            return _Outcome("raise", exc)


class _time_limit:
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
        raise _Timeout("execution exceeded the equivalence time limit")


def _same(a: _Outcome, b: _Outcome) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "raise":
        return type(a.payload) is type(b.payload) and str(a.payload) == str(b.payload)
    return _values_equal(a.payload, b.payload)


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            if a != a and b != b:  # NaN == NaN for our purposes
                return True
            return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError, OverflowError):
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(
            _values_equal(x, y) for x, y in zip(a, b)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_values_equal(a[k], b[k]) for k in a)
    try:
        return type(a) is type(b) and bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def _fmt_args(kwargs: dict[str, Any]) -> str:
    return "(" + ", ".join(f"{k}={v!r}" for k, v in kwargs.items()) + ")"
