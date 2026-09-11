from triage.mutate import generate_mutants

SRC = """def f(a: int, b: int) -> int:
    if a > b:
        return a + b
    raise ValueError("a must exceed b")
"""


def test_mutants_are_restricted_to_target_lines():
    only_line_3 = generate_mutants("f.py", SRC, {3})
    assert {m.lineno for m in only_line_3} == {3}
    assert any(m.operator == "arith" for m in only_line_3)


def test_every_executable_line_gets_at_least_one_probe():
    src = "def f(x: int) -> None:\n    cache = {}\n    cache[x] = x\n"
    assert {m.lineno for m in generate_mutants("f.py", src, {2, 3})} >= {2, 3}


def test_exception_messages_are_not_mutated():
    mutants = generate_mutants("f.py", SRC, {1, 2, 3, 4})
    assert not [m for m in mutants if m.operator == "str-const"]


def test_mutants_stay_syntactically_valid_and_keep_line_numbers():
    for mutant in generate_mutants("f.py", SRC, {1, 2, 3, 4}):
        compile(mutant.source, "m", "exec")
        assert len(mutant.source.splitlines()) == len(SRC.splitlines())


def test_limit_spreads_across_lines_rather_than_truncating():
    src = "def f(a: int, b: int) -> int:\n    x = a + 1\n    y = b * 2\n    return x - y\n"
    picked = generate_mutants("f.py", src, {2, 3, 4}, limit=3)
    assert len({m.lineno for m in picked}) == 3
