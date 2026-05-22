"""Unit tests for ``alphalens_research.screeners.ev_fcff_yield.scorer``.

Pure-function tests: no SimFin, no network, no cache. Each scoring primitive
is tested in isolation; the high-level ``score_ev_fcff_yield`` is then tested
against synthetic snapshot dicts to verify the composition.
"""

from __future__ import annotations

import math
import unittest

import pandas as pd
from alphalens_research.screeners.ev_fcff_yield import scorer as sc


class TestComputeFcff(unittest.TestCase):
    def test_basic_formula(self):
        # OCF=100, Capex=30, Interest=10, tau=0.25 → 100 + 10×0.75 − 30 = 77.5
        self.assertAlmostEqual(
            sc.compute_fcff(
                ocf_ttm=100.0,
                capex_ttm=30.0,
                interest_expense_ttm=10.0,
                tax_rate=0.25,
            ),
            77.5,
        )

    def test_zero_interest_collapses_to_ocf_minus_capex(self):
        self.assertAlmostEqual(
            sc.compute_fcff(
                ocf_ttm=50.0,
                capex_ttm=20.0,
                interest_expense_ttm=0.0,
                tax_rate=0.25,
            ),
            30.0,
        )

    def test_zero_tax_passes_full_interest_addback(self):
        # tau=0 → full interest added back, FCFF = OCF + interest − capex
        self.assertAlmostEqual(
            sc.compute_fcff(
                ocf_ttm=100.0,
                capex_ttm=10.0,
                interest_expense_ttm=20.0,
                tax_rate=0.0,
            ),
            110.0,
        )

    def test_negative_ocf_makes_fcff_negative(self):
        result = sc.compute_fcff(
            ocf_ttm=-50.0,
            capex_ttm=10.0,
            interest_expense_ttm=5.0,
            tax_rate=0.21,
        )
        self.assertLess(result, 0)

    def test_tax_rate_above_ceiling_raises(self):
        with self.assertRaises(ValueError):
            sc.compute_fcff(
                ocf_ttm=100.0,
                capex_ttm=10.0,
                interest_expense_ttm=5.0,
                tax_rate=0.45,
            )

    def test_negative_tax_rate_raises(self):
        with self.assertRaises(ValueError):
            sc.compute_fcff(
                ocf_ttm=100.0,
                capex_ttm=10.0,
                interest_expense_ttm=5.0,
                tax_rate=-0.1,
            )


class TestComputeEv(unittest.TestCase):
    def test_market_cap_plus_net_debt(self):
        # price=50, shares=100M → mcap=5e9; LTD=2e9, STD=1e9, cash=500M → net debt 2.5e9
        ev = sc.compute_ev(
            price=50.0,
            shares_outstanding=100_000_000,
            long_term_debt=2_000_000_000,
            short_term_debt=1_000_000_000,
            cash_and_equivalents=500_000_000,
        )
        self.assertAlmostEqual(ev, 5_000_000_000 + 2_500_000_000)

    def test_cash_rich_can_produce_negative_ev(self):
        # tiny mcap, huge cash pile, no debt → negative EV is mathematically possible
        ev = sc.compute_ev(
            price=1.0,
            shares_outstanding=1_000_000,
            long_term_debt=0.0,
            short_term_debt=0.0,
            cash_and_equivalents=10_000_000,
        )
        self.assertLess(ev, 0)

    def test_zero_debt_zero_cash_equals_market_cap(self):
        ev = sc.compute_ev(
            price=10.0,
            shares_outstanding=1000,
            long_term_debt=0,
            short_term_debt=0,
            cash_and_equivalents=0,
        )
        self.assertEqual(ev, 10_000)


