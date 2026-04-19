import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock


def _bar(ticker, close, volume=1000):
    from alphalens.lean_screener.polygon_client import GroupedBar

    return GroupedBar(
        ticker=ticker,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        timestamp_ms=1_713_484_800_000,  # 2026-04-17 in UTC
    )


class TestSyncDate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_sync(self, client, universe):
        from alphalens.lean_screener.data_sync import PolygonLeanSync
        from alphalens.lean_screener.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        state_path = self.dir / "sync_state.json"
        return PolygonLeanSync(client=client, writer=writer, universe=universe, state_path=state_path), writer

    def test_filters_to_universe(self):
        client = MagicMock()
        client.grouped_daily.return_value = [
            _bar("AAPL", 100.0),
            _bar("OUTSIDER", 50.0),
            _bar("MSFT", 200.0),
        ]
        sync, writer = self._build_sync(client, ["AAPL", "MSFT"])

        t, b = sync.sync_date(date(2026, 4, 17))

        self.assertEqual(t, 2)
        self.assertEqual(b, 2)
        self.assertEqual(len(writer.read_bars("AAPL")), 1)
        self.assertEqual(len(writer.read_bars("MSFT")), 1)
        self.assertEqual(writer.read_bars("OUTSIDER"), [])

    def test_uppercases_universe_for_filtering(self):
        client = MagicMock()
        client.grouped_daily.return_value = [_bar("AAPL", 100.0)]
        sync, writer = self._build_sync(client, ["aapl"])  # lowercase input

        sync.sync_date(date(2026, 4, 17))

        self.assertEqual(len(writer.read_bars("AAPL")), 1)

    def test_empty_day_writes_nothing(self):
        client = MagicMock()
        client.grouped_daily.return_value = []
        sync, _ = self._build_sync(client, ["AAPL"])

        t, b = sync.sync_date(date(2026, 4, 5))  # weekend

        self.assertEqual((t, b), (0, 0))


class TestSyncRange(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_sync(self, client, universe):
        from alphalens.lean_screener.data_sync import PolygonLeanSync
        from alphalens.lean_screener.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        state_path = self.dir / "sync_state.json"
        return PolygonLeanSync(client=client, writer=writer, universe=universe, state_path=state_path), writer, state_path

    def test_skips_weekends(self):
        client = MagicMock()
        client.grouped_daily.return_value = [_bar("AAPL", 100.0)]
        sync, _, _ = self._build_sync(client, ["AAPL"])

        # 2026-04-17 Fri, 2026-04-18 Sat, 2026-04-19 Sun, 2026-04-20 Mon
        sync.sync_range(date(2026, 4, 17), date(2026, 4, 20))

        called_dates = [c.args[0] for c in client.grouped_daily.call_args_list]
        self.assertIn("2026-04-17", called_dates)
        self.assertIn("2026-04-20", called_dates)
        self.assertNotIn("2026-04-18", called_dates)
        self.assertNotIn("2026-04-19", called_dates)

    def test_rejects_inverted_range(self):
        client = MagicMock()
        sync, _, _ = self._build_sync(client, ["AAPL"])

        with self.assertRaises(ValueError):
            sync.sync_range(date(2026, 4, 20), date(2026, 4, 17))

    def test_writes_state_file_on_success(self):
        client = MagicMock()
        client.grouped_daily.return_value = [_bar("AAPL", 100.0)]
        sync, _, state_path = self._build_sync(client, ["AAPL"])

        sync.sync_range(date(2026, 4, 17), date(2026, 4, 17))

        self.assertTrue(state_path.exists())
        self.assertEqual(sync.load_last_synced(), date(2026, 4, 17))

    def test_skips_state_write_when_nothing_synced(self):
        client = MagicMock()
        client.grouped_daily.return_value = []  # market closed every day
        sync, _, state_path = self._build_sync(client, ["AAPL"])

        sync.sync_range(date(2026, 4, 18), date(2026, 4, 19))  # Sat+Sun

        self.assertFalse(state_path.exists())


class TestIncrementalSync(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build_sync(self, client, universe):
        from alphalens.lean_screener.data_sync import PolygonLeanSync
        from alphalens.lean_screener.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        state_path = self.dir / "sync_state.json"
        return PolygonLeanSync(client=client, writer=writer, universe=universe, state_path=state_path), state_path

    def test_bootstrap_when_no_state(self):
        client = MagicMock()
        client.grouped_daily.return_value = []
        sync, _ = self._build_sync(client, ["AAPL"])

        sync.incremental_sync(today=date(2026, 4, 17), bootstrap_days=3)

        # 2026-04-14..2026-04-16: Tue/Wed/Thu → 3 weekday calls
        self.assertEqual(client.grouped_daily.call_count, 3)

    def test_resumes_from_last_synced(self):
        client = MagicMock()
        client.grouped_daily.return_value = []
        sync, state_path = self._build_sync(client, ["AAPL"])

        state_path.write_text('{"last_synced":"2026-04-15"}')  # Wed

        sync.incremental_sync(today=date(2026, 4, 17), bootstrap_days=500)

        # Should only fetch 2026-04-16 (Thu) — yesterday.
        called = [c.args[0] for c in client.grouped_daily.call_args_list]
        self.assertEqual(called, ["2026-04-16"])

    def test_noop_when_already_up_to_date(self):
        client = MagicMock()
        sync, state_path = self._build_sync(client, ["AAPL"])

        state_path.write_text('{"last_synced":"2026-04-16"}')

        report = sync.incremental_sync(today=date(2026, 4, 17), bootstrap_days=500)

        client.grouped_daily.assert_not_called()
        self.assertEqual(report.dates_synced, [])


if __name__ == "__main__":
    unittest.main()
