import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd


def _synthetic_ohlcv(n_bars: int = 50) -> pd.DataFrame:
    idx = pd.date_range("2024-01-03", periods=n_bars, freq="B")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000_000,
        },
        index=idx,
    )


class TestDownloadAndCache(unittest.TestCase):
    def test_writes_parquet_per_ticker(self):
        from alphalens.alt_data.yfinance_cache import download_and_cache

        fetcher = MagicMock(side_effect=lambda t, s, e: _synthetic_ohlcv())

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)

            count = download_and_cache(
                tickers=["AAPL", "MSFT"],
                start=date(2024, 1, 1),
                end=date(2024, 3, 31),
                cache_dir=cache,
                fetcher=fetcher,
                sleep_between=0,
            )

            self.assertEqual(count, 2)
            self.assertTrue((cache / "AAPL.parquet").exists())
            self.assertTrue((cache / "MSFT.parquet").exists())

    def test_skips_already_cached(self):
        from alphalens.alt_data.yfinance_cache import download_and_cache

        fetcher = MagicMock(side_effect=lambda t, s, e: _synthetic_ohlcv())

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)

            download_and_cache(
                tickers=["AAPL"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )
            # Second call should not re-fetch.
            new = download_and_cache(
                tickers=["AAPL", "MSFT"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )

        self.assertEqual(new, 1)  # only MSFT newly cached
        self.assertEqual(fetcher.call_count, 2)  # AAPL fetched once, MSFT once

    def test_fetch_error_ticker_skipped(self):
        from alphalens.alt_data.yfinance_cache import download_and_cache

        def fetcher(t, s, e):
            if t == "BAD":
                raise RuntimeError("delisted or network error")
            return _synthetic_ohlcv()

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)

            count = download_and_cache(
                tickers=["GOOD", "BAD"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )

            self.assertEqual(count, 1)
            self.assertTrue((cache / "GOOD.parquet").exists())
            self.assertFalse((cache / "BAD.parquet").exists())

    def test_empty_dataframe_skipped(self):
        """yfinance returns an empty DataFrame for delisted tickers."""
        from alphalens.alt_data.yfinance_cache import download_and_cache

        fetcher = MagicMock(side_effect=lambda t, s, e: pd.DataFrame())

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)

            count = download_and_cache(
                tickers=["DEAD"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )

        self.assertEqual(count, 0)
        self.assertFalse((cache / "DEAD.parquet").exists())


class TestLoadCachedHistories(unittest.TestCase):
    def test_loads_cached_parquets(self):
        from alphalens.alt_data.yfinance_cache import download_and_cache, load_cached_histories

        fetcher = MagicMock(side_effect=lambda t, s, e: _synthetic_ohlcv())

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            download_and_cache(
                tickers=["AAPL", "MSFT"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )

            histories = load_cached_histories(["AAPL", "MSFT"], cache)

        self.assertEqual(set(histories), {"AAPL", "MSFT"})
        self.assertIn("close", histories["AAPL"].columns)
        self.assertEqual(len(histories["AAPL"]), 50)

    def test_missing_ticker_silently_skipped(self):
        from alphalens.alt_data.yfinance_cache import load_cached_histories

        with tempfile.TemporaryDirectory() as td:
            histories = load_cached_histories(["NOPE"], Path(td))

        self.assertEqual(histories, {})

    def test_round_trip_preserves_index(self):
        from alphalens.alt_data.yfinance_cache import download_and_cache, load_cached_histories

        fetcher = MagicMock(side_effect=lambda t, s, e: _synthetic_ohlcv())

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            download_and_cache(
                tickers=["AAPL"],
                start=date(2024, 1, 1), end=date(2024, 3, 31),
                cache_dir=cache, fetcher=fetcher, sleep_between=0,
            )
            histories = load_cached_histories(["AAPL"], cache)

        self.assertIsInstance(histories["AAPL"].index, pd.DatetimeIndex)


class TestDefaultFetcher(unittest.TestCase):
    def test_default_fetcher_normalizes_columns_to_lowercase(self):
        """yfinance returns capitalized OHLCV; fetcher must lowercase them
        so HistoryStore contract is satisfied."""
        from alphalens.alt_data.yfinance_cache import _normalize_ohlcv

        raw = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Volume": [1000],
            },
            index=pd.DatetimeIndex(["2024-01-03"]),
        )

        normalized = _normalize_ohlcv(raw)

        self.assertEqual(
            set(normalized.columns),
            {"open", "high", "low", "close", "volume"},
        )

    def test_normalize_drops_extra_columns(self):
        from alphalens.alt_data.yfinance_cache import _normalize_ohlcv

        raw = pd.DataFrame(
            {
                "Open": [100.0], "High": [101.0], "Low": [99.0],
                "Close": [100.5], "Volume": [1000],
                "Dividends": [0.0], "Stock Splits": [0.0],
            },
            index=pd.DatetimeIndex(["2024-01-03"]),
        )

        normalized = _normalize_ohlcv(raw)

        self.assertEqual(
            list(normalized.columns),
            ["open", "high", "low", "close", "volume"],
        )

    def test_normalize_empty_raises(self):
        """Missing required columns must fail loud, not silently drop."""
        from alphalens.alt_data.yfinance_cache import _normalize_ohlcv

        raw = pd.DataFrame({"Close": [100.0]}, index=pd.DatetimeIndex(["2024-01-03"]))

        with self.assertRaises(KeyError):
            _normalize_ohlcv(raw)

    def test_normalize_strips_timezone_from_index(self):
        """yfinance returns NY-localized DatetimeIndex; strip so downstream
        naive-Timestamp comparisons (close_as_of) don't TypeError."""
        from alphalens.alt_data.yfinance_cache import _normalize_ohlcv

        raw = pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0],
             "Close": [100.5], "Volume": [1000]},
            index=pd.DatetimeIndex(["2024-01-03"], tz="America/New_York"),
        )

        normalized = _normalize_ohlcv(raw)

        self.assertIsNone(normalized.index.tz)


if __name__ == "__main__":
    unittest.main()
