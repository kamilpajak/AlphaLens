import tempfile
import unittest
from pathlib import Path

import yaml


class TestLoadIwmCurrent(unittest.TestCase):
    def _write(self, content: dict) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        tmp.write(yaml.safe_dump(content))
        tmp.close()
        return Path(tmp.name)

    def test_loads_list_of_tickers(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        path = self._write({"tickers": ["AAPL", "MSFT", "NVDA"]})

        self.assertEqual(load_iwm_current(path), ["AAPL", "MSFT", "NVDA"])

    def test_empty_tickers_returns_empty_list(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        path = self._write({"tickers": []})

        self.assertEqual(load_iwm_current(path), [])

    def test_missing_file_raises(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        with self.assertRaises(FileNotFoundError):
            load_iwm_current(Path("/nonexistent/iwm.yaml"))

    def test_missing_tickers_key_raises(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        path = self._write({"other_key": ["AAPL"]})

        with self.assertRaises(KeyError):
            load_iwm_current(path)

    def test_dedups_preserving_first_occurrence(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        path = self._write({"tickers": ["AAPL", "MSFT", "AAPL", "NVDA", "MSFT"]})

        self.assertEqual(load_iwm_current(path), ["AAPL", "MSFT", "NVDA"])

    def test_uppercases_tickers(self):
        from alphalens.data.alt_data.russell_universe import load_iwm_current

        path = self._write({"tickers": ["aapl", "MsFt"]})

        self.assertEqual(load_iwm_current(path), ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
