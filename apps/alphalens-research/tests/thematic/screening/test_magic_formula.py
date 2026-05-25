"""Unit tests for Magic Formula cohort ranker (Layer 4).

Greenblatt-style multi-factor rank-sum on the daily candidate basket.
Value factors: P/E, EV/EBITDA, P/S, FCFF yield. Quality: ROIC, ROE.
Health gate (drop pre-rank): EBIT > 0 AND net_debt / EBIT < 5.
"""

from __future__ import annotations

import math
import unittest

import pandas as pd
from alphalens_pipeline.thematic.screening import magic_formula as mf


def _features(
    *,
    operating_income_ttm=110.0,
    interest_expense_ttm=10.0,
    net_income_ttm=75.0,
    revenue_ttm=1000.0,
    da_ttm=30.0,
    long_term_debt=300.0,
    short_term_debt=100.0,
    cash_and_equivalents=200.0,
    total_equity=800.0,
):
    """Build a minimal SimFin feature dict for compute-helper tests."""
    return {
        "operating_income_ttm": operating_income_ttm,
        "interest_expense_ttm": interest_expense_ttm,
        "net_income_ttm": net_income_ttm,
        "revenue_ttm": revenue_ttm,
        "da_ttm": da_ttm,
        "long_term_debt": long_term_debt,
        "short_term_debt": short_term_debt,
        "cash_and_equivalents": cash_and_equivalents,
        "total_equity": total_equity,
    }


class TestEbit(unittest.TestCase):
    def test_uses_operating_income_when_available(self):
        # Operating Income IS EBIT under SimFin's convention; prefer it.
        f = _features(operating_income_ttm=110.0, interest_expense_ttm=10.0)
        self.assertAlmostEqual(mf.compute_ebit_ttm(f), 110.0)

    def test_returns_none_when_operating_income_missing(self):
        self.assertIsNone(mf.compute_ebit_ttm(_features(operating_income_ttm=None)))


class TestNetDebt(unittest.TestCase):
    def test_lt_plus_st_minus_cash(self):
        f = _features(long_term_debt=300, short_term_debt=100, cash_and_equivalents=200)
        self.assertEqual(mf.compute_net_debt(f), 200.0)

    def test_returns_none_when_any_component_missing(self):
        self.assertIsNone(mf.compute_net_debt(_features(long_term_debt=None)))


class TestEvEbitda(unittest.TestCase):
    def test_basic_formula(self):
        # EV = market_cap + net_debt = 1000 + 200 = 1200
        # EBITDA = EBIT + D&A = 110 + 30 = 140
        # EV/EBITDA = 1200 / 140 ≈ 8.571
        f = _features()
        self.assertAlmostEqual(mf.compute_ev_ebitda(f, market_cap=1000.0), 1200.0 / 140.0)

    def test_returns_none_when_da_missing(self):
        f = _features(da_ttm=None)
        self.assertIsNone(mf.compute_ev_ebitda(f, market_cap=1000.0))

    def test_returns_none_on_zero_or_negative_ebitda(self):
        f = _features(operating_income_ttm=-100.0, da_ttm=50.0)  # EBITDA = -50
        self.assertIsNone(mf.compute_ev_ebitda(f, market_cap=1000.0))


class TestRoic(unittest.TestCase):
    def test_uses_total_capital_denominator(self):
        # Invested capital = total_debt + equity - cash = 400 + 800 - 200 = 1000
        # ROIC = EBIT / invested = 110 / 1000 = 0.11 → 11%
        f = _features()
        self.assertAlmostEqual(mf.compute_roic(f), 11.0)

    def test_returns_none_when_equity_missing(self):
        self.assertIsNone(mf.compute_roic(_features(total_equity=None)))


class TestRoe(unittest.TestCase):
    def test_basic(self):
        # 75 / 800 = 0.09375 → 9.375%
        f = _features(net_income_ttm=75.0, total_equity=800.0)
        self.assertAlmostEqual(mf.compute_roe(f), 9.375)

    def test_returns_none_when_equity_non_positive(self):
        self.assertIsNone(mf.compute_roe(_features(total_equity=0.0)))
        self.assertIsNone(mf.compute_roe(_features(total_equity=-100.0)))


class TestHealthGate(unittest.TestCase):
    def test_passes_when_ebit_positive_and_leverage_under_5x(self):
        # EBIT=110, net_debt=200, ratio=1.82 → PASS
        self.assertTrue(mf.passes_health_gate(_features()))

    def test_fails_on_negative_ebit(self):
        self.assertFalse(mf.passes_health_gate(_features(operating_income_ttm=-50.0)))

    def test_fails_on_zero_ebit(self):
        self.assertFalse(mf.passes_health_gate(_features(operating_income_ttm=0.0)))

    def test_fails_when_net_debt_exceeds_5x_ebit(self):
        # EBIT=100, net_debt=600 → ratio=6 → FAIL
        f = _features(
            operating_income_ttm=100.0,
            long_term_debt=600.0,
            short_term_debt=200.0,
            cash_and_equivalents=200.0,
        )
        self.assertFalse(mf.passes_health_gate(f))

    def test_passes_when_net_cash_position(self):
        # net_debt negative (cash > debt) → no leverage concern → PASS
        f = _features(long_term_debt=50.0, short_term_debt=50.0, cash_and_equivalents=500.0)
        self.assertTrue(mf.passes_health_gate(f))

    def test_fails_when_components_missing(self):
        # Conservative: missing data → can't verify → fail closed.
        self.assertFalse(mf.passes_health_gate(_features(operating_income_ttm=None)))
        self.assertFalse(mf.passes_health_gate(_features(long_term_debt=None)))


