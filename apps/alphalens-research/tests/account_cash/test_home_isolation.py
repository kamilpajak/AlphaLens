"""The cash book's own operator-state guard (#1689, #1696).

This package writes a MONEY store. The guard in ``tests/brokers/__init__.py``
does not reach here, so ``tests/account_cash/__init__.py`` installs its own and
these tests prove it discriminates in BOTH directions -- a guard that cannot
refute anything has tested nothing, and one that refuses unconditionally would
satisfy the "must raise" half vacuously.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.account_cash import paths

from tests.brokers.automanager.home_isolation import (
    IsolatedHomeTestCase,
    OperatorStateReadError,
)


class TheGuardIsInstalledForThisPackage(unittest.TestCase):
    """Deliberately NOT an :class:`IsolatedHomeTestCase` -- it asserts the
    unisolated case, which is what the guard exists to catch."""

    def test_an_unisolated_read_of_the_cash_root_refuses(self) -> None:
        with self.assertRaises(OperatorStateReadError) as ctx:
            paths.cash_events_path("live")
        message = str(ctx.exception)
        self.assertIn("#1696", message)
        self.assertIn(IsolatedHomeTestCase.__name__, message)

    def test_a_patched_home_that_points_back_at_the_operator_still_fails(self) -> None:
        """The guard compares the RESOLVED root, so pointing a patch at the real
        home does not get past it.

        A plain ``TestCase`` on purpose: inside an ``IsolatedHomeTestCase`` the
        ``setUp`` patch is already active, so ``Path.home()`` in the
        ``return_value`` argument evaluates to the TEMP home and the guard
        correctly does not fire.
        """
        with mock.patch("pathlib.Path.home", return_value=Path.home()):
            with self.assertRaises(OperatorStateReadError):
                paths.cash_events_path("live")


class AnIsolatedReadPassesTheGuard(IsolatedHomeTestCase):
    """The other direction. The guard fires on the resolved path, so a module
    that hardcoded a root would pass the "must raise" test above by never
    resolving the home at all."""

    def test_an_isolated_read_lands_under_the_temp_home(self) -> None:
        self.assertEqual(
            paths.cash_events_path("live"),
            self.home / ".alphalens" / "account_cash" / "live" / "cash_events.jsonl",
        )

    def test_the_temporary_home_is_empty(self) -> None:
        self.assertEqual(list(self.home.iterdir()), [])


class ThisPackagesOwnInitIsWhatInstallsTheGuard(unittest.TestCase):
    """The guard reaches this package because ``tests/account_cash/__init__.py``
    installs it -- not because something else happened to.

    Every other test here imports ``home_isolation``, which imports
    ``tests.brokers``, whose own ``__init__`` installs the same guard. So
    commenting out this package's install leaves every assertion above green
    while a module that imports only production code carries no guard at all.
    That is the #1696 shape one package over, and it is why this runs in a
    SUBPROCESS: the claim is about what one import does, and it cannot be
    tested from inside a process where other test modules have already run.

    **Measured residual, so it is not discovered later.** What discriminates is
    the package ``__init__`` as a WHOLE: deleting both of its lines turns the
    first test below red, while deleting only the
    ``install_operator_state_guard()`` CALL leaves it green, because the import
    line above it imports ``tests.brokers``, whose own ``__init__`` installs the
    same guard. The explicit call stays anyway -- the guard should be installed
    on purpose and not as another package's side effect -- but no test here can
    refute its removal, and a test that cannot refute has tested nothing.

    Both halves construct a ``Path`` and nothing else -- no read, no write.
    """

    def _probe(self, *, import_the_package: bool) -> tuple[int, str]:
        import subprocess
        import sys

        program = "from alphalens_pipeline.account_cash import paths\n"
        if import_the_package:
            program += "import tests.account_cash  # installs the guard\n"
        program += 'print(paths.cash_events_path("live"))\n'
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stderr

    def test_importing_this_package_makes_an_unisolated_resolve_refuse(self) -> None:
        code, stderr = self._probe(import_the_package=True)
        self.assertNotEqual(code, 0, stderr)
        self.assertIn("OperatorStateReadError", stderr)

    def test_without_that_import_the_same_resolve_is_unguarded(self) -> None:
        """The negative control. Without it the test above passes for any
        reason at all, including a guard installed by something unrelated."""
        code, stderr = self._probe(import_the_package=False)
        self.assertEqual(code, 0, stderr)


if __name__ == "__main__":
    unittest.main()
