"""TDD red-phase tests for v7 cost model parity (Blocker #1, plan 2026-05-01).

Locks the "30bps RT applied to long-only path" semantic plus the L/S
caller-composition pattern (apply CostModel separately to each leg, sum drags
→ exactly 2× single-leg). Codifies the canonical retail long-only profile.

Per zen 2026-05-01: `CostModel.apply()` is correctly agnostic to long-only vs
L/S — the load-bearing test is caller composition, not internal engine
semantics.
"""

from __future__ import annotations

import unittest

from alphalens_research.attribution.cost_model import CostModel


class CostModelV7ParityTests(unittest.TestCase):
    def test_30bps_rt_long_only_drag_one_rebalance(self):
        # CostModel(annual_drag_bps=30) with 100% turnover, 252 periods/yr:
        # per-bar drag = 30 / 10000 / 252 = 1.190476e-5
        cm = CostModel(annual_drag_bps=30.0)
        net = cm.apply(
            daily_returns=[0.0] * 5,
            daily_turnover=[1.0] * 5,
            periods_per_year=252,
        )
        expected_drag = 30.0 / 10_000.0 / 252
        for value in net.tolist():
            self.assertAlmostEqual(value, -expected_drag, places=10)

    def test_long_short_composition_doubles_drag_of_single_leg(self):
        # Caller composition pattern: apply CostModel separately to each leg,
        # sum the drags → exactly 2× single-leg. Exercises the v7 L/S
        # diagnostic charging mechanic at 60bps total RT.
        cm = CostModel.from_profile("long_only_30bps")
        long_returns = [0.001, 0.002, -0.001, 0.0, 0.003]
        short_returns = [-0.0005, 0.001, 0.002, -0.001, 0.0]
        turnover = [1.0] * 5

        long_net = cm.apply(long_returns, turnover, periods_per_year=252)
        short_net = cm.apply(short_returns, turnover, periods_per_year=252)
        long_drag = sum(r - n for r, n in zip(long_returns, long_net.tolist(), strict=True))
        short_drag = sum(r - n for r, n in zip(short_returns, short_net.tolist(), strict=True))

        self.assertAlmostEqual(long_drag, short_drag, places=10)
        # Composition assertion: total L/S drag = sum of per-leg drags = 2× single
        total_drag = long_drag + short_drag
        self.assertAlmostEqual(total_drag, 2.0 * long_drag, places=10)

    def test_cost_zero_when_turnover_zero(self):
        cm = CostModel(annual_drag_bps=30.0)
        returns = [0.001, -0.002, 0.003, 0.0, 0.001]
        net = cm.apply(daily_returns=returns, daily_turnover=[0.0] * 5, periods_per_year=252)
        for original, after in zip(returns, net.tolist(), strict=True):
            self.assertEqual(original, after)

    def test_cost_proportional_to_turnover(self):
        cm = CostModel(annual_drag_bps=30.0)
        full = cm.apply([0.0] * 5, [1.0] * 5, periods_per_year=252)
        half = cm.apply([0.0] * 5, [0.5] * 5, periods_per_year=252)
        # half-turnover drag = 0.5 × full-turnover drag
        for f, h in zip(full.tolist(), half.tolist(), strict=True):
            self.assertAlmostEqual(h, 0.5 * f, places=12)

    def test_long_only_30bps_profile(self):
        cm = CostModel.from_profile("long_only_30bps")
        self.assertEqual(cm.annual_drag_bps, 30.0)


if __name__ == "__main__":
    unittest.main()
