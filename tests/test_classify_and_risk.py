import ast

from triage.classify import classify, has_executable_change, token_identical
from triage.model import CheckResult, Hunk, Label, Status
from triage.purity import analyze
from triage.risk import RiskModel, features


def hunk(path, added, removed=None):
    return Hunk(path, "", min(added), max(added), added, removed or {})


def test_paths_drive_the_obvious_labels():
    assert classify(hunk("tests/test_a.py", {1: "assert True"})) is Label.TEST
    assert classify(hunk("README.md", {1: "hello"})) is Label.DOCS
    assert classify(hunk("setup.cfg", {1: "x = 1"})) is Label.CONFIG


def test_comment_only_python_changes_are_docs():
    assert classify(hunk("m.py", {1: "# explain the hack", 2: ""})) is Label.DOCS
    assert not has_executable_change(hunk("m.py", {1: "   # note"}))


def test_reformatting_is_recognised_as_a_refactor_claim():
    h = hunk("m.py", {1: "x = foo(a, b)"}, {1: "x = foo(a,   b)"})
    assert token_identical(h) and classify(h) is Label.REFACTOR


def test_reordered_statements_are_not_token_identical():
    assert not token_identical(hunk("m.py", {1: "a = 1", 2: "b = 2"}, {1: "b = 2", 2: "a = 3"}))


def test_purity_accepts_a_self_contained_function():
    tree = ast.parse("import math\ndef f(a: int) -> float:\n    return math.sqrt(abs(a))\n")
    assert analyze(tree.body[1], tree).pure


def test_purity_rejects_hidden_state_and_io():
    for src in [
        "import random\ndef f(a: int) -> int:\n    return random.randint(0, a)\n",
        "def f(a: int) -> int:\n    global C\n    C = a\n    return a\n",
        "def f(xs: list) -> None:\n    xs.sort()\n",
        "def f(a: int):\n    return db.fetch(a)\n",
    ]:
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        assert not analyze(fn, tree).pure, src


def test_purity_requires_annotations_to_generate_inputs():
    tree = ast.parse("def f(a):\n    return a + 1\n")
    report = analyze(tree.body[0], tree)
    assert not report.pure and "annotation" in report.reasons[0]


def test_risk_ranks_the_untested_and_sensitive_above_the_rest():
    model = RiskModel.load(None)
    uncovered = CheckResult("covered", Status.FAIL, "", {"ratio": 0.0})
    covered = CheckResult("covered", Status.PASS, "", {"ratio": 1.0})
    survived = CheckResult("constrained", Status.FAIL, "", {"ms_delta": 0.0})
    killed = CheckResult("constrained", Status.PASS, "", {"ms_delta": 1.0})

    risky = model.score(features(hunk("auth/session.py", {1: "if token == secret:"}),
                                [uncovered, survived]))
    safe = model.score(features(hunk("util.py", {1: "return x"}), [covered, killed]))
    assert risky > 0.8 > safe


def test_a_disproved_refactor_claim_dominates_the_score():
    model = RiskModel.load(None)
    f = features(hunk("m.py", {1: "return x"}),
                 [CheckResult("equivalent", Status.FAIL, "", {})])
    assert f["broken_refactor_claim"] == 1.0
    assert model.score(f) > 0.5


def test_a_test_that_proves_nothing_outranks_an_ordinary_test_edit():
    model = RiskModel.load(None)
    useless = hunk("tests/test_m.py", {1: "    assert callable(f)"})
    useless.label = Label.TEST
    ordinary = hunk("tests/test_m.py", {1: "    assert f(2) == 4"})
    ordinary.label = Label.TEST

    failed = CheckResult("effective", Status.FAIL, "detects nothing", {})
    abstained = CheckResult("effective", Status.ABSTAIN, "existing test edited", {})

    assert model.score(features(useless, [failed])) > model.score(
        features(ordinary, [abstained])
    )
