import tempfile
import unittest
import zipfile
from pathlib import Path


def _mk_bar(date, o, h, l, c, v):
    from alphalens.screeners.lean.lean_csv_writer import DailyBar

    return DailyBar(date=date, open=o, high=h, low=l, close=c, volume=v)


class TestFormatAndParse(unittest.TestCase):
    def test_round_trip_preserves_bar(self):
        from alphalens.screeners.lean.lean_csv_writer import format_bar, parse_bar

        original = _mk_bar("20260417", 100.5, 102.25, 99.75, 101.1, 12345)
        line = format_bar(original)
        round_tripped = parse_bar(line)

        self.assertEqual(round_tripped.date, "20260417")
        self.assertAlmostEqual(round_tripped.open, 100.5, places=4)
        self.assertAlmostEqual(round_tripped.high, 102.25, places=4)
        self.assertAlmostEqual(round_tripped.low, 99.75, places=4)
        self.assertAlmostEqual(round_tripped.close, 101.1, places=4)
        self.assertEqual(round_tripped.volume, 12345)

    def test_format_uses_lean_scaled_prices(self):
        from alphalens.screeners.lean.lean_csv_writer import format_bar

        line = format_bar(_mk_bar("20260417", 100.5, 102.0, 99.0, 101.0, 500))

        # Lean stores prices as int * 10000.
        self.assertEqual(line, "20260417 00:00,1005000,1020000,990000,1010000,500")

    def test_format_includes_hour_marker(self):
        from alphalens.screeners.lean.lean_csv_writer import format_bar

        line = format_bar(_mk_bar("20260417", 1.0, 1.0, 1.0, 1.0, 1))
        self.assertTrue(line.startswith("20260417 00:00,"))

    def test_parse_rejects_malformed(self):
        from alphalens.screeners.lean.lean_csv_writer import parse_bar

        with self.assertRaises(ValueError):
            parse_bar("not,enough,fields")


class TestLeanCsvWriterPaths(unittest.TestCase):
    def test_path_for_lowercases_ticker_and_uses_daily_subtree(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(Path("/tmp/fake"))
        self.assertEqual(
            writer.path_for("AAPL"),
            Path("/tmp/fake/equity/usa/daily/aapl.zip"),
        )


class TestWriteRead(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trip(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        bars = [
            _mk_bar("20260415", 100.0, 101.0, 99.0, 100.5, 1000),
            _mk_bar("20260416", 100.5, 102.0, 100.0, 101.5, 1100),
        ]
        writer.write_bars("AAPL", bars)

        round_tripped = writer.read_bars("AAPL")
        self.assertEqual(len(round_tripped), 2)
        self.assertEqual(round_tripped[0].date, "20260415")
        self.assertEqual(round_tripped[1].date, "20260416")

    def test_write_sorts_bars_by_date(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        writer.write_bars(
            "AAPL",
            [
                _mk_bar("20260416", 1, 1, 1, 1, 1),
                _mk_bar("20260414", 1, 1, 1, 1, 1),
                _mk_bar("20260415", 1, 1, 1, 1, 1),
            ],
        )

        bars = writer.read_bars("AAPL")
        self.assertEqual([b.date for b in bars], ["20260414", "20260415", "20260416"])

    def test_write_refuses_empty(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        with self.assertRaises(ValueError):
            writer.write_bars("AAPL", [])

    def test_zip_contains_correctly_named_csv(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        writer.write_bars("MSFT", [_mk_bar("20260415", 1, 1, 1, 1, 1)])

        target = writer.path_for("MSFT")
        with zipfile.ZipFile(target, "r") as zf:
            names = zf.namelist()
        self.assertEqual(names, ["msft.csv"])

    def test_read_missing_returns_empty(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        self.assertEqual(writer.read_bars("NOPE"), [])

    def test_atomic_write_leaves_no_tmp_file(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        writer.write_bars("AAPL", [_mk_bar("20260415", 1, 1, 1, 1, 1)])

        target = writer.path_for("AAPL")
        tmp = target.with_suffix(target.suffix + ".tmp")
        self.assertTrue(target.exists())
        self.assertFalse(tmp.exists())


class TestUpsert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_upsert_into_empty_writes_all(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        count = writer.upsert_bars(
            "AAPL",
            [
                _mk_bar("20260415", 1, 1, 1, 1, 1),
                _mk_bar("20260416", 1, 1, 1, 1, 1),
            ],
        )
        self.assertEqual(count, 2)
        self.assertEqual(len(writer.read_bars("AAPL")), 2)

    def test_upsert_dedups_by_date_new_wins(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        writer.write_bars(
            "AAPL",
            [_mk_bar("20260415", 100.0, 100.0, 100.0, 100.0, 100)],
        )
        writer.upsert_bars(
            "AAPL",
            [_mk_bar("20260415", 200.0, 200.0, 200.0, 200.0, 999)],
        )

        bars = writer.read_bars("AAPL")
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].close, 200.0, places=4)
        self.assertEqual(bars[0].volume, 999)

    def test_upsert_merges_with_existing(self):
        from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

        writer = LeanCsvWriter(self.dir)
        writer.write_bars(
            "AAPL",
            [
                _mk_bar("20260414", 1, 1, 1, 1, 1),
                _mk_bar("20260415", 1, 1, 1, 1, 1),
            ],
        )
        writer.upsert_bars("AAPL", [_mk_bar("20260416", 1, 1, 1, 1, 1)])

        bars = writer.read_bars("AAPL")
        self.assertEqual([b.date for b in bars], ["20260414", "20260415", "20260416"])


if __name__ == "__main__":
    unittest.main()
