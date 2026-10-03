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

import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import state_paths
from alphalens_pipeline.brokers.saxo import tokens as saxo_tokens


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

    ``self.home`` is that directory. A subclass overriding ``setUp`` MUST chain
    to this one, and :meth:`__init_subclass__` refuses the class at definition
    time if it does not -- a missing ``super().setUp()`` would otherwise leave
    the test reading the real home, and it would only be noticed if the test
    happened to touch broker state.
    """

    home: Path

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        own_setup = cls.__dict__.get("setUp")
        if own_setup is None:
            return
        try:
            source = inspect.getsource(own_setup)
        except (OSError, TypeError):
            return  # generated or wrapped setUp: nothing to read, nothing to claim
        if "super().setUp()" in source or f"{IsolatedHomeTestCase.__name__}.setUp(self)" in source:
            return
        raise TypeError(
            f"{cls.__module__}.{cls.__qualname__}.setUp overrides "
            f"{IsolatedHomeTestCase.__name__}.setUp without calling super().setUp(), "
            "so the test would run against the operator's real home. See #1696."
        )

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
# root a test must never resolve. This assumes ``$HOME`` is stable from the
# moment this module is imported, which holds for every runner the repo uses.
_OPERATOR_STATE_ROOT = Path.home() / state_paths._ALPHALENS_HOME_DIRNAME

# Every broker-tier function that resolves a path under that root.
# ``state_paths._alphalens_home`` is the ADR 0016 seam and covers the journals,
# the pick inbox, the KILL gate and the execution-quality telemetry. It is NOT
# the whole story: ``saxo/tokens.py`` keeps its own ``Path.home()`` join for the
# OAuth token store, which `saxo/client.py` probes with ``.is_file()``. A guard
# on the seam alone would let a test ask whether the operator is logged in.
# ``test_home_isolation_gate.py`` reads this set off the source of the broker
# packages, so a third builder added later cannot stay unguarded quietly.
_GUARDED_BUILDERS: tuple[tuple[object, str], ...] = (
    (state_paths, "_alphalens_home"),
    (saxo_tokens, "default_token_store_path"),
)

_GUARD_MARKER = "_operator_state_guard"


class OperatorStateReadError(AssertionError):
    """A broker test resolved a path under the operator's real ``~/.alphalens``."""


def _is_operator_path(resolved: Path) -> bool:
    return resolved == _OPERATOR_STATE_ROOT or _OPERATOR_STATE_ROOT in resolved.parents


def _guarded(label: str, original):
    def guarded() -> Path:
        resolved = original()
        if _is_operator_path(resolved):
            raise OperatorStateReadError(
                f"{label}() resolved {resolved}, under the operator's real state root "
                f"{_OPERATOR_STATE_ROOT} -- this test's verdict would depend on what "
                f"the live daemon has written. Inherit {IsolatedHomeTestCase.__name__} "
                "(or call isolate_home(self)) so the test gets its own temporary "
                "home. See #1696."
            )
        return resolved

    setattr(guarded, _GUARD_MARKER, True)
    return guarded


def install_operator_state_guard() -> None:
    """Make every unisolated broker-state read fail loud, for the whole suite.

    Installed once, from ``tests/brokers/__init__.py``, so it covers every test
    in the package -- including ones written after this change. An AST rule
    cannot do that job: the first version of this gate looked for tick entry
    points and missed four tests that reach a journal by another path.

    Isolated tests are unaffected: their ``Path.home()`` is a temp directory,
    so nothing they resolve sits under the captured operator root.

    Deliberately installed at package import rather than from unittest's
    ``load_tests`` hook: ``load_tests`` runs under DISCOVERY only, so
    ``python -m unittest tests.brokers.automanager.test_control_loop`` -- how a
    single module is normally run -- would carry no guard at all.
    """
    for module, attr in _GUARDED_BUILDERS:
        current = getattr(module, attr)
        if getattr(current, _GUARD_MARKER, False):
            continue  # idempotent: the marker rides on the wrapper, not on a flag
        setattr(module, attr, _guarded(f"{module.__name__}.{attr}", current))