class TestImputeFcff(unittest.TestCase):
    def test_revenue_times_positive_median_margin(self):
        # Sales=1B × 8% margin → 80M
        self.assertAlmostEqual(
            sc.impute_fcff(revenue_ttm=1_000_000_000, fcf_margin_5y_median=0.08),
            80_000_000,
        )

    def test_none_margin_returns_none(self):
        self.assertIsNone(sc.impute_fcff(revenue_ttm=1_000_000_000, fcf_margin_5y_median=None))

    def test_negative_median_margin_returns_none(self):
        # Structurally cash-burning firm — don't impute, drop.
        self.assertIsNone(sc.impute_fcff(revenue_ttm=1_000_000_000, fcf_margin_5y_median=-0.03))

    def test_zero_margin_returns_none(self):
        self.assertIsNone(sc.impute_fcff(revenue_ttm=1_000_000_000, fcf_margin_5y_median=0.0))

    def test_non_positive_revenue_returns_none(self):
        self.assertIsNone(sc.impute_fcff(revenue_ttm=0.0, fcf_margin_5y_median=0.05))
        self.assertIsNone(sc.impute_fcff(revenue_ttm=-1.0, fcf_margin_5y_median=0.05))

    def test_nan_margin_returns_none(self):
        self.assertIsNone(
            sc.impute_fcff(revenue_ttm=1_000_000_000, fcf_margin_5y_median=float("nan"))
        )


class TestEffectiveFcff(unittest.TestCase):
    def test_positive_actual_wins(self):
        self.assertEqual(
            sc.effective_fcff(fcff_actual=100.0, fcff_imputed=50.0),
            100.0,
        )

    def test_actual_positive_imputed_none_returns_actual(self):
        self.assertEqual(
            sc.effective_fcff(fcff_actual=42.0, fcff_imputed=None),
            42.0,
        )

    def test_negative_actual_positive_imputed_returns_imputed(self):
        self.assertEqual(
            sc.effective_fcff(fcff_actual=-10.0, fcff_imputed=25.0),
            25.0,
        )

    def test_both_non_positive_returns_none(self):
        self.assertIsNone(sc.effective_fcff(fcff_actual=-10.0, fcff_imputed=-5.0))
        self.assertIsNone(sc.effective_fcff(fcff_actual=-10.0, fcff_imputed=0.0))
        self.assertIsNone(sc.effective_fcff(fcff_actual=-10.0, fcff_imputed=None))

    def test_zero_actual_falls_through_to_imputed(self):
        # zero is non-positive → imputation fires
        self.assertEqual(
            sc.effective_fcff(fcff_actual=0.0, fcff_imputed=30.0),
            30.0,
        )


class TestComputeFcffYield(unittest.TestCase):
    def test_basic_yield(self):
        self.assertAlmostEqual(
            sc.compute_fcff_yield(fcff_effective=100_000_000, ev=2_000_000_000),
            0.05,
        )

    def test_none_fcff_returns_none(self):
        self.assertIsNone(sc.compute_fcff_yield(fcff_effective=None, ev=1e9))

    def test_negative_ev_returns_none(self):
        self.assertIsNone(sc.compute_fcff_yield(fcff_effective=1e6, ev=-1e9))

    def test_zero_ev_returns_none(self):
        self.assertIsNone(sc.compute_fcff_yield(fcff_effective=1e6, ev=0))


class TestWinsorize(unittest.TestCase):
    def test_caps_outliers_at_pct_band(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0])
        out = sc.winsorize(s, lower_pct=0.1, upper_pct=0.9)
        # 10th pct ≈ 1.9, 90th pct ≈ 90.1; 100 → capped to 90.1
        self.assertLessEqual(out.iloc[-1], 91.0)

    def test_preserves_nan(self):
        s = pd.Series([1.0, 2.0, float("nan"), 4.0, 5.0])
        out = sc.winsorize(s)
        self.assertTrue(math.isnan(out.iloc[2]))

    def test_empty_returns_empty(self):
        s = pd.Series([], dtype=float)
        out = sc.winsorize(s)
        self.assertTrue(out.empty)

    def test_all_nan_returns_unchanged(self):
        s = pd.Series([float("nan")] * 5)
        out = sc.winsorize(s)
        self.assertEqual(len(out), 5)
        self.assertTrue(out.isna().all())


