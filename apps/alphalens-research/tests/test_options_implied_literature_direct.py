"""Unit tests for v8 literature-direct scorer.

Locks the contract for `score_literature_direct(features)`:
- Score = `-features["ivp30"]` so high score = low IV-percentile = LOW-IV
  names = LONG leg per Xing 2010 negative-IV-return prior.
- NaN ivp30 → NaN score (no silent zero-fill).
- Index preserved verbatim — feeds the same `_portfolio_returns` path used
  by v7 (which does `feat_holdout.assign(_score=scores)`).
- Top-decile selection by descending score recovers LOWEST-ivp30 names.
- Missing `ivp30` column raises KeyError (defensive contract — the v7
  feature joiner always emits ivp30 by construction; v8 must FAIL LOUD
  if upstream changes silently drop it).
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd
from alphalens_research.screeners.options_implied import score_literature_direct


def _features(rows: list[dict]) -> pd.DataFrame:
    """Materialize a feature frame in the shape `build_feature_frame` returns."""
    return pd.DataFrame(rows)


class ScoreLiteratureDirectTests(unittest.TestCase):
    def test_score_is_negation_of_ivp30(self):
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivp30": 10.0},
                {"asof": "2024-05-01", "ticker": "B", "ivp30": 50.0},
                {"asof": "2024-05-01", "ticker": "C", "ivp30": 90.0},
            ]
        )
        scores = score_literature_direct(feats)
        self.assertEqual(list(scores), [-10.0, -50.0, -90.0])

    def test_score_index_matches_features_index(self):
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivp30": 25.0},
                {"asof": "2024-05-01", "ticker": "B", "ivp30": 75.0},
            ]
        )
        feats = feats.set_index(pd.Index([42, 99], name="row_id"))
        scores = score_literature_direct(feats)
        self.assertEqual(list(scores.index), [42, 99])
        self.assertEqual(scores.index.name, "row_id")

    def test_nan_ivp30_propagates_to_nan_score(self):
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivp30": 30.0},
                {"asof": "2024-05-01", "ticker": "B", "ivp30": float("nan")},
                {"asof": "2024-05-01", "ticker": "C", "ivp30": 70.0},
            ]
        )
        scores = score_literature_direct(feats)
        self.assertEqual(scores.iloc[0], -30.0)
        self.assertTrue(math.isnan(scores.iloc[1]))
        self.assertEqual(scores.iloc[2], -70.0)

    def test_empty_features_returns_empty_series(self):
        feats = pd.DataFrame({"ivp30": pd.Series(dtype=float)})
        scores = score_literature_direct(feats)
        self.assertEqual(len(scores), 0)
        self.assertEqual(scores.dtype, float)

    def test_single_row_features(self):
        feats = _features([{"asof": "2024-05-01", "ticker": "A", "ivp30": 42.0}])
        scores = score_literature_direct(feats)
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores.iloc[0], -42.0)

    def test_missing_ivp30_column_raises_keyerror(self):
        """Defensive contract: build_feature_frame guarantees ivp30 in output;
        any caller passing a frame without it should fail loud, not silent.
        """
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivx30": 0.25, "ivp30_typo": 30.0},
            ]
        )
        with self.assertRaises(KeyError):
            score_literature_direct(feats)

    def test_top_decile_selection_picks_lowest_ivp30(self):
        """End-to-end check that the score wiring matches the engine's
        top_n=descending-by-score convention used by v7's _portfolio_returns.
        """
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "LOW1", "ivp30": 5.0},
                {"asof": "2024-05-01", "ticker": "LOW2", "ivp30": 10.0},
                {"asof": "2024-05-01", "ticker": "MID", "ivp30": 50.0},
                {"asof": "2024-05-01", "ticker": "HIGH1", "ivp30": 90.0},
                {"asof": "2024-05-01", "ticker": "HIGH2", "ivp30": 95.0},
            ]
        )
        scores = score_literature_direct(feats)
        # Top-2 by score (descending) must be the two lowest-ivp30 names.
        top2_idx = scores.sort_values(ascending=False).head(2).index
        top2_tickers = feats.loc[top2_idx, "ticker"].tolist()
        self.assertEqual(set(top2_tickers), {"LOW1", "LOW2"})

    def test_dtype_is_float(self):
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivp30": 10},
                {"asof": "2024-05-01", "ticker": "B", "ivp30": 50},
            ]
        )
        scores = score_literature_direct(feats)
        self.assertTrue(np.issubdtype(scores.dtype, np.floating))

    def test_score_name_is_score(self):
        """Series name = 'score' — matches v7 predict_scores convention so
        downstream code (verdict, multi-phase audit) can rely on it.
        """
        feats = _features([{"asof": "2024-05-01", "ticker": "A", "ivp30": 30.0}])
        scores = score_literature_direct(feats)
        self.assertEqual(scores.name, "score")

    def test_deterministic_across_repeat_calls(self):
        feats = _features(
            [
                {"asof": "2024-05-01", "ticker": "A", "ivp30": 12.5},
                {"asof": "2024-05-01", "ticker": "B", "ivp30": 87.5},
            ]
        )
        s1 = score_literature_direct(feats)
        s2 = score_literature_direct(feats)
        pd.testing.assert_series_equal(s1, s2)


if __name__ == "__main__":
    unittest.main()
