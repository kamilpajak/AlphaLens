import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


def _mk_bar(d, price, vol=100_000):
    from alphalens.screeners.lean.lean_csv_writer import DailyBar

    return DailyBar(
        date=d, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=vol
    )


def _prime_store(tmpdir: Path, per_ticker_bars: dict[str, list]):
    """Load data into a HistoryStore from the given bar lists."""
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

    writer = LeanCsvWriter(tmpdir)
    for t, bars in per_ticker_bars.items():
        writer.write_bars(t, bars)
    histories = load_lean_histories(tmpdir, list(per_ticker_bars.keys()))
    return HistoryStore(histories)


def _long_history(n: int, start_price: float, drift: float = 0.0):
    """Generate `n` trading-day-esque bars starting 2024-01-01, advancing by weekdays."""
    dates = pd.bdate_range(start="2024-01-01", periods=n)
    prices = start_price * np.exp(np.cumsum(np.full(n, drift)))
    return [_mk_bar(d.strftime("%Y%m%d"), float(p)) for d, p in zip(dates, prices)]


class TestBacktestEngineBasic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _tiny_config(self):
        # Small windows so we can use short histories in tests.
        return {
            "roc_short": 5,
            "roc_medium": 10,
            "roc_long": 20,
            "sma_short": 5,
            "sma_medium": 10,
            "sma_long": 20,
            "volume_window": 5,
            "breakout_window": 10,
            "breakout_volume_multiple": 1.0,
            "near_high_window": 20,
            "min_price": 1.0,
            "max_price": 10_000.0,
            "min_avg_dollar_volume": 0.0,
            "weight_roc20": 0.2,
            "weight_roc60": 0.2,
            "weight_volume_surprise": 0.2,
            "weight_trend_strength": 0.2,
            "weight_breakout": 0.1,
            "weight_near_high": 0.1,
            "top_n": 3,
        }

    def test_runs_and_produces_daily_results(self):
        from alphalens.backtest.engine import BacktestEngine
        from alphalens.screeners.lean.lean_project.scorer import rank_universe

        rank_universe.MIN_BARS_REQUIRED = 25  # tiny tests — declare via scorer

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(150, 400.0, drift=0.001),
                "AAA": _long_history(150, 100.0, drift=0.0008),
                "BBB": _long_history(150, 100.0, drift=-0.0005),
                "CCC": _long_history(150, 100.0, drift=0.0002),
                "DDD": _long_history(150, 100.0, drift=0.0003),
            },
        )
        engine = BacktestEngine(
            store,
            scorer=rank_universe,
            scorer_config=self._tiny_config(),
            holding_period=3,
            top_n=2,
            benchmark="SPY",
        )
        report = engine.run(start=date(2024, 4, 1), end=date(2024, 4, 30))

        self.assertGreater(len(report.daily_results), 0)
        self.assertEqual(report.top_n, 2)
        self.assertEqual(report.holding_period, 3)

    def test_uses_injected_scorer(self):
        """Engine should call whatever scorer callable is injected."""
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(120, 400.0),
                "AAA": _long_history(120, 100.0, drift=0.001),
                "BBB": _long_history(120, 100.0, drift=-0.001),
            },
        )

        def fake_scorer(histories, config):
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "BBB", "score": 0.0},
                ]
            )

        fake_scorer.MIN_BARS_REQUIRED = 25

        engine = BacktestEngine(
            store,
            scorer=fake_scorer,
            scorer_config={},
            holding_period=2,
            top_n=1,
            benchmark="SPY",
            screener_tickers=["AAA", "BBB"],
        )

        report = engine.run(start=date(2024, 3, 1), end=date(2024, 3, 15))

        self.assertGreater(len(report.daily_results), 0)
        for r in report.daily_results:
            self.assertEqual(r.top_n_tickers[0], "AAA")

    def test_top_n_picks_highest_scores(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(120, 400.0),
                "A": _long_history(120, 100.0),
                "B": _long_history(120, 100.0),
                "C": _long_history(120, 100.0),
                "D": _long_history(120, 100.0),
                "E": _long_history(120, 100.0),
            },
        )

        def fake_scorer(histories, config):
            return pd.DataFrame(
                [
                    {"ticker": "A", "score": 5.0},
                    {"ticker": "B", "score": 4.0},
                    {"ticker": "C", "score": 3.0},
                    {"ticker": "D", "score": 2.0},
                    {"ticker": "E", "score": 1.0},
                ]
            )

        fake_scorer.MIN_BARS_REQUIRED = 25

        engine = BacktestEngine(
            store,
            scorer=fake_scorer,
            scorer_config={},
            holding_period=2,
            top_n=3,
            benchmark="SPY",
            screener_tickers=["A", "B", "C", "D", "E"],
        )
        report = engine.run(start=date(2024, 3, 1), end=date(2024, 3, 15))

        for r in report.daily_results:
            self.assertEqual(r.top_n_tickers[:3], ["A", "B", "C"])

    def test_skips_days_without_enough_history(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(50, 400.0),
                "A": _long_history(50, 100.0),
            },
        )
        impossible_scorer = lambda h, c: pd.DataFrame([{"ticker": "A", "score": 1.0}])
        impossible_scorer.MIN_BARS_REQUIRED = 1000  # impossible minimum
        engine = BacktestEngine(
            store,
            scorer=impossible_scorer,
            scorer_config={},
            holding_period=2,
            top_n=1,
            benchmark="SPY",
            screener_tickers=["A"],
        )
        report = engine.run(start=date(2024, 1, 2), end=date(2024, 3, 1))

        self.assertEqual(len(report.daily_results), 0)


