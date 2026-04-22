import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


SAMPLE_Q4_CSV = """\
DATE,R_F,R_MKT,R_ME,R_IA,R_ROE,R_EG
20200102,0.0054,0.8135,-0.3421,0.2100,-0.1500,0.0500
20200103,0.0054,-0.6719,-0.1200,-0.0800,0.2100,0.1100
20200106,0.0054,0.5500,0.2300,0.1400,-0.0500,0.0200
"""


class TestParseQ4Csv(unittest.TestCase):
    def test_parses_date_index_and_renames_columns(self):
        from alphalens.backtest.factors import _parse_q4_csv

        df = _parse_q4_csv(SAMPLE_Q4_CSV)

        self.assertEqual(list(df.columns), ["Mkt-RF", "ME", "IA", "ROE", "RF"])
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(df.index[0], pd.Timestamp("2020-01-02"))

    def test_percent_converted_to_decimal(self):
        from alphalens.backtest.factors import _parse_q4_csv

        df = _parse_q4_csv(SAMPLE_Q4_CSV)

        # R_MKT=0.8135 percent → 0.008135 decimal
        self.assertAlmostEqual(df.loc["2020-01-02", "Mkt-RF"], 0.008135, places=6)
        self.assertAlmostEqual(df.loc["2020-01-02", "RF"], 0.000054, places=6)

    def test_r_eg_dropped(self):
        """R_EG is the q5 Expected-Growth factor, not part of Q4."""
        from alphalens.backtest.factors import _parse_q4_csv

        df = _parse_q4_csv(SAMPLE_Q4_CSV)

        self.assertNotIn("R_EG", df.columns)
        self.assertNotIn("EG", df.columns)

    def test_missing_required_column_raises(self):
        from alphalens.backtest.factors import _parse_q4_csv

        bad_csv = "DATE,R_F,R_MKT\n20200102,0.005,0.1\n"

        with self.assertRaises(ValueError):
            _parse_q4_csv(bad_csv)


class TestLoadQ4Daily(unittest.TestCase):
    def _synth_csv(self, year: int) -> str:
        idx = pd.date_range(f"{year}-01-03", periods=5, freq="B")
        rows = ["DATE,R_F,R_MKT,R_ME,R_IA,R_ROE,R_EG"]
        for d in idx:
            rows.append(
                f"{d.strftime('%Y%m%d')},0.005,0.05,0.02,0.01,0.03,0.01"
            )
        return "\n".join(rows) + "\n"

    def test_concatenates_cumulative_and_yearly_files(self):
        from alphalens.backtest.factors import load_q4_daily

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            # Fetcher returns a distinct stubbed CSV per URL.
            def fetch(url):
                if "daily.csv" in url and "_" not in url.rsplit("/", 1)[-1].replace("q5_factors_daily", ""):
                    return self._synth_csv(2018)
                # Yearly
                for y in range(2019, 2025):
                    if f"_{y}.csv" in url:
                        return self._synth_csv(y)
                raise ValueError(f"unexpected url {url}")

            df = load_q4_daily(cache_dir=cache, fetch=fetch)

        # Expect 7 distinct years × 5 bars = 35 rows.
        self.assertEqual(len(df), 35)
        self.assertEqual(df.index.min().year, 2018)
        self.assertEqual(df.index.max().year, 2024)

    def test_cache_avoids_refetch(self):
        from alphalens.backtest.factors import load_q4_daily

        call_count = {"n": 0}

        def fetch(url):
            call_count["n"] += 1
            if "_" in url.rsplit("/", 1)[-1].replace("q5_factors_daily", "").split(".")[0]:
                return self._synth_csv(2020)
            return self._synth_csv(2018)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)

            load_q4_daily(cache_dir=cache, fetch=fetch)
            first_n = call_count["n"]

            load_q4_daily(cache_dir=cache, fetch=fetch)

        self.assertGreater(first_n, 0)
        # Second call must hit cache only — no additional fetches
        self.assertEqual(call_count["n"], first_n)

    def test_date_range_filter_applied(self):
        from alphalens.backtest.factors import load_q4_daily

        def fetch(url):
            for y in range(2019, 2025):
                if f"_{y}.csv" in url:
                    return self._synth_csv(y)
            return self._synth_csv(2018)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            df = load_q4_daily(
                cache_dir=cache,
                fetch=fetch,
                start=date(2021, 1, 1),
                end=date(2022, 12, 31),
            )

        self.assertEqual(df.index.min().year, 2021)
        self.assertEqual(df.index.max().year, 2022)


class TestQ4Attribution(unittest.TestCase):
    def _synth_factors(self, n: int = 500) -> pd.DataFrame:
        rng = np.random.default_rng(3)
        idx = pd.date_range("2022-01-03", periods=n, freq="B")
        return pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0.0004, 0.010, n),
                "ME": rng.normal(0.0001, 0.005, n),
                "IA": rng.normal(0.0000, 0.004, n),
                "ROE": rng.normal(0.0002, 0.005, n),
                "RF": 0.00008,
            },
            index=idx,
        )

    def test_returns_q4_alpha_result(self):
        from alphalens.backtest.factor_analysis import run_q4_attribution

        factors = self._synth_factors()
        rng = np.random.default_rng(17)
        portfolio = (
            0.0002 + 0.5 * factors["Mkt-RF"] + factors["RF"]
            + rng.normal(0, 0.005, len(factors))
        )

        result = run_q4_attribution(portfolio, factors)

        self.assertEqual(result.spec_name, "Q4")
        self.assertEqual(set(result.betas.keys()), {"Mkt-RF", "ME", "IA", "ROE"})


if __name__ == "__main__":
    unittest.main()
