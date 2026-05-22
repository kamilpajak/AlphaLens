"""Unit tests for ``alphalens_research.screeners.idiosyncratic_momentum.adapter``.

Covers the BacktestEngine adapter contract + the FF compounding helper.
Adapter tests use synthetic in-memory histories so no yfinance / FF CSV
dependency is required.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from alphalens_research.screeners.idiosyncratic_momentum.adapter import (
    IdiosyncraticMomentumScorer,
    _derive_asof,
    ff3_monthly_from_carhart_daily,
    rf_monthly_from_carhart_daily,
)


def _synthetic_daily_carhart(
    n_days: int = 252, seed: int = 0, start: str = "2014-01-02"
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0005, 0.01, n_days),
            "SMB": rng.normal(0.0001, 0.005, n_days),
            "HML": rng.normal(0.0001, 0.005, n_days),
            "RF": np.full(n_days, 0.00005),
        },
        index=idx,
    )


class TestFf3MonthlyFromCarhartDaily(unittest.TestCase):
    def test_returns_month_end_index(self):
        daily = _synthetic_daily_carhart(252)
        monthly = ff3_monthly_from_carhart_daily(daily)
        # All index entries should be month-end timestamps.
        for ts in monthly.index:
            self.assertEqual(ts, ts + pd.offsets.MonthEnd(0))

    def test_compounding_correct(self):
        # Trivial: constant daily return of 0 → monthly return of 0.
        idx = pd.bdate_range("2020-01-02", "2020-02-28")
        daily = pd.DataFrame(
            {
                "Mkt-RF": np.zeros(len(idx)),
                "SMB": np.zeros(len(idx)),
                "HML": np.zeros(len(idx)),
                "RF": np.zeros(len(idx)),
            },
            index=idx,
        )
        monthly = ff3_monthly_from_carhart_daily(daily)
        np.testing.assert_allclose(monthly.values, 0.0)

    def test_columns_preserved(self):
        daily = _synthetic_daily_carhart(60)
        monthly = ff3_monthly_from_carhart_daily(daily)
        self.assertEqual(set(monthly.columns), {"Mkt-RF", "SMB", "HML", "RF"})

    def test_missing_column_raises(self):
        bad = _synthetic_daily_carhart(60).drop(columns=["RF"])
        with self.assertRaises(ValueError):
            ff3_monthly_from_carhart_daily(bad)

    def test_rf_helper_extracts_series(self):
        daily = _synthetic_daily_carhart(60)
        rf = rf_monthly_from_carhart_daily(daily)
        self.assertIsInstance(rf, pd.Series)
        self.assertGreater(len(rf), 0)
        # Same index as the full monthly resample.
        full_monthly = ff3_monthly_from_carhart_daily(daily)
        pd.testing.assert_index_equal(rf.index, full_monthly.index)


class TestDeriveAsof(unittest.TestCase):
    def test_picks_latest_across_histories(self):
        idx_a = pd.bdate_range("2020-01-01", periods=10)
        idx_b = pd.bdate_range("2020-01-01", periods=15)  # extends later
        hist = {
            "A": pd.DataFrame({"close": np.arange(10)}, index=idx_a),
            "B": pd.DataFrame({"close": np.arange(15)}, index=idx_b),
        }
        self.assertEqual(_derive_asof(hist), idx_b[-1])

    def test_skips_empty_dataframes(self):
        idx_a = pd.bdate_range("2020-01-01", periods=10)
        hist = {
            "A": pd.DataFrame({"close": np.arange(10)}, index=idx_a),
            "B": pd.DataFrame({"close": []}, index=pd.DatetimeIndex([])),
            "C": None,
        }
        self.assertEqual(_derive_asof(hist), idx_a[-1])

    def test_all_empty_returns_none(self):
        hist = {"A": None, "B": pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))}
        self.assertIsNone(_derive_asof(hist))


class TestIdiosyncraticMomentumScorer(unittest.TestCase):
    def _build_synthetic_histories(
        self,
        tickers: list[str],
        n_days: int = 1000,
        seed: int = 0,
    ) -> dict[str, pd.DataFrame]:
        rng = np.random.default_rng(seed)
        idx = pd.bdate_range("2017-01-02", periods=n_days)
        hist = {}
        for i, t in enumerate(tickers):
            closes = 100.0 * (1.0 + rng.normal(0.0005, 0.01, n_days)).cumprod()
            # Inject a small ticker-specific bias to differentiate residuals.
            closes *= 1.0 + 0.01 * i
            hist[t] = pd.DataFrame({"close": closes}, index=idx)
        return hist

    def setUp(self):
        # FF3 covers 2014→2020 (~1750 bdays) so 36-month overlap with the
        # 2017→2020 ticker histories is guaranteed.
        carhart = _synthetic_daily_carhart(1750, seed=42, start="2014-01-02")
        self._ff3_monthly = ff3_monthly_from_carhart_daily(carhart)[["Mkt-RF", "SMB", "HML"]]
        self._rf_monthly = rf_monthly_from_carhart_daily(carhart)

    def test_min_bars_required_post_zen_bump(self):
        # MIN_BARS_REQUIRED=900 per zen H1 follow-up.
        self.assertEqual(IdiosyncraticMomentumScorer.MIN_BARS_REQUIRED, 900)

    def test_missing_factor_column_raises_on_init(self):
        bad = self._ff3_monthly.drop(columns=["HML"])
        with self.assertRaises(ValueError):
            IdiosyncraticMomentumScorer(bad, self._rf_monthly)

    def test_call_returns_score_dataframe(self):
        scorer = IdiosyncraticMomentumScorer(self._ff3_monthly, self._rf_monthly)
        hist = self._build_synthetic_histories(["A", "B", "C"], n_days=1000)
        out = scorer(hist, {})
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(list(out.columns), ["ticker", "score"])
        self.assertGreater(len(out), 0)

    def test_ticker_below_price_floor_dropped(self):
        scorer = IdiosyncraticMomentumScorer(
            self._ff3_monthly, self._rf_monthly, price_floor=10_000.0
        )
        hist = self._build_synthetic_histories(["A", "B"], n_days=1000)
        out = scorer(hist, {})
        self.assertTrue(out.empty)

    def test_benchmark_filtered_out(self):
        scorer = IdiosyncraticMomentumScorer(self._ff3_monthly, self._rf_monthly)
        hist = self._build_synthetic_histories(["A", "B", "BENCH"], n_days=1000)
        out = scorer(hist, {"benchmark": "BENCH"})
        self.assertNotIn("BENCH", set(out["ticker"]))

    def test_empty_histories_returns_empty(self):
        scorer = IdiosyncraticMomentumScorer(self._ff3_monthly, self._rf_monthly)
        out = scorer({}, {})
        self.assertTrue(out.empty)

    def test_output_sorted_descending(self):
        scorer = IdiosyncraticMomentumScorer(self._ff3_monthly, self._rf_monthly)
        hist = self._build_synthetic_histories(list("ABCDE"), n_days=1000)
        out = scorer(hist, {})
        scores = out["score"].tolist()
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