class TestCohortRank(unittest.TestCase):
    """``compute_cohort_rank`` operates on a DataFrame pre-populated with the
    6 metric columns (pe, ev_ebitda, ps, fcff_yield_pct, roic_pct, roe_pct)
    + a ``magic_formula_health_pass`` boolean. It returns a rank Series
    aligned with the input index: 1 = best (highest combined value+quality),
    N = worst; NaN for health-gate-failed or too-small cohorts.
    """

    def _df(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_returns_all_nan_when_cohort_lt_3_post_health_gate(self):
        df = self._df(
            [
                {
                    "ticker": "A",
                    "valuation_pe": 10.0,
                    "valuation_ev_ebitda": 5.0,
                    "valuation_ps": 1.0,
                    "fcff_yield_pct": 8.0,
                    "roic_pct": 15.0,
                    "roe_pct": 20.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "B",
                    "valuation_pe": 12.0,
                    "valuation_ev_ebitda": 6.0,
                    "valuation_ps": 2.0,
                    "fcff_yield_pct": 6.0,
                    "roic_pct": 10.0,
                    "roe_pct": 15.0,
                    "magic_formula_health_pass": True,
                },
            ]
        )
        ranks = mf.compute_cohort_rank(df)
        self.assertEqual(len(ranks), 2)
        self.assertTrue(all(pd.isna(r) for r in ranks))

    def test_ranks_5_stock_cohort_correctly(self):
        # 5 tickers; A dominates on all 6 (lowest pe/ev_eb/ps + highest yields/ROIC/ROE),
        # E is worst. Expected rank: A=1, E=5.
        df = self._df(
            [
                {
                    "ticker": "A",
                    "valuation_pe": 5.0,
                    "valuation_ev_ebitda": 3.0,
                    "valuation_ps": 0.5,
                    "fcff_yield_pct": 12.0,
                    "roic_pct": 25.0,
                    "roe_pct": 30.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "B",
                    "valuation_pe": 8.0,
                    "valuation_ev_ebitda": 5.0,
                    "valuation_ps": 1.0,
                    "fcff_yield_pct": 10.0,
                    "roic_pct": 20.0,
                    "roe_pct": 25.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "C",
                    "valuation_pe": 12.0,
                    "valuation_ev_ebitda": 7.0,
                    "valuation_ps": 2.0,
                    "fcff_yield_pct": 7.0,
                    "roic_pct": 15.0,
                    "roe_pct": 18.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "D",
                    "valuation_pe": 18.0,
                    "valuation_ev_ebitda": 10.0,
                    "valuation_ps": 4.0,
                    "fcff_yield_pct": 5.0,
                    "roic_pct": 10.0,
                    "roe_pct": 12.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "E",
                    "valuation_pe": 30.0,
                    "valuation_ev_ebitda": 18.0,
                    "valuation_ps": 8.0,
                    "fcff_yield_pct": 2.0,
                    "roic_pct": 5.0,
                    "roe_pct": 6.0,
                    "magic_formula_health_pass": True,
                },
            ]
        )
        ranks = mf.compute_cohort_rank(df)
        rank_by_ticker = dict(zip(df["ticker"], ranks, strict=True))
        self.assertEqual(rank_by_ticker["A"], 1)
        self.assertEqual(rank_by_ticker["B"], 2)
        self.assertEqual(rank_by_ticker["C"], 3)
        self.assertEqual(rank_by_ticker["D"], 4)
        self.assertEqual(rank_by_ticker["E"], 5)

    def test_health_failed_rows_get_nan_rank_and_excluded_from_n(self):
        # 4 survive health gate, 1 fails; survivors share rank space 1..4
        # and the failed row gets NaN.
        df = self._df(
            [
                {
                    "ticker": "A",
                    "valuation_pe": 5.0,
                    "valuation_ev_ebitda": 3.0,
                    "valuation_ps": 0.5,
                    "fcff_yield_pct": 12.0,
                    "roic_pct": 25.0,
                    "roe_pct": 30.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "B",
                    "valuation_pe": 8.0,
                    "valuation_ev_ebitda": 5.0,
                    "valuation_ps": 1.0,
                    "fcff_yield_pct": 10.0,
                    "roic_pct": 20.0,
                    "roe_pct": 25.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "C",
                    "valuation_pe": 12.0,
                    "valuation_ev_ebitda": 7.0,
                    "valuation_ps": 2.0,
                    "fcff_yield_pct": 7.0,
                    "roic_pct": 15.0,
                    "roe_pct": 18.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "D",
                    "valuation_pe": 18.0,
                    "valuation_ev_ebitda": 10.0,
                    "valuation_ps": 4.0,
                    "fcff_yield_pct": 5.0,
                    "roic_pct": 10.0,
                    "roe_pct": 12.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "X-fail",
                    "valuation_pe": float("nan"),
                    "valuation_ev_ebitda": float("nan"),
                    "valuation_ps": float("nan"),
                    "fcff_yield_pct": float("nan"),
                    "roic_pct": float("nan"),
                    "roe_pct": float("nan"),
                    "magic_formula_health_pass": False,
                },
            ]
        )
        ranks = mf.compute_cohort_rank(df)
        rank_by_ticker = dict(zip(df["ticker"], ranks, strict=True))
        self.assertTrue(math.isnan(rank_by_ticker["X-fail"]))
        # Survivors form a 1..4 contiguous integer ranking.
        survivor_ranks = {rank_by_ticker[t] for t in ["A", "B", "C", "D"]}
        self.assertEqual(survivor_ranks, {1, 2, 3, 4})

    def test_metric_nan_pushes_to_worst_rank_not_drop(self):
        # 4 rows; B has one missing metric. B should rank worse than peers
        # that beat it on that metric, but should still appear in the cohort
        # (not dropped — only health-gate failure drops).
        df = self._df(
            [
                {
                    "ticker": "A",
                    "valuation_pe": 5.0,
                    "valuation_ev_ebitda": 3.0,
                    "valuation_ps": 0.5,
                    "fcff_yield_pct": 12.0,
                    "roic_pct": 25.0,
                    "roe_pct": 30.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "B",
                    "valuation_pe": float("nan"),
                    "valuation_ev_ebitda": 5.0,
                    "valuation_ps": 1.0,
                    "fcff_yield_pct": 10.0,
                    "roic_pct": 20.0,
                    "roe_pct": 25.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "C",
                    "valuation_pe": 12.0,
                    "valuation_ev_ebitda": 7.0,
                    "valuation_ps": 2.0,
                    "fcff_yield_pct": 7.0,
                    "roic_pct": 15.0,
                    "roe_pct": 18.0,
                    "magic_formula_health_pass": True,
                },
                {
                    "ticker": "D",
                    "valuation_pe": 18.0,
                    "valuation_ev_ebitda": 10.0,
                    "valuation_ps": 4.0,
                    "fcff_yield_pct": 5.0,
                    "roic_pct": 10.0,
                    "roe_pct": 12.0,
                    "magic_formula_health_pass": True,
                },
            ]
        )
        ranks = mf.compute_cohort_rank(df)
        rank_by_ticker = dict(zip(df["ticker"], ranks, strict=True))
        # A still best (dominates).
        self.assertEqual(rank_by_ticker["A"], 1)
        # All 4 survivors must have integer ranks 1..4.
        self.assertEqual(
            sorted(rank_by_ticker[t] for t in ["A", "B", "C", "D"]),
            [1, 2, 3, 4],
        )

    def test_returns_int_ranks(self):
        df = self._df(
            [
                {
                    "ticker": t,
                    "valuation_pe": float(i + 5),
                    "valuation_ev_ebitda": float(i + 3),
                    "valuation_ps": float(i + 1),
                    "fcff_yield_pct": float(20 - i * 3),
                    "roic_pct": float(30 - i * 4),
                    "roe_pct": float(35 - i * 5),
                    "magic_formula_health_pass": True,
                }
                for i, t in enumerate(["A", "B", "C", "D", "E"])
            ]
        )
        ranks = mf.compute_cohort_rank(df)
        for r in ranks.dropna():
            self.assertEqual(r, int(r))


class TestMagicFormulaTopQuartile(unittest.TestCase):
    def test_true_when_rank_in_top_quartile(self):
        # cohort_n=8, top quartile = ranks 1-2
        self.assertTrue(mf.is_top_quartile(rank=1, cohort_n=8))
        self.assertTrue(mf.is_top_quartile(rank=2, cohort_n=8))
        self.assertFalse(mf.is_top_quartile(rank=3, cohort_n=8))

    def test_false_when_rank_is_nan(self):
        self.assertFalse(mf.is_top_quartile(rank=float("nan"), cohort_n=8))

    def test_false_when_cohort_too_small(self):
        # n<3 means rank itself is NaN, so quartile is False.
        self.assertFalse(mf.is_top_quartile(rank=1, cohort_n=2))

    def test_minimum_quartile_size_is_1(self):
        # cohort_n=3, n/4 floor = 0 — must round up to 1 so rank=1 still qualifies.
        self.assertTrue(mf.is_top_quartile(rank=1, cohort_n=3))
        self.assertFalse(mf.is_top_quartile(rank=2, cohort_n=3))


if __name__ == "__main__":
    unittest.main()
