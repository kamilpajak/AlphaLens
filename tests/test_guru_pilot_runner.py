"""Tests for alphalens.archive.guru.pilot_runner — sample → score → top-N → 1y return."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from alphalens.archive.guru.llm_scorer import ConvictionResult


def _price_series(daily_ret: float, n_bars: int = 520, start: str = "2017-01-02") -> pd.Series:
    idx = pd.date_range(start, periods=n_bars, freq="B")
    return pd.Series((1.0 + daily_ret) ** np.arange(n_bars) * 100.0, index=idx)


class TestSampleTickers(unittest.TestCase):
    def test_deterministic_with_seed(self):
        from alphalens.archive.guru.pilot_runner import sample_tickers

        universe = [f"T{i:03d}" for i in range(500)]
        s1 = sample_tickers(universe, size=30, seed=42)
        s2 = sample_tickers(universe, size=30, seed=42)
        s3 = sample_tickers(universe, size=30, seed=43)

        self.assertEqual(s1, s2)
        self.assertNotEqual(s1, s3)
        self.assertEqual(len(s1), 30)
        self.assertEqual(len(set(s1)), 30)  # no duplicates

    def test_size_larger_than_universe_raises(self):
        from alphalens.archive.guru.pilot_runner import sample_tickers

        with self.assertRaises(ValueError):
            sample_tickers(["A", "B"], size=5, seed=1)


class TestRunSingleYear(unittest.TestCase):
    def _stub_scorer(self, conviction_by_ticker: dict[str, float]) -> MagicMock:
        scorer = MagicMock()

        def _score(*, ticker, asof, context_text):
            c = conviction_by_ticker.get(ticker, 50.0)
            return ConvictionResult(
                ticker=ticker,
                asof=asof,
                conviction=c,
                rationale=f"stub for {ticker}",
                prompt_sha="x" * 64,
                raw_response="{}",
                input_tokens=1000,
                output_tokens=50,
                cost_usd=0.001,
            )

        scorer.score.side_effect = _score
        return scorer

    def _stub_context_builder(self) -> MagicMock:
        return MagicMock(return_value={"ticker": "X", "price_summary": {"latest_close": 100}})

    def _stub_price_store(self, tickers_with_prices: dict[str, pd.Series]) -> MagicMock:
        store = MagicMock()
        store.full.side_effect = lambda t: pd.DataFrame({"close": tickers_with_prices[t]})
        return store

    def test_runs_pilot_for_single_year_and_returns_outperformance(self):
        from alphalens.archive.guru.pilot_runner import run_single_year

        tickers = [f"T{i:03d}" for i in range(40)]
        # T000..T009 are winners (0.001/day, +28% annual)
        # T010..T039 are losers (-0.0005/day, -12% annual)
        prices = {}
        for i, t in enumerate(tickers):
            prices[t] = _price_series(0.001 if i < 10 else -0.0005)
        prices["SPY"] = _price_series(0.0004)

        # Scorer ranks winners highest (conviction 90) losers lowest (20)
        conviction = {t: 90.0 if i < 10 else 20.0 for i, t in enumerate(tickers)}
        scorer = self._stub_scorer(conviction)

        result = run_single_year(
            year=2018,
            universe=tickers,
            sample_size=20,
            top_n=5,
            seed=42,
            scorer=scorer,
            context_builder=self._stub_context_builder(),
            price_store=self._stub_price_store(prices),
            benchmark="SPY",
        )

        self.assertEqual(result.year, 2018)
        self.assertEqual(len(result.picks), 5)
        # All top-5 picks should have conviction 90 (winners)
        self.assertTrue(all(p.conviction == 90.0 for p in result.picks))
        # Portfolio return should significantly exceed SPY benchmark
        self.assertGreater(result.portfolio_return, result.benchmark_return)
        self.assertGreater(result.outperformance, 0.0)

    def test_skips_tickers_without_context_data(self):
        from alphalens.archive.guru.pilot_runner import run_single_year

        tickers = ["A", "B", "C", "D", "E"]
        prices = {t: _price_series(0.0004) for t in tickers}
        prices["SPY"] = _price_series(0.0004)

        # Context builder returns None for "B" and "D" (missing AV data)
        ctx_builder = MagicMock()

        def _build(*, ticker, asof, price_series):
            if ticker in ("B", "D"):
                return None
            return {"ticker": ticker}

        ctx_builder.side_effect = _build

        scorer = self._stub_scorer(dict.fromkeys(tickers, 70.0))

        result = run_single_year(
            year=2018,
            universe=tickers,
            sample_size=5,
            top_n=3,
            seed=42,
            scorer=scorer,
            context_builder=ctx_builder,
            price_store=self._stub_price_store(prices),
            benchmark="SPY",
        )

        # Only A, C, E should be scored — and top-3 = all 3
        self.assertEqual(len(result.picks), 3)
        picked_tickers = {p.ticker for p in result.picks}
        self.assertTrue(picked_tickers.issubset({"A", "C", "E"}))

    def test_picks_top_n_by_conviction(self):
        from alphalens.archive.guru.pilot_runner import run_single_year

        tickers = ["A", "B", "C", "D", "E"]
        prices = {t: _price_series(0.0004) for t in tickers}
        prices["SPY"] = _price_series(0.0004)

        conviction = {"A": 95, "B": 20, "C": 80, "D": 10, "E": 70}
        scorer = self._stub_scorer(conviction)

        result = run_single_year(
            year=2018,
            universe=tickers,
            sample_size=5,
            top_n=3,
            seed=42,
            scorer=scorer,
            context_builder=self._stub_context_builder(),
            price_store=self._stub_price_store(prices),
            benchmark="SPY",
        )

        picked_tickers = {p.ticker for p in result.picks}
        # Top-3 by conviction = A (95), C (80), E (70)
        self.assertEqual(picked_tickers, {"A", "C", "E"})

    def test_context_builder_exception_treats_ticker_as_skipped(self):
        """AV rate limit / network errors in build_context shouldn't kill year."""
        from alphalens.archive.guru.pilot_runner import run_single_year

        tickers = ["A", "B", "C", "D", "E"]
        prices = {t: _price_series(0.0004) for t in tickers}
        prices["SPY"] = _price_series(0.0004)

        # Context builder raises for B (e.g. AV rate limit), returns dict for others
        def _ctx(*, ticker, asof, price_series):
            if ticker == "B":
                raise RuntimeError("AV rate limit exceeded")
            return {"ticker": ticker}

        scorer = self._stub_scorer(dict.fromkeys(tickers, 70.0))

        result = run_single_year(
            year=2018,
            universe=tickers,
            sample_size=5,
            top_n=3,
            seed=42,
            scorer=scorer,
            context_builder=_ctx,
            price_store=self._stub_price_store(prices),
            benchmark="SPY",
        )

        # B skipped due to AV exception; A, C, D, E scored → top-3 from those
        self.assertEqual(len(result.picks), 3)
        self.assertNotIn("B", {p.ticker for p in result.picks})
        self.assertIn("B", result.skipped_tickers)

    def test_totals_cost_across_all_scorer_calls(self):
        from alphalens.archive.guru.pilot_runner import run_single_year

        tickers = ["A", "B", "C", "D", "E"]
        prices = {t: _price_series(0.0004) for t in tickers}
        prices["SPY"] = _price_series(0.0004)

        scorer = self._stub_scorer(dict.fromkeys(tickers, 50.0))

        result = run_single_year(
            year=2018,
            universe=tickers,
            sample_size=5,
            top_n=3,
            seed=42,
            scorer=scorer,
            context_builder=self._stub_context_builder(),
            price_store=self._stub_price_store(prices),
            benchmark="SPY",
        )

        # 5 tickers scored, $0.001 each → $0.005 total
        self.assertAlmostEqual(result.total_cost_usd, 0.005, places=6)


if __name__ == "__main__":
    unittest.main()
