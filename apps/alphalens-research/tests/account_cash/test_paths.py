"""The cash book's path seam: one home resolver, per-env roots (#1689, memo section 3).

The store root is new and NOT inside ``broker_orders/<env>/``: that root is the
daemon's live money-path state, and a derived record among four queue journals
would eventually be read as queue state. Per-env, never pooled.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from alphalens_pipeline import account_cash
from alphalens_pipeline.account_cash import paths

from tests.brokers.automanager.home_isolation import (
    IsolatedHomeTestCase,
    OperatorStateReadError,
)


def _package_sources() -> list[Path]:
    package_dir = Path(account_cash.__file__).resolve().parent
    return sorted(package_dir.rglob("*.py"))


def _returns_a_path(node: ast.FunctionDef) -> bool:
    """Whether ``node``'s RETURN ANNOTATION mentions ``Path`` anywhere.

    Not ``node.returns.id == "Path"``. That form is an ``ast.Name`` and only
    matches a bare ``-> Path``: a builder annotated ``-> Path | None`` is an
    ``ast.BinOp`` and ``getattr(node.returns, "id", "")`` answers ``""``, so it
    was invisible to this scan and to every subTest loop fed by it. Walking the
    annotation covers ``Path | None``, ``Optional[Path]`` and
    ``dict[str, Path]`` alike.
    """
    if node.returns is None:
        return False
    return any(
        isinstance(inner, ast.Name) and inner.id == "Path" for inner in ast.walk(node.returns)
    )


def _public_path_builder_names(tree: ast.Module) -> list[str]:
    """Every public module-level function of ``tree`` annotated to return a ``Path``.

    Takes a tree rather than reading a file, so the predicate itself can be run
    against a synthetic module -- a scan whose predicate is never exercised on
    the shape it is meant to catch is how both blind spots above survived.
    """
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and _returns_a_path(node)
    )


def _public_path_builders() -> list[str]:
    """Every public path builder in the package, from the package's own source.

    Derived rather than typed out here, so a builder added later is covered
    without anyone remembering. Mirrors
    ``test_home_isolation_gate._public_state_path_builders`` with ONE change:
    that predicate skips any node where ``len(args) != len(defaults)``, i.e.
    anything not callable with no arguments, and every builder here takes a
    REQUIRED ``env``. A verbatim copy would therefore return ``[]`` and turn
    the whole subTest loop below into a no-op, which is why the floor is the
    vacuity control and must not be lowered.
    """
    found: list[str] = []
    for source in _package_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        found.extend(_public_path_builder_names(tree))
    return sorted(found)


#: The two call shapes that resolve the operator's home directory.
#: ``expanduser`` is here because the guard's predicate matches ``home`` only,
#: so ``Path("~/.alphalens").expanduser()`` is BOTH unguarded at runtime and
#: invisible to the anti-rot gate -- the exact hazard this module's docstring
#: states in prose and this scan exists to hold.
_HOME_RESOLVING_ATTRS = frozenset({"home", "expanduser"})


def _home_resolving_function_names(tree: ast.Module) -> list[str]:
    """Every function in ``tree`` whose body calls ``.home()`` or ``.expanduser()``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        resolves_home = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _HOME_RESOLVING_ATTRS
            for call in ast.walk(node)
        )
        if resolves_home:
            found.append(node.name)
    return sorted(found)


def _functions_resolving_the_home_directory() -> list[tuple[str, str]]:
    """``(module stem, function name)`` for every home resolver in the package."""
    found: list[tuple[str, str]] = []
    for source in _package_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        found.extend((source.stem, name) for name in _home_resolving_function_names(tree))
    return sorted(found)


class EveryPathBuilderGoesThroughTheOneSeam(unittest.TestCase):
    """A plain ``TestCase``: the assertion IS that an unisolated call refuses."""

    def test_every_path_builder_is_covered_by_the_one_seam(self) -> None:
        builders = _public_path_builders()
        self.assertGreaterEqual(len(builders), 6, builders)
        for name in builders:
            with self.subTest(builder=name):
                with self.assertRaises(OperatorStateReadError):
                    getattr(paths, name)("live")

    def test_only_one_function_in_the_package_resolves_the_home_directory(self) -> None:
        """``Path.home()`` through :func:`_alphalens_home` is the one form the
        guard can wrap -- it replaces a NAMED builder. A function resolving
        ``Path("~/.alphalens").expanduser()`` itself could not be guarded at
        all, so the scan counts it here and this assertion is what refuses it.
        The scan's own control is in
        :class:`BothScansSeeTheShapeTheyExistToCatch`."""
        self.assertEqual(_functions_resolving_the_home_directory(), [("paths", "_alphalens_home")])


