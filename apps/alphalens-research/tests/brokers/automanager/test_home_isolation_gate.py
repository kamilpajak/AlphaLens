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


if __name__ == "__main__":
    unittest.main()
