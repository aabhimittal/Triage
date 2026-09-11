"""Running the test suite, and turning one run into a test-impact index.

The single most important efficiency idea in TRIAGE lives here. We pay for one
instrumented test run and get back, for every source line, *the set of tests
that executed it*. Mutation testing then runs only the tests that can possibly
observe a given mutant, instead of the whole suite. That turns

    cost = mutants x full_suite_runtime

into

    cost = mutants x impacted_suite_runtime

which is the difference between "unusable on a real repo" and "runs in CI".
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from triage.config import Config

_COVERAGERC = """\
[run]
branch = True
relative_files = True
dynamic_context = test_function
parallel = False
source = {sources}

[report]
ignore_errors = True
"""


@dataclass
class TestRunResult:
    ok: bool
    returncode: int
    duration: float
    output: str = ""
    timed_out: bool = False

    @property
    def no_tests_ran(self) -> bool:
        return self.returncode == 5  # pytest's exit code for "no tests collected"


@dataclass
class CoverageIndex:
    """Per-line coverage plus the reverse map line -> tests."""

    covered: dict[str, set[int]] = field(default_factory=dict)
    statements: dict[str, set[int]] = field(default_factory=dict)
    tests_by_line: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    all_tests: set[str] = field(default_factory=set)
    baseline: TestRunResult | None = None

    def is_measured(self, path: str) -> bool:
        return path in self.statements

    def executable(self, path: str, linenos: list[int]) -> list[int]:
        stmts = self.statements.get(path, set())
        return [ln for ln in linenos if ln in stmts]

    def uncovered(self, path: str, linenos: list[int]) -> list[int]:
        cov = self.covered.get(path, set())
        return [ln for ln in self.executable(path, linenos) if ln not in cov]

    def tests_for(self, path: str, linenos: list[int]) -> set[str]:
        by_line = self.tests_by_line.get(path, {})
        out: set[str] = set()
        for ln in linenos:
            out |= by_line.get(ln, set())
        return out


def resolve_command(cfg: Config) -> list[str]:
    """Always invoke the runner as ``python -m <runner>``.

    Not cosmetic. ``python -m pytest`` puts the current directory on sys.path;
    the bare ``pytest`` console script does not. A project whose package sits at
    the repo root imports fine under one and raises ImportError under the other,
    and if the *mutant* runs use the failing form then every mutant looks killed
    and every hunk looks verified. That is the exact false-confidence failure
    this tool exists to avoid, so the invocation is normalised in one place and
    the baseline and mutant runs are forced to agree.
    """
    cmd = list(cfg.test_command)
    if not cmd:
        return [sys.executable, "-m", "pytest"]
    if Path(cmd[0]).name in ("python", "python3") or cmd[0] == sys.executable:
        return [sys.executable, *cmd[1:]]
    return [sys.executable, "-m", *cmd]


def run_tests(
    workdir: Path,
    cfg: Config,
    node_ids: list[str] | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> TestRunResult:
    cmd = resolve_command(cfg)
    if node_ids:
        cmd += node_ids
    elif cfg.test_paths:
        cmd += [p for p in cfg.test_paths if (workdir / p).exists()]
    started = time.time()
    full_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    try:
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True,
            timeout=timeout, env=full_env,
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(False, -1, time.time() - started, "timeout", timed_out=True)
    except FileNotFoundError as exc:
        return TestRunResult(False, 127, time.time() - started, str(exc))
    return TestRunResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        duration=time.time() - started,
        output=(proc.stdout + proc.stderr)[-8000:],
    )


def collect_coverage(workdir: Path, cfg: Config) -> CoverageIndex:
    """One instrumented run -> coverage + the test-impact index."""
    rcfile = workdir / ".triage-coveragerc"
    rcfile.write_text(_COVERAGERC.format(sources="\n    ".join(cfg.source_roots)))
    data_file = workdir / ".triage-coverage"

    runner = resolve_command(cfg)[1:]  # drop the interpreter; coverage supplies it
    cmd = [sys.executable, "-m", "coverage", "run", f"--rcfile={rcfile}", *runner]
    cmd += [p for p in cfg.test_paths if (workdir / p).exists()]

    started = time.time()
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True,
        env={
            **os.environ,
            "COVERAGE_FILE": str(data_file),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    baseline = TestRunResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        duration=time.time() - started,
        output=(proc.stdout + proc.stderr)[-8000:],
    )
    index = _read_coverage(workdir, data_file, rcfile)
    index.baseline = baseline
    return index


def build_module_index(workdir: Path) -> dict[str, str]:
    """Map importable module names to repo-relative paths.

    coverage records a test context as a dotted module path, but which dotted
    path depends on how pytest imported the file: ``tests/test_cart.py`` is
    ``tests.test_cart`` when the directory is a package and plain ``test_cart``
    when it is not (rootdir insertion). Resolving the name back to a pytest node
    id therefore needs a real index of the tree, not string surgery. Bare stems
    are only registered when unambiguous, so a repo with two ``test_utils.py``
    files degrades to "unresolvable" rather than to "wrong test".
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for path in workdir.rglob("*.py"):
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        rel = path.relative_to(workdir).as_posix()
        dotted = rel[:-3].replace("/", ".")
        index[dotted] = rel
        stem = path.stem
        if stem in index and index[stem] != rel:
            ambiguous.add(stem)
        else:
            index.setdefault(stem, rel)
    for stem in ambiguous:
        index.pop(stem, None)
    return index


