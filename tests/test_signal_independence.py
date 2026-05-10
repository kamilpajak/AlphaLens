"""TDD coverage for signal-independence pre-screen.

Used in compound-experiment design: before registering a 2-component (or N-component)
compound test, verify the components are sufficiently orthogonal that combining
them carries independent information rather than just doubling the same signal.

Decision rule (locked, per session 2026-05-10 plan + zen review):
- mean cross-sectional Spearman ρ ∈ [-0.5, 0.5] → orthogonal-enough → PROCEED
- mean ρ > 0.5 → redundant (latent common factor) → REJECT
- mean ρ < -0.5 → degenerate / sign-flip → ABORT (one scorer probably flipped sign)

Bug-fix discipline carried over from PR #88: cover all edge cases (NaN inputs,
length mismatches, short calendars, sign-flip detection, zero-variance inputs)
before trusting any verdict for a load-bearing pre-reg decision.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from alphalens.attribution.signal_independence import (
    IndependenceVerdict,
    classify_independence,
    pairwise_rank_ic_correlation,
)


def _make_scorer_panel(scores_per_asof: dict[pd.Timestamp, dict[str, float]]) -> pd.DataFrame:
    """Build a long-format scorer output panel from {asof -> {ticker -> score}}."""
    rows = []
    for asof, ticker_scores in scores_per_asof.items():
        for ticker, score in ticker_scores.items():
            rows.append({"asof": asof, "ticker": ticker, "score": score})
    return pd.DataFrame(rows)


class TestPairwiseRankIcCorrelation(unittest.TestCase):
    def test_perfectly_correlated_returns_rho_one(self):
        # Identical scores → rho ≈ 1.0
        rng = np.random.default_rng(0)
        asofs = pd.date_range("2015-01-01", periods=20, freq="MS")
        scorer_a_data = {}
        scorer_b_data = {}
        for asof in asofs:
            scores = rng.normal(0, 1, 50)
            tickers = [f"T{i:03d}" for i in range(50)]
            scorer_a_data[asof] = dict(zip(tickers, scores, strict=True))
            scorer_b_data[asof] = dict(zip(tickers, scores, strict=True))
        a = _make_scorer_panel(scorer_a_data)
        b = _make_scorer_panel(scorer_b_data)

        result = pairwise_rank_ic_correlation(a, b)

        self.assertGreater(result.mean_rho, 0.95)
        self.assertEqual(len(result.per_asof_rhos), len(asofs))

    def test_perfectly_anticorrelated_returns_rho_neg_one(self):
        rng = np.random.default_rng(1)
        asofs = pd.date_range("2015-01-01", periods=20, freq="MS")
        scorer_a_data = {}
        scorer_b_data = {}
        for asof in asofs:
            scores = rng.normal(0, 1, 50)
            tickers = [f"T{i:03d}" for i in range(50)]
            scorer_a_data[asof] = dict(zip(tickers, scores, strict=True))
            scorer_b_data[asof] = dict(zip(tickers, -scores, strict=True))
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data), _make_scorer_panel(scorer_b_data)
        )
        self.assertLess(result.mean_rho, -0.95)

    def test_independent_scores_returns_rho_near_zero(self):
        rng = np.random.default_rng(2)
        asofs = pd.date_range("2015-01-01", periods=40, freq="MS")
        scorer_a_data = {}
        scorer_b_data = {}
        for asof in asofs:
            tickers = [f"T{i:03d}" for i in range(100)]
            scorer_a_data[asof] = dict(zip(tickers, rng.normal(0, 1, 100), strict=True))
            scorer_b_data[asof] = dict(zip(tickers, rng.normal(0, 1, 100), strict=True))
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data), _make_scorer_panel(scorer_b_data)
        )
        # 40 asofs × 100 tickers should yield rho_mean tightly around 0 (|rho_mean| < 0.05 with high probability)
        self.assertLess(abs(result.mean_rho), 0.10)

    def test_intersection_only_skips_unique_tickers(self):
        # Scorer A covers tickers A,B,C,D; Scorer B covers C,D,E,F.
        # Intersection is {C,D}. Per-asof correlation computed on that pair only.
        asofs = [pd.Timestamp("2015-01-01"), pd.Timestamp("2015-02-01")]
        scorer_a_data = {
            asofs[0]: {"A": 0.5, "B": -0.5, "C": 1.0, "D": -1.0},
            asofs[1]: {"A": 0.3, "B": -0.3, "C": 0.8, "D": -0.8},
        }
        scorer_b_data = {
            asofs[0]: {"C": 1.0, "D": -1.0, "E": 0.5, "F": -0.5},
            asofs[1]: {"C": 0.8, "D": -0.8, "E": 0.3, "F": -0.3},
        }
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data),
            _make_scorer_panel(scorer_b_data),
            min_intersection=2,
            min_asofs=1,
        )
        # Both asofs: C ranks higher than D in both → ρ = 1.0 (max with N=2)
        self.assertGreater(result.mean_rho, 0.99)

    def test_handles_nan_scores(self):
        # NaN scores are dropped pairwise; correlation computed on valid pairs only
        asofs = [pd.Timestamp("2015-01-01")]
        scorer_a_data = {asofs[0]: {"A": 1.0, "B": np.nan, "C": 0.5, "D": -0.5, "E": 0.0}}
        scorer_b_data = {asofs[0]: {"A": 1.0, "B": 0.0, "C": 0.5, "D": -0.5, "E": np.nan}}
        # Intersection-with-non-NaN: A, C, D (3 tickers)
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data),
            _make_scorer_panel(scorer_b_data),
            min_intersection=3,
            min_asofs=1,
        )
        # All three values rank-match perfectly: A>C>D in both → ρ = 1.0
        self.assertAlmostEqual(result.per_asof_rhos.iloc[0], 1.0, places=4)

    def test_empty_intersection_per_asof_yields_nan_dropped(self):
        # Two asofs: one with intersection, one without.
        asofs = [pd.Timestamp("2015-01-01"), pd.Timestamp("2015-02-01")]
        # Asof 1: 5 common tickers, identical → ρ=1
        # Asof 2: NO common tickers → ρ=NaN (dropped from mean)
        scorer_a_data = {
            asofs[0]: {f"T{i}": float(i) for i in range(5)},
            asofs[1]: {"X": 1.0, "Y": -1.0},
        }
        scorer_b_data = {
            asofs[0]: {f"T{i}": float(i) for i in range(5)},
            asofs[1]: {"M": 1.0, "N": -1.0},
        }
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data),
            _make_scorer_panel(scorer_b_data),
            min_intersection=5,
            min_asofs=1,
        )
        # Only asof 1 contributes: ρ=1.0
        self.assertAlmostEqual(result.mean_rho, 1.0, places=4)
        self.assertEqual(result.n_asofs_with_valid_rho, 1)

    def test_minimum_intersection_size_filter(self):
        # When per-asof intersection has < min_intersection tickers, that asof's ρ is NaN.
        asofs = [pd.Timestamp("2015-01-01"), pd.Timestamp("2015-02-01")]
        # Asof 1: 2 common tickers (below min_intersection=5) → dropped
        # Asof 2: 5 common tickers → kept
        scorer_a_data = {
            asofs[0]: {"A": 1.0, "B": 0.0},
            asofs[1]: {f"T{i}": float(i) for i in range(5)},
        }
        scorer_b_data = {
            asofs[0]: {"A": 1.0, "B": 0.0},
            asofs[1]: {f"T{i}": float(i) for i in range(5)},
        }
        result = pairwise_rank_ic_correlation(
            _make_scorer_panel(scorer_a_data),
            _make_scorer_panel(scorer_b_data),
            min_intersection=5,
            min_asofs=1,
        )
        # Only asof 2 contributes (asof 1 below min_intersection=5)
        self.assertEqual(result.n_asofs_with_valid_rho, 1)

    def test_too_few_valid_asofs_raises(self):
        # If after filtering only < min_asofs asofs have valid ρ, raise ValueError
        asofs = [pd.Timestamp("2015-01-01")]
        scorer_a_data = {asofs[0]: {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}}
        scorer_b_data = {asofs[0]: {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}}
        with self.assertRaisesRegex(ValueError, "at least"):
            pairwise_rank_ic_correlation(
                _make_scorer_panel(scorer_a_data),
                _make_scorer_panel(scorer_b_data),
                min_asofs=5,
            )

    def test_required_columns_missing_raises(self):
        bad = pd.DataFrame({"date": [pd.Timestamp("2015-01-01")], "name": ["A"], "value": [1.0]})
        good = _make_scorer_panel({pd.Timestamp("2015-01-01"): {"A": 1.0, "B": 2.0}})
        with self.assertRaisesRegex(ValueError, "asof|ticker|score"):
            pairwise_rank_ic_correlation(bad, good)


class TestClassifyIndependence(unittest.TestCase):
    """Bug-fix epicenter: must handle all sign + magnitude cases of mean ρ."""

    def _make_result(self, mean_rho, n_asofs=20, std_rho=0.1):
        per_asof = pd.Series(np.random.default_rng(0).normal(mean_rho, std_rho, n_asofs))
        return type(
            "R",
            (),
            {
                "mean_rho": float(mean_rho),
                "per_asof_rhos": per_asof,
                "n_asofs_with_valid_rho": int(n_asofs),
                "t_stat": float(mean_rho / (std_rho / math.sqrt(n_asofs)))
                if std_rho > 0
                else float("inf"),
            },
        )()

    def test_orthogonal_proceeds(self):
        v = classify_independence(self._make_result(mean_rho=0.0))
        self.assertEqual(v.classification, "orthogonal")
        self.assertTrue(v.proceed)

    def test_weakly_correlated_proceeds(self):
        v = classify_independence(self._make_result(mean_rho=0.3))
        self.assertEqual(v.classification, "orthogonal")
        self.assertTrue(v.proceed)

    def test_at_threshold_proceeds_inclusive(self):
        # rho exactly at +0.5 → still PROCEED (inclusive boundary)
        v = classify_independence(self._make_result(mean_rho=0.5))
        self.assertEqual(v.classification, "orthogonal")
        self.assertTrue(v.proceed)

    def test_above_threshold_rejects_redundant(self):
        v = classify_independence(self._make_result(mean_rho=0.7))
        self.assertEqual(v.classification, "REDUNDANT (latent common factor)")
        self.assertFalse(v.proceed)

    def test_strongly_correlated_rejects(self):
        v = classify_independence(self._make_result(mean_rho=0.9))
        self.assertEqual(v.classification, "REDUNDANT (latent common factor)")
        self.assertFalse(v.proceed)

    def test_anti_correlated_at_threshold_proceeds(self):
        # rho = -0.5 (inclusive) still PROCEED
        v = classify_independence(self._make_result(mean_rho=-0.5))
        self.assertEqual(v.classification, "orthogonal")
        self.assertTrue(v.proceed)

    def test_strongly_anti_correlated_aborts(self):
        v = classify_independence(self._make_result(mean_rho=-0.7))
        self.assertEqual(v.classification, "DEGENERATE (sign-flip suspected)")
        self.assertIsNone(v.proceed)

    def test_returns_dataclass_with_required_fields(self):
        v = classify_independence(self._make_result(mean_rho=0.0))
        self.assertIsInstance(v, IndependenceVerdict)
        self.assertIsInstance(v.mean_rho, float)
        self.assertIsInstance(v.classification, str)
        self.assertIsInstance(v.rationale, str)
        self.assertIn(v.proceed, (True, False, None))

    def test_rationale_quotes_threshold(self):
        v = classify_independence(self._make_result(mean_rho=0.7))
        self.assertIn("0.5", v.rationale)


if __name__ == "__main__":
    unittest.main()
