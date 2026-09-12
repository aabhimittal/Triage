"""Check 5 - Does this code crash on an input its own signature admits?

Every other check in TRIAGE measures the *tests*. Coverage, mutation score and
effectiveness all answer "is this change pinned down by the suite?", and none of
them can tell you the code is wrong -- only that nothing would notice if it
were. This one is different: it runs the new code on inputs drawn from its own
type annotations and reports any exception the author did not ask for.

    split_evenly(cents=0, ways=0) -> ZeroDivisionError

That is a defect, found without a test existing for it and without a mutant
standing in for it. It is also the check most likely to be *wrong*, so its
place in the design is deliberately limited:

**It is a veto, never a credential.** A crash demotes a hunk to residual. The
absence of a crash on 200 generated inputs credentials nothing -- it is weak
evidence, and letting weak evidence verify is the failure mode this whole tool
is built against. So a PASS here does not help a hunk get verified, and an
ABSTAIN costs nothing.

**Declared rejections are not crashes.** `raise ValueError("negative amount")`
on a negative input is the function working. Only exception types the code
never raises or catches count, which is why `sandbox.declared_exceptions`
over-approximates: fewer findings, higher precision.

**The honest false positive.** A function with an undocumented precondition --
"callers always pass a non-empty list" -- will be flagged for the empty list its
annotation admits. Python has no precondition language for us to read, so the
options are to flag it or to miss the real ones. It is flagged, with a
counterexample a reviewer can dismiss in seconds, and `# triage: allow-crash`
in the function silences it.
"""

from __future__ import annotations

import ast
import random
import re
from typing import Any

from triage import inputs, purity
from triage.classify import enclosing_definitions
from triage.config import Config
from triage.model import CheckResult, Hunk, Status
from triage.sandbox import Timeout, declared_exceptions, fmt_args, invoke, materialize

NAME = "crash"

_SUPPRESS = re.compile(r"#\s*triage:\s*allow-crash", re.I)

# Exceptions that almost always mean "this code is broken" rather than "this
# input was rejected". TypeError is deliberately absent: it is raised as often
# on purpose as by accident, and guessing wrong here spends the reviewer's
# trust on noise.
_BUG_SHAPED = {
    "IndexError", "KeyError", "ZeroDivisionError", "AttributeError",
    "UnboundLocalError", "OverflowError", "RecursionError", "StopIteration",
    "UnicodeDecodeError", "UnicodeEncodeError",
}


def check(hunk: Hunk, new_source: str, cfg: Config) -> CheckResult:
    if not hunk.path.endswith(".py") or not new_source:
        return CheckResult(NAME, Status.SKIP, "not Python source", {})
    try:
        tree = ast.parse(new_source)
    except SyntaxError as exc:
        return CheckResult(NAME, Status.ABSTAIN, f"unparsable source: {exc}", {})

    functions = [
        n for n in enclosing_definitions(new_source, hunk.added_linenos)
        if isinstance(n, ast.FunctionDef)
    ]
    if not functions:
        return CheckResult(NAME, Status.SKIP, "no function encloses these lines", {})

    skipped: list[str] = []
    examined: list[str] = []
    for func in functions[:3]:
        if _SUPPRESS.search(ast.get_source_segment(new_source, func) or ""):
            skipped.append(f"{func.name}: suppressed by a triage:allow-crash marker")
            continue
        report = purity.analyze(func, tree)
        if not report.pure:
            skipped.append(f"{func.name}: {report.reasons[0]}")
            continue
        try:
            fn, module = materialize(tree, func)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{func.name}: could not load ({exc!r})")
            continue

        seeds = tuple(_mine_literals(func))
        try:
            cases = _build_cases(func, cfg.crash_cases, seeds)
        except inputs.Unsupported as exc:
            skipped.append(f"{func.name}: {exc}")
            continue

        allowed = declared_exceptions(module)
        examined.append(func.name)
        for args in cases:
            if not _affordable(args):
                # A correct function that allocates proportionally to its input
                # will exhaust time or memory on a huge one. That is arithmetic,
                # not a defect, and reporting it would spend the reviewer's
                # trust on noise. Boundary values that large belong to the
                # equivalence check, which compares two implementations and can
                # afford them.
                continue
            outcome = invoke(fn, args, cfg.equivalence_timeout_seconds)
            if not outcome.raised:
                continue
            name = type(outcome.payload).__name__
            if isinstance(outcome.payload, Timeout):
                if not _small(args):
                    continue  # big input, slow run: says nothing
                return CheckResult(
                    NAME, Status.FAIL,
                    f"{func.name}{fmt_args(args)} did not terminate within "
                    f"{cfg.equivalence_timeout_seconds}s on a small input",
                    {"function": func.name, "counterexample": fmt_args(args),
                     "exception": "timeout"},
                )
            if name in allowed or name not in _BUG_SHAPED:
                continue
            return CheckResult(
                NAME, Status.FAIL,
                f"{func.name}{fmt_args(args)} raises {name}: "
                f"{outcome.payload}. The signature admits this input and the "
                "function never raises or catches that type deliberately",
                {
                    "function": func.name,
                    "counterexample": fmt_args(args),
                    "exception": name,
                    "message": str(outcome.payload)[:200],
                },
            )

    if not examined:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "could not execute anything here: " + "; ".join(skipped[:2]),
            {"skipped": skipped},
        )
    return CheckResult(
        NAME, Status.PASS,
        f"no unexpected exception from {', '.join(examined)} over "
        f"{cfg.crash_cases} generated inputs (weak evidence; never verifies on its own)",
        {"functions": examined, "cases": cfg.crash_cases, "skipped": skipped},
    )


_AFFORDABLE_INT = 10 ** 6
_SMALL_INT = 10 ** 3


def _affordable(args: dict[str, Any]) -> bool:
    """Reject cases whose sheer size would dominate the result."""
    return all(_magnitude(v) <= _AFFORDABLE_INT for v in args.values())


def _small(args: dict[str, Any]) -> bool:
    """A hang on an input this small cannot be blamed on the input."""
    return all(_magnitude(v) <= _SMALL_INT for v in args.values())


def _magnitude(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return abs(value)
    if isinstance(value, float):
        return abs(value) if value == value and abs(value) != float("inf") else 0
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        return max([len(value)] + [_magnitude(v) for v in _members(value)])
    return 0


def _members(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return list(value.keys()) + list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return []


def _mine_literals(func: ast.FunctionDef) -> list[Any]:
    out: list[Any] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
            if isinstance(node.value, str) and len(node.value) > 40:
                continue
            out.append(node.value)
    return out[:20]


def _build_cases(
    func: ast.FunctionDef, count: int, seeds: tuple[Any, ...]
) -> list[dict[str, Any]]:
    rng = random.Random(0x0CEA5)
    a = func.args
    if a.vararg or a.kwarg:
        raise inputs.Unsupported("*args/**kwargs cannot be generated from annotations")
    positional = [*a.posonlyargs, *a.args, *a.kwonlyargs]
    columns: dict[str, list[Any]] = {}
    for arg in positional:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        columns[arg.arg] = inputs.generate(annotation, rng, count, seeds)
    if not columns:
        return [{}]
    return [{name: values[i] for name, values in columns.items()} for i in range(count)]
