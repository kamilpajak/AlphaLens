"""Tests for the slippage regime-amplified cost diagnostic.

Pre-reg memo: docs/research/insider_form4_opportunistic_slippage_stress_design_2026_05_12.md.

Verifies the small set of pure helpers that the diagnostic script
(scripts/diagnostics/insider_form4_slippage_regime.py) imports. The
diagnostic itself is single-purpose glue around these helpers + the
existing factor regression + cyclicality classifier.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _bdate_range(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n)


class TestEffectiveHalfSpread(unittest.TestCase):
    """The regime amplification function:
    effective(t) = base * (1 + beta * max(0, (sigma_60d(t) - sigma_median)/sigma_median))
    """

    def test_beta_zero_returns_base_uniformly(self):
        from alphalens.diagnostics.slippage_regime import compute_effective_half_spread

        dates = _bdate_range(50)
        vol = pd.Series(np.linspace(0.10, 0.30, 50), index=dates)
        result = compute_effective_half_spread(vol, base_bps=50.0, beta=0.0)
        np.testing.assert_allclose(result.values, 50.0)

    def test_returns_base_when_below_median(self):
        from alphalens.diagnostics.slippage_regime import compute_effective_half_spread

        dates = _bdate_range(50)
        vol = pd.Series(np.linspace(0.10, 0.30, 50), index=dates)
        result = compute_effective_half_spread(vol, base_bps=50.0, beta=2.0)
        median = vol.median()
        below_median_mask = vol < median
        np.testing.assert_allclose(result.loc[below_median_mask].values, 50.0)

    def test_amplifies_above_median_by_beta_times_excess_fraction(self):
        from alphalens.diagnostics.slippage_regime import compute_effective_half_spread

        # Pin sigma_median explicitly so the test predicts the multiplier deterministically.
        dates = _bdate_range(50)
        vol = pd.Series(np.linspace(0.10, 0.30, 50), index=dates)
        sigma_median = 0.20
        # vol value 2x median → excess_fraction = 1.0 → multiplier = 1 + 2 = 3
        peak_vol = 2 * sigma_median
        target_idx = dates[25]
        vol.loc[target_idx] = peak_vol
        result = compute_effective_half_spread(
            vol, base_bps=50.0, beta=2.0, sigma_median=sigma_median
        )
        self.assertAlmostEqual(result.loc[target_idx], 50.0 * 3.0, places=6)

    def test_returns_nan_when_vol_is_nan(self):
        from alphalens.diagnostics.slippage_regime import compute_effective_half_spread

        dates = _bdate_range(50)
        vol = pd.Series(np.linspace(0.10, 0.30, 50), index=dates)
        vol.iloc[10] = np.nan
        result = compute_effective_half_spread(vol, base_bps=50.0, beta=2.0)
        self.assertTrue(pd.isna(result.iloc[10]))
        self.assertFalse(pd.isna(result.iloc[11]))


class TestBroadcastTurnoverToDaily(unittest.TestCase):
    """Per-rebalance turnover → daily series, broadcast only over the
    21-day post-rebalance window. Drag concentrates at execution; do NOT
    smear across the entire holding period via forward-fill (would smooth
    Q5 panic clusters)."""

    def test_drag_concentrated_at_rebalance_day_only(self):
        from alphalens.diagnostics.slippage_regime import broadcast_turnover_to_daily

        daily_idx = _bdate_range(60)
        rebal_dates = pd.DatetimeIndex(
            [daily_idx[0], daily_idx[21], daily_idx[42]], name="rebalance_date"
        )
        turnover_df = pd.DataFrame(
            {"turnover": [np.nan, 0.50, 0.30], "n_tickers": [200, 200, 200]},
            index=rebal_dates,
        )
        broadcast = broadcast_turnover_to_daily(
            turnover_df, daily_idx, holding_days=21, mode="concentrate"
        )
        # Mode 'concentrate' puts full turnover on rebalance day, 0 elsewhere
        self.assertAlmostEqual(broadcast.loc[daily_idx[21]], 0.50)
        self.assertAlmostEqual(broadcast.loc[daily_idx[22]], 0.0)
        self.assertAlmostEqual(broadcast.loc[daily_idx[42]], 0.30)
        # First rebalance (NaN turnover) → 0 drag
        self.assertAlmostEqual(broadcast.loc[daily_idx[0]], 0.0)

    def test_amortize_mode_spreads_across_holding_window(self):
        from alphalens.diagnostics.slippage_regime import broadcast_turnover_to_daily

        daily_idx = _bdate_range(60)
        rebal_dates = pd.DatetimeIndex([daily_idx[0], daily_idx[21]], name="rebalance_date")
        turnover_df = pd.DataFrame(
            {"turnover": [np.nan, 0.50], "n_tickers": [200, 200]},
            index=rebal_dates,
        )
        broadcast = broadcast_turnover_to_daily(
            turnover_df, daily_idx, holding_days=21, mode="amortize"
        )
        # Mode 'amortize' spreads 0.50 over the 21 days starting at rebal day
        window_slice = broadcast.iloc[21:42]
        self.assertAlmostEqual(window_slice.sum(), 0.50, places=6)
        self.assertAlmostEqual(broadcast.iloc[20], 0.0)
        self.assertAlmostEqual(broadcast.iloc[42], 0.0)

    def test_default_mode_is_concentrate(self):
        """The pre-reg memo §6 mandates drag at execution, not smeared.
        Default must reflect that intent."""
        from alphalens.diagnostics.slippage_regime import broadcast_turnover_to_daily

        daily_idx = _bdate_range(30)
        rebal_dates = pd.DatetimeIndex([daily_idx[0], daily_idx[15]])
        turnover_df = pd.DataFrame(
            {"turnover": [np.nan, 0.40], "n_tickers": [200, 200]}, index=rebal_dates
        )
        broadcast = broadcast_turnover_to_daily(turnover_df, daily_idx, holding_days=15)
        # Concentrate would put 0.40 on day 15, ~0 elsewhere; amortize would
        # spread 0.40/15 ≈ 0.0267 across 15 days
        self.assertAlmostEqual(broadcast.loc[daily_idx[15]], 0.40)


class TestApplyRegimeDrag(unittest.TestCase):
    """End-to-end drag application: gross daily + effective spread + turnover → net daily.

    Drag formula per RealisticCostModel.primary_period_drag_bps semantics:
        per_period_bps = (half_spread + adverse_selection) * turnover * 2  # round-trip
        per_day_decimal = per_period_bps / 10000
    """

    def test_zero_turnover_yields_no_drag(self):
        from alphalens.diagnostics.slippage_regime import apply_regime_drag

        daily_idx = _bdate_range(30)
        gross = pd.Series(np.full(30, 0.001), index=daily_idx)
        effective_spread = pd.Series(np.full(30, 100.0), index=daily_idx)
        turnover_daily = pd.Series(np.zeros(30), index=daily_idx)

        result = apply_regime_drag(
            gross, effective_spread, turnover_daily, adverse_selection_bps=5.0
        )
        np.testing.assert_allclose(result["rets_net"].values, gross.values, atol=1e-12)
        np.testing.assert_allclose(result["drag_daily"].values, 0.0, atol=1e-12)

    def test_constant_spread_matches_realistic_cost_model(self):
        """Cross-check against alphalens.attribution.cost_model.RealisticCostModel."""
        from alphalens.attribution.cost_model import RealisticCostModel
        from alphalens.diagnostics.slippage_regime import apply_regime_drag

        daily_idx = _bdate_range(30)
        gross = pd.Series(np.zeros(30), index=daily_idx)
        half_spread = 100.0
        turnover = 0.65
        effective_spread = pd.Series(np.full(30, half_spread), index=daily_idx)
        turnover_daily = pd.Series(np.full(30, turnover), index=daily_idx)

        result = apply_regime_drag(
            gross, effective_spread, turnover_daily, adverse_selection_bps=5.0
        )
        # RealisticCostModel.primary_period_drag_bps gives drag in bps for ONE round-trip
        cost_model = RealisticCostModel(adverse_selection_bps=5.0)
        expected_per_rebal_bps = cost_model.primary_period_drag_bps(half_spread, turnover)
        # Our diagnostic applies drag per-day uniformly when turnover is daily-constant;
        # so per-day drag in bps × 30 days should match per-rebal bps if rebalance every day.
        # When turnover is given daily directly (already broadcast), drag = (hs+as) * turn * 2 / 10000.
        expected_daily_bps = (half_spread + 5.0) * turnover * 2
        np.testing.assert_allclose(
            result["drag_daily"].values * 10000, expected_daily_bps, atol=1e-9
        )
        # Sanity vs cost_model: total drag from 30 days of constant turnover ==
        # 30 single-period drags
        np.testing.assert_allclose(
            result["drag_daily"].sum() * 10000, 30 * expected_per_rebal_bps, atol=1e-6
        )

    def test_cost_monotonicity_in_half_spread(self):
        """αt_net (and total drag) must monotonically decrease (increase) in half_spread."""
        from alphalens.diagnostics.slippage_regime import apply_regime_drag

        rng = np.random.default_rng(42)
        daily_idx = _bdate_range(252)
        gross = pd.Series(rng.normal(0.001, 0.01, 252), index=daily_idx)
        turnover_daily = pd.Series(np.full(252, 0.30), index=daily_idx)

        prev_total_drag = 0.0
        for hs in [25.0, 50.0, 100.0, 200.0, 500.0]:
            effective_spread = pd.Series(np.full(252, hs), index=daily_idx)
            result = apply_regime_drag(
                gross, effective_spread, turnover_daily, adverse_selection_bps=5.0
            )
            total_drag = float(result["drag_daily"].sum())
            self.assertGreater(
                total_drag, prev_total_drag, f"drag did not increase from prev to hs={hs}"
            )
            prev_total_drag = total_drag


class TestPerQuintileBucket(unittest.TestCase):
    """Per-vol-quintile mean return computation on a daily return series."""

    def test_returns_one_value_per_quintile(self):
        from alphalens.diagnostics.slippage_regime import per_quintile_mean_returns

        rng = np.random.default_rng(7)
        n = 252
        daily_idx = _bdate_range(n)
        vol = pd.Series(np.abs(rng.normal(0.20, 0.10, n)), index=daily_idx)
        rets = pd.Series(rng.normal(0.001, 0.01, n), index=daily_idx)

        result = per_quintile_mean_returns(rets, vol)
        self.assertEqual(set(result.index), {"Q1", "Q2", "Q3", "Q4", "Q5"})
        self.assertTrue(result.notna().all())

    def test_counter_cyclical_returns_show_q5_gt_q1(self):
        """When we construct returns positively correlated with vol (counter-cyclical
        in our convention), Q5 mean must exceed Q1 mean."""
        from alphalens.diagnostics.slippage_regime import per_quintile_mean_returns

        n = 252
        daily_idx = _bdate_range(n)
        vol = pd.Series(np.linspace(0.10, 0.40, n), index=daily_idx)
        # Strong positive correlation: high-vol days → high returns
        rets = pd.Series(np.linspace(-0.02, 0.04, n), index=daily_idx)
        result = per_quintile_mean_returns(rets, vol)
        self.assertGreater(result["Q5"], result["Q1"])


class TestRunOneSlippageCombo(unittest.TestCase):
    """Single (half_spread, beta) → αt_net, α_ann_net, Sharpe_net.

    Tested via synthetic factors with known beta=0 alpha to verify the
    regression machinery is wired correctly. Uses a small Carhart-4F-like
    factor frame.
    """

    def _make_factors(self, n: int) -> pd.DataFrame:
        rng = np.random.default_rng(11)
        daily_idx = _bdate_range(n)
        return pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0.0003, 0.01, n),
                "SMB": rng.normal(0.0, 0.005, n),
                "HML": rng.normal(0.0, 0.005, n),
                "Mom": rng.normal(0.0, 0.005, n),
                "RF": np.full(n, 0.00015),
            },
            index=daily_idx,
        )

    def test_alpha_falls_when_half_spread_increases(self):
        from alphalens.diagnostics.slippage_regime import run_one_slippage_combo

        n = 300
        factors = self._make_factors(n)
        daily_idx = factors.index
        # Synthetic gross returns with consistent positive alpha + noise
        rng = np.random.default_rng(13)
        gross = pd.Series(0.0008 + rng.normal(0, 0.005, n), index=daily_idx)
        vol = pd.Series(np.abs(rng.normal(0.20, 0.05, n)), index=daily_idx)
        turnover_daily = pd.Series(np.full(n, 0.30), index=daily_idx)

        low_cost = run_one_slippage_combo(
            gross_daily=gross,
            turnover_daily=turnover_daily,
            vol_series=vol,
            factors=factors,
            half_spread_bps=25.0,
            beta=0.0,
            hac_maxlags=126,
        )
        high_cost = run_one_slippage_combo(
            gross_daily=gross,
            turnover_daily=turnover_daily,
            vol_series=vol,
            factors=factors,
            half_spread_bps=200.0,
            beta=0.0,
            hac_maxlags=126,
        )
        self.assertGreater(low_cost["alpha_t_net"], high_cost["alpha_t_net"])
        self.assertGreater(low_cost["alpha_annualized_net"], high_cost["alpha_annualized_net"])

    def test_regime_amplification_increases_drag_in_high_vol(self):
        """β=2 must put more drag on Q5 dates than β=0."""
        from alphalens.diagnostics.slippage_regime import run_one_slippage_combo

        n = 300
        factors = self._make_factors(n)
        daily_idx = factors.index
        rng = np.random.default_rng(17)
        gross = pd.Series(0.0008 + rng.normal(0, 0.005, n), index=daily_idx)
        # Strongly time-varying vol so β=2 has noticeable effect
        vol = pd.Series(np.linspace(0.10, 0.50, n), index=daily_idx)
        turnover_daily = pd.Series(np.full(n, 0.30), index=daily_idx)

        beta0 = run_one_slippage_combo(
            gross_daily=gross,
            turnover_daily=turnover_daily,
            vol_series=vol,
            factors=factors,
            half_spread_bps=50.0,
            beta=0.0,
            hac_maxlags=126,
        )
        beta2 = run_one_slippage_combo(
            gross_daily=gross,
            turnover_daily=turnover_daily,
            vol_series=vol,
            factors=factors,
            half_spread_bps=50.0,
            beta=2.0,
            hac_maxlags=126,
        )
        # β=2 amplification must INCREASE total drag (more cost on above-median
        # vol days) → net alpha lower than β=0.
        self.assertGreater(beta0["total_drag_decimal"], 0)
        self.assertGreater(beta2["total_drag_decimal"], beta0["total_drag_decimal"])
        self.assertGreater(beta0["alpha_annualized_net"], beta2["alpha_annualized_net"])


if __name__ == "__main__":
    unittest.main()
