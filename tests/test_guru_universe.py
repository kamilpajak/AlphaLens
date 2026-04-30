"""Tests for alphalens.archive.guru.universe — S&P 500 point-in-time membership loader."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml


def _write_snapshot(tmpdir: Path, year: int, tickers: list[str]) -> Path:
    path = tmpdir / f"{year}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "year": year,
                "as_of": f"{year}-01-01",
                "source": "wikipedia_test",
                "tickers": tickers,
            }
        )
    )
    return path


class TestLoadSp500Pit(unittest.TestCase):
    def test_loads_known_year_as_list_of_tickers(self):
        from alphalens.archive.guru.universe import load_sp500_pit

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tickers = [f"T{i:03d}" for i in range(500)]
            _write_snapshot(tmp_path, 2018, tickers)

            result = load_sp500_pit(year=2018, data_dir=tmp_path)

        self.assertEqual(len(result), 500)
        self.assertEqual(result[0], "T000")

    def test_rejects_unsupported_year(self):
        from alphalens.archive.guru.universe import UniverseError, load_sp500_pit

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(UniverseError):
            load_sp500_pit(year=1999, data_dir=Path(tmp))

    def test_loads_as_of_date_finds_nearest_past_snapshot(self):
        """asof=2018-06-15 → loads 2018 snapshot (not 2020)."""
        import pandas as pd

        from alphalens.archive.guru.universe import load_sp500_pit_for_date

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_snapshot(tmp_path, 2018, ["A", "B"])
            _write_snapshot(tmp_path, 2020, ["C", "D"])

            # Date between snapshots → use 2018 (most recent past)
            mid = load_sp500_pit_for_date(pd.Timestamp("2018-06-15"), data_dir=tmp_path)
            self.assertEqual(sorted(mid), ["A", "B"])

            # Date exactly matches 2020 snapshot
            exact = load_sp500_pit_for_date(pd.Timestamp("2020-01-01"), data_dir=tmp_path)
            self.assertEqual(sorted(exact), ["C", "D"])

            # Date before any snapshot → UniverseError
            from alphalens.archive.guru.universe import UniverseError

            with self.assertRaises(UniverseError):
                load_sp500_pit_for_date(pd.Timestamp("2017-01-01"), data_dir=tmp_path)


if __name__ == "__main__":
    unittest.main()
