"""A test that never runs is worse than no test: forbid module-level ``test_*``.

CI runs ``python -m unittest discover``, and ``unittest`` collects ONLY methods
of ``TestCase`` subclasses. A module-level ``def test_x()`` is not an error and
not a warning -- it is silently never executed, and the run stays green. Verified
by driving the loader directly in ``TheLoaderReallySkipsThemTest`` below rather
than asserting it from documentation.

That is a live hazard for this tree specifically, because the property suite is
written as ``@given`` on ``TestCase`` methods while the technique's own idiom
elsewhere is a bare decorated function. Copy an idiomatic Hypothesis or pytest
example into ``tests/`` and the file imports, the decorator applies, nothing
runs, and nothing says so. The convention was recorded in three docstrings
(``tests/property/__init__.py``, ``tests/property/base.py``,
``tests/brokers/automanager/test_position_manager_properties.py``) and enforced
nowhere.

SCOPE, deliberately narrow. This catches ONE shape: a top-level function whose
name begins with ``test``. It does not look at classes without test methods --
35 of those exist and every one is a legitimate private base (``_DoorCase``,
``PropertyTestCase``, ...), so a gate there would be a false-positive minefield.

It finds nothing today. That is the point of adding it while the tree is clean.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

TEST_TREE = "apps/alphalens-research/tests"

# The prefix `unittest` itself uses for method discovery (`TestLoader`'s default
# `testMethodPrefix`), so the gate and the runner agree on what "looks like a
# test" means.
TEST_PREFIX = "test"


def _module_level_test_functions(source: str) -> list[str]:
    """Names of top-level ``test*`` functions in ``source``.

    Only the module body is walked: a nested ``def test_...`` inside a method is
    a closure, not a discovery candidate, and flagging it would be noise.
    """
    tree = ast.parse(source)
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith(TEST_PREFIX)
    ]


def _python_test_sources():
    for path in (WORKSPACE_ROOT / TEST_TREE).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class NoBareTestFunctionTest(unittest.TestCase):
    def test_no_module_level_test_function_exists(self) -> None:
        offenders: list[str] = []
        for path in _python_test_sources():
            for name in _module_level_test_functions(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(WORKSPACE_ROOT)}::{name}")
        self.assertEqual(
            offenders,
            [],
            "module-level test functions are NEVER run by `unittest discover` and the "
            "suite stays green without them; move each into a TestCase subclass:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scan_reaches_a_real_number_of_files(self) -> None:
        # Anti-rot: a glob that silently stops matching turns the gate above
        # into a tautology. The tree had ~700 files when this was written.
        self.assertGreater(len(list(_python_test_sources())), 300)


class ThePositiveControlTest(unittest.TestCase):
    """The detector must actually detect. Without this the gate above passes
    forever if the AST walk is wrong, because the tree is clean today."""

    def test_a_bare_test_function_is_found(self) -> None:
        found = _module_level_test_functions("def test_bare():\n    assert False\n")
        self.assertEqual(found, ["test_bare"])

    def test_an_async_bare_test_function_is_found(self) -> None:
        found = _module_level_test_functions("async def test_bare_async():\n    assert False\n")
        self.assertEqual(found, ["test_bare_async"])

    def test_a_testcase_method_is_not_flagged(self) -> None:
        source = (
            "import unittest\nclass T(unittest.TestCase):\n    def test_real(self):\n        pass\n"
        )
        self.assertEqual(_module_level_test_functions(source), [])

    def test_a_nested_function_is_not_flagged(self) -> None:
        source = "def helper():\n    def test_inner():\n        pass\n    return test_inner\n"
        self.assertEqual(_module_level_test_functions(source), [])

    def test_a_helper_that_merely_mentions_test_is_not_flagged(self) -> None:
        # `latest_...` starts with neither `test` nor anything discovery cares
        # about; the rule is a prefix, not a substring, exactly as the loader's.
        self.assertEqual(_module_level_test_functions("def latest_thing():\n    pass\n"), [])


class TheLoaderReallySkipsThemTest(unittest.TestCase):
    """The premise, executed rather than cited: `unittest` does not collect a
    module-level test function even when it would fail loudly if it ran."""

    def test_unittest_collects_the_method_and_not_the_bare_function(self) -> None:
        import types

        module = types.ModuleType("_bare_probe")
        source = (
            "import unittest\n"
            "def test_bare():\n"
            "    raise AssertionError('this would fail if it ever ran')\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_real(self):\n"
            "        pass\n"
        )
        exec(compile(source, "_bare_probe", "exec"), module.__dict__)
        suite = unittest.TestLoader().loadTestsFromModule(module)
        collected = [t.id().rsplit(".", 1)[-1] for t in suite._tests[0]]  # type: ignore[attr-defined]
        self.assertEqual(collected, ["test_real"])
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual((result.failures, result.errors), ([], []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
