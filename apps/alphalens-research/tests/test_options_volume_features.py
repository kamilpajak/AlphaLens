"""Tests for options_volume feature joiner.

Pre-registered as `pc_abnormal_volume_retrospective_pre_2018_2026_05_05`.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from alphalens_research.screeners.options_volume.features import (
    FEATURE_COLUMNS,
    build_feature_frame,
)


def _make_smd_history(
    ticker: str = "AAPL",
    n_days: int = 200,
    start: str = "2010-01-04",
    optvol_put: float = 50.0,
    optvol_call: float = 100.0,
    close_growth: float = 1.0005,
    market_cap: float = 5e10,
) -> pd.DataFrame:
    """Synthetic smd response with deterministic close growth."""
    dates = pd.bdate_range(start, periods=n_days)
    closes = [100.0 * (close_growth**i) for i in range(n_days)]
    return pd.DataFrame(
        {
            "symbol": ticker,
            "tradeDate": [d.strftime("%Y-%m-%d") for d in dates],
            "exchange": "NYSE",
            "close": closes,
            "stockVolume": [10_000_000] * n_days,
            "optVol": [1000.0] * n_days,
            "optVolPut": [optvol_put] * n_days,
            "optVolCall": [optvol_call] * n_days,
            "openInterestCall": [5000.0] * n_days,
            "openInterestPut": [3000.0] * n_days,
            "marketCap": [market_cap] * n_days,
        }
    )


class TestBuildFeatureFrame(unittest.TestCase):
    def test_emits_one_row_per_ticker_asof_when_eligible(self):
        history = _make_smd_history(n_days=200)
        loader = lambda t: history.copy() if t == "AAPL" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 1)
        self.assertEqual(set(df.columns), {"asof", "ticker", *FEATURE_COLUMNS})
        self.assertNotIn("log_marketCap", df.columns)  # pre-reg amendment 2026-05-05
        self.assertEqual(df.iloc[0]["ticker"], "AAPL")

    def test_drops_ticker_with_insufficient_history(self):
        # Need ≥127 trading days for momentum_6m
        history = _make_smd_history(n_days=50)
        loader = lambda t: history.copy() if t == "AAPL" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL"],
            asof_dates=["2010-03-15"],
        )
        self.assertEqual(len(df), 0)

    def test_drops_ticker_with_zero_optvol(self):
        history = _make_smd_history(n_days=200, optvol_put=0.0)
        loader = lambda t: history.copy() if t == "AAPL" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        # abnormal_pcr will be NaN because pcr is NaN every day
        self.assertEqual(len(df), 0)

    def test_drops_non_us_exchange(self):
        history = _make_smd_history(n_days=200)
        history["exchange"] = "TSX"
        loader = lambda t: history.copy() if t == "TSX_TICKER" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["TSX_TICKER"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)

    def test_handles_missing_marketcap_does_not_block(self):
        # Pre-reg amendment 2026-05-05: marketCap NOT in controls; missing marketCap
        # must NOT block feature emission (vendor pre-2018 cache has marketCap=NaN).
        history = _make_smd_history(n_days=200)
        history["marketCap"] = np.nan
        loader = lambda t: history.copy() if t == "AAPL" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 1)

    def test_abnormal_pcr_is_zero_for_constant_volume_history(self):
        # Constant put/call ratio → pcr_t = log(0.5), rolling_mean = log(0.5),
        # abnormal_pcr_t = 0 once warmup completes.
        history = _make_smd_history(n_days=200)
        loader = lambda t: history.copy() if t == "AAPL" else None  # noqa: E731
        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["abnormal_pcr"], 0.0, places=10)

    def test_multiple_tickers_multiple_asofs(self):
        h1 = _make_smd_history(ticker="AAPL", n_days=200)
        h2 = _make_smd_history(ticker="MSFT", n_days=200)
        loader_map = {"AAPL": h1, "MSFT": h2}

        def loader(t):
            return (
                loader_map.get(t, pd.DataFrame()).copy() if loader_map.get(t) is not None else None
            )

        df = build_feature_frame(
            smd_loader=loader,
            universe=["AAPL", "MSFT"],
            asof_dates=["2010-09-01", "2010-09-15"],
        )
        # 2 tickers × 2 asofs = 4 rows
        self.assertEqual(len(df), 4)
        self.assertEqual(set(df["ticker"].unique()), {"AAPL", "MSFT"})
        self.assertEqual(set(df["asof"].unique()), {"2010-09-01", "2010-09-15"})

    def test_empty_universe_returns_empty_frame_with_columns(self):
        df = build_feature_frame(
            smd_loader=lambda t: None,
            universe=[],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)
        self.assertIn("abnormal_pcr", df.columns)
        self.assertIn("rv_30d", df.columns)


class TestEarlyReturnBranches(unittest.TestCase):
    """Cover early-return branches in features.py for SonarCloud coverage."""

    def test_filter_us_primary_no_exchange_column(self):
        # Line 51: returns coerced copy when no exchange column
        from alphalens_research.screeners.options_volume.features import _filter_us_primary

        df = pd.DataFrame({"close": [1.0, 2.0], "tradeDate": ["2024-01-02", "2024-01-03"]})
        out = _filter_us_primary(df)
        self.assertEqual(len(out), 2)

    def test_slice_pit_empty_history(self):
        # Line 63-64: empty history returns as-is
        from alphalens_research.screeners.options_volume.features import _slice_pit

        df = pd.DataFrame()
        out = _slice_pit(df, "2024-01-15")
        self.assertTrue(out.empty)

    def test_slice_pit_no_tradedate_column(self):
        from alphalens_research.screeners.options_volume.features import _slice_pit

        df = pd.DataFrame({"close": [1.0]})
        out = _slice_pit(df, "2024-01-15")
        self.assertEqual(len(out), 1)

    def test_slice_pit_filters_close_nan(self):
        from alphalens_research.screeners.options_volume.features import _slice_pit

        df = pd.DataFrame(
            {
                "tradeDate": ["2024-01-03", "2024-01-02", "2024-01-04"],
                "close": [100.0, np.nan, 102.0],
            }
        )
        out = _slice_pit(df, "2024-01-15")
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[0]["tradeDate"], "2024-01-03")

    def test_compute_equity_controls_insufficient_history(self):
        from alphalens_research.screeners.options_volume.features import _compute_equity_controls

        df = pd.DataFrame({"close": [100.0] * 50})
        self.assertIsNone(_compute_equity_controls(df))

    def test_compute_equity_controls_non_finite_close(self):
        from alphalens_research.screeners.options_volume.features import _compute_equity_controls

        closes = [100.0] * 130
        closes[-50] = np.inf
        df = pd.DataFrame({"close": closes})
        self.assertIsNone(_compute_equity_controls(df))

    def test_compute_equity_controls_close_now_zero(self):
        from alphalens_research.screeners.options_volume.features import _compute_equity_controls

        closes = [100.0] * 130
        closes[-1] = 0.0
        df = pd.DataFrame({"close": closes})
        self.assertIsNone(_compute_equity_controls(df))

    def test_compute_equity_controls_close_21_zero(self):
        from alphalens_research.screeners.options_volume.features import _compute_equity_controls

        closes = [100.0] * 130
        closes[-22] = 0.0
        df = pd.DataFrame({"close": closes})
        self.assertIsNone(_compute_equity_controls(df))

    def test_compute_equity_controls_log_rets_non_finite(self):
        from alphalens_research.screeners.options_volume.features import _compute_equity_controls

        closes = [100.0] * 130
        closes[-15] = 1e-300  # log of tiny becomes huge negative; check finite-fail path
        # Actually a near-zero close still gives finite log; use a NaN injection instead
        closes[-15] = float("nan")
        df = pd.DataFrame({"close": closes})
        # Returns None either via "not all finite" trailing-window check
        self.assertIsNone(_compute_equity_controls(df))

    def test_build_feature_frame_loader_returns_none(self):
        df = build_feature_frame(
            smd_loader=lambda _t: None,
            universe=["UNKNOWN"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)

    def test_build_feature_frame_no_close_column_after_filter(self):
        history = pd.DataFrame(
            {
                "exchange": ["NYSE"],
                "tradeDate": ["2024-01-02"],
            }
        )
        df = build_feature_frame(
            smd_loader=lambda _t: history.copy(),
            universe=["X"],
            asof_dates=["2024-02-01"],
        )
        self.assertEqual(len(df), 0)

    def test_build_feature_frame_no_optvol_columns(self):
        history = _make_smd_history(n_days=200)
        history = history.drop(columns=["optVolPut", "optVolCall"])
        df = build_feature_frame(
            smd_loader=lambda _t: history.copy(),
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)

    def test_build_feature_frame_optionable_filter_fails(self):
        history = _make_smd_history(n_days=200)
        history["optVol"] = 0
        history["openInterestCall"] = 0
        history["openInterestPut"] = 0
        df = build_feature_frame(
            smd_loader=lambda _t: history.copy(),
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)

    def test_build_feature_frame_adv_filter_fails(self):
        history = _make_smd_history(n_days=200)
        history["stockVolume"] = 1
        df = build_feature_frame(
            smd_loader=lambda _t: history.copy(),
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
            adv_min_dollar=1_000_000_000.0,
        )
        self.assertEqual(len(df), 0)

    def test_build_feature_frame_close_below_min_price(self):
        history = _make_smd_history(n_days=200, close_growth=1.0)
        history["close"] = 0.5
        df = build_feature_frame(
            smd_loader=lambda _t: history.copy(),
            universe=["AAPL"],
            asof_dates=["2010-09-01"],
        )
        self.assertEqual(len(df), 0)


if __name__ == "__main__":
    unittest.main()