class TestBacktestReport(unittest.TestCase):
    def _make_report(self, daily_results):
        from alphalens.backtest.engine import BacktestReport

        return BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=3,
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            benchmark="SPY",
            universe_ticker_count=10,
            daily_results=daily_results,
        )

    def _mock_daily(self, date_str, port_ret=0.01, tickers=None, ic=0.05):
        from alphalens.backtest.engine import DailyResult

        tickers = tickers or ["A", "B", "C"]
        return DailyResult(
            date=pd.Timestamp(date_str),
            scored_count=10,
            top_n_tickers=tickers,
            top_n_scores=[1.0, 0.9, 0.8],
            top_n_forward_returns=[port_ret, port_ret, port_ret],
            portfolio_return=port_ret,
            portfolio_return_holding=port_ret * 5,
            universe_median_return=0.005,
            ic=ic,
        )

    def test_portfolio_returns_series(self):
        r = self._make_report(
            [self._mock_daily("2024-01-02", 0.01), self._mock_daily("2024-01-03", 0.02)]
        )
        s = r.portfolio_returns
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s.iloc[0], 0.01)
        self.assertAlmostEqual(s.iloc[1], 0.02)

    def test_ic_series(self):
        r = self._make_report(
            [
                self._mock_daily("2024-01-02", ic=0.1),
                self._mock_daily("2024-01-03", ic=0.05),
            ]
        )
        self.assertEqual(list(r.ic_series.values), [0.1, 0.05])

    def test_turnover_across_snapshots(self):
        r = self._make_report(
            [
                self._mock_daily("2024-01-02", tickers=["A", "B", "C"]),
                self._mock_daily("2024-01-03", tickers=["A", "X", "Y"]),
            ]
        )
        self.assertAlmostEqual(r.turnover, 2 / 3, places=6)


