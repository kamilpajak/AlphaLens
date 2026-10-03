"""Home-directory isolation for the broker tests.

Every mutable broker-state path resolves ``Path.home()`` AT CALL TIME through
``state_paths`` (ADR 0016 D2-D4), so patching ``pathlib.Path.home`` is the one
seam that redirects the journals, the pick inbox, the KILL gate and the
execution-quality telemetry together -- without moving every other
home-relative path, which is what setting ``$HOME`` would do.

Two forms of the same mechanism:

* :class:`IsolatedHomeTestCase` -- the base class for any test that runs a
  daemon tick. Each test gets its OWN fresh temporary home, not one shared by
  the module: several of these tests seed a journal, and a shared home would
  let one test read what another seeded. Required by
  ``test_home_isolation_gate.py`` (#1696).
* :func:`isolate_home` -- the function form, for a class that already has a
  base or that needs a second home inside one test.

Why it is not optional (#1696): without it a test reads the REAL
``~/.alphalens/broker_orders/<env>/`` tree, which on a developer machine
running the live SIM daemon holds journal lines that arrive while the suite is
running. The same commit produced a green run and 49 errors forty minutes
apart.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import state_paths


def isolate_home(case: unittest.TestCase) -> Path:
    """Patch ``Path.home()`` to a fresh, empty temp directory for ``case``.

    Returns the temporary home. Cleanup is registered on ``case``, so the
    patch and the directory outlive the call and end with the test.
    """
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


class IsolatedHomeTestCase(unittest.TestCase):
    """A ``TestCase`` whose ``Path.home()`` is a fresh temp dir per test.

    ``self.home`` is that directory. A subclass overriding ``setUp`` must call
    ``super().setUp()``.
    """

    home: Path

    def setUp(self) -> None:
        super().setUp()
        self.home = isolate_home(self)


def seed_legacy_flat_state(home: Path) -> Path:
    """Write ONE pre-migration flat legacy journal file under ``home`` --
    enough to trip ``assert_no_legacy_flat_state`` (ADR 0016 D4)."""
    legacy_dir = home / ".alphalens" / "broker_orders"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "submissions.jsonl"
    legacy_file.write_text("", encoding="utf-8")
    return legacy_file


# --- The guard that makes forgetting impossible (#1696) ----------------------

# Captured at import, BEFORE any test patches ``Path.home()``: the one state
# root a test must never resolve.
_OPERATOR_STATE_ROOT = Path.home() / state_paths._ALPHALENS_HOME_DIRNAME


class OperatorStateReadError(AssertionError):
    """A broker test resolved the operator's real ``~/.alphalens`` tree."""


def _guarded_alphalens_home() -> Path:
    resolved = Path.home() / state_paths._ALPHALENS_HOME_DIRNAME
    if resolved == _OPERATOR_STATE_ROOT:
        raise OperatorStateReadError(
            f"this test resolved the operator's real state root {resolved} -- its "
            "verdict would depend on what the live daemon has written. Inherit "
            f"{IsolatedHomeTestCase.__name__} (or call isolate_home(self)) so the "
            "test gets its own temporary home. See #1696."
        )
    return resolved


def install_operator_state_guard() -> None:
    """Make every unisolated broker-state read fail loud, for the whole suite.

    Installed once, from ``tests/brokers/__init__.py``, so it covers every test
    in the package -- including ones written after this change. An AST rule
    cannot do that job: the first version of this gate looked for tick entry
    points and missed four tests that reach a journal by another path.

    Isolated tests are unaffected: their ``Path.home()`` is a temp directory,
    so the resolved root differs from the captured operator root.
    """
    state_paths._alphalens_home = _guarded_alphalens_home
