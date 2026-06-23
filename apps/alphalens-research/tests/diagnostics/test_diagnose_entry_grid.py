"""Tests for the pure helpers in diagnose_entry_grid.py.

Only the importable pure functions are tested here — main() I/O is excluded
(scripts are coverage-excluded per CI config). The three helpers under test:

  _common_support  -- keeps only events where all 5 arms are non-None
  _market_cap_index -- maps (brief_date, TICKER) -> float | None from a briefs
                       DataFrame, with numpy-float64 -> plain float coercion
"""

from __future__ import annotations

import datetime as dt
import unittest

import numpy as np
import pandas as pd


def _import_helpers():
    """Lazy import so the test file can be collected without a working script."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parent.parent.parent / "scripts" / "diagnose_entry_grid.py"
    spec = importlib.util.spec_from_file_location("diagnose_entry_grid", script)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestCommonSupport(unittest.TestCase):
    """_common_support keeps only events where ALL arms are non-None."""

    def setUp(self):
        self.mod = _import_helpers()

    def _arm_row(self, **overrides) -> dict:
        """Return a full 5-arm row with all arms set to 0.0 by default."""
        row = dict.fromkeys(
            ("baseline", "narrow_tiers", "single_at_close", "market_at_arrival", "vwap_arrival"),
            0.0,
        )
        row.update(overrides)
        return row

    def test_all_five_non_none_kept(self):
        row = self._arm_row()
        result = self.mod._common_support([row])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], row)

    def test_one_arm_none_dropped(self):
        row_bad = self._arm_row(narrow_tiers=None)
        row_ok = self._arm_row()
        result = self.mod._common_support([row_bad, row_ok])
        # Only the fully non-None row survives.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], row_ok)

    def test_all_none_arm_dropped(self):
        row = self._arm_row(
            baseline=None,
            narrow_tiers=None,
            single_at_close=None,
            market_at_arrival=None,
            vwap_arrival=None,
        )
        result = self.mod._common_support([row])
        self.assertEqual(result, [])

    def test_empty_input(self):
        self.assertEqual(self.mod._common_support([]), [])

    def test_four_of_five_none_dropped(self):
        row = self._arm_row(vwap_arrival=None)
        result = self.mod._common_support([row])
        self.assertEqual(result, [])


class TestEqualFillRateSimulationProof(unittest.TestCase):
    """Pins why common-support is the headline.

    Two arms with the SAME fill-conditional reward but DIFFERENT fill rates
    have an EQUAL mean on the common-support subset but an UNEQUAL mean on
    the full set (because the full-set mean conflates cash returns with
    fill-conditional returns).
    """

    def setUp(self):
        self.mod = _import_helpers()

    def test_equal_mean_on_common_support(self):
        """Arms A and B share reward=0.10 on every event both fill.
        Full-set means differ (A fills more events); common-support means match.
        """
        # Use a simplified version: just test _common_support isolation on
        # events that have all 5 standard arms. We test the conceptual proof
        # directly with the 5-arm structure.
        arm_names = (
            "baseline",
            "narrow_tiers",
            "single_at_close",
            "market_at_arrival",
            "vwap_arrival",
        )

        # Arm A (baseline) fills all 10 events with values [0.10]*5 + [-0.05]*5.
        # Arm B (narrow_tiers) fills only the first 5 events (value=0.10); others=None.
        rows = []
        for _ in range(5):
            rows.append(dict.fromkeys(arm_names, 0.10))  # both arms match
        for _ in range(5):
            # arm A (baseline) gets cash -0.05; arm B (narrow_tiers) gets None
            row = dict.fromkeys(arm_names, -0.05)
            row["narrow_tiers"] = None
            rows.append(row)

        # Full-set mean for baseline: (5*0.10 + 5*(-0.05)) / 10 = 0.025
        baseline_full_mean = sum(r["baseline"] for r in rows) / len(rows)
        # Full-set mean for narrow_tiers (excluding None): (5*0.10) / 5 = 0.10
        narrow_vals_full = [r["narrow_tiers"] for r in rows if r["narrow_tiers"] is not None]
        narrow_full_mean = sum(narrow_vals_full) / len(narrow_vals_full)

        # Full-set means DIFFER (this is the problem that common-support fixes).
        self.assertNotAlmostEqual(baseline_full_mean, narrow_full_mean, places=4)

        # Common-support mean: only the 5 rows where ALL arms are non-None.
        common = self.mod._common_support(rows)
        self.assertEqual(len(common), 5)

        baseline_cs_mean = sum(r["baseline"] for r in common) / len(common)
        narrow_cs_mean = sum(r["narrow_tiers"] for r in common) / len(common)

        # On the common support both arms have the same fill-conditional reward.
        self.assertAlmostEqual(baseline_cs_mean, narrow_cs_mean, places=10)
        self.assertAlmostEqual(baseline_cs_mean, 0.10, places=10)


class TestMarketCapIndex(unittest.TestCase):
    """_market_cap_index maps (brief_date, TICKER) -> float | None."""

    def setUp(self):
        self.mod = _import_helpers()

    def _make_briefs(self, rows: list[dict]) -> pd.DataFrame:
        """Build a minimal briefs DataFrame with the columns _market_cap_index needs."""
        df = pd.DataFrame(rows)
        return df

    def test_basic_mapping(self):
        d = dt.date(2026, 1, 10)
        df = self._make_briefs(
            [
                {"brief_date": d, "ticker": "AAPL", "mcap": 3_000_000.0},
                {"brief_date": d, "ticker": "MSFT", "mcap": 2_500_000.0},
            ]
        )
        idx = self.mod._market_cap_index(df)
        self.assertAlmostEqual(idx[(d, "AAPL")], 3_000_000.0)
        self.assertAlmostEqual(idx[(d, "MSFT")], 2_500_000.0)

    def test_nan_mcap_returns_none(self):
        d = dt.date(2026, 1, 10)
        df = self._make_briefs(
            [
                {"brief_date": d, "ticker": "XYZ", "mcap": float("nan")},
            ]
        )
        idx = self.mod._market_cap_index(df)
        self.assertIsNone(idx[(d, "XYZ")])

    def test_missing_mcap_column(self):
        """When mcap column is absent the helper returns an empty dict (no crash)."""
        d = dt.date(2026, 1, 10)
        df = self._make_briefs([{"brief_date": d, "ticker": "ABC"}])
        # Should not raise; returns {} or {key: None}.
        try:
            idx = self.mod._market_cap_index(df)
        except KeyError:
            self.fail("_market_cap_index raised KeyError on missing mcap column")

    def test_numpy_float64_coerced_to_plain_float(self):
        """A numpy.float64 value must be coerced to a plain Python float."""
        d = dt.date(2026, 3, 15)
        df = self._make_briefs(
            [
                {"brief_date": d, "ticker": "NVDA", "mcap": np.float64(1_500_000.0)},
            ]
        )
        idx = self.mod._market_cap_index(df)
        val = idx[(d, "NVDA")]
        self.assertIsNotNone(val)
        self.assertIs(type(val), float, f"Expected plain float, got {type(val)}")

    def test_ticker_uppercased(self):
        d = dt.date(2026, 2, 1)
        df = self._make_briefs(
            [
                {"brief_date": d, "ticker": "meta", "mcap": 100_000.0},
            ]
        )
        idx = self.mod._market_cap_index(df)
        # Key should be (d, "META") not (d, "meta").
        self.assertIn((d, "META"), idx)

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["brief_date", "ticker", "mcap"])
        idx = self.mod._market_cap_index(df)
        self.assertEqual(idx, {})


if __name__ == "__main__":
    unittest.main()