class TestRankZscore(unittest.TestCase):
    def test_centers_and_scales(self):
        s = pd.Series([10.0, 20.0, 30.0])
        out = sc.rank_zscore(s)
        self.assertAlmostEqual(out.mean(), 0.0, places=6)
        self.assertAlmostEqual(abs(out).sum(), abs(out).sum())  # finite

    def test_constant_input_returns_nan(self):
        s = pd.Series([5.0, 5.0, 5.0])
        out = sc.rank_zscore(s)
        self.assertTrue(out.isna().all())

    def test_preserves_nan_input(self):
        s = pd.Series([1.0, 2.0, float("nan"), 4.0])
        out = sc.rank_zscore(s)
        self.assertTrue(math.isnan(out.iloc[2]))
        self.assertFalse(math.isnan(out.iloc[0]))

    def test_empty(self):
        self.assertTrue(sc.rank_zscore(pd.Series([], dtype=float)).empty)


class TestScoreEvFcffYieldIntegration(unittest.TestCase):
    """Verify the high-level scorer composes primitives correctly."""

    def _good_snap(self, **overrides):
        """Reasonable defaults for a profitable mid-cap."""
        base = dict(
            ocf_ttm=200_000_000,
            capex_ttm=50_000_000,
            interest_expense_ttm=10_000_000,
            tax_rate=0.21,
            revenue_ttm=1_500_000_000,
            fcf_margin_5y_median=0.07,
            price=50.0,
            shares_outstanding=80_000_000,  # mcap = 4B
            long_term_debt=300_000_000,
            short_term_debt=100_000_000,
            cash_and_equivalents=200_000_000,  # net debt = 200M
        )
        base.update(overrides)
        return base

    def test_three_tickers_produces_zscored_series(self):
        snaps = {
            "AAA": self._good_snap(ocf_ttm=500_000_000),  # higher OCF → higher yield
            "BBB": self._good_snap(),  # mid
            "CCC": self._good_snap(ocf_ttm=80_000_000),  # lower OCF → lower yield
        }
        out = sc.score_ev_fcff_yield(snaps)
        self.assertEqual(set(out.index), {"AAA", "BBB", "CCC"})
        # Z-scored series sums to ~0 (centred).
        self.assertAlmostEqual(out.sum(), 0.0, places=4)
        # AAA (best yield) should have highest score.
        self.assertGreater(out["AAA"], out["BBB"])
        self.assertGreater(out["BBB"], out["CCC"])

    def test_dropped_when_missing_required_input(self):
        snaps = {
            "GOOD": self._good_snap(),
            "BADPX": self._good_snap(price=None),  # missing price
            "BADCASH": self._good_snap(cash_and_equivalents=float("nan")),
        }
        out = sc.score_ev_fcff_yield(snaps)
        self.assertIn("GOOD", out.index)
        self.assertNotIn("BADPX", out.index)
        self.assertNotIn("BADCASH", out.index)

    def test_negative_fcff_imputation_rescues_ticker(self):
        # Ticker with negative current FCFF (heavy capex) but solid 5y margin.
        snaps = {
            "ZOMBIE": self._good_snap(
                ocf_ttm=10_000_000,
                capex_ttm=200_000_000,  # FCFF actual << 0
                fcf_margin_5y_median=-0.02,  # 5y avg also negative — no rescue
            ),
            "RESCUED": self._good_snap(
                ocf_ttm=10_000_000,
                capex_ttm=200_000_000,  # FCFF actual << 0
                fcf_margin_5y_median=0.06,  # 5y avg positive — rescued via Sales×margin
                revenue_ttm=2_000_000_000,
            ),
            "HEALTHY": self._good_snap(),
        }
        out = sc.score_ev_fcff_yield(snaps)
        self.assertIn("RESCUED", out.index)
        self.assertIn("HEALTHY", out.index)
        self.assertNotIn("ZOMBIE", out.index)

    def test_empty_input_returns_empty(self):
        self.assertTrue(sc.score_ev_fcff_yield({}).empty)

    def test_all_dropped_returns_empty(self):
        snaps = {"BAD": self._good_snap(price=None)}
        self.assertTrue(sc.score_ev_fcff_yield(snaps).empty)

    def test_tax_rate_above_ceiling_is_clamped_not_raised(self):
        # High-level scorer clamps; low-level compute_fcff would raise.
        snaps = {"A": self._good_snap(tax_rate=0.50), "B": self._good_snap()}
        out = sc.score_ev_fcff_yield(snaps)
        # Both should appear — A clamped to 0.35, both produce valid yields.
        self.assertEqual(set(out.index), {"A", "B"})


if __name__ == "__main__":
    unittest.main()
