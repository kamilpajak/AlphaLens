import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd


def _mk_bar(d, price, vol=1000):
    from alphalens.lean_screener.lean_csv_writer import DailyBar

    return DailyBar(date=d, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=vol)


def _prime_dir(tmpdir: Path, tickers_to_bars: dict[str, list]) -> Path:
    from alphalens.lean_screener.lean_csv_writer import LeanCsvWriter

    writer = LeanCsvWriter(tmpdir)
    for t, bars in tickers_to_bars.items():
        writer.write_bars(t, bars)
    return tmpdir


class TestHistoryStoreLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_populates_cache(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 101)],
                "MSFT": [_mk_bar("20240419", 200), _mk_bar("20240422", 202)],
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL", "MSFT"])

        self.assertEqual(sorted(store.tickers()), ["AAPL", "MSFT"])
        self.assertEqual(len(store.full("AAPL")), 2)

    def test_unknown_ticker_ignored_silently(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        store = HistoryStore(self.dir)
        store.load(["NOPE"])
        self.assertNotIn("NOPE", store.tickers())

    def test_full_raises_on_unknown(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        store = HistoryStore(self.dir)
        with self.assertRaises(KeyError):
            store.full("AAPL")


class TestTruncateTo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_truncate_excludes_future_bars(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "AAPL": [
                    _mk_bar("20240419", 100),
                    _mk_bar("20240422", 101),
                    _mk_bar("20240423", 102),
                    _mk_bar("20240424", 103),
                ],
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        df = store.truncate_to("AAPL", date(2024, 4, 22))
        self.assertEqual(len(df), 2)
        self.assertEqual(df.index[-1].date(), date(2024, 4, 22))

    def test_truncate_inclusive_of_asof(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 101)],
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        df = store.truncate_to("AAPL", date(2024, 4, 22))
        self.assertEqual(len(df), 2)  # inclusive

    def test_truncate_unknown_ticker_returns_empty(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        store = HistoryStore(self.dir)
        df = store.truncate_to("NOPE", date(2024, 4, 22))
        self.assertTrue(df.empty)


class TestForwardReturn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_forward_return_basic(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        # close prices: 100, 110, 115, 120, 130 on consecutive days
        _prime_dir(
            self.dir,
            {
                "AAPL": [
                    _mk_bar("20240419", 100),
                    _mk_bar("20240422", 110),
                    _mk_bar("20240423", 115),
                    _mk_bar("20240424", 120),
                    _mk_bar("20240425", 130),
                ],
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        # Score on 20240419 (close=100). Enter next trading day (20240422, close=110).
        # Exit 3 days later (20240425, close=130). Return = 130/110 - 1 = 18.18%.
        fwd = store.forward_return("AAPL", date(2024, 4, 19), holding_period=3)
        self.assertAlmostEqual(fwd, 130 / 110 - 1, places=6)

    def test_forward_return_none_when_insufficient_history(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {"AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 110)]},
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        # Score on 20240419 → need 3 bars after, only have 1.
        self.assertIsNone(store.forward_return("AAPL", date(2024, 4, 19), holding_period=3))

    def test_forward_return_none_for_unknown_ticker(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        store = HistoryStore(self.dir)
        self.assertIsNone(store.forward_return("NOPE", date(2024, 4, 19), holding_period=1))

    def test_forward_return_strict_after_asof(self):
        """Entry is NEXT trading day after asof — never same day."""
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "AAPL": [
                    _mk_bar("20240419", 100),
                    _mk_bar("20240422", 110),
                    _mk_bar("20240423", 120),
                ],
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        # Score on 20240422 — entry must be 20240423, not 20240422
        # 1-bar forward return = close[04-23]/close[04-23] - 1 = 0 is wrong
        # Correct: entry at next bar index; since only one bar after 04-22, fwd(1) needs 2 bars after.
        self.assertIsNone(store.forward_return("AAPL", date(2024, 4, 22), holding_period=1))


class TestTradingDays(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_trading_days_filter_inclusive(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "SPY": [
                    _mk_bar("20240418", 500),
                    _mk_bar("20240419", 501),
                    _mk_bar("20240422", 502),
                    _mk_bar("20240423", 503),
                ]
            },
        )
        store = HistoryStore(self.dir)
        store.load(["SPY"])

        days = store.trading_days_between("SPY", date(2024, 4, 19), date(2024, 4, 22))
        self.assertEqual(len(days), 2)
        self.assertEqual(days[0].date(), date(2024, 4, 19))
        self.assertEqual(days[-1].date(), date(2024, 4, 22))

    def test_benchmark_calendar_delegates(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {"SPY": [_mk_bar("20240419", 500), _mk_bar("20240422", 501)]},
        )
        store = HistoryStore(self.dir)
        store.load(["SPY"])

        cal = HistoryStore.benchmark_calendar(store, "SPY", date(2024, 4, 19), date(2024, 4, 22))
        self.assertEqual(len(cal), 2)


class TestDataFrameShape(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returned_dataframe_has_ohlcv_columns(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(self.dir, {"AAPL": [_mk_bar("20240419", 100)]})
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        df = store.full("AAPL")
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertIsInstance(df.index, pd.DatetimeIndex)

    def test_data_sorted_ascending(self):
        from alphalens.lean_screener.backtest.history_store import HistoryStore

        _prime_dir(
            self.dir,
            {
                "AAPL": [
                    _mk_bar("20240422", 101),
                    _mk_bar("20240419", 100),
                    _mk_bar("20240423", 102),
                ]
            },
        )
        store = HistoryStore(self.dir)
        store.load(["AAPL"])

        df = store.full("AAPL")
        self.assertTrue(df.index.is_monotonic_increasing)


if __name__ == "__main__":
    unittest.main()
