import tempfile
import unittest
from pathlib import Path


def _mk_bar(d, price, vol=1000):
    from alphalens.screeners.lean.lean_csv_writer import DailyBar

    return DailyBar(date=d, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=vol)


def _prime_dir(tmpdir: Path, tickers_to_bars: dict[str, list]) -> Path:
    from alphalens.screeners.lean.lean_csv_writer import LeanCsvWriter

    writer = LeanCsvWriter(tmpdir)
    for t, bars in tickers_to_bars.items():
        writer.write_bars(t, bars)
    return tmpdir


class TestLoadLeanHistories(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_dict_of_dataframes(self):
        from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

        _prime_dir(
            self.dir,
            {
                "AAPL": [_mk_bar("20240419", 100), _mk_bar("20240422", 101)],
                "MSFT": [_mk_bar("20240419", 200), _mk_bar("20240422", 202)],
            },
        )
        histories = load_lean_histories(self.dir, ["AAPL", "MSFT"])
        self.assertEqual(sorted(histories.keys()), ["AAPL", "MSFT"])
        self.assertEqual(len(histories["AAPL"]), 2)
        self.assertEqual(list(histories["AAPL"].columns), ["open", "high", "low", "close", "volume"])

    def test_unknown_ticker_skipped(self):
        from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

        histories = load_lean_histories(self.dir, ["NOPE"])
        self.assertEqual(histories, {})

    def test_dedup_tickers(self):
        from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

        _prime_dir(self.dir, {"AAPL": [_mk_bar("20240419", 100)]})
        histories = load_lean_histories(self.dir, ["AAPL", "aapl", "AAPL"])
        self.assertEqual(list(histories.keys()), ["AAPL"])

    def test_data_sorted_ascending(self):
        from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

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
        histories = load_lean_histories(self.dir, ["AAPL"])
        df = histories["AAPL"]
        self.assertTrue(df.index.is_monotonic_increasing)


if __name__ == "__main__":
    unittest.main()
