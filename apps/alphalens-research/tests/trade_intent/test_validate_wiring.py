"""Where ``validate_intent`` is wired, and where it deliberately is NOT (#1404).

Two facts about the code that would otherwise live only in a PR description.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BROKER_CLI = (
    _REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli" / "commands" / "broker.py"
)
_THEMATIC_CLI = (
    _REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli" / "commands" / "thematic.py"
)
_MANUAL_INTENT = (
    _REPO_ROOT
    / "apps"
    / "alphalens-pipeline"
    / "alphalens_pipeline"
    / "brokers"
    / "automanager"
    / "manual_intent.py"
)


def _function(source: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {source}")


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class WhereTheValidatorRunsTest(unittest.TestCase):
    """Every document that reaches a pick inbox passed `validate_intent`.

    Until #1469 the brief path (`broker arm`) deliberately skipped it, on the
    grounds that `parse_brief_to_spec` carries non-positive limit rows the
    validator refuses. Measured on the VPS briefs on 2026-09-16, 0 of 995
    plannable rows carry one, and the brief producer now sends its document
    through the door like any other author.
    """

    def test_the_door_calls_it(self) -> None:
        self.assertIn("validate_intent", _calls(_function(_BROKER_CLI, "arm_intent_command")))

    def test_arm_manual_reaches_it_through_the_builder(self) -> None:
        # A second path, until `arm-manual` goes in #1470.
        self.assertIn("validate_intent", _calls(_function(_MANUAL_INTENT, "build_manual_intent")))

    def test_the_brief_producer_does_not_bypass_the_door(self) -> None:
        # Positive control for the claim above: the producer writes a document
        # and appends nothing, so the door is the only way its picks arm.
        self.assertNotIn("arm_pick", _calls(_function(_THEMATIC_CLI, "intent_command")))


class NoImportCycleTest(unittest.TestCase):
    """`validate` imports `sizing`, and `sizing` imports `trade_intent.schema`.

    That is only acyclic because `trade_intent/__init__.py` has no imports of its
    own. Adding a convenience re-export of `validate` there would close the loop,
    and the failure would surface as an ImportError somewhere far away — so pin
    it here, in both directions.
    """

    def _import_in_order(self, first: str, second: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", f"import {first}; import {second}"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_sizing_then_validate(self) -> None:
        self._import_in_order("broker_contract.sizing", "broker_contract.trade_intent.validate")

    def test_validate_then_sizing(self) -> None:
        self._import_in_order("broker_contract.trade_intent.validate", "broker_contract.sizing")

    def test_trade_intent_package_import_does_not_pull_in_validate(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, broker_contract.trade_intent; "
                "print('broker_contract.trade_intent.validate' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class TheContractPackageStaysStdlibOnlyTest(unittest.TestCase):
    def test_validate_imports_nothing_outside_stdlib_and_the_contract(self) -> None:
        tree = ast.parse(
            (
                _REPO_ROOT
                / "apps"
                / "alphalens-broker-contract"
                / "broker_contract"
                / "trade_intent"
                / "validate.py"
            ).read_text(encoding="utf-8")
        )
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
        allowed = {
            "__future__",
            "broker_contract",
            "collections",
            "dataclasses",
            "math",
            "re",
            "types",
            "typing",
        }
        self.assertEqual(roots - allowed, set())
        # Positive control: a module that imported nothing would satisfy the
        # assertion above vacuously.
        self.assertIn("broker_contract", roots)


if __name__ == "__main__":
    unittest.main()
