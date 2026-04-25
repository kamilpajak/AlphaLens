"""Enforce that every layer/screener package declares an explicit lifecycle status.

Status markers (`__status__`, optional `__closed_date__`, `__closed_reason__`)
let a reader see at-a-glance which warstwa is live, archived, or research-only
without grepping CLAUDE.md and memory. The test is also the gate that prevents
adding a new layer silently — it must be added to LAYERS_WITH_STATUS below.
"""

from __future__ import annotations

import importlib
import unittest

VALID_STATUSES = {"ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"}

LAYERS_WITH_STATUS = (
    "alphalens.backtest",
    "alphalens.events",
    "alphalens.guru",
    "alphalens.macro",
    "alphalens.rotation",
    "alphalens.screeners.insider",
    "alphalens.screeners.lean",
    "alphalens.screeners.prescreener",
    "alphalens.screeners.themed",
    "alphalens.watchdog",
)


class TestLayerStatus(unittest.TestCase):
    def test_each_layer_declares_status(self):
        missing = []
        for pkg in LAYERS_WITH_STATUS:
            module = importlib.import_module(pkg)
            if not hasattr(module, "__status__"):
                missing.append(pkg)
        self.assertEqual(missing, [], f"packages missing __status__: {missing}")

    def test_status_value_in_allowlist(self):
        invalid = []
        for pkg in LAYERS_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status not in VALID_STATUSES:
                invalid.append((pkg, status))
        self.assertEqual(invalid, [], f"invalid __status__ values: {invalid}")

    def test_closed_layers_have_reason(self):
        """CLOSED and ARCHIVED layers should explain why — easy for future reader to act on."""
        missing_reason = []
        for pkg in LAYERS_WITH_STATUS:
            module = importlib.import_module(pkg)
            status = getattr(module, "__status__", None)
            if status in {"CLOSED", "ARCHIVED"} and not getattr(module, "__closed_reason__", None):
                missing_reason.append(pkg)
        self.assertEqual(
            missing_reason,
            [],
            f"CLOSED/ARCHIVED packages missing __closed_reason__: {missing_reason}",
        )


if __name__ == "__main__":
    unittest.main()
