"""Tests for time-series position-sizing overlays on portfolio returns.

`alphalens.risk_overlay` is a sibling layer to `alphalens.gates`
sitting downstream of the BacktestEngine: regime_gate modifies *which*
tickers are selected; risk_overlay modifies *how much* total exposure
the selected portfolio carries based on its own realized vol.

Concrete sizing rule covered here: vol-targeting per Moreira & Muir 2017
(Journal of Finance, "Volatility-Managed Portfolios"). The overlay sees
only past-and-current portfolio returns — no cross-section, no factor
data — and emits a daily multiplier in [0, max_leverage].
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _returns(values: list[float], start: str = "2020-01-06") -> pd.Series:
    """Weekly Series — one return per period (= one rebalance).

    `freq="W-MON"` anchors each period at calendar Monday (good enough for
    test fixtures; a real backtest's portfolio_returns index would track
    the engine's actual rebalance grid).
    """
    idx = pd.date_range(start=start, periods=len(values), freq="W-MON")
    return pd.Series(values, index=idx, name="portfolio")


class VolTargetScalingTests(unittest.TestCase):
    def test_apply_returns_input_when_history_insufficient(self):
        """First `lookback` periods cannot estimate vol — scale must be 1.0."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        rets = _returns([0.01, -0.01, 0.005, -0.005, 0.01])
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        scaled = apply_vol_target(rets, targeter)

        # All 5 points are before lookback is satisfied → scale=1.0 → identity.
        pd.testing.assert_series_equal(scaled, rets, check_names=False)

    def test_scale_factor_decreases_when_realized_vol_exceeds_target(self):
        """High-vol regime → scale < 1 (de-risk)."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        # ±5% weekly = ~36% annualized — well above 10% target.
        rets = _returns([0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05])
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        scaled = apply_vol_target(rets, targeter)

        # First 5 untouched, points 6+ scale below 1.
        self.assertEqual(scaled.iloc[0], rets.iloc[0])
        post_history_scales = (scaled.iloc[5:] / rets.iloc[5:]).abs()
        self.assertTrue((post_history_scales < 1.0).all())

    def test_scale_factor_increases_when_realized_vol_below_target(self):
        """Low-vol regime → scale > 1 (lever up), capped at max_leverage."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        # ±0.2% weekly = ~1.4% annualized — well below 10% target.
        rets = _returns([0.002, -0.002, 0.002, -0.002, 0.002, -0.002, 0.002, -0.002, 0.002])
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52, max_leverage=1.5)

        scaled = apply_vol_target(rets, targeter)

        post_history_scales = (scaled.iloc[5:] / rets.iloc[5:]).abs()
        self.assertTrue((post_history_scales > 1.0).all())
        self.assertTrue((post_history_scales <= 1.5 + 1e-9).all())

    def test_max_leverage_caps_scale(self):
        """Near-zero realized vol explodes the raw multiplier — cap clamps it."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        # Alternating 1bp returns — realized std → ~0.0001, so target/rv → ~10000.
        rets = _returns([0.0001, -0.0001] * 5)
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52, max_leverage=1.5)

        scaled = apply_vol_target(rets, targeter)

        post_history_scales = (scaled.iloc[5:] / rets.iloc[5:]).abs()
        # Without the cap this would be O(10000); with cap exactly 1.5.
        self.assertTrue(np.allclose(post_history_scales, 1.5))

    def test_no_lookahead_in_scale_factor(self):
        """scale[t] uses returns strictly before t — modifying returns[t]
        must not change scale[t]."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        base = _returns([0.01, -0.01, 0.01, -0.01, 0.01, 0.02, -0.02])
        perturbed = base.copy()
        perturbed.iloc[5] = 0.50  # huge perturbation at t=5

        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        scaled_base = apply_vol_target(base, targeter)
        scaled_perturbed = apply_vol_target(perturbed, targeter)

        # scale[5] is computed from returns[<5], unchanged → scaled[5] differs only
        # because returns[5] differs (scale × different return). The RATIO
        # scaled / returns at t=5 must match across the two.
        ratio_base = scaled_base.iloc[5] / base.iloc[5]
        ratio_perturbed = scaled_perturbed.iloc[5] / perturbed.iloc[5]
        self.assertAlmostEqual(ratio_base, ratio_perturbed, places=12)

    def test_empty_returns_returns_empty(self):
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        rets = pd.Series([], dtype=float, index=pd.DatetimeIndex([]), name="portfolio")
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        scaled = apply_vol_target(rets, targeter)

        self.assertEqual(len(scaled), 0)

    def test_zero_or_negative_target_vol_raises(self):
        from alphalens.risk_overlay import VolTargeter

        with self.assertRaises(ValueError):
            VolTargeter(target_vol=0.0, lookback=5, periods_per_year=52)
        with self.assertRaises(ValueError):
            VolTargeter(target_vol=-0.05, lookback=5, periods_per_year=52)

    def test_non_positive_max_leverage_raises(self):
        from alphalens.risk_overlay import VolTargeter

        with self.assertRaises(ValueError):
            VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52, max_leverage=0.0)
        with self.assertRaises(ValueError):
            VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52, max_leverage=-0.5)

    def test_lookback_below_2_raises(self):
        from alphalens.risk_overlay import VolTargeter

        with self.assertRaises(ValueError):
            VolTargeter(target_vol=0.10, lookback=1, periods_per_year=52)

    def test_nan_in_returns_yields_neutral_scale_not_propagation(self):
        """A NaN entry in the returns history must not produce a NaN scale.

        NaN propagation through `np.std` would silently corrupt every
        downstream point that depends on the NaN-containing window. The
        contract: if the realised-vol estimator can't produce a finite
        positive number (e.g. because the window contains NaN), the
        wrapper falls back to scale=1.0 (identity)."""
        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        rets = _returns([0.01, -0.01, float("nan"), 0.01, -0.01, 0.05, -0.05, 0.05])
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        scaled = apply_vol_target(rets, targeter)

        # Indices where input is finite: scaled output must also be finite.
        # NaN scales would silently corrupt downstream cost/Sharpe math.
        finite_input_mask = rets.notna()
        finite_scaled = scaled[finite_input_mask]
        self.assertTrue(
            finite_scaled.notna().all(),
            f"NaN propagated to scaled returns: {scaled.tolist()}",
        )

    def test_zero_realized_vol_logs_warning_not_silent_one(self):
        """Distinguish "insufficient history" (scale=1.0 silently) from
        "realized vol is exactly zero" (scale=1.0 with a warning) — the
        latter is a degenerate state worth flagging in research logs."""
        import logging

        from alphalens.risk_overlay import VolTargeter, apply_vol_target

        # Constant returns → realized std = 0 → degenerate.
        rets = _returns([0.005] * 8)
        targeter = VolTargeter(target_vol=0.10, lookback=5, periods_per_year=52)

        with self.assertLogs("alphalens.risk_overlay.vol_target", level=logging.WARNING) as cm:
            scaled = apply_vol_target(rets, targeter)

        # All scales = 1.0 (fallback), no NaN propagation.
        self.assertTrue(((scaled / rets).iloc[5:] == 1.0).all())
        # And at least one warning logged for the zero-vol degenerate state.
        self.assertTrue(
            any("zero" in line.lower() or "degenerate" in line.lower() for line in cm.output)
        )


class RiskOverlayPackageStatusTest(unittest.TestCase):
    def test_package_declares_research_only_status(self):
        """No concrete sizing rule has earned ACTIVE status yet — first
        hypothesis (vol-target on mom+lowvol) is pre-registered for audit
        2026-04-30. Package mirrors `alphalens.gates` and `macro/`."""
        import alphalens.risk_overlay as pkg

        self.assertEqual(pkg.__status__, "RESEARCH_ONLY")


if __name__ == "__main__":
    unittest.main()
