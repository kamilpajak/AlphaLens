"""Tests for alphalens.backtest.factors — FF5 / UMD / Industry12 loaders."""
from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory


def _write_ff5(tmp: Path) -> Path:
    content = (
        "This file was created by using the 202602 CRSP database.\n"
        "Preamble line 2.\n"
        "\n"
        ",Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "20240102,   1.00,   0.50,  -0.30,   0.10,  -0.20,   0.01\n"
        "20240103,  -0.50,  -0.20,   0.15,  -0.05,   0.10,   0.01\n"
        "20240104,   0.75,   0.30,   0.00,   0.20,   0.05,   0.01\n"
        "\n"
        "Copyright 2026 Fama/French\n"
    )
    p = tmp / "ff5.csv"
    p.write_text(content)
    return p


def _write_umd(tmp: Path) -> Path:
    content = (
        "This file was created by using the 202602 CRSP database.\n"
        "Preamble.\n"
        "\n"
        ",Mom\n"
        "20240102,   0.40\n"
        "20240103,  -0.25\n"
        "20240104,   0.60\n"
        "\n"
        "Copyright\n"
    )
    p = tmp / "umd.csv"
    p.write_text(content)
    return p


def _write_ind12(tmp: Path) -> Path:
    # Real Dartmouth file has value-weighted first, then equal-weighted. Loader must stop at EW.
    content = (
        "This file was created using the 202602 CRSP database.\n"
        "It contains value- and equal-weighted returns for 12 industry portfolios.\n"
        "\n"
        "Missing data are indicated by -99.99 or -999.\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "  Average Value Weighted Returns -- Daily\n"
        ",NoDur,Durbl,Manuf,Enrgy,Chems,BusEq,Telcm,Utils,Shops,Hlth,Money,Other\n"
        "20240102,  0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20\n"
        "20240103, -0.10,-0.20,-0.30,-0.40,-0.50,-0.60,-0.70,-0.80,-0.90,-1.00,-1.10,-1.20\n"
        "20240104,  0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95, 1.05, 1.15\n"
        "\n"
        "  Average Equal Weighted Returns -- Daily\n"
        ",NoDur,Durbl,Manuf,Enrgy,Chems,BusEq,Telcm,Utils,Shops,Hlth,Money,Other\n"
        "20240102,  9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99\n"
        "20240103,  9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99, 9.99\n"
        "\n"
        "Copyright\n"
    )
    p = tmp / "ind12.csv"
    p.write_text(content)
    return p


class TestLoadFF5Daily(unittest.TestCase):
    def test_returns_six_expected_columns(self):
        from alphalens.backtest.factors import load_ff5_daily

        with TemporaryDirectory() as tmp:
            df = load_ff5_daily(path=_write_ff5(Path(tmp)))
            self.assertEqual(list(df.columns), ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])
            self.assertEqual(len(df), 3)

    def test_values_converted_from_percent_to_decimal(self):
        from alphalens.backtest.factors import load_ff5_daily

        with TemporaryDirectory() as tmp:
            df = load_ff5_daily(path=_write_ff5(Path(tmp)))
            # Row 2024-01-02: Mkt-RF=1.00% → 0.01, RF=0.01% → 0.0001, HML=-0.30% → -0.003
            self.assertAlmostEqual(df.loc["2024-01-02", "Mkt-RF"], 0.01, places=6)
            self.assertAlmostEqual(df.loc["2024-01-02", "RF"], 0.0001, places=7)
            self.assertAlmostEqual(df.loc["2024-01-02", "HML"], -0.003, places=6)

    def test_date_range_filter(self):
        from alphalens.backtest.factors import load_ff5_daily

        with TemporaryDirectory() as tmp:
            path = _write_ff5(Path(tmp))
            df = load_ff5_daily(path=path, start=date(2024, 1, 3))
            self.assertEqual(len(df), 2)
            df = load_ff5_daily(path=path, start=date(2024, 1, 3), end=date(2024, 1, 3))
            self.assertEqual(len(df), 1)

    def test_missing_file_raises(self):
        from alphalens.backtest.factors import load_ff5_daily

        with self.assertRaises(FileNotFoundError):
            load_ff5_daily(path=Path("/tmp/definitely_missing_ff5.csv"))

    def test_malformed_file_raises(self):
        from alphalens.backtest.factors import load_ff5_daily

        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.csv"
            bad.write_text("no header row anywhere in this file\n")
            with self.assertRaises(ValueError):
                load_ff5_daily(path=bad)


class TestLoadUMDDaily(unittest.TestCase):
    def test_returns_single_mom_column(self):
        from alphalens.backtest.factors import load_umd_daily

        with TemporaryDirectory() as tmp:
            df = load_umd_daily(path=_write_umd(Path(tmp)))
            self.assertEqual(list(df.columns), ["Mom"])
            self.assertEqual(len(df), 3)

    def test_values_decimal(self):
        from alphalens.backtest.factors import load_umd_daily

        with TemporaryDirectory() as tmp:
            df = load_umd_daily(path=_write_umd(Path(tmp)))
            self.assertAlmostEqual(df.loc["2024-01-02", "Mom"], 0.004, places=6)
            self.assertAlmostEqual(df.loc["2024-01-03", "Mom"], -0.0025, places=6)


