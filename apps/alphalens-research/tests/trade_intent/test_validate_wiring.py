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


class TheBriefPathIsNotValidatedTest(unittest.TestCase):
    """`broker arm` must NOT call the validator, and the reason is measured.

    `parse_brief_to_spec` deliberately carries non-positive limit/target rows
    through (the money-half `compute_setup_plan` is what drops them) and keeps
    `order_ttl_days`'s 0 sentinel. Validating there would turn a documented
    tolerance into a refusal on the real-money path — the one thing #1404 says
    would be a defect rather than a feature.

    Concretely, such a brief would be refused TWICE over: a zero-limit tier makes
    `min(limit_price)` zero, so `stop_above_entry` fires as well as
    `entry_price_non_positive`. Dropping a single rule would not restore it,
    which is why the answer is "do not wire it", not "soften the rule".
    """

    def test_arm_command_does_not_call_validate_intent(self) -> None:
        self.assertNotIn("validate_intent", _calls(_function(_BROKER_CLI, "arm_command")))

    def test_arm_manual_reaches_it_through_the_builder(self) -> None:
        # The positive control: without it this suite would still pass if
        # `validate_intent` had been wired nowhere at all.
        self.assertIn("validate_intent", _calls(_function(_MANUAL_INTENT, "build_manual_intent")))


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
        allowed = {"broker_contract", "collections", "dataclasses", "types", "typing", "__future__"}
        self.assertEqual(roots - allowed, set())


if __name__ == "__main__":
    unittest.main()
