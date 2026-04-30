"""Tests for `alphalens.risk_overlay.assess`.

`assess.py` is the headless equivalent of the per-script `assess_overlay`
in `scripts/experiment_vol_target_overlay.py`: a pure function that takes
(raw portfolio returns, per-period top-N snapshots, scales, factors,
benchmark) and emits the per-config metrics dict the experiment driver
logs and persists. Pulling it into the package proper makes it
unit-testable and gives the documented Sharpe-improvement metric a
single canonical implementation.
"""

from __future__ import annotations

import unittest

import pandas as pd


def _portfolio(values: list[float], start: str = "2020-01-06") -> pd.Series:
    idx = pd.date_range(start=start, periods=len(values), freq="W-MON")
    return pd.Series(values, index=idx, name="portfolio")


def _snapshots(seqs: list[list[str]]) -> list[list[str]]:
    return [list(s) for s in seqs]


class PerPeriodTurnoverTests(unittest.TestCase):
    def test_returns_zero_for_first_snapshot(self):
        from alphalens.risk_overlay.assess import per_period_turnover

        s = per_period_turnover(_snapshots([["A", "B", "C"]]))

        # One snapshot → no transition, turnover series is one zero.
        self.assertEqual(len(s), 1)
        self.assertEqual(s.iloc[0], 0.0)

    def test_single_swap_yields_proportional_turnover(self):
        from alphalens.risk_overlay.assess import per_period_turnover

        s = per_period_turnover(_snapshots([["A", "B", "C"], ["A", "B", "D"]]))

        self.assertEqual(s.iloc[0], 0.0)  # first rebalance has no predecessor
        self.assertAlmostEqual(s.iloc[1], 1 / 3, places=10)  # one of three replaced

    def test_full_reshuffle_yields_one(self):
        from alphalens.risk_overlay.assess import per_period_turnover

        s = per_period_turnover(_snapshots([["A", "B", "C"], ["X", "Y", "Z"]]))

        self.assertAlmostEqual(s.iloc[1], 1.0, places=10)


class DynamicCostDragTests(unittest.TestCase):
    def test_drag_combines_position_and_leverage_turnover(self):
        """Per-rebalance cost is base * scale_t (position-side) plus
        |scale_t - scale_{t-1}| (leverage-side) in turnover units, then
        scaled by cost_bps. zen pushback: constant-drag shortcut would
        ignore the leverage-side."""
        from alphalens.risk_overlay.assess import dynamic_cost_drag

        scales = pd.Series([1.0, 1.5, 0.5], index=pd.RangeIndex(3))
        base_turnover = pd.Series([0.10, 0.10, 0.10], index=pd.RangeIndex(3))

        drag = dynamic_cost_drag(scales, base_turnover, cost_half_spread_bps=10.0)

        # Period 0: scale=1.0, base_turn=0.10, scale_change=0 → turnover=0.10
        # Period 1: scale=1.5, base_turn=0.10, scale_change=0.5 → turnover=0.65
        # Period 2: scale=0.5, base_turn=0.10, scale_change=1.0 → turnover=1.05
        # Cost = turnover * 10/10000
        expected = pd.Series(
            [0.10 * 10 / 10_000, 0.65 * 10 / 10_000, 1.05 * 10 / 10_000],
            index=pd.RangeIndex(3),
        )
        pd.testing.assert_series_equal(drag, expected, check_names=False)


class ComputeOverlayStatsTests(unittest.TestCase):
    def test_includes_sharpe_unscaled_and_improvement_keys(self):
        """ADR 0007 + pre-reg success gate are written in terms of
        Sharpe-improvement vs ungated BASE. The returned dict MUST carry
        both Sharpes and the explicit improvement so the audit JSON has
        the canonical metric, not just the (distorted) overlay alpha-t."""
        from alphalens.risk_overlay import VolTargeter
        from alphalens.risk_overlay.assess import compute_overlay_stats

        # Synthetic 12-period weekly series: low-vol regime.
        raw = _portfolio([0.005, -0.005, 0.005, -0.005] * 3)
        targeter = VolTargeter(target_vol=0.10, lookback=4, periods_per_year=52, max_leverage=1.5)
        snapshots = _snapshots([["A", "B"]] * 12)

        stats = compute_overlay_stats(
            raw_returns=raw,
            targeter=targeter,
            top_n_snapshots=snapshots,
            cost_half_spread_bps=5.0,
            periods_per_year=52,
        )

        # Required keys per ADR 0007 + pre-reg.
        self.assertIn("sharpe_unscaled_gross", stats)
        self.assertIn("sharpe_unscaled_net", stats)
        self.assertIn("sharpe_scaled_gross", stats)
        self.assertIn("sharpe_scaled_net", stats)
        self.assertIn("sharpe_improvement_net", stats)
        self.assertIn("mean_scale", stats)
        self.assertIn("min_scale", stats)
        self.assertIn("max_scale", stats)
        self.assertIn("cost_drag_ann", stats)

        # Improvement is signed difference of net Sharpes.
        self.assertAlmostEqual(
            stats["sharpe_improvement_net"],
            stats["sharpe_scaled_net"] - stats["sharpe_unscaled_net"],
            places=10,
        )

    def test_empty_returns_returns_n_zero(self):
        from alphalens.risk_overlay import VolTargeter
        from alphalens.risk_overlay.assess import compute_overlay_stats

        raw = pd.Series([], dtype=float, index=pd.DatetimeIndex([]), name="portfolio")
        targeter = VolTargeter(target_vol=0.10, lookback=4, periods_per_year=52)

        stats = compute_overlay_stats(
            raw_returns=raw,
            targeter=targeter,
            top_n_snapshots=[],
            cost_half_spread_bps=5.0,
            periods_per_year=52,
        )

        self.assertEqual(stats["n"], 0)


if __name__ == "__main__":
    unittest.main()
