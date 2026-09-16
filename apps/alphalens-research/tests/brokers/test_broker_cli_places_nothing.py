"""No ``alphalens`` CLI module may place or amend an order (#1466).

The daemon (``alphalens broker manage``) is the only placement path. A command
hands it work by appending an armed pick, never by calling the broker's write
surface itself. ``broker submit`` was the exception; it placed brackets without
a pick, and it is gone.

This gate replaces the ``_cli_broker(mutating=...)`` flag that used to refuse
ad-hoc placement on LIVE. That flag was declared by the caller, so a future
command could opt out by passing ``False``. An AST walk over every CLI module
cannot be opted out of.

``cancel_order`` is deliberately allowed: cancelling is risk-reducing, and the
LIVE manual-flatten runbook depends on ``broker cancel``.

The walk matches attribute accesses AND string literals, so ``getattr`` with a
literal name, or a name kept in a constant, is caught too. What it cannot see: a
method name assembled at runtime, and a placement call inside a helper outside
``alphalens_cli`` that a command delegates to. Neither shape exists today.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import alphalens_cli

# Every write method on the broker contract that creates or changes an order,
# plus the precheck call that only exists to precede a placement.
_FORBIDDEN_ATTRIBUTE = re.compile(r"^(place_\w+|amend_\w+|precheck_bracket_order)$")


def _referenced_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _offending_attributes(source: str) -> list[str]:
    names = (_referenced_name(node) for node in ast.walk(ast.parse(source)))
    return sorted({name for name in names if name and _FORBIDDEN_ATTRIBUTE.match(name)})


class CliModulesPlaceNothingTest(unittest.TestCase):
    def test_the_gate_flags_a_placement_call(self):
        # Positive control: without it a broken pattern would pass every module.
        source = "def run(broker, request):\n    broker.place_bracket_order(request)\n"

        self.assertEqual(_offending_attributes(source), ["place_bracket_order"])

    def test_the_gate_flags_a_method_named_by_string(self):
        # The removed `submit` reached precheck through getattr with a string,
        # which an attribute-only walk cannot see.
        source = "def run(broker):\n    return getattr(broker, 'precheck_bracket_order', None)\n"

        self.assertEqual(_offending_attributes(source), ["precheck_bracket_order"])

    def test_the_gate_flags_a_method_name_held_in_a_variable(self):
        source = (
            "METHOD = 'place_market_order'\n\ndef run(broker):\n    getattr(broker, METHOD)()\n"
        )

        self.assertEqual(_offending_attributes(source), ["place_market_order"])

    def test_the_gate_allows_cancel(self):
        source = "def run(broker, order_id):\n    broker.cancel_order(order_id)\n"

        self.assertEqual(_offending_attributes(source), [])

    def test_no_cli_module_references_an_order_write_method(self):
        package_dir = Path(alphalens_cli.__file__).parent
        modules = sorted(package_dir.rglob("*.py"))
        self.assertTrue(modules, "no CLI modules found; the gate would pass vacuously")

        offenders = {
            str(path.relative_to(package_dir)): found
            for path in modules
            if (found := _offending_attributes(path.read_text(encoding="utf-8")))
        }

        self.assertEqual(offenders, {}, "only the broker daemon may place or amend orders")


if __name__ == "__main__":
    unittest.main()
