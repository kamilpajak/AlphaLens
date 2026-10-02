"""Per-function structure measurements (``scripts/arch/hotspots.py``).

SonarCloud already computes cognitive complexity, but it cannot answer the
question this audit asks. Its ``S3776`` rule is PER FUNCTION, so an
11 000-line file whose functions are each under the threshold passes it, and
the Sonar job runs ``continue-on-error: true`` anyway. ``ruff`` ignores
``PLR0915`` (statement count), ``PLR0912`` (branches) and ``PLR0911``
(returns) repo-wide, so function size is ungated here by choice.

So these numbers are measured directly, and the tests below pin the arithmetic
on hand-worked fixtures — a metric nobody can recompute by hand is a metric the
report cannot defend.
"""

from __future__ import annotations

import textwrap
import unittest

from scripts.arch import hotspots


def _m(source: str) -> dict[str, hotspots.FunctionMetrics]:
    return {f.qualname: f for f in hotspots.measure_source(textwrap.dedent(source).lstrip())}


class CyclomaticComplexityTest(unittest.TestCase):
    def test_a_straight_line_function_scores_one(self) -> None:
        self.assertEqual(_m("def f():\n    return 1\n")["f"].cyclomatic, 1)

    def test_each_if_adds_one(self) -> None:
        got = _m(
            """
            def f(a, b):
                if a:
                    return 1
                if b:
                    return 2
                return 3
            """
        )["f"]
        self.assertEqual(got.cyclomatic, 3)

    def test_a_boolean_operator_adds_one_per_extra_operand(self) -> None:
        """`a and b and c` is two decisions, not one."""
        self.assertEqual(_m("def f(a, b, c):\n    return a and b and c\n")["f"].cyclomatic, 3)

    def test_loops_and_except_handlers_count_one_each(self) -> None:
        got = _m(
            """
            def f(xs):
                for x in xs:
                    try:
                        pass
                    except ValueError:
                        pass
                    except KeyError:
                        pass
                while True:
                    break
                return x
            """
        )["f"]
        # 1 base + for + 2 handlers + while
        self.assertEqual(got.cyclomatic, 5)

    def test_a_with_statement_is_not_a_decision(self) -> None:
        """Standard McCabe does not treat entering a context manager as a
        branch. Counting it inflated every figure against the definition the
        report claims to use."""
        got = _m(
            """
            def f(path):
                with open(path) as fh, open(path) as gh:
                    return fh, gh
            """
        )["f"]
        self.assertEqual(got.cyclomatic, 1)

    def test_a_decision_in_a_nested_class_body_belongs_to_the_function(self) -> None:
        """A class body executes when the enclosing function runs, so its
        branches are the function's. Its methods are measured separately."""
        got = _m(
            """
            def outer(flag):
                class Inner:
                    if flag:
                        value = 1

                    def method(self, a):
                        if a:
                            return 1
                        return 2

                return Inner
            """
        )
        self.assertEqual(got["outer"].cyclomatic, 2, "base + the class body's if")
        self.assertEqual(got["outer.Inner.method"].cyclomatic, 2, "measured in its own right")

    def test_a_comprehension_if_is_counted_like_a_plain_if(self) -> None:
        """An external review called this a double count. It is not: the
        comprehension's own loop is the extra point, and the `if` plus its
        boolean operator are counted exactly as in a plain statement."""
        plain = _m("def f(a, b):\n    if a and b:\n        return 1\n    return 2\n")["f"]
        comp = _m("def f(xs, a, b):\n    return [x for x in xs if a and b]\n")["f"]
        bare = _m("def f(xs):\n    return [x for x in xs]\n")["f"]
        self.assertEqual(plain.cyclomatic, 3, "base + if + boolop")
        self.assertEqual(bare.cyclomatic, 2, "base + the comprehension's loop")
        self.assertEqual(
            comp.cyclomatic,
            4,
            "base + loop + if + boolop: one more than the statement, and that one is the loop",
        )

    def test_an_else_does_not_add_a_decision(self) -> None:
        """Positive control: `if/else` is one decision. A counter that walked
        `orelse` as a branch would say two."""
        got = _m(
            """
            def f(a):
                if a:
                    return 1
                else:
                    return 2
            """
        )["f"]
        self.assertEqual(got.cyclomatic, 2)

    def test_a_nested_function_is_measured_separately_not_folded_in(self) -> None:
        got = _m(
            """
            def outer(a):
                def inner(b):
                    if b:
                        return 1
                    return 2

                return inner(a)
            """
        )
        self.assertEqual(got["outer"].cyclomatic, 1)
        self.assertEqual(got["outer.inner"].cyclomatic, 2)


