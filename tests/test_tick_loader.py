"""TDD for TickLoader + TickStore (alphalens/tick_data/)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock


class TestTickStoreEmpty(unittest.TestCase):
    def test_returns_empty_frame_for_missing_parquet(self):
        from alphalens.tick_data.store import TickStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TickStore(Path(tmp))
            df = store.get_trades("AAPL", date(2024, 4, 1))

        self.assertTrue(df.empty)
        # Schema is preserved even on empty result.
        for col in ("sip_timestamp_ns", "price", "size", "exchange", "trf_id"):
            self.assertIn(col, df.columns)


class TestTickLoaderIdempotent(unittest.TestCase):
    def test_skips_fetch_when_parquet_exists(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader

        fake_client = MagicMock()
        fake_client.trades.return_value = [
            Trade(
                ticker="AAPL",
                sip_timestamp_ns=1,
                price=100.0,
                size=100,
                conditions=[12],
                exchange=4,
                trf_id=202,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            loader = TickLoader(client=fake_client, cache_dir=Path(tmp))
            # First call writes the parquet.
            loader.fetch_day("AAPL", date(2024, 4, 1))
            self.assertEqual(fake_client.trades.call_count, 1)

            # Second call with same arguments is a no-op.
            loader.fetch_day("AAPL", date(2024, 4, 1))
            self.assertEqual(fake_client.trades.call_count, 1)

    def test_force_refresh_re_fetches(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader

        fake_client = MagicMock()
        fake_client.trades.return_value = [
            Trade(
                ticker="AAPL",
                sip_timestamp_ns=1,
                price=100.0,
                size=100,
                conditions=[12],
                exchange=4,
                trf_id=202,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            loader = TickLoader(client=fake_client, cache_dir=Path(tmp))
            loader.fetch_day("AAPL", date(2024, 4, 1))
            loader.fetch_day("AAPL", date(2024, 4, 1), force=True)
            self.assertEqual(fake_client.trades.call_count, 2)


class TestTickStoreRoundtrip(unittest.TestCase):
    def test_read_after_write_preserves_fields(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader
        from alphalens.tick_data.store import TickStore

        trades = [
            Trade(
                ticker="AAPL",
                sip_timestamp_ns=1711972800000000000,
                price=100.5,
                size=200,
                conditions=[12, 37],
                exchange=4,
                trf_id=202,
            ),
            Trade(
                ticker="AAPL",
                sip_timestamp_ns=1711972801000000000,
                price=100.6,
                size=150,
                conditions=[12],
                exchange=11,
                trf_id=None,
            ),
        ]

        fake_client = MagicMock()
        fake_client.trades.return_value = trades

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loader = TickLoader(client=fake_client, cache_dir=cache_dir)
            loader.fetch_day("AAPL", date(2024, 4, 1))

            store = TickStore(cache_dir)
            df = store.get_trades("AAPL", date(2024, 4, 1))

        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.iloc[0]["price"], 100.5)
        self.assertEqual(int(df.iloc[0]["size"]), 200)
        self.assertEqual(int(df.iloc[0]["exchange"]), 4)
        self.assertEqual(int(df.iloc[0]["trf_id"]), 202)
        # Row 2 has no TRF — should be missing (pandas NA-friendly).
        import pandas as pd

        self.assertTrue(pd.isna(df.iloc[1]["trf_id"]))


class TestTickStoreDarkPoolFilter(unittest.TestCase):
    def test_dark_pool_trades_excludes_lit_prints(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader
        from alphalens.tick_data.store import TickStore

        trades = [
            Trade(ticker="AAPL", sip_timestamp_ns=1, price=100.0, size=10,
                  conditions=[12], exchange=4, trf_id=202),
            Trade(ticker="AAPL", sip_timestamp_ns=2, price=100.0, size=20,
                  conditions=[12], exchange=11, trf_id=None),
            Trade(ticker="AAPL", sip_timestamp_ns=3, price=100.0, size=30,
                  conditions=[12], exchange=4, trf_id=201),
        ]

        fake_client = MagicMock()
        fake_client.trades.return_value = trades

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loader = TickLoader(client=fake_client, cache_dir=cache_dir)
            loader.fetch_day("AAPL", date(2024, 4, 1))

            store = TickStore(cache_dir)
            dark_only = store.dark_pool_trades("AAPL", date(2024, 4, 1))

        # Two TRF prints (trf_id 202 + 201); one lit (trf_id None).
        self.assertEqual(len(dark_only), 2)


class TestTickLoaderRange(unittest.TestCase):
    def test_fetch_range_iterates_weekdays(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader

        fake_client = MagicMock()
        fake_client.trades.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            loader = TickLoader(client=fake_client, cache_dir=Path(tmp))
            # 2024-04-01 (Mon) .. 2024-04-05 (Fri) = 5 weekdays
            paths = loader.fetch_range("AAPL", date(2024, 4, 1), date(2024, 4, 5))

        self.assertEqual(len(paths), 5)
        self.assertEqual(fake_client.trades.call_count, 5)

    def test_fetch_range_skips_weekends(self):
        from alphalens.screeners.lean.polygon_client import Trade
        from alphalens.tick_data.loader import TickLoader

        fake_client = MagicMock()
        fake_client.trades.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            loader = TickLoader(client=fake_client, cache_dir=Path(tmp))
            # 2024-04-05 (Fri) .. 2024-04-08 (Mon) — Sat/Sun excluded.
            paths = loader.fetch_range("AAPL", date(2024, 4, 5), date(2024, 4, 8))

        self.assertEqual(len(paths), 2)  # Fri + Mon
        self.assertEqual(fake_client.trades.call_count, 2)


if __name__ == "__main__":
    unittest.main()
