"""Importing the CLI must not mutate ``os.environ`` (#1176).

``load_dotenv()`` used to sit at module scope in ``alphalens_cli/main.py``, so
it ran on IMPORT. Sixteen test modules import that module, and every one of
them silently loaded the operator's real root ``.env`` into the test process.

Two consequences, both bad:

  * a test could pass only because a real credential happened to be present.
    ``tests.brokers.test_saxo_sim_only_rail`` did exactly that — green inside
    full discovery, ``SaxoAuthError: SAXO_APP_KEY ... not set`` when its module
    ran alone. The suite's own ordering was load-bearing.
  * anything reached from a test ran with live API keys in the environment, so
    a missed mock could make a real, billable vendor call.

Reading a dotenv file is a PROCESS ENTRY concern: it belongs in the console
script, the only place a human really starts the CLI. In-process callers
(``CliRunner``, plain imports) bring their own environment and must keep it.

The AST gate below is the part that runs everywhere, CI included. The
behavioural check needs a discoverable dotenv file to have anything to leak, so
it skips loudly rather than passing vacuously when there is none.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

# tests/ is two levels below the workspace root: apps/alphalens-research/tests.
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PIPELINE_ROOT = WORKSPACE_ROOT / "apps" / "alphalens-pipeline"
CLI_MAIN = PIPELINE_ROOT / "alphalens_cli" / "main.py"
PYPROJECT = PIPELINE_ROOT / "pyproject.toml"

CONSOLE_SCRIPT = "alphalens"
DOTENV_CALL = "load_dotenv"


def _is_dotenv_call(node: ast.AST) -> bool:
    """``load_dotenv(...)`` or ``dotenv.load_dotenv(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == DOTENV_CALL
    return isinstance(func, ast.Attribute) and func.attr == DOTENV_CALL


def import_time_dotenv_lines(source: str) -> list[int]:
    """Line numbers of ``load_dotenv`` calls that run when the module is
    imported.

    A function BODY is deferred, so it does not count. Everything else does —
    including class bodies, and including a function's decorators and argument
    defaults, which are evaluated at def time (this is the same trap that makes
    ``typer.Option`` defaults import-time values).
    """
    lines: list[int] = []

    def visit(node: ast.AST, *, deferred: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                # Decorators and defaults evaluate now; the body does not.
                for evaluated_now in [*child.decorator_list, child.args]:
                    visit_node(evaluated_now, deferred=deferred)
                for stmt in child.body:
                    visit_node(stmt, deferred=True)
                continue
            visit_node(child, deferred=deferred)

    def visit_node(node: ast.AST, *, deferred: bool) -> None:
        if not deferred and isinstance(node, ast.Call) and _is_dotenv_call(node):
            lines.append(node.lineno)
        visit(node, deferred=deferred)

    visit(ast.parse(source), deferred=False)
    return sorted(lines)


def _console_script_target() -> tuple[str, str]:
    """``("alphalens_cli.main", "main")`` from ``[project.scripts]``."""
    scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]
    module, _, attr = scripts[CONSOLE_SCRIPT].partition(":")
    return module, attr


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


class TestNoDotenvAtImportTime(unittest.TestCase):
    def test_no_importable_module_loads_dotenv_at_import_time(self):
        """Both packages, not just ``main.py``.

        ``alphalens_cli`` and ``alphalens_pipeline`` are imported by tests, by
        each other and by the research lab, so ANY module-scope
        ``load_dotenv()`` in them reintroduces the same leak. Research
        ``scripts/`` are excluded on purpose: a script IS a process entry, so
        loading a dotenv file at its top level is the correct place.
        """
        offenders: list[str] = []
        for package in (PIPELINE_ROOT / "alphalens_cli", PIPELINE_ROOT / "alphalens_pipeline"):
            for module in sorted(package.rglob("*.py")):
                for line in import_time_dotenv_lines(module.read_text(encoding="utf-8")):
                    offenders.append(f"{module.relative_to(WORKSPACE_ROOT)}:{line}")
        self.assertEqual(
            offenders,
            [],
            f"{DOTENV_CALL}() runs at import time in: {offenders}. Importing "
            "these packages must not mutate os.environ — move the call into "
            "the console-script entry point.",
        )

    def test_detector_flags_a_module_scope_call(self):
        """Positive control: the gate above must not pass vacuously."""
        source = "from dotenv import load_dotenv\n\nload_dotenv()\n"
        self.assertEqual(import_time_dotenv_lines(source), [3])

    def test_detector_flags_a_call_hidden_in_an_argument_default(self):
        """Second positive control: a default is evaluated at def time, so
        parking the call there is still an import-time side effect."""
        source = "def main(_loaded=load_dotenv()):\n    pass\n"
        self.assertEqual(import_time_dotenv_lines(source), [1])

    def test_detector_flags_a_call_in_a_class_body(self):
        """A class body runs when the class statement is executed, so at
        module scope it is import time. Pinned because it is the one shape a
        reader is most likely to mistake for deferred and 'fix' into a miss.
        """
        source = "class C:\n    X = load_dotenv()\n"
        self.assertEqual(import_time_dotenv_lines(source), [2])

    def test_detector_flags_a_call_inside_a_module_scope_lambda(self):
        """Deliberate over-strictness, recorded so nobody relaxes it by
        accident: a lambda BODY is not evaluated at def time, so this is a
        false alarm in the strict sense. The gate still reports it — for a
        credential leak a false alarm is cheap and a miss is not, and a
        module-scope lambda wrapping load_dotenv has no legitimate use.
        """
        source = "loader = lambda: load_dotenv()\n"
        self.assertEqual(import_time_dotenv_lines(source), [1])

    def test_detector_accepts_a_call_inside_a_function_body(self):
        source = "def main():\n    load_dotenv()\n"
        self.assertEqual(import_time_dotenv_lines(source), [])

    def test_detector_accepts_a_call_inside_a_nested_function_body(self):
        """Deferral is inherited: an inner def inside a deferred body stays
        deferred."""
        source = "def outer():\n    def inner():\n        load_dotenv()\n"
        self.assertEqual(import_time_dotenv_lines(source), [])


class TestConsoleScriptStillLoadsDotenv(unittest.TestCase):
    """The fix must MOVE the call, not delete it — production reads its keys
    from the repo-root ``.env`` on every ``alphalens ...`` invocation."""

    def setUp(self):
        self.tree = ast.parse(CLI_MAIN.read_text(encoding="utf-8"))
        self.module, self.attr = _console_script_target()

    def test_console_script_points_at_cli_main(self):
        self.assertEqual(self.module, "alphalens_cli.main")

    def test_console_script_entry_point_loads_dotenv(self):
        entry = _function_named(self.tree, self.attr)
        if entry is None:
            self.fail(
                f"[project.scripts] {CONSOLE_SCRIPT} names '{self.attr}', which "
                f"is not a module-level function in {CLI_MAIN.name}"
            )
        calls = [node for node in ast.walk(entry) if _is_dotenv_call(node)]
        self.assertTrue(
            calls,
            f"{self.attr}() is the only process entry point, so it must call "
            f"{DOTENV_CALL}() — otherwise the deployed CLI stops reading .env",
        )

    def test_module_main_guard_uses_the_same_entry_point(self):
        """``python path/to/main.py`` must load .env too, or the two ways of
        starting the CLI disagree about the environment."""
        called: list[str] = []
        for node in self.tree.body:
            if not isinstance(node, ast.If):
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    called.append(call.func.id)
        self.assertIn(
            self.attr,
            called,
            f"the `if __name__ == '__main__'` guard must call {self.attr}(), "
            "not the bare Typer app",
        )


class TestImportingTheCliLeavesTheEnvironmentAlone(unittest.TestCase):
    """Behavioural counterpart to the AST gate.

    Needs a dotenv file somewhere above the CLI package to have anything to
    leak. There is none in CI, so this SKIPS there rather than reporting a
    pass it did not earn.
    """

    def setUp(self):
        from dotenv import find_dotenv

        if not find_dotenv(usecwd=False):
            self.skipTest("no dotenv file discoverable from this checkout")

    def test_importing_alphalens_cli_main_adds_no_environment_variables(self):
        probe = (
            "import json, os, sys\n"
            "before = set(os.environ)\n"
            "import alphalens_cli.main\n"
            "print(json.dumps(sorted(set(os.environ) - before)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=WORKSPACE_ROOT,
            env={**os.environ, "PYTHONWARNINGS": "ignore"},
        )
        added = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            added,
            [],
            f"importing the CLI injected these variables into the process environment: {added}",
        )


if __name__ == "__main__":
    unittest.main()
