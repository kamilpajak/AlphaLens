import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd


def _mk_bar(d, price, vol=1000):
    from alphalens.archive.screeners.lean.lean_csv_writer import DailyBar

    return DailyBar(
        date=d, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=vol
    )


def _prime_dir(tmpdir: Path, tickers_to_bars: dict[str, list]) -> Path:
    from alphalens.archive.screeners.lean.lean_csv_writer import LeanCsvWriter

    writer = LeanCsvWriter(tmpdir)
    for t, bars in tickers_to_bars.items():
        writer.write_bars(t, bars)
    return tmpdir


def _store_with(tmpdir: Path, bars: dict[str, list]):
    """Build HistoryStore by priming a dir with zips and loading via lean CSV loader."""
    from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.data.store.history import HistoryStore

    _prime_dir(tmpdir, bars)
    histories = load_lean_histories(tmpdir, list(bars.keys()))
    return HistoryStore(histories)


class TestHistoryStoreConstruction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_construct_from_dict_exposes_tickers(self):
        store = _store_with(
            self.dir,
            {
                "AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 101)],
                "MSFT": [_mk_bar("20240419", 200), _mk_bar("20240422", 202)],
            },
        )
        self.assertEqual(sorted(store.tickers()), ["AAPL", "MSFT"])
        self.assertEqual(len(store.full("AAPL")), 2)

    def test_empty_dict_has_no_tickers(self):
        from alphalens.data.store.history import HistoryStore

        store = HistoryStore({})
        self.assertEqual(store.tickers(), [])

    def test_full_raises_on_unknown(self):
        from alphalens.data.store.history import HistoryStore

        store = HistoryStore({})
        with self.assertRaises(KeyError):
            store.full("AAPL")


class TestTruncateTo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_truncate_excludes_future_bars(self):
        store = _store_with(
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
        df = store.truncate_to("AAPL", date(2024, 4, 22))
        self.assertEqual(len(df), 2)
        self.assertEqual(df.index[-1].date(), date(2024, 4, 22))

    def test_truncate_inclusive_of_asof(self):
        store = _store_with(
            self.dir,
            {"AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 101)]},
        )
        df = store.truncate_to("AAPL", date(2024, 4, 22))
        self.assertEqual(len(df), 2)

    def test_truncate_unknown_ticker_returns_empty(self):
        from alphalens.data.store.history import HistoryStore

        store = HistoryStore({})
        df = store.truncate_to("NOPE", date(2024, 4, 22))
        self.assertTrue(df.empty)


class TestForwardReturn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_forward_return_basic(self):
        store = _store_with(
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
        fwd = store.forward_return("AAPL", date(2024, 4, 19), holding_period=3)
        self.assertAlmostEqual(fwd, 130 / 110 - 1, places=6)

    def test_forward_return_none_when_insufficient_history(self):
        store = _store_with(
            self.dir,
            {"AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 110)]},
        )
        self.assertIsNone(store.forward_return("AAPL", date(2024, 4, 19), holding_period=3))

    def test_forward_return_none_for_unknown_ticker(self):
        from alphalens.data.store.history import HistoryStore

        store = HistoryStore({})
        self.assertIsNone(store.forward_return("NOPE", date(2024, 4, 19), holding_period=1))

    def test_forward_return_strict_after_asof(self):
        store = _store_with(
            self.dir,
            {
                "AAPL": [
                    _mk_bar("20240419", 100),
                    _mk_bar("20240422", 110),
                    _mk_bar("20240423", 120),
                ],
            },
        )
        self.assertIsNone(store.forward_return("AAPL", date(2024, 4, 22), holding_period=1))


class TestTradingDays(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_trading_days_filter_inclusive(self):
        store = _store_with(
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
        days = store.trading_days_between("SPY", date(2024, 4, 19), date(2024, 4, 22))
        self.assertEqual(len(days), 2)
        self.assertEqual(days[0].date(), date(2024, 4, 19))
        self.assertEqual(days[-1].date(), date(2024, 4, 22))

    def test_benchmark_calendar_delegates(self):
        from alphalens.data.store.history import HistoryStore

        store = _store_with(
            self.dir,
            {"SPY": [_mk_bar("20240419", 500), _mk_bar("20240422", 501)]},
        )
        cal = HistoryStore.benchmark_calendar(store, "SPY", date(2024, 4, 19), date(2024, 4, 22))
        self.assertEqual(len(cal), 2)


class TestDataFrameShape(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returned_dataframe_has_ohlcv_columns(self):
        store = _store_with(self.dir, {"AAPL": [_mk_bar("20240419", 100)]})
        df = store.full("AAPL")
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertIsInstance(df.index, pd.DatetimeIndex)


if __name__ == "__main__":
    unittest.main()
