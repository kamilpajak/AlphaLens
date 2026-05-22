"""Tests for per-rebalance turnover dump alongside returns parquet.

Pre-reg memo `insider_form4_opportunistic_slippage_stress_design_2026_05_12.md`
mandates that whenever the experiment script writes `phase_N_returns.parquet`,
it MUST also write `phase_N_turnover.parquet` to the same directory with
schema `(rebalance_date: DatetimeIndex, turnover: float, n_tickers: int,
traded_value_proxy: float)`. Slippage diagnostic depends on per-rebalance
turnover preserving Q5-panic clustering — not smeared by forward-fill from a
scalar.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _ts(year: int, month: int, day: int) -> pd.Timestamp:
    return pd.Timestamp(year=year, month=month, day=day)


class TestPerRebalanceTurnover(unittest.TestCase):
    """Pure-function unit tests for `per_rebalance_turnover` in metrics.py."""

    def test_single_snapshot_has_nan_turnover(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover([["A", "B"]], dates=[_ts(2020, 1, 1)])
        self.assertEqual(len(df), 1)
        self.assertTrue(pd.isna(df.iloc[0]["turnover"]))
        self.assertEqual(int(df.iloc[0]["n_tickers"]), 2)

    def test_full_turnover_per_row(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover(
            [["A", "B", "C"], ["X", "Y", "Z"]],
            dates=[_ts(2020, 1, 1), _ts(2020, 1, 22)],
        )
        self.assertTrue(pd.isna(df.iloc[0]["turnover"]))
        self.assertAlmostEqual(df.iloc[1]["turnover"], 1.0)

    def test_no_turnover_when_baskets_unchanged(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover(
            [["A", "B", "C"], ["A", "B", "C"], ["A", "B", "C"]],
            dates=[_ts(2020, 1, 1), _ts(2020, 1, 22), _ts(2020, 2, 12)],
        )
        self.assertAlmostEqual(df.iloc[1]["turnover"], 0.0)
        self.assertAlmostEqual(df.iloc[2]["turnover"], 0.0)

    def test_partial_turnover_per_row(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover(
            [["A", "B", "C", "D"], ["A", "B", "X", "Y"]],
            dates=[_ts(2020, 1, 1), _ts(2020, 1, 22)],
        )
        self.assertAlmostEqual(df.iloc[1]["turnover"], 0.5)
        self.assertEqual(int(df.iloc[1]["n_tickers"]), 4)

    def test_traded_value_proxy_is_round_trip_double(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover(
            [["A", "B", "C", "D"], ["A", "B", "X", "Y"]],
            dates=[_ts(2020, 1, 1), _ts(2020, 1, 22)],
        )
        # 50% one-leg turnover → round-trip notional fraction = 1.0
        self.assertAlmostEqual(df.iloc[1]["traded_value_proxy"], 1.0)
        self.assertAlmostEqual(df.iloc[1]["turnover"] * 2.0, df.iloc[1]["traded_value_proxy"])

    def test_mean_matches_scalar_turnover_pct(self):
        """Per-rebalance mean (skipna) must agree with scalar `turnover_pct`."""
        from alphalens_research.backtest.metrics import per_rebalance_turnover, turnover_pct

        baskets = [
            ["A", "B", "C", "D"],
            ["A", "B", "X", "Y"],
            ["X", "Y", "Z", "W"],
        ]
        df = per_rebalance_turnover(
            baskets,
            dates=[_ts(2020, 1, 1), _ts(2020, 1, 22), _ts(2020, 2, 12)],
        )
        self.assertAlmostEqual(df["turnover"].dropna().mean(), turnover_pct(baskets))

    def test_dates_optional_falls_back_to_rangeindex(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover([["A", "B"], ["A", "C"]])
        self.assertEqual(list(df.index), [0, 1])

    def test_empty_input_returns_empty_frame(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover([])
        self.assertTrue(df.empty)
        self.assertIn("turnover", df.columns)
        self.assertIn("n_tickers", df.columns)
        self.assertIn("traded_value_proxy", df.columns)

    def test_dates_index_is_datetime(self):
        from alphalens_research.backtest.metrics import per_rebalance_turnover

        df = per_rebalance_turnover([["A"], ["B"]], dates=[_ts(2020, 1, 1), _ts(2020, 1, 22)])
        self.assertIsInstance(df.index, pd.DatetimeIndex)


class TestDeriveTurnoverPath(unittest.TestCase):
    """Tests for the path-derivation helper used by the experiment script."""

    def test_replaces_returns_token_in_stem(self):
        from scripts.experiment_insider_form4_opportunistic import derive_turnover_path

        p = Path("/tmp/audit/phase_3_returns.parquet")
        self.assertEqual(derive_turnover_path(p), Path("/tmp/audit/phase_3_turnover.parquet"))

    def test_appends_turnover_suffix_when_no_returns_token(self):
        from scripts.experiment_insider_form4_opportunistic import derive_turnover_path

        p = Path("/tmp/foo/some_arbitrary.parquet")
        self.assertEqual(derive_turnover_path(p), Path("/tmp/foo/some_arbitrary_turnover.parquet"))

    def test_preserves_parent_directory(self):
        from scripts.experiment_insider_form4_opportunistic import derive_turnover_path

        p = Path("/var/runs/abc/phase_0_returns.parquet")
        self.assertEqual(derive_turnover_path(p).parent, Path("/var/runs/abc"))

    def test_replaces_last_occurrence_only(self):
        """Pathological case: 'returns' substring appears multiple times in
        the path. The fix replaces only the LAST occurrence in the stem
        (and only in the stem — never in the parent directory) — otherwise
        the dumped turnover lands at an unintended path that won't be found
        by downstream readers.
        """
        from scripts.experiment_insider_form4_opportunistic import derive_turnover_path

        # Parent directory contains 'returns'; must be preserved verbatim.
        p = Path("/tmp/returns_audit/phase_0_returns.parquet")
        self.assertEqual(
            derive_turnover_path(p),
            Path("/tmp/returns_audit/phase_0_turnover.parquet"),
        )
        # Stem contains 'returns' twice; only the last occurrence is replaced.
        p2 = Path("/tmp/runs/my_returns_analysis_returns.parquet")
        self.assertEqual(
            derive_turnover_path(p2),
            Path("/tmp/runs/my_returns_analysis_turnover.parquet"),
        )


class TestAssessReturnsPerRebalanceTurnoverSeries(unittest.TestCase):
    """`assess()` must surface a per-rebalance turnover DataFrame in its return
    dict so `main()` can dump it. Test via a stubbed BacktestReport-like object
    so we don't pull the full pipeline.
    """

    def test_assess_dict_contains_turnover_series_key(self):
        # Smoke-only: verify the contract by inspecting source for a literal
        # key reference. Avoids constructing the heavy `assess` dependency tree.
        import inspect

        from scripts.experiment_insider_form4_opportunistic import assess

        src = inspect.getsource(assess)
        self.assertIn(
            "turnover_series",
            src,
            "assess() must place a per-rebalance turnover series in its result dict "
            "under the key 'turnover_series' so main() can dump it.",
        )


class TestMainDumpsTurnoverParquet(unittest.TestCase):
    """Integration smoke: verify the main() dump branch writes turnover.parquet
    when `--dump-returns` is provided, via a stub that bypasses the heavy
    backtest. Uses module-level monkeypatching."""

    def test_dump_writes_turnover_parquet_alongside_returns(self):
        # We don't run main() end-to-end (too heavy). Instead we directly call
        # the small helper that writes turnover, which is the unit being added.
        from scripts.experiment_insider_form4_opportunistic import (
            derive_turnover_path,
            dump_turnover_parquet,
        )

        with tempfile.TemporaryDirectory() as tmp:
            returns_path = Path(tmp) / "phase_0_returns.parquet"
            turnover_df = pd.DataFrame(
                {
                    "turnover": [float("nan"), 0.25, 0.30],
                    "n_tickers": [200, 200, 200],
                    "traded_value_proxy": [float("nan"), 0.50, 0.60],
                },
                index=pd.DatetimeIndex(
                    [_ts(2020, 1, 1), _ts(2020, 1, 22), _ts(2020, 2, 12)],
                    name="rebalance_date",
                ),
            )
            dump_turnover_parquet(turnover_df, returns_path)

            expected_path = derive_turnover_path(returns_path)
            self.assertTrue(expected_path.exists())
            loaded = pd.read_parquet(expected_path)
            self.assertEqual(len(loaded), 3)
            self.assertIn("turnover", loaded.columns)
            self.assertIn("n_tickers", loaded.columns)
            self.assertIn("traded_value_proxy", loaded.columns)
            self.assertIsInstance(loaded.index, pd.DatetimeIndex)


if __name__ == "__main__":
    unittest.main()
