"""Tests for ``alphalens.paper_trade.scorer_v9d``.

Heavy integration tests against actual SMD cache + PIT universe live in
manual smoke runs, not unit tests. These tests cover (a) closure
memoization on the SMD loader, (b) ``compute_realized_return`` arithmetic
on injected synthetic data, (c) graceful empty-features fallback in
``score_top_decile``.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import pandas as pd

from alphalens.paper_trade.scorer_v9d import (
    ScoringResult,
    benchmark_return,
    compute_realized_return,
    latest_trading_asof,
    make_smd_loader,
    score_top_decile,
)


def _synth_smd_df(start: date, n_days: int, close: float = 100.0) -> pd.DataFrame:
    """Tiny synthetic SMD frame — enough columns for forward_raw_return.

    Uses calendar-day stride and Mon-Fri filter; `forward_raw_return`
    counts trading-day forward bars in the cached frame, so we generate
    weekday-only rows here."""
    rows = []
    d = start
    while len(rows) < n_days:
        if d.weekday() < 5:  # Mon-Fri
            rows.append(
                {
                    "tradeDate": d.isoformat(),
                    "close": close * (1.0 + 0.001 * len(rows)),  # 10bp/day drift
                    "ivp30": 0.5,
                    "exchange": "NYSE",
                }
            )
        d = d + timedelta(days=1)
    return pd.DataFrame(rows)


class MakeSmdLoaderTests(unittest.TestCase):
    def test_memoizes_results(self):
        call_count = {"n": 0}

        def fake_loader(ticker: str, cache_dir):  # pragma: no cover
            call_count["n"] += 1
            return _synth_smd_df(date(2026, 5, 1), 5)

        # The closure under test wraps load_cached_smd; we patch with a
        # double-wrapper so we can count actual disk hits.
        from unittest.mock import patch

        loader = make_smd_loader()
        with patch(
            "alphalens.paper_trade.scorer_v9d.load_cached_smd", side_effect=fake_loader
        ) as mock_load:
            loader("AAPL")
            loader("AAPL")
            loader("MSFT")
            self.assertEqual(mock_load.call_count, 2)


class ComputeRealizedReturnTests(unittest.TestCase):
    def test_empty_holdings_returns_nan(self):
        ret, n = compute_realized_return([], date(2026, 5, 4))
        self.assertTrue(ret != ret)  # NaN
        self.assertEqual(n, 0)

    def test_uses_injected_smd_loader(self):
        synth = _synth_smd_df(date(2026, 5, 1), 30)

        def fake_loader(ticker: str):
            return synth

        ret, n = compute_realized_return(
            ["AAPL"],
            date(2026, 5, 4),
            holding_period_days=5,
            smd_loader=fake_loader,
        )
        # Synthetic curve: stockClose at t increases by 10bp per day, so
        # the 5-day-forward return is ~ (close[t+5]/close[t]) - 1 ≈ +0.5%.
        self.assertGreater(ret, 0.0)
        self.assertEqual(n, 1)


class BenchmarkReturnTests(unittest.TestCase):
    def test_uses_injected_smd_loader(self):
        synth = _synth_smd_df(date(2026, 5, 1), 30)

        def fake_loader(ticker: str):
            self.assertEqual(ticker, "MDY")
            return synth

        r = benchmark_return(date(2026, 5, 4), holding_period_days=5, smd_loader=fake_loader)
        self.assertGreater(r, 0.0)


class LatestTradingAsofTests(unittest.TestCase):
    def test_returns_most_recent_valid_date_on_or_before_today(self):
        synth = _synth_smd_df(date(2026, 4, 27), 5)  # 4-27 → 5-1

        def fake_loader(ticker: str):
            return synth

        # today=2026-05-04 → most recent ivp30-valid date ≤ today is 2026-05-01
        result = latest_trading_asof(today=date(2026, 5, 4), smd_loader=fake_loader)
        self.assertEqual(result, date(2026, 5, 1))

    def test_none_when_loader_returns_no_data(self):
        result = latest_trading_asof(today=date(2026, 5, 4), smd_loader=lambda t: None)
        self.assertIsNone(result)


class ScoreTopDecileEmptyFeaturesTests(unittest.TestCase):
    def test_empty_features_returns_empty_result(self):
        """If ``build_feature_frame`` returns an empty DataFrame (no
        ticker has data on the asof), the function must not crash."""
        # Inject a loader that always returns None → build_feature_frame
        # produces an empty frame.
        result = score_top_decile(
            date(2026, 5, 4),
            universe=["BOGUS1", "BOGUS2"],
            smd_loader=lambda t: None,
        )
        self.assertIsInstance(result, ScoringResult)
        self.assertEqual(result.universe_size, 2)
        self.assertEqual(result.n_scored, 0)
        self.assertEqual(result.top_decile_tickers, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