class TestLoadIndustry12Daily(unittest.TestCase):
    def test_returns_12_industry_columns_value_weighted(self):
        from alphalens.backtest.factors import load_industry12_daily

        with TemporaryDirectory() as tmp:
            df = load_industry12_daily(path=_write_ind12(Path(tmp)))
            expected = [
                "NoDur", "Durbl", "Manuf", "Enrgy", "Chems", "BusEq",
                "Telcm", "Utils", "Shops", "Hlth", "Money", "Other",
            ]
            self.assertEqual(list(df.columns), expected)

    def test_stops_before_equal_weighted_section(self):
        # Fixture has 3 VW rows + 2 EW rows with distinct values (9.99 = 0.0999 decimal).
        # Loader must return exactly 3 VW rows, never pulling 9.99 in.
        from alphalens.backtest.factors import load_industry12_daily

        with TemporaryDirectory() as tmp:
            df = load_industry12_daily(path=_write_ind12(Path(tmp)))
            self.assertEqual(len(df), 3)
            # No cell should be 0.0999 (the EW marker value from fixture).
            self.assertFalse((df == 0.0999).any().any())

    def test_values_decimal(self):
        from alphalens.backtest.factors import load_industry12_daily

        with TemporaryDirectory() as tmp:
            df = load_industry12_daily(path=_write_ind12(Path(tmp)))
            self.assertAlmostEqual(df.loc["2024-01-02", "NoDur"], 0.001, places=6)
            self.assertAlmostEqual(df.loc["2024-01-02", "Other"], 0.012, places=6)


class TestLoadCarhartDaily(unittest.TestCase):
    """Convenience wrapper: FF5-subset (Mkt-RF, SMB, HML, RF) + UMD (Mom), inner-joined."""

    def test_returns_five_expected_columns(self):
        from alphalens.backtest.factors import load_carhart_daily

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            df = load_carhart_daily(
                ff5_path=_write_ff5(tmp_path),
                umd_path=_write_umd(tmp_path),
            )
            self.assertEqual(set(df.columns), {"Mkt-RF", "SMB", "HML", "Mom", "RF"})
            self.assertEqual(len(df), 3)

    def test_inner_join_drops_unmatched_dates(self):
        from alphalens.backtest.factors import load_carhart_daily

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ff5_path = _write_ff5(tmp_path)
            # UMD with only 2 dates overlapping with FF5's 3:
            umd_path = tmp_path / "umd_short.csv"
            umd_path.write_text(
                "Preamble.\n\n,Mom\n"
                "20240102,   0.40\n"
                "20240104,   0.60\n"
                "\nCopyright\n"
            )
            df = load_carhart_daily(ff5_path=ff5_path, umd_path=umd_path)
            self.assertEqual(len(df), 2)


class TestRealFactorFiles(unittest.TestCase):
    """Integration: load actual CSVs from ~/.alphalens/factors/ if present."""

    def test_ff5_smoke(self):
        from alphalens.backtest.factors import DEFAULT_FF5_PATH, load_ff5_daily

        if not DEFAULT_FF5_PATH.exists():
            self.skipTest(f"FF5 file not at {DEFAULT_FF5_PATH}")
        df = load_ff5_daily(start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertGreater(len(df), 200)  # ~252 trading days
        self.assertEqual(list(df.columns), ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])

    def test_umd_smoke(self):
        from alphalens.backtest.factors import DEFAULT_UMD_PATH, load_umd_daily

        if not DEFAULT_UMD_PATH.exists():
            self.skipTest(f"UMD file not at {DEFAULT_UMD_PATH}")
        df = load_umd_daily(start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertGreater(len(df), 200)
        self.assertEqual(list(df.columns), ["Mom"])

    def test_industry12_smoke(self):
        from alphalens.backtest.factors import (
            DEFAULT_INDUSTRY12_PATH,
            load_industry12_daily,
        )

        if not DEFAULT_INDUSTRY12_PATH.exists():
            self.skipTest(f"Industry12 file not at {DEFAULT_INDUSTRY12_PATH}")
        df = load_industry12_daily(start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertGreater(len(df), 200)
        self.assertEqual(len(df.columns), 12)

    def test_carhart_merged_smoke(self):
        from alphalens.backtest.factors import load_carhart_daily

        try:
            df = load_carhart_daily(start=date(2024, 1, 1), end=date(2024, 12, 31))
        except FileNotFoundError:
            self.skipTest("default factor files not all present")
        self.assertGreater(len(df), 200)
        self.assertEqual(set(df.columns), {"Mkt-RF", "SMB", "HML", "Mom", "RF"})


if __name__ == "__main__":
    unittest.main()
