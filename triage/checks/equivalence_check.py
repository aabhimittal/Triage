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
from typing import Any

from triage import inputs, purity
from triage.classify import enclosing_definitions
from triage.config import Config
from triage.model import CheckResult, Hunk, Status
from triage.sandbox import Timeout, fmt_args, invoke, materialize, same

NAME = "equivalent"


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
        old_fn, _ = materialize(old_tree, func_old)
        new_fn, _ = materialize(new_tree, func_new)
    except Exception as exc:  # noqa: BLE001 - any failure here means "we can't tell"
        return CheckResult(NAME, Status.ABSTAIN, f"could not load function: {exc!r}", {})

    seeds = _mine_literals(func_old) + _mine_literals(func_new)
    try:
        cases = _build_cases(func_new, cfg.equivalence_cases, tuple(seeds))
    except inputs.Unsupported as exc:
        return CheckResult(NAME, Status.ABSTAIN, str(exc), {"function": func_new.name})

    checked = 0
    for args in cases:
        first = invoke(new_fn, args, cfg.equivalence_timeout_seconds)
        second = invoke(new_fn, args, cfg.equivalence_timeout_seconds)
        if not same(first, second):
            return CheckResult(
                NAME, Status.ABSTAIN,
                f"'{func_new.name}' is nondeterministic: two calls on "
                f"{fmt_args(args)} gave {first.describe()} and {second.describe()}",
                {"function": func_new.name},
            )
        old_out = invoke(old_fn, args, cfg.equivalence_timeout_seconds)
        if isinstance(old_out.payload, Timeout) or isinstance(first.payload, Timeout):
            return CheckResult(
                NAME, Status.ABSTAIN,
                f"execution timed out on {fmt_args(args)}", {"function": func_new.name},
            )
        checked += 1
        if not same(old_out, first):
            return CheckResult(
                NAME, Status.FAIL,
                f"not equivalent: {func_new.name}{fmt_args(args)} returned "
                f"{old_out.describe()} before and {first.describe()} after",
                {
                    "function": func_new.name,
                    "counterexample": fmt_args(args),
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


