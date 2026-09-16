"""Home-directory isolation shared by the broker CLI tests.

Moved out of the deleted ``test_arm_cli.py`` (#1469), which several CLI test
modules imported it from.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


def _isolate_home(case: unittest.TestCase) -> Path:
    """Patch ``Path.home()`` to a fresh, empty temp directory for ``case``.

    The arming and journal commands run the legacy-layout guard
    (``state_paths.assert_no_legacy_flat_state``, ADR 0016 D4) before touching
    a journal, so every test that invokes one must be isolated from the REAL
    ``~/.alphalens/broker_orders/`` tree. On a developer machine running the
    live SIM daemon that tree genuinely holds a pre-ADR-0016 flat layout and
    would make these hermetic tests fail depending on host state."""
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def _seed_legacy_flat_state(home: Path) -> Path:
    """Write ONE pre-migration flat legacy journal file under ``home`` —
    enough to trip ``assert_no_legacy_flat_state`` (ADR 0016 D4)."""
    legacy_dir = home / ".alphalens" / "broker_orders"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "submissions.jsonl"
    legacy_file.write_text("", encoding="utf-8")
    return legacy_file