class TestIntegrationWithRealScorer(unittest.TestCase):
    """Use the production `rank_universe` — skip if we can't synthesise enough data."""

    def test_real_scorer_integrates(self):
        from alphalens.backtest.engine import BacktestEngine
        from alphalens.screeners.lean.config import LEAN_DEFAULTS
        from alphalens.screeners.lean.lean_project.scorer import rank_universe

        with tempfile.TemporaryDirectory() as tmp:
            store = _prime_store(
                Path(tmp),
                {
                    "SPY": _long_history(300, 400.0, drift=0.0005),
                    "UP1": _long_history(300, 50.0, drift=0.001),
                    "UP2": _long_history(300, 80.0, drift=0.0008),
                    "FLAT": _long_history(300, 30.0, drift=0.0),
                    "DOWN": _long_history(300, 60.0, drift=-0.0008),
                },
            )
            engine = BacktestEngine(
                store,
                scorer=rank_universe,
                scorer_config=LEAN_DEFAULTS,
                holding_period=3,
                top_n=2,
                benchmark="SPY",
                screener_tickers=["UP1", "UP2", "FLAT", "DOWN"],
            )
            report = engine.run(start=date(2024, 12, 1), end=date(2024, 12, 31))

        self.assertGreater(len(report.daily_results), 0)
        top_tickers = [t for r in report.daily_results for t in r.top_n_tickers]
        self.assertGreater(
            top_tickers.count("UP1") + top_tickers.count("UP2"),
            top_tickers.count("DOWN"),
        )


class TestBacktestEngineScorerMinBars(unittest.TestCase):
    """Engine must respect a scorer's declared MIN_BARS_REQUIRED (max against default).

    Rationale: without this, scorers whose indicators need >220 bars (Jegadeesh 11-1,
    52w high, long SMA) silently run on insufficient warmup, producing noisy scores
    that inflate or deflate Sharpe/alpha in hard-to-diagnose ways. This invalidated
    the Apr 21 CLI re-run of the early-stage scorer.
    """

    def test_engine_uses_scorer_declared_min_bars_when_higher_than_default(self):
        from alphalens.backtest.engine import BacktestEngine

        tickers_seen_at_scorer: list[int] = []

        def scorer(histories, config):
            tickers_seen_at_scorer.append(len(histories))
            tickers = list(histories.keys())
            return pd.DataFrame({"ticker": tickers, "score": np.linspace(1.0, 0.1, len(tickers))})

        scorer.MIN_BARS_REQUIRED = 300  # way above engine default 220

        with tempfile.TemporaryDirectory() as tmp:
            store = _prime_store(
                Path(tmp),
                {
                    "SPY": _long_history(260, 400.0, drift=0.0005),
                    "A": _long_history(260, 50.0, drift=0.001),
                    "B": _long_history(260, 80.0, drift=0.0008),
                },
            )
            engine = BacktestEngine(
                store,
                scorer=scorer,
                scorer_config={},
                holding_period=2,
                top_n=1,
                benchmark="SPY",
                screener_tickers=["A", "B"],
            )
            engine.run(start=date(2024, 12, 1), end=date(2024, 12, 31))

        # 260 bars < 300 declared → no ticker ever qualifies → scorer never called.
        self.assertEqual(
            tickers_seen_at_scorer,
            [],
            "scorer.MIN_BARS_REQUIRED=300 was not respected by engine "
            "(tickers only had 260 bars but were passed to scorer anyway)",
        )

    def test_engine_falls_back_to_class_default_when_scorer_has_no_attribute(self):
        from alphalens.backtest.engine import BacktestEngine

        called_count: list[int] = []

        def scorer(histories, config):
            called_count.append(len(histories))
            tickers = list(histories.keys())
            return pd.DataFrame({"ticker": tickers, "score": np.linspace(1.0, 0.1, len(tickers))})

        # Note: no MIN_BARS_REQUIRED attribute on this scorer

        with tempfile.TemporaryDirectory() as tmp:
            store = _prime_store(
                Path(tmp),
                {
                    "SPY": _long_history(260, 400.0, drift=0.0005),
                    "A": _long_history(260, 50.0, drift=0.001),
                    "B": _long_history(260, 80.0, drift=0.0008),
                },
            )
            engine = BacktestEngine(
                store,
                scorer=scorer,
                scorer_config={},
                holding_period=2,
                top_n=1,
                benchmark="SPY",
                screener_tickers=["A", "B"],
            )
            engine.run(start=date(2024, 12, 1), end=date(2024, 12, 31))

        # 260 > default 220 → scorer should have been called on most days.
        self.assertGreater(
            len(called_count),
            0,
            "without scorer.MIN_BARS_REQUIRED engine should use class default (220), "
            "and 260 bars > 220 means scorer should have been called",
        )


if __name__ == "__main__":
    unittest.main()
