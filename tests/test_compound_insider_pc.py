"""Tests for the insider_form4 x pc_abnormal_volume compound scorer.

Pre-reg: insider_pc_compound_2026_05_10. The locked design memo is
docs/research/insider_pc_compound_design_2026_05_10.md.

These tests exercise the pure cross-sectional math:
  - per-component z-score (NaN-preserving, infinity filtering, degenerate-safe)
  - strict-intersection equal-weight average

Per zen review 2026-05-10: degenerate single-component cases (sigma=0, n<2)
must NOT propagate NaN through the intersection — that would discard the
asof entirely and waste the OTHER component's signal. They return zeros
("neutral cross-sectional preference") so the second component drives the
compound score for that asof.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from alphalens.screeners.compound_insider_pc import compound_score_from_components
from alphalens.screeners.compound_insider_pc.zscore_compound import _xsec_zscore


class TestXsecZscore(unittest.TestCase):
    def test_two_inputs_zero_mean_unit_std(self):
        s = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        z = _xsec_zscore(s)
        self.assertAlmostEqual(z.mean(), 0.0, places=12)
        self.assertAlmostEqual(z.std(ddof=1), 1.0, places=12)

    def test_constant_returns_zeros(self):
        # sigma == 0 -> neutralize, do NOT NaN out (per zen review)
        s = pd.Series([5.0, 5.0, 5.0], index=["A", "B", "C"])
        z = _xsec_zscore(s)
        np.testing.assert_array_equal(z.to_numpy(), np.zeros(3))

    def test_singleton_returns_zero(self):
        # n < 2 -> neutralize
        s = pd.Series([5.0], index=["A"])
        z = _xsec_zscore(s)
        self.assertEqual(z.loc["A"], 0.0)

    def test_propagates_nan(self):
        s = pd.Series([1.0, np.nan, 3.0], index=["A", "B", "C"])
        z = _xsec_zscore(s)
        # NaN ticker stays NaN
        self.assertTrue(np.isnan(z.loc["B"]))
        # finite tickers are z-scored using only the finite subset
        finite = z.dropna()
        self.assertAlmostEqual(finite.mean(), 0.0, places=12)
        self.assertAlmostEqual(finite.std(ddof=1), 1.0, places=12)

    def test_filters_inf(self):
        # +/- inf must be treated as invalid (NOT NaN per pandas notna gotcha)
        s = pd.Series([1.0, np.inf, 3.0, -np.inf, 5.0], index=list("ABCDE"))
        z = _xsec_zscore(s)
        self.assertTrue(np.isnan(z.loc["B"]))
        self.assertTrue(np.isnan(z.loc["D"]))
        finite = z.dropna()
        self.assertAlmostEqual(finite.mean(), 0.0, places=12)
        self.assertAlmostEqual(finite.std(ddof=1), 1.0, places=12)


class TestCompoundScore(unittest.TestCase):
    def test_strict_intersection(self):
        f4 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        pc = pd.Series([4.0, 5.0, 6.0], index=["B", "C", "D"])
        out = compound_score_from_components(f4, pc)
        self.assertEqual(set(out.index), {"B", "C"})

    def test_equal_weight_average(self):
        # Manually crafted z-scores: f4 = [-1, 0, +1], pc = [+1, 0, -1]
        # -> compound = 0 for all three
        f4 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        pc = pd.Series([3.0, 2.0, 1.0], index=["A", "B", "C"])
        out = compound_score_from_components(f4, pc)
        np.testing.assert_allclose(out.to_numpy(), np.zeros(3), atol=1e-12)

    def test_empty_intersection_returns_empty_series(self):
        f4 = pd.Series([1.0, 2.0], index=["A", "B"])
        pc = pd.Series([3.0, 4.0], index=["C", "D"])
        out = compound_score_from_components(f4, pc)
        self.assertTrue(out.empty)
        self.assertEqual(out.dtype, np.float64)

    def test_orthogonal_signals_preserve_separation(self):
        # f4 sorts ABCD ascending; pc sorts ABCD descending; compound = flat (~0)
        f4 = pd.Series([1.0, 2.0, 3.0, 4.0], index=list("ABCD"))
        pc = pd.Series([4.0, 3.0, 2.0, 1.0], index=list("ABCD"))
        out = compound_score_from_components(f4, pc)
        np.testing.assert_allclose(out.to_numpy(), np.zeros(4), atol=1e-12)

    def test_score_name_is_score(self):
        f4 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        pc = pd.Series([3.0, 2.0, 1.0], index=["A", "B", "C"])
        out = compound_score_from_components(f4, pc)
        self.assertEqual(out.name, "score")

    def test_index_misalignment_handled(self):
        # Same tickers but shuffled index order -> output respects pandas alignment
        f4 = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        pc = pd.Series([1.0, 2.0, 3.0], index=["C", "B", "A"])  # reverse
        out = compound_score_from_components(f4, pc)
        # Both components z-score to [-1, 0, +1] in their OWN order:
        #   f4: A=-1, B=0, C=+1
        #   pc: C=-1, B=0, A=+1
        # Compound (per ticker): A=0, B=0, C=0 -> all zeros
        self.assertEqual(set(out.index), {"A", "B", "C"})
        np.testing.assert_allclose(out.sort_index().to_numpy(), np.zeros(3), atol=1e-12)

    def test_one_sided_singleton(self):
        # f4 has 1 ticker, pc has many. Intersection = {A}.
        # f4 alone -> n<2 -> z=0 for A. pc has many -> proper z-scores.
        # Compound on intersection {A}: (0 + z_pc[A]) / 2 = z_pc[A] / 2.
        f4 = pd.Series([5.0], index=["A"])
        pc_vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        pc = pd.Series(pc_vals, index=list("ABCDE"))
        z_pc_a = (pc_vals[0] - np.mean(pc_vals)) / np.std(pc_vals, ddof=1)
        out = compound_score_from_components(f4, pc)
        self.assertEqual(set(out.index), {"A"})
        self.assertAlmostEqual(out.loc["A"], z_pc_a / 2.0, places=12)

    def test_one_component_degenerate_uses_other(self):
        # f4 fully degenerate (constant) -> all zeros (NOT NaN).
        # pc has real signal. Compound = (0 + z_pc) / 2 over intersection.
        # Critical: degenerate component must NOT zero the asof out.
        f4 = pd.Series([7.0, 7.0, 7.0, 7.0], index=list("ABCD"))
        pc_vals = [1.0, 2.0, 3.0, 4.0]
        pc = pd.Series(pc_vals, index=list("ABCD"))
        out = compound_score_from_components(f4, pc)
        self.assertEqual(set(out.index), {"A", "B", "C", "D"})
        z_pc = (np.array(pc_vals) - np.mean(pc_vals)) / np.std(pc_vals, ddof=1)
        np.testing.assert_allclose(out.sort_index().to_numpy(), z_pc / 2.0, atol=1e-12)


class TestCompoundInsiderPcScorerContract(unittest.TestCase):
    """Contract test for the BacktestEngine adapter wrapper.

    Exercises the composition glue (DataFrame -> Series -> compound -> DataFrame)
    without spinning up real Form-4 / iVolatility stores. Locks the public
    output contract: a DataFrame with exactly the columns ['ticker', 'score']
    and no NaN scores. Catches future drift in shape conversion that the
    smoke run alone might miss.
    """

    def test_call_returns_strict_intersection_with_required_columns(self):
        from unittest.mock import patch

        import scripts.experiment_insider_pc_compound as mod

        scorer = mod._CompoundInsiderPcScorer.__new__(mod._CompoundInsiderPcScorer)

        # Form-4 inner: tickers {A, B, C} score range
        scorer._form4_inner = lambda histories, cfg: pd.DataFrame(
            {"ticker": ["A", "B", "C"], "score": [1.0, 2.0, 3.0]}
        )
        scorer._smd_loader = lambda t: None  # bypassed by the build_feature_frame patch

        pc_features_stub = pd.DataFrame(
            {
                "asof": ["2020-01-21", "2020-01-21"],
                "ticker": ["B", "C"],
                "abnormal_pcr": [0.1, 0.2],
                "reversal_1m": [0.0, 0.0],
                "momentum_6m": [0.0, 0.0],
                "rv_30d": [0.01, 0.01],
            }
        )
        pc_scores_stub = pd.Series([0.5, -0.5], index=pc_features_stub.index, name="score")

        histories = {
            "A": pd.DataFrame({"close": [10.0]}, index=[pd.Timestamp("2020-01-21")]),
        }

        with (
            patch.object(mod, "build_feature_frame", return_value=pc_features_stub),
            patch.object(mod, "score_pc_abnormal_residual", return_value=pc_scores_stub),
        ):
            out = scorer(histories, config={"asof": pd.Timestamp("2020-01-21")})

        # Strict intersection of {A,B,C} (Form-4) ∩ {B,C} (P/C) = {B, C}
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(list(out.columns), ["ticker", "score"])
        self.assertEqual(set(out["ticker"]), {"B", "C"})
        self.assertFalse(out["score"].isna().any())

    def test_call_returns_empty_frame_when_form4_empty(self):
        import scripts.experiment_insider_pc_compound as mod

        scorer = mod._CompoundInsiderPcScorer.__new__(mod._CompoundInsiderPcScorer)
        scorer._form4_inner = lambda histories, cfg: pd.DataFrame(columns=["ticker", "score"])
        scorer._smd_loader = lambda t: None

        out = scorer({"A": pd.DataFrame()}, config={"asof": pd.Timestamp("2020-01-21")})
        self.assertEqual(list(out.columns), ["ticker", "score"])
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
