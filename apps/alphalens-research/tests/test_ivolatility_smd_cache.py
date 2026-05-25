"""Unit tests for the iVolatility smd raw-data cache layer.

The cache is the immutable raw-data tier underneath v7. Caching strategy:
- One parquet per ticker under `cache_dir/{TICKER}.parquet`
- Range-mode pull (single API call per ticker for full window)
- Resumable: existing parquets are skipped
- Pure passthrough: vendor columns preserved verbatim — feature joiner is the
  only consumer that interprets / filters / derives.

Tests use an injected fetcher callable so no live iVol API calls happen here.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import (
    download_and_cache,
    load_cached_smd,
)


def _make_smd_response(
    ticker: str = "AAPL", n_days: int = 5, exchanges: list[str] | None = None
) -> pd.DataFrame:
    """Synthetic smd response: subset of vendor columns + multi-exchange option."""
    n = n_days if exchanges is None else n_days * len(exchanges)
    rows = []
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    for d in dates:
        if exchanges is None:
            rows.append(
                {
                    "symbol": ticker,
                    "tradeDate": d.strftime("%Y-%m-%d"),
                    "exchange": "NASDAQ",
                    "ivx30": 0.25,
                    "ivp30": 50.0,
                    "ivx180": 0.27,
                    "hv20": 0.20,
                    "close": 180.0,
                    "open": 179.0,
                    "high": 181.0,
                    "low": 178.0,
                    "stockVolume": 50_000_000,
                }
            )
        else:
            for ex in exchanges:
                rows.append(
                    {
                        "symbol": ticker,
                        "tradeDate": d.strftime("%Y-%m-%d"),
                        "exchange": ex,
                        "ivx30": 0.25 if ex in {"NYSE", "NASDAQ"} else None,
                        "ivp30": 50.0 if ex in {"NYSE", "NASDAQ"} else None,
                        "close": 180.0 if ex in {"NYSE", "NASDAQ"} else None,
                    }
                )
    return pd.DataFrame(rows)


class TestDownloadAndCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_parquet_per_ticker(self):
        """Each ticker gets its own parquet file named {TICKER}.parquet."""
        fetcher = MagicMock(return_value=_make_smd_response("AAPL", n_days=10))
        n_new = download_and_cache(
            tickers=["AAPL"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        self.assertEqual(n_new, 1)
        self.assertTrue((self.cache_dir / "AAPL.parquet").exists())
        df = pd.read_parquet(self.cache_dir / "AAPL.parquet")
        self.assertEqual(len(df), 10)

    def test_fetcher_called_with_range_params(self):
        """Fetcher receives (ticker, start, end) — single range call per ticker."""
        fetcher = MagicMock(return_value=_make_smd_response("AAPL", n_days=2))
        download_and_cache(
            tickers=["AAPL"],
            start=date(2018, 4, 30),
            end=date(2026, 4, 30),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        fetcher.assert_called_once_with("AAPL", date(2018, 4, 30), date(2026, 4, 30))

    def test_skips_existing_parquet(self):
        """If parquet exists on disk, fetcher must not be called for that ticker."""
        # Pre-create cached file
        existing = _make_smd_response("AAPL", n_days=3)
        existing.to_parquet(self.cache_dir / "AAPL.parquet")

        fetcher = MagicMock()
        n_new = download_and_cache(
            tickers=["AAPL"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        self.assertEqual(n_new, 0)
        fetcher.assert_not_called()

    def test_empty_response_does_not_write_file(self):
        """If vendor returns empty DataFrame, no parquet is created (sentinel)."""
        fetcher = MagicMock(return_value=pd.DataFrame())
        n_new = download_and_cache(
            tickers=["DELISTED"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        self.assertEqual(n_new, 0)
        self.assertFalse((self.cache_dir / "DELISTED.parquet").exists())

    def test_none_response_does_not_write_file(self):
        """Vendor sometimes returns None (wrapper failure, 404, etc.)."""
        fetcher = MagicMock(return_value=None)
        n_new = download_and_cache(
            tickers=["UNKNOWN"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        self.assertEqual(n_new, 0)
        self.assertFalse((self.cache_dir / "UNKNOWN.parquet").exists())

    def test_fetcher_exception_is_logged_not_propagated(self):
        """Per-ticker fetch failures must not abort the whole batch."""
        fetcher = MagicMock(
            side_effect=[
                Exception("simulated 503"),
                _make_smd_response("MSFT", n_days=3),
            ]
        )
        n_new = download_and_cache(
            tickers=["AAPL", "MSFT"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        self.assertEqual(n_new, 1)
        self.assertFalse((self.cache_dir / "AAPL.parquet").exists())
        self.assertTrue((self.cache_dir / "MSFT.parquet").exists())

    def test_preserves_all_vendor_columns_verbatim(self):
        """Cache is pure passthrough — no column renaming, no row filtering.

        Multi-exchange filtering is the FEATURE JOINER's concern, not the cache.
        """
        df_in = _make_smd_response("CTT", n_days=2, exchanges=["NYSE", "TSX"])
        fetcher = MagicMock(return_value=df_in)
        download_and_cache(
            tickers=["CTT"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            cache_dir=self.cache_dir,
            fetcher=fetcher,
            sleep_between=0,
        )
        df_out = pd.read_parquet(self.cache_dir / "CTT.parquet")
        # Multi-row preserved (4 rows: 2 days × 2 exchanges)
        self.assertEqual(len(df_out), 4)
        self.assertEqual(set(df_out["exchange"]), {"NYSE", "TSX"})
        # Schema preserved
        self.assertEqual(set(df_in.columns), set(df_out.columns))


class TestLoadCachedSmd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_none_when_parquet_missing(self):
        self.assertIsNone(load_cached_smd("UNKNOWN", self.cache_dir))

    def test_returns_dataframe_when_parquet_present(self):
        df = _make_smd_response("AAPL", n_days=5)
        df.to_parquet(self.cache_dir / "AAPL.parquet")
        out = load_cached_smd("AAPL", self.cache_dir)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 5)
        self.assertIn("ivx30", out.columns)

    def test_ticker_lookup_is_case_insensitive(self):
        """`load_cached_smd("aapl", ...)` should find AAPL.parquet."""
        df = _make_smd_response("AAPL", n_days=2)
        df.to_parquet(self.cache_dir / "AAPL.parquet")
        out = load_cached_smd("aapl", self.cache_dir)
        self.assertIsNotNone(out)


class TestRobustFetcher(unittest.TestCase):
    """Vendor's gzipped-CSV path occasionally returns mismatched-field rows
    (~3-5% of tickers). Robust fetcher retries with on_bad_lines='warn'."""

    def test_passes_through_when_default_succeeds(self):
        from unittest.mock import patch

        from alphalens_pipeline.data.alt_data import ivolatility_smd_cache as mod

        df_in = _make_smd_response("AAPL", n_days=5)
        with patch.object(mod, "_default_smd_fetcher", return_value=df_in):
            df_out = mod._robust_smd_fetcher("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        self.assertIs(df_out, df_in)

    def test_retries_with_lenient_csv_on_parser_error(self):
        from unittest.mock import patch

        from alphalens_pipeline.data.alt_data import ivolatility_smd_cache as mod

        df_recovered = _make_smd_response("AMZN", n_days=3)

        # Default raises ParserError once; lenient retry returns recovered df
        side_effects = [
            pd.errors.ParserError("Expected 150 fields, saw 151"),
            df_recovered,
        ]
        with patch.object(mod, "_default_smd_fetcher", side_effect=side_effects):
            df_out = mod._robust_smd_fetcher("AMZN", date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(df_out), 3)


class TestDefaultFetcher(unittest.TestCase):
    """Smoke check that the default fetcher invokes the ivolatility wrapper.

    Patches `ivolatility.setMethod` so no real API call is made. The contract
    we're locking: default fetcher uses range-mode (`from_=start, to=end`) on
    the `/equities/stock-market-data` endpoint.
    """

    def test_default_fetcher_uses_smd_endpoint_with_range_params(self):
        from unittest.mock import patch

        from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import _default_smd_fetcher

        smd_query = MagicMock(return_value=_make_smd_response("AAPL", n_days=3))

        with patch(
            "alphalens_pipeline.data.alt_data.ivolatility_smd_cache._smd_query_fn",
            return_value=smd_query,
        ):
            df = _default_smd_fetcher("AAPL", date(2024, 1, 1), date(2024, 1, 31))

        smd_query.assert_called_once_with(symbols="AAPL", from_="2024-01-01", to="2024-01-31")
        self.assertEqual(len(df), 3)


if __name__ == "__main__":
    unittest.main()