def _read_coverage(workdir: Path, data_file: Path, rcfile: Path) -> CoverageIndex:
    index = CoverageIndex()
    modules = build_module_index(workdir)
    try:
        import coverage
    except ImportError:  # pragma: no cover - coverage is a hard dep in practice
        return index
    if not data_file.exists():
        return index

    cov = coverage.Coverage(data_file=str(data_file), config_file=str(rcfile))
    cov.load()
    data = cov.get_data()
    for abs_path in data.measured_files():
        rel = _relative(abs_path, workdir)
        if rel is None:
            continue
        index.covered[rel] = set(data.lines(abs_path) or [])
        try:
            # coverage stores relative paths (relative_files=True) but resolves
            # source against *its* cwd, which is not ours -- hand it the real one.
            _, statements, _, _, _ = cov.analysis2(str(workdir / rel))
            index.statements[rel] = set(statements)
        except Exception:
            # Deliberately do NOT fall back to "statements == covered lines".
            # That fallback makes every uncovered line invisible and turns an
            # untested hunk into a passing coverage check. Leaving the file out
            # of the statement map makes the check abstain instead.
            continue
        by_line: dict[int, set[str]] = {}
        try:
            raw = data.contexts_by_lineno(abs_path)
        except Exception:
            raw = {}
        for lineno, contexts in raw.items():
            node_ids = {
                nid for nid in (context_to_node_id(c, modules) for c in contexts) if nid
            }
            if node_ids:
                by_line[lineno] = node_ids
                index.all_tests |= node_ids
        index.tests_by_line[rel] = by_line
    return index


def _relative(abs_path: str, workdir: Path) -> str | None:
    p = Path(abs_path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.relative_to(workdir).as_posix()
    except ValueError:
        return None


def context_to_node_id(context: str, modules: dict[str, str]) -> str | None:
    """Turn coverage's dotted test context into a runnable pytest node id.

    ``tests.test_math.TestAdd.test_negative`` (or ``test_math.TestAdd...``)
    becomes ``tests/test_math.py::TestAdd::test_negative``. We take the longest
    dotted prefix present in the module index, so class-nested tests survive.
    The empty context -- code executed at import time, outside any test -- maps
    to None and is simply not attributed to any test.
    """
    context = (context or "").split("|", 1)[0].strip()
    if not context:
        return None
    if "::" in context:
        return context
    parts = context.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        rel = modules.get(".".join(parts[:cut]))
        if rel:
            rest = parts[cut:]
            return "::".join([rel, *rest]) if rest else None
    return None