class ShapeTest(unittest.TestCase):
    def test_a_method_is_qualified_by_its_class(self) -> None:
        got = _m(
            """
            class Loop:
                def tick(self):
                    return 1
            """
        )
        self.assertIn("Loop.tick", got)

    def test_length_counts_the_lines_the_function_spans(self) -> None:
        got = _m(
            """
            def f():
                a = 1
                b = 2
                return a + b
            """
        )["f"]
        self.assertEqual(got.lines, 4)

    def test_it_reports_parameter_count_and_return_count(self) -> None:
        got = _m(
            """
            def f(a, b, *, c=1, **kw):
                if a:
                    return 1
                return 2
            """
        )["f"]
        self.assertEqual(got.parameters, 4)
        self.assertEqual(got.returns, 2)

    def test_an_async_function_is_measured(self) -> None:
        self.assertIn("f", _m("async def f():\n    return 1\n"))


class ModuleStateTest(unittest.TestCase):
    def test_it_finds_module_level_mutable_bindings(self) -> None:
        names = hotspots.module_level_mutable_state(
            textwrap.dedent(
                """
                CONSTANT = 3
                NAME = "x"
                CACHE: dict[str, int] = {}
                SEEN = set()
                ITEMS = []
                """
            ).lstrip()
        )
        self.assertEqual(sorted(names), ["CACHE", "ITEMS", "SEEN"])

    def test_an_immutable_constant_is_not_state(self) -> None:
        """Positive control: a tuple and a frozenset are not mutable state, so
        a detector keying on 'is a collection' would over-report."""
        names = hotspots.module_level_mutable_state(
            "PAIR = (1, 2)\nKEYS = frozenset({'a'})\nTOTAL = 3\n"
        )
        self.assertEqual(names, [])


class CallSiteTest(unittest.TestCase):
    def test_it_finds_calls_whose_attribute_name_matches(self) -> None:
        found = hotspots.call_sites(
            textwrap.dedent(
                """
                def go(broker):
                    broker.place_bracket(1)
                    broker.cancel(2)

                class K:
                    def run(self, b):
                        b.place_stop(3)
                """
            ).lstrip(),
            names=("place_bracket", "place_stop"),
        )
        self.assertEqual(
            sorted((c.qualname, c.callee) for c in found),
            [("K.run", "place_stop"), ("go", "place_bracket")],
        )

    def test_a_matching_name_that_is_not_called_is_not_a_call_site(self) -> None:
        """Positive control: reading or passing the attribute is not calling it."""
        found = hotspots.call_sites(
            "def go(b):\n    f = b.place_bracket\n    return f\n", names=("place_bracket",)
        )
        self.assertEqual(found, [])


class RealFileTest(unittest.TestCase):
    """Anti-rot: the file the audit is about must actually be measurable."""

    def test_the_control_loop_is_measured_and_is_large(self) -> None:
        from scripts.arch.graph import repo_root

        path = (
            repo_root()
            / "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py"
        )
        self.assertTrue(path.is_file(), "the audit's headline file must exist")
        functions = hotspots.measure_source(path.read_text(encoding="utf-8"))
        self.assertGreater(len(functions), 100)


if __name__ == "__main__":
    unittest.main()
