"""TDD for engine extensions — DailyResult.top_n_weights, .trades; Trade dataclass;
BacktestEngine.portfolio_value kwarg."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


def _mk_bar(d, price, vol=100_000):
    from alphalens.screeners.lean.lean_csv_writer import DailyBar

    return DailyBar(
        date=d,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        volume=vol,
    )


def _prime_store(tmpdir: Path, per_ticker_bars: dict[str, list]):
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

    writer = LeanCsvWriter(tmpdir)
    for t, bars in per_ticker_bars.items():
        writer.write_bars(t, bars)
    histories = load_lean_histories(tmpdir, list(per_ticker_bars.keys()))
    return HistoryStore(histories)


def _long_history(n: int, start_price: float, drift: float = 0.0):
    dates = pd.bdate_range(start="2024-01-01", periods=n)
    prices = start_price * np.exp(np.cumsum(np.full(n, drift)))
    return [_mk_bar(d.strftime("%Y%m%d"), float(p)) for d, p in zip(dates, prices)]


class TestDailyResultExtensions(unittest.TestCase):
    def test_daily_result_has_top_n_weights_field(self):
        from alphalens.backtest.engine import DailyResult

        res = DailyResult(
            date=pd.Timestamp("2024-04-01"),
            scored_count=2,
            top_n_tickers=["AAA", "BBB"],
            top_n_scores=[1.0, 0.5],
            top_n_forward_returns=[0.02, 0.01],
            portfolio_return=0.015,
            portfolio_return_holding=0.05,
            universe_median_return=0.01,
            ic=0.7,
        )
        # Backwards-compatible default: empty list if not provided.
        self.assertEqual(res.top_n_weights, [])
        self.assertIsNone(res.trades)

    def test_daily_result_accepts_weights_and_trades(self):
        from alphalens.backtest.engine import DailyResult, Trade

        trades = [
            Trade(date=pd.Timestamp("2024-04-01"), ticker="AAA",
                  notional=10_000.0, direction="enter", price=100.0),
        ]
        res = DailyResult(
            date=pd.Timestamp("2024-04-01"),
            scored_count=1,
            top_n_tickers=["AAA"],
            top_n_scores=[1.0],
            top_n_forward_returns=[0.02],
            portfolio_return=0.02,
            portfolio_return_holding=0.05,
            universe_median_return=0.01,
            ic=0.5,
            top_n_weights=[1.0],
            trades=trades,
        )
        self.assertEqual(res.top_n_weights, [1.0])
        self.assertEqual(res.trades, trades)


class TestTradeDataclass(unittest.TestCase):
    def test_trade_fields_and_frozen(self):
        from alphalens.backtest.engine import Trade

        t = Trade(
            date=pd.Timestamp("2024-04-01"),
            ticker="AAA",
            notional=5_000.0,
            direction="enter",
            price=100.0,
        )
        self.assertEqual(t.ticker, "AAA")
        self.assertEqual(t.direction, "enter")
        self.assertAlmostEqual(t.notional, 5_000.0)

        # Frozen — assignment should fail.
        with self.assertRaises(Exception):
            t.ticker = "BBB"  # type: ignore[misc]


class TestEnginePortfolioValue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_portfolio_value_is_100k(self):
        from alphalens.backtest.engine import BacktestEngine
        from alphalens.backtest.history_store import HistoryStore

        store = HistoryStore({})
        engine = BacktestEngine(
            store, scorer=lambda h, c: pd.DataFrame(columns=["ticker", "score"]),
            scorer_config={},
        )
        self.assertAlmostEqual(engine.portfolio_value, 100_000.0)

    def test_portfolio_value_propagates_to_report(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(120, 400.0, drift=0.0005),
                "AAA": _long_history(120, 100.0, drift=0.001),
                "BBB": _long_history(120, 100.0, drift=-0.0005),
            },
        )

        def fake_scorer(histories, config):
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "BBB", "score": 0.5},
                ]
            )

        engine = BacktestEngine(
            store,
            scorer=fake_scorer,
            scorer_config={},
            holding_period=2,
            top_n=2,
            benchmark="SPY",
            screener_tickers=["AAA", "BBB"],
            portfolio_value=250_000.0,
        )
        engine.MIN_BARS_REQUIRED = 25

        report = engine.run(start=date(2024, 3, 1), end=date(2024, 3, 15))
        self.assertAlmostEqual(report.portfolio_value, 250_000.0)


class TestTradesPopulation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_day_all_enters(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(60, 400.0),
                "AAA": _long_history(60, 100.0),
                "BBB": _long_history(60, 100.0),
            },
        )

        def fake_scorer(histories, config):
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "BBB", "score": 0.5},
                ]
            )

        engine = BacktestEngine(
            store, scorer=fake_scorer, scorer_config={},
            holding_period=2, top_n=2, benchmark="SPY",
            screener_tickers=["AAA", "BBB"],
            portfolio_value=100_000.0,
        )
        engine.MIN_BARS_REQUIRED = 25

        report = engine.run(start=date(2024, 2, 20), end=date(2024, 3, 15))
        first = report.daily_results[0]
        # On first day both picks are new positions → 2 enter trades.
        self.assertIsNotNone(first.trades)
        self.assertEqual(len(first.trades), 2)
        self.assertTrue(all(t.direction == "enter" for t in first.trades))

    def test_top_n_unchanged_no_trades(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(60, 400.0),
                "AAA": _long_history(60, 100.0),
                "BBB": _long_history(60, 100.0),
            },
        )

        def fake_scorer(histories, config):
            # Deterministic — same picks every day.
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "BBB", "score": 0.5},
                ]
            )

        engine = BacktestEngine(
            store, scorer=fake_scorer, scorer_config={},
            holding_period=2, top_n=2, benchmark="SPY",
            screener_tickers=["AAA", "BBB"],
            portfolio_value=100_000.0,
        )
        engine.MIN_BARS_REQUIRED = 25

        report = engine.run(start=date(2024, 2, 20), end=date(2024, 3, 15))
        # After first day, subsequent days have identical top-N AND weights
        # (equal weighting) → no trades.
        self.assertGreater(len(report.daily_results), 1)
        for snap in report.daily_results[1:]:
            self.assertEqual(snap.trades, [])

    def test_swap_produces_enter_and_exit(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(60, 400.0),
                "AAA": _long_history(60, 100.0),
                "BBB": _long_history(60, 100.0),
                "CCC": _long_history(60, 100.0),
            },
        )

        _state = {"day": 0}

        def fake_scorer(histories, config):
            _state["day"] += 1
            # Day 1-2: pick AAA & BBB. Day 3+: pick AAA & CCC (BBB exits, CCC enters).
            if _state["day"] <= 2:
                return pd.DataFrame(
                    [
                        {"ticker": "AAA", "score": 1.0},
                        {"ticker": "BBB", "score": 0.5},
                    ]
                )
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "CCC", "score": 0.5},
                ]
            )

        engine = BacktestEngine(
            store, scorer=fake_scorer, scorer_config={},
            holding_period=2, top_n=2, benchmark="SPY",
            screener_tickers=["AAA", "BBB", "CCC"],
            portfolio_value=100_000.0,
        )
        engine.MIN_BARS_REQUIRED = 25

        report = engine.run(start=date(2024, 2, 20), end=date(2024, 3, 15))
        # Day 3 should have 1 exit (BBB) + 1 enter (CCC).
        swap_day = report.daily_results[2]
        exits = [t for t in swap_day.trades if t.direction == "exit"]
        enters = [t for t in swap_day.trades if t.direction == "enter"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(len(enters), 1)
        self.assertEqual(exits[0].ticker, "BBB")
        self.assertEqual(enters[0].ticker, "CCC")

    def test_trade_notional_equals_weight_delta_times_portfolio_value(self):
        from alphalens.backtest.engine import BacktestEngine

        store = _prime_store(
            self.dir,
            {
                "SPY": _long_history(60, 400.0),
                "AAA": _long_history(60, 100.0),
                "BBB": _long_history(60, 100.0),
            },
        )

        def fake_scorer(histories, config):
            return pd.DataFrame(
                [
                    {"ticker": "AAA", "score": 1.0},
                    {"ticker": "BBB", "score": 0.5},
                ]
            )

        pv = 100_000.0
        engine = BacktestEngine(
            store, scorer=fake_scorer, scorer_config={},
            holding_period=2, top_n=2, benchmark="SPY",
            screener_tickers=["AAA", "BBB"],
            portfolio_value=pv,
        )
        engine.MIN_BARS_REQUIRED = 25

        report = engine.run(start=date(2024, 2, 20), end=date(2024, 3, 15))
        first = report.daily_results[0]
        # Equal weighting top-2 → each trade notional = 0.5 × pv = $50k.
        for trade in first.trades:
            self.assertAlmostEqual(trade.notional, 50_000.0, places=2)


if __name__ == "__main__":
    unittest.main()
