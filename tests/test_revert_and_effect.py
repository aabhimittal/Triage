"""Definition-level reverting and the test-effectiveness check."""

import ast

from triage.checks.test_effect_check import (
    collect_test_names,
    enclosing_tests,
)
from triage.revert import changed_symbols, referenced_names, revert_symbols, top_level_spans

OLD = '''LIMIT = 10


def scale(n: int) -> int:
    return n * 2


def keep(n: int) -> int:
    return n
'''

NEW = '''LIMIT = 20


def scale(n: int) -> int:
    return n * 3


def keep(n: int) -> int:
    return n


def added(n: int) -> int:
    return n - 1
'''


def test_changed_symbols_finds_edits_and_additions_only():
    assert changed_symbols(OLD, NEW) == {"LIMIT", "scale", "added"}


def test_reindentation_alone_is_not_a_change():
    reformatted = OLD.replace("    return n * 2", "    return n * 2  ")
    assert "scale" not in changed_symbols(OLD, reformatted)


def test_reverting_one_definition_leaves_the_others_alone():
    result = revert_symbols(NEW, OLD, {"scale"})
    assert "return n * 2" in result          # scale restored
    assert "def added" in result             # unrelated new code survives
    assert "LIMIT = 20" in result            # untargeted change survives
    ast.parse(result)


def test_reverting_a_new_definition_removes_it():
    result = revert_symbols(NEW, OLD, {"added"})
    assert "def added" not in result
    ast.parse(result)


def test_spans_cover_decorated_functions():
    src = "import functools\n\n\n@functools.cache\ndef f(n: int) -> int:\n    return n\n"
    span = top_level_spans(src)["f"]
    assert span.text.startswith("@functools.cache")


def test_referenced_names_include_attribute_bases():
    node = ast.parse("def t():\n    assert money.split(3) == cart\n")
    assert {"money", "cart"} <= referenced_names(node)


def test_enclosing_tests_addresses_class_nested_tests():
    src = "class TestX:\n    def test_y(self):\n        assert 1\n"
    names = [name for name, _ in enclosing_tests(src, [3])]
    assert names == ["TestX::test_y"]
    assert collect_test_names(src) == {"test_y"}
