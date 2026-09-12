"""Check 4 - Effective? Does this test detect anything this PR changed?

Test code cannot be verified by the suite that contains it: a weakened
assertion passes happily. But there is one question about a test that *can* be
answered mechanically, and it is the question that matters most:

    Revert the source files this PR touched, leave the new tests in place,
    and run them. A test that still passes pins down nothing the PR did.

This is the standard "does the test fail for the right reason" discipline,
automated. A new test that passes against the pre-change implementation is
either testing something that already worked (harmless but not evidence) or,
far more often, asserting something too weak to notice the behaviour it is
supposed to guard.

Scope, stated precisely, because this check is easy to over-claim:

  * It proves the test *constrains* behaviour the PR introduced. It does not
    prove the test asserts the *correct* behaviour -- same standard as the
    mutation check, and the same reason "verified" never means "correct".
  * It is only conclusive for *newly added* test functions. An edit to an
    existing test can weaken it while the test still fails against old code
    (because the PR also changed the behaviour it covers), so modified tests
    stay residual and a human still reads them.
  * Reverting happens one *definition* at a time, not one file at a time (see
    triage.revert for why the file-level version is worthless), and only for
    the definitions the test under examination actually references. A test that
    references nothing the change touched fails the check without being run:
    it cannot be evidence about this PR whatever it asserts.
"""

from __future__ import annotations

import ast
from pathlib import Path

from triage.config import Config
from triage.model import CheckResult, Hunk, Status
from triage.revert import changed_symbols, referenced_names, revert_symbols
from triage.runner import run_tests
from triage.workspace import reverted_files

NAME = "effective"


def check(
    hunk: Hunk,
    workdir: Path,
    base_sources: dict[str, str | None],
    old_test_source: str | None,
    cfg: Config,
) -> CheckResult:
    """`base_sources` maps each changed non-test path to its pre-change content
    (None when the PR created it, so reverting means removing it)."""
    test_path = workdir / hunk.path
    if not hunk.path.endswith(".py") or not test_path.exists():
        return CheckResult(NAME, Status.SKIP, "not a Python test file", {})
    if not base_sources:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "this change touches no source outside the tests, so there is no "
            "behaviour change for the test to detect",
            {},
        )

    new_source = test_path.read_text()
    covering = enclosing_tests(new_source, hunk.added_linenos)
    if not covering:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "no test function encloses these lines (fixture, import or helper "
            "edit), so there is nothing to run against the old implementation",
            {},
        )
    new_nodes = [name for name, _ in covering]

    # Only revert what these tests actually touch. Reverting everything would
    # break the module's imports and make every test look effective.
    wanted: set[str] = set()
    for _, node in covering:
        wanted |= referenced_names(node)

    patches: dict[str, str] = {}
    targets: dict[str, set[str]] = {}
    for rel, old_source in base_sources.items():
        source_path = workdir / rel
        if not source_path.exists():
            continue
        current = source_path.read_text()
        hit = wanted & changed_symbols(old_source, current)
        if hit:
            targets[rel] = hit
            patches[rel] = revert_symbols(current, old_source, hit)

    if not targets:
        names = ", ".join(_leaf(n) for n in new_nodes[:3])
        return CheckResult(
            NAME, Status.FAIL,
            f"{names} references nothing this change touched, so it cannot "
            "detect anything the PR did, whatever it asserts",
            {"tests": new_nodes, "referenced": sorted(wanted)[:20]},
        )

    previously_defined = (
        collect_test_names(old_test_source) if old_test_source else set()
    )
    added = [n for n in new_nodes if _leaf(n) not in previously_defined]
    modified = [n for n in new_nodes if _leaf(n) in previously_defined]

    node_ids = [f"{hunk.path}::{n}" for n in new_nodes]
    with reverted_files(workdir, patches):
        result = run_tests(
            workdir, cfg, node_ids=node_ids, timeout=cfg.mutant_timeout_seconds * 2
        )

    evidence = {
        "tests": node_ids,
        "added_tests": [_leaf(n) for n in added],
        "modified_tests": [_leaf(n) for n in modified],
        "reverted_definitions": {k: sorted(v) for k, v in targets.items()},
        "passed_against_old_code": result.ok,
    }

    if result.timed_out:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "the test timed out against the pre-change implementation", evidence,
        )
    if result.no_tests_ran:
        return CheckResult(
            NAME, Status.ABSTAIN,
            "pytest collected none of these tests against the old tree", evidence,
        )
    if result.ok:
        names = ", ".join(_leaf(n) for n in new_nodes[:3])
        reverted = ", ".join(sorted({d for v in targets.values() for d in v})[:4])
        return CheckResult(
            NAME, Status.FAIL,
            f"{names} still passes with {reverted} reverted to the pre-change "
            "implementation: it does not pin down any behaviour this PR introduced",
            evidence,
        )
    if modified:
        return CheckResult(
            NAME, Status.ABSTAIN,
            f"{len(modified)} existing test(s) were edited; failing against old "
            "code does not rule out an assertion having been weakened, so a "
            "human still reads this",
            evidence,
        )
    return CheckResult(
        NAME, Status.PASS,
        f"{len(added)} new test(s) fail against the pre-change implementation, "
        "so they constrain what this PR changed",
        evidence,
    )


def enclosing_tests(source: str, linenos: list[int]) -> list[tuple[str, ast.AST]]:
    """(pytest node name, AST node) for each test function covering `linenos`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[str, ast.AST]] = []
    for node, prefix in _walk_definitions(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = getattr(node, "end_lineno", start)
        if any(start <= ln <= end for ln in linenos):
            hits.append(("::".join([*prefix, node.name]), node))
    return hits


def collect_test_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node, _ in _walk_definitions(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def _walk_definitions(tree: ast.AST, prefix: tuple[str, ...] = ()):
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef):
            yield node, prefix
            yield from _walk_definitions(node, (*prefix, node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, prefix


def _leaf(node_name: str) -> str:
    return node_name.rsplit("::", 1)[-1]
