"""Panel disagreement scalar — raw spread, sparsity, unconditional config_version.

The spread is the absolute gap between the two 0-100 composites; None whenever
fewer than two scores are present (a 0.0 spread means "agree", distinct from "could
not compute"). panel_config_version is stamped on EVERY row for the calibration corpus.
"""

from __future__ import annotations

import math
import unittest

import pandas as pd
from alphalens_pipeline.experts import disagreement
from alphalens_pipeline.experts.disagreement import PANEL_CONFIG_VERSION, compute_spread, enrich


class TestComputeSpread(unittest.TestCase):
    def test_two_present_is_abs_difference(self):
        self.assertAlmostEqual(compute_spread({"buffett": 31.0, "oneil": 78.0}), 47.0)
        self.assertAlmostEqual(compute_spread({"buffett": 90.0, "oneil": 90.0}), 0.0)

    def test_one_present_is_none(self):
        self.assertIsNone(compute_spread({"buffett": 50.0, "oneil": None}))
        self.assertIsNone(compute_spread({"buffett": None, "oneil": 50.0}))

    def test_none_present_is_none(self):
        self.assertIsNone(compute_spread({"buffett": None, "oneil": None}))
        self.assertIsNone(compute_spread({}))

    def test_non_finite_excluded(self):
        # A NaN/inf score is not "present" — must not poison the spread.
        self.assertIsNone(compute_spread({"buffett": float("nan"), "oneil": 50.0}))
        self.assertIsNone(compute_spread({"buffett": float("inf"), "oneil": 50.0}))

    def test_zero_spread_is_not_none(self):
        # Agreement (0.0) is a real value, never confused with missing (None).
        result = compute_spread({"buffett": 60.0, "oneil": 60.0})
        self.assertIsNotNone(result)
        self.assertEqual(result, 0.0)


class TestEnrich(unittest.TestCase):
    def _frame(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_both_scores_present_spread_computed(self):
        out = enrich(
            self._frame([{"ticker": "AAA", "buffett_quality_score": 31.0, "oneil_score": 78.0}])
        )
        self.assertAlmostEqual(out.iloc[0]["expert_spread"], 47.0)
        self.assertEqual(out.iloc[0]["panel_config_version"], PANEL_CONFIG_VERSION)

    def test_one_score_none_spread_null_but_config_stamped(self):
        out = enrich(
            self._frame([{"ticker": "AAA", "buffett_quality_score": 50.0, "oneil_score": None}])
        )
        self.assertTrue(pd.isna(out.iloc[0]["expert_spread"]))
        # config_version is stamped UNCONDITIONALLY, even on a null-spread row.
        self.assertEqual(out.iloc[0]["panel_config_version"], PANEL_CONFIG_VERSION)

    def test_score_columns_absent_spread_null(self):
        # A frame missing the score columns entirely (e.g. expert enrich skipped)
        # still gets both panel columns: spread null, config stamped.
        out = enrich(self._frame([{"ticker": "AAA"}]))
        self.assertIn("expert_spread", out.columns)
        self.assertTrue(pd.isna(out.iloc[0]["expert_spread"]))
        self.assertEqual(out.iloc[0]["panel_config_version"], PANEL_CONFIG_VERSION)

    def test_empty_frame_adds_both_columns_stable_dtype(self):
        out = enrich(self._frame([]))
        self.assertIn("expert_spread", out.columns)
        self.assertIn("panel_config_version", out.columns)
        self.assertEqual(out["expert_spread"].dtype, "float64")
        self.assertEqual(len(out), 0)

    def test_preserves_existing_columns(self):
        out = enrich(
            self._frame(
                [
                    {
                        "ticker": "AAA",
                        "rationale": "keep",
                        "buffett_quality_score": 10.0,
                        "oneil_score": 20.0,
                    }
                ]
            )
        )
        self.assertEqual(out.iloc[0]["rationale"], "keep")
        self.assertAlmostEqual(out.iloc[0]["expert_spread"], 10.0)

    def test_config_version_format_pinned(self):
        # Pin the token (folded into the calibration corpus; a change must be deliberate).
        self.assertEqual(PANEL_CONFIG_VERSION, "panel-v1-absdiff-2x")
        self.assertEqual(disagreement.PANEL_COLUMNS, ("expert_spread", "panel_config_version"))

    def test_spread_finite_when_present(self):
        out = enrich(
            self._frame([{"ticker": "AAA", "buffett_quality_score": 0.0, "oneil_score": 100.0}])
        )
        self.assertTrue(math.isfinite(out.iloc[0]["expert_spread"]))
        self.assertAlmostEqual(out.iloc[0]["expert_spread"], 100.0)


if __name__ == "__main__":
    unittest.main()
