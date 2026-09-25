"""A tripwire on spec section 2.1: the engine modules that hold the run
configuration and the path classes must not reach the environment, a file or a
deployment value.

This is a TRIPWIRE, not the proof. The proof that nothing is filled in on the
caller's behalf is behavioural and lives in ``test_config.py`` (an empty
mapping names every key, removing any leaf names exactly that leaf, no
dataclass field carries a default). What this file adds is an early, cheap
signal when a later edit reaches for ``os.environ`` or ``open``.

Why a local test and not a second rule in ``test_module_dependencies.py``: the
engine rule there is an allow-list that admits EVERY stdlib module (``os``,
``posix``, ``sys``, ``importlib`` included), and that file pins exactly one
``intent_replay`` rule. So this scanner is the only barrier for this
predicate, and it carries one positive control per arm, in the style of
``tests/brokers/test_broker_cli_places_nothing.py``.

What it does not catch, stated so nobody mistakes it for a proof: a bare
``__builtins__`` name, object-introspection gadgets, and anything a permitted
import (``broker_contract.failure``) might do on its own.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = WORKSPACE_ROOT / "apps" / "intent-replay" / "intent_replay"
PACKAGE = "intent_replay"

# Every import the two modules are allowed to make. An addition is a
# deliberate edit of this list, never an incidental one.
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "config.py": frozenset(
        {
            "__future__",
            "collections.abc",
            "dataclasses",
            "math",
            "types",
            "typing",
            "broker_contract.failure",
            "intent_replay.refusal",
        }
    ),
    "classification.py": frozenset(
        {
            "__future__",
            "collections.abc",
            "types",
            "typing",
            "broker_contract.failure",
            "intent_replay.refusal",
        }
    ),
}

# Names that reach the environment or the filesystem without ``import os``:
# the engine dependency rule admits every stdlib module, so each of these is a
# way around an import allow-list.
DENIED_NAMES: frozenset[str] = frozenset(
    {"__import__", "importlib", "sys", "posix", "nt", "builtins", "open", "environ", "getenv"}
)


def _imported_modules(tree: ast.Module) -> set[str]:
    """Dotted module names imported by ``import`` AND ``from ... import``,
    with a relative import resolved to its absolute name inside the package.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                modules.add(f"{PACKAGE}.{node.module}" if node.module else PACKAGE)
            elif node.module:
                modules.add(node.module)
    return modules


def denied_name_uses(source: str) -> list[str]:
    """Every use of a denied name: as a bare name, as an attribute, or as a
    string constant (``getattr(m, "environ")``). Empty for a clean module.
    """
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DENIED_NAMES:
            hits.append(f"name:{node.id}@{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in DENIED_NAMES:
            hits.append(f"attr:{node.attr}@{node.lineno}")
        elif isinstance(node, ast.Constant) and node.value in DENIED_NAMES:
            hits.append(f"str:{node.value}@{node.lineno}")
        elif isinstance(node, ast.alias) and node.name.split(".")[0] in DENIED_NAMES:
            hits.append(f"import:{node.name}")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in DENIED_NAMES:
            hits.append(f"from:{node.module}")
    return hits


def disallowed_imports(source: str, allowed: frozenset[str]) -> set[str]:
    return _imported_modules(ast.parse(source)) - allowed


class EngineModulesReadNothingTest(unittest.TestCase):
    def test_each_module_imports_only_its_allow_list(self) -> None:
        for name, allowed in ALLOWED_IMPORTS.items():
            with self.subTest(name):
                source = (PACKAGE_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(disallowed_imports(source, allowed), set())

    def test_each_module_uses_no_denied_name(self) -> None:
        for name in ALLOWED_IMPORTS:
            with self.subTest(name):
                source = (PACKAGE_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(denied_name_uses(source), [])


class ScannerPositiveControlsTest(unittest.TestCase):
    """One control per arm, so a dead arm cannot hide behind a live one."""

    def test_the_import_arm_fires_alone(self) -> None:
        # ``os`` is caught by the import allow-list, not by the denied names:
        # the two arms are separate, and this control shows the first alone.
        self.assertEqual(disallowed_imports("import os\n", frozenset({"math"})), {"os"})
        self.assertEqual(denied_name_uses("import os\n"), [])

    def test_the_from_import_arm_fires_alone(self) -> None:
        # An ``ast.Import``-only scan misses this form.
        self.assertEqual(disallowed_imports("from os import environ\n", frozenset()), {"os"})
        self.assertEqual(denied_name_uses("from os import environ\n"), ["import:environ"])

    def test_the_attribute_arm_fires_without_an_import(self) -> None:
        hits = denied_name_uses('__import__("os").environ["X"]\n')
        self.assertIn("attr:environ@1", hits)
        self.assertIn("name:__import__@1", hits)

    def test_the_bare_name_arm_fires_alone(self) -> None:
        self.assertEqual(denied_name_uses('open("f")\n'), ["name:open@1"])

    def test_the_string_constant_arm_fires_alone(self) -> None:
        self.assertEqual(denied_name_uses('getattr(m, "environ")\n'), ["str:environ@1"])

    def test_a_stdlib_bypass_module_is_named(self) -> None:
        # ``posix.environ`` is the same mapping as ``os.environ``.
        self.assertIn("from:posix", denied_name_uses("from posix import environ as e\n"))

    def test_a_relative_import_resolves_inside_the_package(self) -> None:
        self.assertEqual(
            disallowed_imports(
                "from .refusal import refuse\n", frozenset({"intent_replay.refusal"})
            ),
            set(),
        )

    def test_the_negative_control_is_clean(self) -> None:
        source = "import math\nfrom dataclasses import dataclass\nx = math.isfinite(1.0)\n"
        self.assertEqual(disallowed_imports(source, frozenset({"math", "dataclasses"})), set())
        self.assertEqual(denied_name_uses(source), [])


if __name__ == "__main__":
    unittest.main()
