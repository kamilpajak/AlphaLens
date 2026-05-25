"""Tests for pure-function helpers in
``scripts/experiment_v9d_retrospective_pre_2018.py``.

The driver itself orchestrates I/O-heavy steps (smd cache, FF factor data,
scorer module) and is exercised by smoke runs; only deterministic helpers
are unit-tested here."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import experiment_v9d_retrospective_pre_2018 as driver


class TurnoverTests(unittest.TestCase):
    def test_returns_nan_for_single_period(self) -> None:
        import math

        self.assertTrue(math.isnan(driver._turnover([{"AAA"}])))
        self.assertTrue(math.isnan(driver._turnover([])))

    def test_zero_turnover_when_holdings_static(self) -> None:
        history = [{"A", "B", "C"}, {"A", "B", "C"}, {"A", "B", "C"}]
        self.assertEqual(driver._turnover(history), 0.0)

    def test_full_turnover_when_holdings_replaced(self) -> None:
        # 100% replacement each rebalance → turnover = 1.0
        history = [{"A", "B"}, {"C", "D"}, {"E", "F"}]
        self.assertEqual(driver._turnover(history), 1.0)

    def test_partial_turnover_averages_per_rebalance(self) -> None:
        # Step 1: {A,B,C}→{A,B,D} = 1/3 new; step 2: {A,B,D}→{A,E,D} = 1/3 new
        # Mean = 1/3
        history = [{"A", "B", "C"}, {"A", "B", "D"}, {"A", "E", "D"}]
        self.assertAlmostEqual(driver._turnover(history), 1.0 / 3, places=6)

    def test_skips_empty_holding_in_denominator(self) -> None:
        # Empty current holding → contributes 0 to total; denominator counts pair
        history = [{"A", "B"}, set(), {"C", "D"}]
        # First pair: empty current → skip in numerator (no division by 0)
        # Second pair: {} → {C,D}: new_names = {C,D} - {} = 2/2 = 1.0
        # Sum = 1.0 / 2 pairs = 0.5
        self.assertAlmostEqual(driver._turnover(history), 0.5, places=6)


class SubPeriodsLockTests(unittest.TestCase):
    def test_subperiod_keys_match_pre_reg(self) -> None:
        # The driver's SUB_PERIODS dict locks the same names as
        # ``params_v9d_retrospective_pre_2018_2026_05_05.json``.
        expected = {"GFC_recovery", "mid_cycle_eu_debt", "late_cycle_china_shock"}
        self.assertEqual(set(driver.SUB_PERIODS.keys()), expected)

    def test_subperiods_contiguous_and_match_pre_reg_dates(self) -> None:
        from datetime import date

        self.assertEqual(
            driver.SUB_PERIODS["GFC_recovery"],
            (date(2008, 4, 30), date(2011, 12, 31)),
        )
        self.assertEqual(
            driver.SUB_PERIODS["mid_cycle_eu_debt"],
            (date(2012, 1, 1), date(2014, 12, 31)),
        )
        self.assertEqual(
            driver.SUB_PERIODS["late_cycle_china_shock"],
            (date(2015, 1, 1), date(2018, 4, 29)),
        )


if __name__ == "__main__":
    unittest.main()
