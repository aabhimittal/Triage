"""Static purity gate for differential (old vs new) testing.

Running two versions of a function on generated inputs only proves anything if
the function is a *function*: deterministic, side-effect free, fully described
by its arguments and return value. Most production code is not. Rather than
pretend otherwise, this module is a conservative whitelist that answers "may I
compare these two implementations by running them?" with yes or with a reason.

Every unrecognised construct is impure. False "impure" verdicts cost us reach
(the hunk goes to a human). False "pure" verdicts cost us correctness (we might
stamp VERIFIED on a change that differs only in its side effects). Those are
not symmetric, so the analysis is biased hard toward refusing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "bytes", "chr", "complex", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hash", "hex", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "oct", "ord", "pow", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    # Exceptions are constructed, not called for effect.
    "ArithmeticError", "AssertionError", "AttributeError", "Exception",
    "IndexError", "KeyError", "LookupError", "NotImplementedError",
    "OverflowError", "RuntimeError", "StopIteration", "TypeError", "ValueError",
    "ZeroDivisionError",
}

SAFE_MODULES = {
    "math", "cmath", "itertools", "functools", "operator", "string", "decimal",
    "fractions", "statistics", "heapq", "bisect", "collections", "typing",
    "dataclasses", "enum", "re", "textwrap", "unicodedata", "base64", "struct",
    "hashlib", "json", "copy", "numbers", "array",
}

# Deterministic, non-mutating methods on builtin containers/strings.
SAFE_METHODS = {
    "upper", "lower", "title", "strip", "lstrip", "rstrip", "split", "rsplit",
    "splitlines", "join", "format", "startswith", "endswith", "replace",
    "find", "rfind", "index", "count", "encode", "decode", "zfill", "ljust",
    "rjust", "center", "capitalize", "casefold", "isdigit", "isalpha",
    "isalnum", "isspace", "get", "keys", "values", "items", "copy",
    "union", "intersection", "difference", "symmetric_difference",
    "issubset", "issuperset", "hexdigest", "digest", "group", "groups",
    "bit_length", "to_bytes", "from_bytes", "conjugate", "as_integer_ratio",
}

# Mutating methods: allowed only on objects the function itself created.
MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem",
}

SAFE_DECORATORS = {"staticmethod", "cache", "lru_cache", "total_ordering"}


@dataclass
class PurityReport:
    pure: bool
    reasons: list[str] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)

    def reject(self, reason: str) -> None:
        self.pure = False
        if reason not in self.reasons:
            self.reasons.append(reason)


class _ModuleFacts:
    """What the module offers a function: pure defs, constants, safe imports."""

    def __init__(self, tree: ast.Module):
        self.functions: dict[str, ast.FunctionDef] = {}
        self.constants: dict[str, ast.stmt] = {}
        self.safe_imports: dict[str, str] = {}
        self.other_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_import(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                literal = _is_literal(node.value) if node.value is not None else False
                for t in targets:
                    if isinstance(t, ast.Name):
                        (self.constants if literal else self.other_names).__setitem__(
                            t.id, node
                        ) if literal else self.other_names.add(t.id)
            elif isinstance(node, ast.ClassDef):
                self.other_names.add(node.name)

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                name = alias.asname or root
                if root in SAFE_MODULES:
                    self.safe_imports[name] = alias.name
                else:
                    self.other_names.add(name)
        else:
            root = (node.module or "").split(".")[0]
            for alias in node.names:
                name = alias.asname or alias.name
                if root in SAFE_MODULES:
                    self.safe_imports[name] = f"{node.module}.{alias.name}"
                else:
                    self.other_names.add(name)


def _is_literal(node: ast.AST | None) -> bool:
    if node is None:
        return False
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, SyntaxError, TypeError):
        return False


def analyze(func: ast.FunctionDef, module: ast.Module, _seen: set[str] | None = None) -> PurityReport:
    """Decide whether `func` may be compared by execution."""
    report = PurityReport(pure=True)
    facts = _ModuleFacts(module)
    seen = _seen or set()

    if isinstance(func, ast.AsyncFunctionDef):
        report.reject("async function: needs an event loop, out of scope")
        return report
    for dec in func.decorator_list:
        name = _decorator_name(dec)
        if name not in SAFE_DECORATORS:
            report.reject(f"decorator @{name} may change behaviour or carry state")
    params = _param_names(func)
    if params and params[0] in ("self", "cls"):
        report.reject("method: requires a constructed receiver, not generatable")

    for arg in _all_args(func):
        if arg.annotation is None:
            report.reject(f"parameter '{arg.arg}' has no type annotation to generate from")
    for default in list(func.args.defaults) + [d for d in func.args.kw_defaults if d]:
        if not _is_literal(default):
            report.reject("non-literal default argument is shared mutable state")

    local_objects: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            report.reject("global/nonlocal: writes state outside the call")
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            report.reject("generator: laziness makes output comparison unsound here")
        elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            report.reject("async construct")
        elif isinstance(node, ast.With):
            report.reject("context manager: almost always an external resource")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = node.names[0].name.split(".")[0] if isinstance(node, ast.Import) else (node.module or "")
            if mod.split(".")[0] not in SAFE_MODULES:
                report.reject(f"imports '{mod}' inside the function body")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    local_objects.add(t.id)
                elif isinstance(t, (ast.Attribute, ast.Subscript)):
                    base = _base_name(t)
                    if base in params:
                        report.reject(f"mutates its argument '{base}' in place")
        elif isinstance(node, ast.Call):
            _check_call(node, facts, report, params, local_objects, seen, module)
    report.dependencies = {
        n.id
        for n in ast.walk(func)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and (n.id in facts.functions or n.id in facts.constants or n.id in facts.safe_imports)
    }
    return report


def _check_call(
    node: ast.Call,
    facts: _ModuleFacts,
    report: PurityReport,
    params: list[str],
    local_objects: set[str],
    seen: set[str],
    module: ast.Module,
) -> None:
    fn = node.func
    if isinstance(fn, ast.Name):
        name = fn.id
        if name in SAFE_BUILTINS or name in facts.safe_imports:
            return
        if name in facts.functions:
            if name in seen:
                return  # recursion: already being analysed up the stack
            sub = analyze(facts.functions[name], module, seen | {name})
            if not sub.pure:
                report.reject(f"calls impure helper '{name}': {sub.reasons[0]}")
            return
        report.reject(f"calls unknown name '{name}' (not a safe builtin or local pure function)")
    elif isinstance(fn, ast.Attribute):
        base = _base_name(fn)
        attr = fn.attr
        if base in facts.safe_imports:
            return
        if attr in MUTATING_METHODS:
            if base in params:
                report.reject(f"mutates argument '{base}' via .{attr}()")
            elif base not in local_objects and base is not None:
                report.reject(f"mutates non-local object '{base}' via .{attr}()")
            return
        if attr not in SAFE_METHODS:
            report.reject(f"calls method .{attr}() with unknown effects")
    else:
        report.reject("calls a dynamically computed callable")


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "<expr>"


def _base_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _all_args(func: ast.FunctionDef) -> list[ast.arg]:
    a = func.args
    return [*a.posonlyargs, *a.args, *a.kwonlyargs] + [x for x in (a.vararg, a.kwarg) if x]


def _param_names(func: ast.FunctionDef) -> list[str]:
    return [a.arg for a in _all_args(func)]