class BothScansSeeTheShapeTheyExistToCatch(unittest.TestCase):
    """Positive controls on the two predicates above, run against a synthetic
    module rather than the real one.

    Both scans used to be blind to the same single escape: a public builder
    annotated ``-> Path | None`` whose body resolved
    ``Path("~/.alphalens").expanduser()``. ``_public_path_builders`` missed it
    because the annotation is an ``ast.BinOp`` and the predicate read
    ``node.returns.id``; ``_functions_resolving_the_home_directory`` missed it
    because the predicate matched ``home`` only. Neither scan can report that
    it is blind, so the shape is fed to each one directly.
    """

    _MUTANT = (
        "from pathlib import Path\n"
        "\n"
        "def legacy_cash_events_path(env: str) -> Path | None:\n"
        '    return Path("~/.alphalens").expanduser() / env\n'
    )

    def test_a_path_or_none_builder_is_found_by_the_builder_scan(self) -> None:
        tree = ast.parse(self._MUTANT)
        self.assertEqual(_public_path_builder_names(tree), ["legacy_cash_events_path"])

    def test_an_expanduser_body_is_found_by_the_home_resolver_scan(self) -> None:
        tree = ast.parse(self._MUTANT)
        self.assertEqual(_home_resolving_function_names(tree), ["legacy_cash_events_path"])

    def test_a_bare_path_annotation_is_still_found(self) -> None:
        """The widening must not have replaced the original shape."""
        tree = ast.parse(
            "from pathlib import Path\n\ndef p(env: str) -> Path:\n    return Path(env)\n"
        )
        self.assertEqual(_public_path_builder_names(tree), ["p"])

    def test_a_non_path_return_and_a_private_name_are_both_skipped(self) -> None:
        """The negative control: a predicate that matched everything would
        satisfy the three tests above and report every function in the
        package as a path builder."""
        tree = ast.parse(
            "from pathlib import Path\n"
            "\n"
            "def text(env: str) -> str:\n"
            "    return env\n"
            "\n"
            "def _private(env: str) -> Path:\n"
            "    return Path(env)\n"
        )
        self.assertEqual(_public_path_builder_names(tree), [])
        self.assertEqual(_home_resolving_function_names(tree), [])


class TheEnvironmentIsPartOfThePath(unittest.TestCase):
    def test_an_unknown_environment_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            paths.validate_environment("prod")
        self.assertIn("prod", str(ctx.exception))

    def test_the_vocabulary_is_exactly_sim_and_live(self) -> None:
        self.assertEqual(paths.ENVIRONMENTS, (paths.ENV_SIM, paths.ENV_LIVE))
        self.assertEqual((paths.ENV_SIM, paths.ENV_LIVE), ("sim", "live"))

    def test_the_module_reads_no_environment_variable(self) -> None:
        """A write must never be steered by an ambient variable (#1377): the
        CLI resolves ``--env`` and passes it in, so there is no second spelling
        of ``ALPHALENS_BROKER_ENVIRONMENT`` to disagree with the keeper's.

        An AST walk over the IMPORTS, not a substring search for
        ``os.environ``: the module docstring names that env var on purpose, to
        say why it does not read one, and a text match cannot tell a stated
        reason from a call.
        """
        tree = ast.parse(inspect.getsource(paths))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertEqual(imported, ["__future__", "pathlib"], imported)

    def test_no_builder_takes_a_default_environment(self) -> None:
        """``env`` is REQUIRED on every builder. A default would reintroduce
        the ambient-value shape by another route: a caller that forgot the
        argument would write to whichever environment the default names."""
        for name in _public_path_builders():
            with self.subTest(builder=name):
                signature = inspect.signature(getattr(paths, name))
                self.assertEqual(
                    [
                        parameter.name
                        for parameter in signature.parameters.values()
                        if parameter.default is not inspect.Parameter.empty
                    ],
                    [],
                )


class TheLayoutUnderAnIsolatedHome(IsolatedHomeTestCase):
    def test_each_environment_gets_its_own_root(self) -> None:
        """SIM and LIVE are distinct measurement sources and must never pool
        (the ``exec_quality_parquet`` precedent), and neither root may sit
        under the daemon's own live money-path state root."""
        sim_root = paths.account_cash_root("sim")
        live_root = paths.account_cash_root("live")
        self.assertNotEqual(sim_root, live_root)
        for root in (sim_root, live_root):
            with self.subTest(root=root):
                self.assertNotIn("broker_orders", root.parts)
                self.assertEqual(root.parent, self.home / ".alphalens" / "account_cash")

    def test_the_four_journals_and_the_lock_are_siblings_under_the_env_root(self) -> None:
        root = paths.account_cash_root("live")
        self.assertEqual(
            [
                paths.cash_events_path("live"),
                paths.trade_rows_path("live"),
                paths.balances_path("live"),
                paths.sweeps_path("live"),
                paths.sweep_lock_path("live"),
            ],
            [
                root / "cash_events.jsonl",
                root / "trade_rows.jsonl",
                root / "balances.jsonl",
                root / "sweeps.jsonl",
                root / ".sweep.lock",
            ],
        )

    def test_every_builder_resolves_under_the_patched_home(self) -> None:
        """The other direction of the guard: a builder that hardcoded a root
        would satisfy the "must raise" loop above by never resolving home."""
        for name in _public_path_builders():
            with self.subTest(builder=name):
                self.assertIn(self.home, getattr(paths, name)("live").parents)


if __name__ == "__main__":
    unittest.main()
