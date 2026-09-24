"""Tests for ``paper/sizing.py::planned_blended_entry``.

This file also pinned ``build_exit_declaration``, the exit the brief producer
declared for every brief pick (#1414). Both went with the producer in #1552:
every pick is a hand-written document now, and its author states the exit.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.paper.sizing import planned_blended_entry


def _setup(
    *,
    entries: list[tuple[float, float]],
    tps: list[tuple[float, float]] | None = None,
    stop: float = 90.0,
    status: str = "OK",
    atr: float | None = None,
) -> dict:
    setup: dict = {
        "status": status,
        "disaster_stop": stop,
        "entry_tiers": [{"limit": p, "alloc_pct": w} for p, w in entries],
        "tp_tranches": [{"target": p, "tranche_pct": w} for p, w in (tps or [])],
    }
    if atr is not None:
        setup["atr"] = atr
    return setup


class TestPlannedBlendedEntry(unittest.TestCase):
    def test_single_tier_returns_the_limit(self) -> None:
        self.assertAlmostEqual(planned_blended_entry(_setup(entries=[(100.0, 100.0)])), 100.0)

    def test_alloc_weighted_over_all_intended_tiers(self) -> None:
        setup = _setup(entries=[(100.0, 75.0), (90.0, 25.0)])
        self.assertAlmostEqual(planned_blended_entry(setup), 97.5)

    def test_equal_weight_fallback_when_alloc_pct_absent(self) -> None:
        setup = {"entry_tiers": [{"limit": 100.0}, {"limit": 90.0}]}
        self.assertAlmostEqual(planned_blended_entry(setup), 95.0)

    def test_drops_non_positive_limit_tiers(self) -> None:
        setup = {
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 50.0},
                {"limit": 0.0, "alloc_pct": 50.0},
            ]
        }
        self.assertAlmostEqual(planned_blended_entry(setup), 100.0)

    def test_none_when_no_entry_tiers(self) -> None:
        self.assertIsNone(planned_blended_entry(_setup(entries=[])))

    def test_none_when_not_a_mapping(self) -> None:
        self.assertIsNone(planned_blended_entry(object()))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
