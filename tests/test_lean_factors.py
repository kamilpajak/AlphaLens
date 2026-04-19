import tempfile
import unittest
from datetime import date
from pathlib import Path


_SAMPLE_CSV = """This file was created by using the 202602 CRSP database.
The Tbill return is the simple daily rate.
Starting from 202406, the 1-month TBill rate is from ICE BofA.

,Mkt-RF,SMB,HML,RF
20240102,    0.50,   -0.10,    0.20,    0.02
20240103,   -0.30,    0.15,   -0.05,    0.02
20240104,    1.25,    0.00,    0.50,    0.02
20240105,    0.10,   -0.40,    0.30,    0.02

Copyright 2026 Eugene F. Fama and Kenneth R. French
"""


def _write_csv(tmp: Path, content: str = _SAMPLE_CSV) -> Path:
    path = tmp / "ff3.csv"
    path.write_text(content)
    return path


class TestLoadFF3Daily(unittest.TestCase):
    def test_skips_preamble_and_footer(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with tempfile.TemporaryDirectory() as tmp:
            df = load_ff3_daily(path=_write_csv(Path(tmp)))

        self.assertEqual(len(df), 4)
        self.assertListEqual(list(df.columns), ["Mkt-RF", "SMB", "HML", "RF"])

    def test_values_converted_to_decimals(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with tempfile.TemporaryDirectory() as tmp:
            df = load_ff3_daily(path=_write_csv(Path(tmp)))

        self.assertAlmostEqual(df.iloc[0]["Mkt-RF"], 0.005, places=6)
        self.assertAlmostEqual(df.iloc[0]["RF"], 0.0002, places=6)

    def test_index_is_datetime(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with tempfile.TemporaryDirectory() as tmp:
            df = load_ff3_daily(path=_write_csv(Path(tmp)))

        import pandas as pd
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(df.index[0].date(), date(2024, 1, 2))

    def test_start_end_filtering(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with tempfile.TemporaryDirectory() as tmp:
            df = load_ff3_daily(
                path=_write_csv(Path(tmp)),
                start=date(2024, 1, 3),
                end=date(2024, 1, 4),
            )

        self.assertEqual(len(df), 2)
        self.assertEqual(df.index[0].date(), date(2024, 1, 3))
        self.assertEqual(df.index[-1].date(), date(2024, 1, 4))

    def test_missing_file_raises(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with self.assertRaises(FileNotFoundError):
            load_ff3_daily(path=Path("/tmp/definitely_nope_ff3.csv"))

    def test_missing_header_raises(self):
        from alphalens.lean_screener.factors import load_ff3_daily

        with tempfile.TemporaryDirectory() as tmp:
            bad = _write_csv(Path(tmp), "no header here\njust garbage\n")
            with self.assertRaises(ValueError):
                load_ff3_daily(path=bad)

    def test_real_file_loads_if_present(self):
        """Smoke test against the actual Ken French download on this machine."""
        from alphalens.lean_screener.config import FF3_DAILY_PATH
        from alphalens.lean_screener.factors import load_ff3_daily

        if not FF3_DAILY_PATH.exists():
            self.skipTest(f"FF3 CSV not present at {FF3_DAILY_PATH}")

        df = load_ff3_daily(start=date(2024, 4, 1), end=date(2024, 12, 31))
        self.assertGreater(len(df), 100)  # ~190 trading days
        self.assertTrue((df["RF"] >= 0).all())        # risk-free ≥ 0
        self.assertLess(df["Mkt-RF"].abs().max(), 0.20)  # no single-day |Mkt-RF| > 20%


if __name__ == "__main__":
    unittest.main()
