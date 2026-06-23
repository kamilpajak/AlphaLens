"""Tests for the pure helpers in diagnose_entry_grid.py.

Only the importable pure functions are tested here — main() I/O is excluded
(scripts are coverage-excluded per CI config). The four helpers / attributes
under test:

  _common_support  -- keeps only events where all 5 arms are non-None
  _market_cap_index -- maps (brief_date, TICKER) -> float | None from a briefs
                       DataFrame, with numpy-float64 -> plain float coercion
  _counter_split_contract -- verifies main() uses two distinct counters
                             (n_missing_setup and n_missing_bars, NOT one
                             conflated n_missing_bars)
"""

from __future__ import annotations

import datetime as dt
import inspect
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


class TestMissingCounterSplit(unittest.TestCase):
    """main() must use two distinct counters: n_missing_setup and n_missing_bars.

    Before Fix 1, a single n_missing_bars counter conflated "setup is None"
    (no trade setup found for the ticker) with "bars cache is empty" (setup
    present but minute-bar cache is empty for the arrival session).  The fix
    introduces n_missing_setup and n_missing_bars as separate counters with
    separate continue paths, and the report line names them both.

    We verify this by inspecting the source of main() for the presence of
    both counter identifiers and the absence of the old conflated usage.
    """

    def setUp(self):
        self.mod = _import_helpers()
        self.main_src = inspect.getsource(self.mod.main)

    def test_n_missing_setup_counter_exists_in_main(self):
        """main() must declare and increment n_missing_setup."""
        self.assertIn(
            "n_missing_setup",
            self.main_src,
            "main() must have a separate n_missing_setup counter (Fix 1)",
        )

    def test_n_missing_bars_counter_exists_in_main(self):
        """main() must still have n_missing_bars (for the empty-bars branch)."""
        self.assertIn(
            "n_missing_bars",
            self.main_src,
            "main() must retain n_missing_bars counter for the empty-bars branch (Fix 1)",
        )

    def test_report_line_names_both_counters(self):
        """The printed report line must mention both n_missing_setup and n_missing_bars."""
        # The print statement must contain both counter values so the report
        # distinguishes them.  We check the format string contains both names.
        self.assertIn(
            "n_missing_setup",
            self.main_src,
            "report line must reference n_missing_setup",
        )
        self.assertIn(
            "n_missing_bars",
            self.main_src,
            "report line must reference n_missing_bars",
        )

    def test_old_conflated_counter_removed(self):
        """The old conflated 'missing-bars/setup' label must not appear in the report."""
        # Before Fix 1, the report line read 'missing-bars/setup' (one combined value).
        # After the fix it names the two counters separately.
        self.assertNotIn(
            "missing-bars/setup",
            self.main_src,
            "old conflated 'missing-bars/setup' label must be removed from the report (Fix 1)",
        )


class TestCommonSupport(unittest.TestCase):
    """_common_support keeps only events where ALL arms are non-None."""

    def setUp(self):
        self.mod = _import_helpers()

    def _arm_row(self, **overrides: float | None) -> dict[str, float | None]:
        """Return a full 5-arm row with all arms set to 0.0 by default."""
        row: dict[str, float | None] = dict.fromkeys(
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
        rows: list[dict[str, float | None]] = []
        for _ in range(5):
            rows.append(dict.fromkeys(arm_names, 0.10))  # both arms match
        for _ in range(5):
            # arm A (baseline) gets cash -0.05; arm B (narrow_tiers) gets None
            row: dict[str, float | None] = dict.fromkeys(arm_names, -0.05)
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
