import tempfile
import unittest
from pathlib import Path

import yaml


class TestTickerCikMap(unittest.TestCase):
    def _write(self, content: dict) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        tmp.write(yaml.safe_dump(content))
        tmp.close()
        return Path(tmp.name)

    def test_lookup_returns_zero_padded_10_digit_cik(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": 320193, "MSFT": 789019})
        m = TickerCikMap.load(path)

        self.assertEqual(m.lookup("AAPL"), "0000320193")
        self.assertEqual(m.lookup("MSFT"), "0000789019")

    def test_unknown_ticker_returns_none(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": 320193})
        m = TickerCikMap.load(path)

        self.assertIsNone(m.lookup("NOPE"))

    def test_case_insensitive_lookup(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": 320193})
        m = TickerCikMap.load(path)

        self.assertEqual(m.lookup("aapl"), "0000320193")
        self.assertEqual(m.lookup("Aapl"), "0000320193")

    def test_accepts_string_cik(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": "320193"})
        m = TickerCikMap.load(path)

        self.assertEqual(m.lookup("AAPL"), "0000320193")

    def test_rejects_non_numeric_cik(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": "abc123"})

        with self.assertRaises(ValueError):
            TickerCikMap.load(path)

    def test_rejects_cik_longer_than_10_digits(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({"AAPL": 12345678901})

        with self.assertRaises(ValueError):
            TickerCikMap.load(path)

    def test_missing_file_raises(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        with self.assertRaises(FileNotFoundError):
            TickerCikMap.load(Path("/nonexistent/map.yaml"))

    def test_empty_map_lookup_returns_none(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        path = self._write({})
        m = TickerCikMap.load(path)

        self.assertIsNone(m.lookup("AAPL"))


if __name__ == "__main__":
    unittest.main()
