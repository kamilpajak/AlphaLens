"""The guard that keeps broker tests off the operator's live state (#1696).

49 tests in this package used to read the REAL
``~/.alphalens/broker_orders/<env>/standalone_stops.jsonl`` -- the journal the
live SIM daemon writes -- because nothing redirected it. Their verdict depended
on what the daemon had appended minutes earlier: the same commit ran green at
11:33 and produced 49 errors at 12:0x, after a trailing-stop line arrived.

``tests/brokers/__init__.py`` installs
:func:`home_isolation.install_operator_state_guard` for the whole package, so a
test that forgets to isolate FAILS instead of reading operator data. These
tests prove the guard discriminates in both directions -- a guard that cannot
refute anything has tested nothing.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.automanager import state_paths

from tests.brokers.automanager.home_isolation import (
    _GUARDED_BUILDERS,
    IsolatedHomeTestCase,
    OperatorStateReadError,
    isolate_home,
)


def _public_state_path_builders() -> list[str]:
    """Every public ``state_paths`` function that returns a ``Path`` and is
    callable with no arguments.

    Derived from the module's own source rather than typed out here, so a
    builder added later is covered without anyone remembering. The predicate is
    the RETURN TYPE, not "calls ``_alphalens_home``": five of these reach the
    state root indirectly, through ``broker_orders_root``, and a direct-call
    predicate silently missed them."""
    tree = ast.parse(inspect.getsource(state_paths))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        if len(node.args.args) != len(node.args.defaults):
            continue  # not callable with no arguments
        if getattr(node.returns, "id", "") != "Path":
            continue
        found.append(node.name)
    return sorted(found)


class TheGuardIsInstalledForThisPackage(unittest.TestCase):
    """Deliberately NOT an :class:`IsolatedHomeTestCase` -- it asserts the
    unisolated case, which is what the guard exists to catch."""

    def test_an_unisolated_state_read_fails_loud(self) -> None:
        with self.assertRaises(OperatorStateReadError) as ctx:
            state_paths.standalone_stops_path()
        message = str(ctx.exception)
        self.assertIn("#1696", message)
        self.assertIn(IsolatedHomeTestCase.__name__, message)

    def test_the_guard_names_the_root_it_refused(self) -> None:
        with self.assertRaises(OperatorStateReadError) as ctx:
            state_paths.broker_orders_root()
        self.assertIn(str(Path.home() / ".alphalens"), str(ctx.exception))

    def test_every_state_path_builder_is_covered_by_the_one_seam(self) -> None:
        """Positive control on REACH. The guard sits on ``_alphalens_home``, so
        it must cover every builder that resolves it -- and the list is DERIVED
        from the module's own source, not typed out here, so a builder added
        later is covered without anyone remembering to add it."""
        builders = _public_state_path_builders()
        self.assertGreaterEqual(len(builders), 8, builders)
        for name in builders:
            with self.subTest(builder=name):
                with self.assertRaises(OperatorStateReadError):
                    getattr(state_paths, name)()


class AnIsolatedTestPassesTheGuard(IsolatedHomeTestCase):
    def test_the_journal_resolves_under_the_temporary_home(self) -> None:
        self.assertEqual(
            state_paths.standalone_stops_path(),
            self.home / ".alphalens" / "broker_orders" / "sim" / "standalone_stops.jsonl",
        )

    def test_the_temporary_home_is_empty(self) -> None:
        self.assertEqual(list(self.home.iterdir()), [])

    def test_each_test_gets_its_own_home(self) -> None:
        """Isolation is per TEST, not per module: several tests seed a journal
        and must not see what an earlier test seeded."""
        type(self)._seen = getattr(type(self), "_seen", set())
        self.assertNotIn(self.home, type(self)._seen)
        type(self)._seen.add(self.home)


class TheFunctionFormWorksTheSameWay(unittest.TestCase):
    def test_isolate_home_also_satisfies_the_guard(self) -> None:
        home = isolate_home(self)
        self.assertEqual(state_paths.broker_orders_root().parents[1], home / ".alphalens")

    def test_a_patched_home_that_points_back_at_the_operator_still_fails(self) -> None:
        """The guard compares the RESOLVED root, so pointing a patch at the
        real home does not get past it."""
        with mock.patch("pathlib.Path.home", return_value=Path.home()):
            with self.assertRaises(OperatorStateReadError):
                state_paths.broker_orders_root()


_BROKER_PACKAGE_ROOTS = (
    "apps/alphalens-pipeline/alphalens_pipeline/brokers",
    "apps/alphalens-broker-contract/broker_contract",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _broker_tier_home_builders() -> set[tuple[str, str]]:
    """Every broker-tier function whose body calls ``Path.home()``.

    Read off the source of the broker packages, so a third path builder added
    later shows up here whether or not anyone remembers this gate. Returned as
    ``(module file stem, function name)`` pairs."""
    found: set[tuple[str, str]] = set()
    root = _repo_root()
    for relroot in _BROKER_PACKAGE_ROOTS:
        for path in sorted((root / relroot).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls_home = any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "home"
                    for call in ast.walk(node)
                )
                if calls_home:
                    found.add((path.stem, node.name))
    return found


class EveryBrokerTierHomeBuilderIsGuarded(unittest.TestCase):
    """Anti-rot. The guard is only as complete as its list of builders, and the
    ADR 0016 seam is NOT that list: `saxo/tokens.py` keeps its own join."""

    def test_the_guarded_set_matches_what_the_source_says_exists(self) -> None:
        guarded = {(module.__name__.rsplit(".", 1)[-1], attr) for module, attr in _GUARDED_BUILDERS}
        self.assertEqual(
            _broker_tier_home_builders(),
            guarded,
            "a broker-tier function resolves Path.home() and is not covered by "
            "_GUARDED_BUILDERS, so a test can read operator state past the guard (#1696)",
        )

    def test_the_source_scan_finds_something(self) -> None:
        self.assertGreaterEqual(len(_broker_tier_home_builders()), 2)


class TheSeamIsActuallyUsedByTheBuilders(IsolatedHomeTestCase):
    """Positive control on the OTHER direction. The guard fires on the resolved
    path, so a builder that hardcoded a path would pass the "must raise" test
    above by never resolving the home at all. These assert the opposite: under
    isolation every builder lands inside this test's own temporary home."""

    def test_every_state_path_builder_resolves_under_the_temporary_home(self) -> None:
        for name in _public_state_path_builders():
            with self.subTest(builder=name):
                self.assertTrue(
                    self.home in getattr(state_paths, name)().parents,
                    f"{name}() does not resolve under the patched home",
                )

    def test_the_token_store_path_also_resolves_under_it(self) -> None:
        from alphalens_pipeline.brokers.saxo.tokens import default_token_store_path

        self.assertTrue(self.home in default_token_store_path().parents)


class ASubclassCannotLoseIsolationSilently(unittest.TestCase):
    """Positive control on `__init_subclass__`: a setUp override that does not
    chain is refused at class-definition time, not at the first state read."""

    def test_a_non_chaining_setup_is_refused(self) -> None:
        with self.assertRaises(TypeError) as ctx:

            class Forgot(IsolatedHomeTestCase):
                def setUp(self) -> None:
                    self.value = 1

        message = str(ctx.exception)
        self.assertIn("super().setUp()", message)
        self.assertIn("#1696", message)

    def test_a_chaining_setup_is_accepted(self) -> None:
        class Chains(IsolatedHomeTestCase):
            def setUp(self) -> None:
                super().setUp()
                self.value = 1

        self.assertTrue(issubclass(Chains, IsolatedHomeTestCase))

    def test_a_subclass_with_no_setup_of_its_own_is_accepted(self) -> None:
        class Inherits(IsolatedHomeTestCase):
            pass

        self.assertTrue(issubclass(Inherits, IsolatedHomeTestCase))


if __name__ == "__main__":
    unittest.main()
