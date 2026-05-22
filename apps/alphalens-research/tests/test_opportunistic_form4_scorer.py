"""Opportunistic Form-4 scorer — TDD.

Tests cover:
  1. ``aggregate_opportunistic_signal`` — filters records (P/S codes, officer
     /director status, non-routine insiders), aggregates net USD across
     opportunistic insiders, imputes missing prices.
  2. ``score_opportunistic_form4`` — per-asof OLS residual of signal_raw on
     equity controls; same shape as v9D's ``score_cross_sectional_residual``.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd
from alphalens_research.screeners.insider_activity.cohen_malloy_classifier import (
    CohenMalloyLabel,
)
from alphalens_research.screeners.insider_activity.opportunistic_form4 import (
    EQUITY_CONTROLS_FOR_RESIDUAL,
    aggregate_opportunistic_signal,
    score_opportunistic_form4,
)


def _record_row(
    *,
    person_cik: str = "0000000100",
    transaction_date: date = date(2022, 5, 1),
    code: str = "P",
    shares: float = 1000.0,
    price: float | None = 50.0,
    is_officer: bool = True,
    is_director: bool = False,
    is_ten_percent_owner: bool = False,
) -> dict:
    return {
        "reporting_owner_cik": person_cik,
        "transaction_date": transaction_date,
        "transaction_code": code,
        "transaction_shares": shares,
        "transaction_price_per_share": price,
        "is_officer": is_officer,
        "is_director": is_director,
        "is_ten_percent_owner": is_ten_percent_owner,
    }


class _FixedLabelCache:
    """Test double — returns a pre-set label per person_cik, ignores year."""

    def __init__(self, labels: dict[str, CohenMalloyLabel]):
        self._labels = labels

    def get(self, person_cik: str, classification_year: int) -> CohenMalloyLabel:
        return self._labels.get(person_cik, CohenMalloyLabel.UNCLASSIFIED)


class TestAggregateOpportunisticSignal(unittest.TestCase):
    def test_single_opportunistic_buy_sums_to_net_usd(self):
        records = pd.DataFrame([_record_row(price=50.0, shares=1000.0)])
        cache = _FixedLabelCache({"0000000100": CohenMalloyLabel.OPPORTUNISTIC})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 1000.0 * 50.0)

    def test_buy_minus_sell_signed(self):
        records = pd.DataFrame(
            [
                _record_row(person_cik="A", code="P", shares=1000, price=50),  # +50k
                _record_row(person_cik="B", code="S", shares=400, price=75),  # -30k
            ]
        )
        cache = _FixedLabelCache(
            {"A": CohenMalloyLabel.OPPORTUNISTIC, "B": CohenMalloyLabel.OPPORTUNISTIC}
        )
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 50_000.0 - 30_000.0)

    def test_routine_records_dropped(self):
        records = pd.DataFrame(
            [
                _record_row(person_cik="A", shares=1000, price=50),  # opportunistic
                _record_row(person_cik="B", shares=2000, price=50),  # routine — drop
            ]
        )
        cache = _FixedLabelCache(
            {"A": CohenMalloyLabel.OPPORTUNISTIC, "B": CohenMalloyLabel.ROUTINE}
        )
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 1000.0 * 50.0)

    def test_unclassified_records_dropped(self):
        records = pd.DataFrame(
            [
                _record_row(person_cik="A", shares=1000, price=50),
                _record_row(person_cik="C", shares=999, price=50),  # unclassified
            ]
        )
        cache = _FixedLabelCache(
            {"A": CohenMalloyLabel.OPPORTUNISTIC, "C": CohenMalloyLabel.UNCLASSIFIED}
        )
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 1000.0 * 50.0)

    def test_non_PS_codes_dropped(self):
        # Code A (grant) and M (option exercise) are NOT open-market signals.
        records = pd.DataFrame(
            [
                _record_row(person_cik="A", code="P", shares=1000, price=50),  # keep
                _record_row(person_cik="A", code="A", shares=10_000, price=0),  # drop
                _record_row(person_cik="A", code="M", shares=500, price=10),  # drop
            ]
        )
        cache = _FixedLabelCache({"A": CohenMalloyLabel.OPPORTUNISTIC})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 50_000.0)

    def test_only_ten_percent_owner_excluded_unless_also_officer(self):
        # 10% beneficial owner only → excluded per design (not officer/director).
        records = pd.DataFrame(
            [
                _record_row(
                    person_cik="A",
                    is_officer=False,
                    is_director=False,
                    is_ten_percent_owner=True,
                    shares=10_000,
                    price=10,
                ),
            ]
        )
        cache = _FixedLabelCache({"A": CohenMalloyLabel.OPPORTUNISTIC})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertEqual(signal, 0.0)

    def test_ten_percent_owner_who_is_also_officer_kept(self):
        records = pd.DataFrame(
            [
                _record_row(
                    person_cik="A",
                    is_officer=True,
                    is_ten_percent_owner=True,
                    shares=1000,
                    price=50,
                ),
            ]
        )
        cache = _FixedLabelCache({"A": CohenMalloyLabel.OPPORTUNISTIC})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertAlmostEqual(signal, 50_000.0)

    def test_missing_price_imputed_via_close_lookup(self):
        # Test caller can supply a price-imputer callable.
        records = pd.DataFrame([_record_row(person_cik="A", price=None, shares=1000)])
        cache = _FixedLabelCache({"A": CohenMalloyLabel.OPPORTUNISTIC})

        # Imputer returns 42.0 for whatever (ticker, date) it's called with.
        signal = aggregate_opportunistic_signal(
            records,
            asof=date(2022, 6, 1),
            classifier_cache=cache,
            price_imputer=lambda d: 42.0,
        )
        self.assertAlmostEqual(signal, 1000.0 * 42.0)

    def test_missing_price_no_imputer_drops_record(self):
        records = pd.DataFrame([_record_row(person_cik="A", price=None, shares=1000)])
        cache = _FixedLabelCache({"A": CohenMalloyLabel.OPPORTUNISTIC})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache, price_imputer=None
        )
        self.assertEqual(signal, 0.0)

    def test_empty_records_returns_zero(self):
        records = pd.DataFrame(columns=list(_record_row().keys()))
        cache = _FixedLabelCache({})
        signal = aggregate_opportunistic_signal(
            records, asof=date(2022, 6, 1), classifier_cache=cache
        )
        self.assertEqual(signal, 0.0)


class TestScoreOpportunisticForm4(unittest.TestCase):
    # _MIN_ROWS_PER_ASOF was raised 4→15 (sparsity guard 2026-05-08); helper
    # default sized above floor so existing assertions still exercise the
    # regression path.
    def _make_features(self, n_per_asof: int = 20) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        rows = []
        for asof in (date(2022, 1, 31), date(2022, 2, 28)):
            for i in range(n_per_asof):
                rows.append(
                    {
                        "asof": asof,
                        "ticker": f"T{i}",
                        "signal_raw": rng.normal(0.001, 0.01),
                        "reversal_1m": rng.normal(0, 0.05),
                        "momentum_6m": rng.normal(0.05, 0.1),
                        "rv_30d": rng.normal(0.2, 0.05),
                    }
                )
        return pd.DataFrame(rows)

    def test_returns_series_aligned_to_features_index(self):
        features = self._make_features()
        scores = score_opportunistic_form4(features)
        self.assertIsInstance(scores, pd.Series)
        self.assertEqual(len(scores), len(features))
        self.assertEqual(scores.name, "score")

    def test_residual_sums_to_zero_per_asof(self):
        # OLS residuals always sum to zero per regression unit (intercept absorbs mean).
        features = self._make_features()
        scores = score_opportunistic_form4(features)
        scored = features.assign(score=scores).dropna(subset=["score"])
        for _asof, group in scored.groupby("asof"):
            self.assertAlmostEqual(group["score"].sum(), 0.0, places=8)

    def test_nan_in_required_columns_propagates_to_nan_score(self):
        features = self._make_features()
        features.loc[0, "signal_raw"] = np.nan
        features.loc[1, "reversal_1m"] = np.nan
        scores = score_opportunistic_form4(features)
        self.assertTrue(pd.isna(scores.iloc[0]))
        self.assertTrue(pd.isna(scores.iloc[1]))

    def test_too_few_rows_per_asof_yields_nan_scores(self):
        # 2 rows < _MIN_ROWS_PER_ASOF=15 → NaN scores (sparsity guard).
        rows = [
            {
                "asof": date(2022, 1, 31),
                "ticker": "T0",
                "signal_raw": 0.001,
                "reversal_1m": 0.01,
                "momentum_6m": 0.05,
                "rv_30d": 0.2,
            },
            {
                "asof": date(2022, 1, 31),
                "ticker": "T1",
                "signal_raw": 0.002,
                "reversal_1m": -0.01,
                "momentum_6m": 0.06,
                "rv_30d": 0.18,
            },
        ]
        features = pd.DataFrame(rows)
        scores = score_opportunistic_form4(features)
        self.assertTrue(scores.isna().all())

    def test_min_rows_floor_at_15_skips_groups_with_n_below_15(self):
        # Sparsity guard: n=10 < 15 → all-NaN, no regression call.
        # Per zen review 2026-05-08: per-asof OLS on 5-15 rows with 3
        # regressors + intercept is fitting noise (X^T X near-singular by
        # design when the daily R2000 has only ~4-15 opportunistic insiders).
        features = self._make_features(n_per_asof=10)
        scores = score_opportunistic_form4(features)
        self.assertTrue(scores.isna().all())

        # Sanity: at the floor n=15, regression runs (residuals finite).
        features_at_floor = self._make_features(n_per_asof=15)
        scores_at_floor = score_opportunistic_form4(features_at_floor)
        self.assertTrue(scores_at_floor.notna().any())
        self.assertTrue(np.isfinite(scores_at_floor.dropna()).all())

    def test_zero_variance_feature_skips_group(self):
        # rv_30d held constant within an asof group → zero-variance column,
        # X^T X singular regardless of n. Without explicit skip, lstsq
        # produces NaN/Inf beta and overflow on residuals = y - Xb @ beta.
        rng = np.random.default_rng(7)
        rows = []
        for i in range(20):  # n=20, above floor
            rows.append(
                {
                    "asof": date(2022, 5, 31),
                    "ticker": f"T{i}",
                    "signal_raw": rng.normal(0.001, 0.01),
                    "reversal_1m": rng.normal(0, 0.05),
                    "momentum_6m": rng.normal(0.05, 0.1),
                    "rv_30d": 0.2,  # CONSTANT — zero variance
                }
            )
        features = pd.DataFrame(rows)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            # Must not raise FloatingPointError — fix should preempt the
            # ill-conditioned matmul.
            scores = score_opportunistic_form4(features)
        self.assertTrue(scores.isna().all())

    def test_ill_conditioned_features_skip_group(self):
        # Near-collinear features (rare but realistic — reversal_1m vs
        # momentum_6m can correlate strongly in some regimes). Without an
        # explicit condition-number guard, lstsq returns finite-but-
        # astronomical beta whose `Xb @ beta` overflows in float64.
        rng = np.random.default_rng(11)
        rows = []
        # n=20 above floor; non-zero feature std; but momentum_6m is a
        # near-perfect linear function of reversal_1m → cond(X) → ∞.
        for i in range(20):
            r1 = rng.normal(0, 0.05)
            rows.append(
                {
                    "asof": date(2022, 8, 31),
                    "ticker": f"T{i}",
                    "signal_raw": rng.normal(0.001, 0.01),
                    "reversal_1m": r1,
                    "momentum_6m": -2.0 * r1 + 1e-12 * rng.normal(),  # near-collinear
                    "rv_30d": rng.normal(0.2, 0.05),
                }
            )
        features = pd.DataFrame(rows)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            scores = score_opportunistic_form4(features)
        self.assertTrue(scores.isna().all())

    def test_inf_in_signal_raw_filtered_by_valid_mask(self):
        # pandas .notna() returns True for ±inf — only np.isfinite filters
        # them. Upstream signal_raw = net_usd / mcap can produce +inf for
        # micro-cap tickers; valid_mask must drop those rows BEFORE the
        # regression, otherwise lstsq produces overflow in matmul.
        rng = np.random.default_rng(13)
        rows = []
        for i in range(20):
            rows.append(
                {
                    "asof": date(2022, 9, 30),
                    "ticker": f"T{i}",
                    "signal_raw": np.inf if i == 0 else rng.normal(0.001, 0.01),
                    "reversal_1m": rng.normal(0, 0.05),
                    "momentum_6m": rng.normal(0.05, 0.1),
                    "rv_30d": rng.normal(0.2, 0.05),
                }
            )
        features = pd.DataFrame(rows)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            # Must complete without FloatingPointError — the inf row is
            # filtered before any algebraic op touches it.
            scores = score_opportunistic_form4(features)
        # The inf row scores to NaN (excluded by valid_mask).
        self.assertTrue(pd.isna(scores.iloc[0]))
        # Remaining 19 rows are above floor=15 → regression runs → finite scores.
        finite_scores = scores.iloc[1:]
        self.assertTrue(finite_scores.notna().all())
        self.assertTrue(np.isfinite(finite_scores).all())

    def test_neg_inf_in_feature_filtered_by_valid_mask(self):
        # Same gotcha for ±inf in any equity control (e.g., reversal_1m
        # from a corrupted price series with zero divisor).
        rng = np.random.default_rng(17)
        rows = []
        for i in range(20):
            rows.append(
                {
                    "asof": date(2022, 10, 31),
                    "ticker": f"T{i}",
                    "signal_raw": rng.normal(0.001, 0.01),
                    "reversal_1m": -np.inf if i == 5 else rng.normal(0, 0.05),
                    "momentum_6m": rng.normal(0.05, 0.1),
                    "rv_30d": rng.normal(0.2, 0.05),
                }
            )
        features = pd.DataFrame(rows)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            scores = score_opportunistic_form4(features)
        self.assertTrue(pd.isna(scores.iloc[5]))
        # 19 remaining rows above floor=15 → regression OK.
        rest = scores.drop(index=5)
        self.assertTrue(np.isfinite(rest.dropna()).all())

    def test_well_conditioned_group_yields_finite_score(self):
        # Healthy n=20 group with non-degenerate features → finite scores,
        # rank-ordering preserved (residuals from clean OLS).
        features = self._make_features(n_per_asof=20)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            scores = score_opportunistic_form4(features)
        self.assertTrue(scores.notna().any())
        self.assertTrue(np.isfinite(scores.dropna()).all())

    def test_equity_controls_constant_matches_v9D(self):
        self.assertEqual(
            EQUITY_CONTROLS_FOR_RESIDUAL,
            ("reversal_1m", "momentum_6m", "rv_30d"),
        )


if __name__ == "__main__":
    unittest.main()
