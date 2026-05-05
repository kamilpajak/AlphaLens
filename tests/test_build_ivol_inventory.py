"""Tests for ``scripts/build_ivol_inventory.py``."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ivol_inventory as bii


def _write_smd_parquet(
    path: Path,
    *,
    start: str,
    end: str,
    has_ivp30: bool = True,
    n_ivp30_nan: int = 0,
) -> int:
    """Write a minimal vendor-shape parquet with ``tradeDate`` + ``ivp30``.

    Returns the number of rows written. Used by tests to seed a fake cache."""
    dates = pd.date_range(start=start, end=end, freq="D")
    n = len(dates)
    df = pd.DataFrame({"tradeDate": dates.strftime("%Y-%m-%d")})
    if has_ivp30:
        ivp30 = list(range(n))
        for i in range(min(n_ivp30_nan, n)):
            ivp30[i] = float("nan")
        df["ivp30"] = ivp30
    df.to_parquet(path)
    return n


class ScanParquetTests(unittest.TestCase):
    def test_returns_summary_row_for_valid_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AAPL.parquet"
            _write_smd_parquet(path, start="2010-01-01", end="2010-01-10")

            row = bii._scan_parquet(str(path))

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["ticker"], "AAPL")
            self.assertEqual(row["first_date"], date(2010, 1, 1))
            self.assertEqual(row["last_date"], date(2010, 1, 10))
            self.assertEqual(row["n_rows"], 10)
            self.assertEqual(row["ivp30_rows"], 10)
            self.assertEqual(row["pre_2018_rows"], 10)
            self.assertEqual(row["pre_2008_rows"], 0)

    def test_returns_none_for_empty_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EMPTY.parquet"
            pd.DataFrame({"tradeDate": [], "ivp30": []}).to_parquet(path)

            self.assertIsNone(bii._scan_parquet(str(path)))

    def test_returns_none_when_all_tradedates_unparseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "GARBAGE.parquet"
            pd.DataFrame({"tradeDate": ["not-a-date", "neither"], "ivp30": [1.0, 2.0]}).to_parquet(
                path
            )

            self.assertIsNone(bii._scan_parquet(str(path)))

    def test_handles_missing_ivp30_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "NOIV.parquet"
            _write_smd_parquet(path, start="2015-06-01", end="2015-06-05", has_ivp30=False)

            row = bii._scan_parquet(str(path))

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["ivp30_rows"], 0)
            self.assertEqual(row["n_rows"], 5)

    def test_counts_pre_2008_and_pre_2018_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "OLDIE.parquet"
            _write_smd_parquet(path, start="2007-01-01", end="2020-12-31")

            row = bii._scan_parquet(str(path))

            self.assertIsNotNone(row)
            assert row is not None
            self.assertGreater(row["pre_2008_rows"], 0)
            self.assertGreater(row["pre_2018_rows"], row["pre_2008_rows"])
            self.assertGreater(row["n_rows"], row["pre_2018_rows"])


class BuildInventoryTests(unittest.TestCase):
    def test_writes_inventory_for_multiple_parquets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inventory.parquet"

            _write_smd_parquet(cache / "AAPL.parquet", start="2010-01-01", end="2010-01-10")
            _write_smd_parquet(cache / "MSFT.parquet", start="2007-06-01", end="2007-06-30")

            counts = bii.build_inventory(cache, out)

            self.assertEqual(counts, {"scanned": 2, "ok": 2, "errors": 0})
            self.assertTrue(out.exists())
            inv = pd.read_parquet(out)
            self.assertEqual(len(inv), 2)
            # Sorted alphabetically
            self.assertEqual(inv.iloc[0]["ticker"], "AAPL")
            self.assertEqual(inv.iloc[1]["ticker"], "MSFT")
            self.assertGreater(int(inv.iloc[1]["pre_2008_rows"]), 0)

    def test_skips_empty_parquets_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inventory.parquet"

            _write_smd_parquet(cache / "GOOD.parquet", start="2014-01-01", end="2014-01-15")
            pd.DataFrame({"tradeDate": [], "ivp30": []}).to_parquet(cache / "EMPTY.parquet")

            counts = bii.build_inventory(cache, out)

            self.assertEqual(counts["scanned"], 2)
            self.assertEqual(counts["ok"], 1)
            self.assertEqual(counts["errors"], 0)
            inv = pd.read_parquet(out)
            self.assertEqual(len(inv), 1)
            self.assertEqual(inv.iloc[0]["ticker"], "GOOD")

    def test_logs_and_counts_unreadable_parquets_as_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inventory.parquet"

            _write_smd_parquet(cache / "GOOD.parquet", start="2014-01-01", end="2014-01-05")
            (cache / "BAD.parquet").write_bytes(b"not parquet")

            counts = bii.build_inventory(cache, out)

            self.assertEqual(counts["scanned"], 2)
            self.assertEqual(counts["ok"], 1)
            self.assertEqual(counts["errors"], 1)

    def test_emits_empty_inventory_for_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inventory.parquet"

            counts = bii.build_inventory(cache, out)

            self.assertEqual(counts, {"scanned": 0, "ok": 0, "errors": 0})
            self.assertTrue(out.exists())
            self.assertEqual(len(pd.read_parquet(out)), 0)

    def test_creates_parent_dirs_for_out_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "nested" / "deeper" / "inventory.parquet"

            _write_smd_parquet(cache / "AAA.parquet", start="2012-01-01", end="2012-01-05")

            bii.build_inventory(cache, out)

            self.assertTrue(out.exists())


class MainCliTests(unittest.TestCase):
    def test_main_zero_exit_on_clean_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inv.parquet"
            _write_smd_parquet(cache / "T.parquet", start="2015-01-01", end="2015-01-05")

            rc = bii.main(["--cache-dir", str(cache), "--out", str(out)])

            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())

    def test_main_nonzero_exit_when_errors_encountered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            out = Path(tmp) / "inv.parquet"
            (cache / "BAD.parquet").write_bytes(b"junk")

            rc = bii.main(["--cache-dir", str(cache), "--out", str(out)])

            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
