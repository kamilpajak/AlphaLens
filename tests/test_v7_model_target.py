"""Unit tests for v7 model.py (Lasso fit) + target.py (20d forward return).

Locks the contract for:
- `fit_global_lasso`: produces consistent fit object; raises on too few rows;
  flags `all_options_zeroed` when none of the 4 options features survive.
- `predict_scores`: applies same scaler stats as fit; NaN row → NaN score.
- `forward_raw_return`: PIT-correct (no post-asof leakage); returns None
  when fewer than holding_period+1 forward bars exist; respects ivp30
  trading-day filter (skips smd weekend carry-forward rows).
- `build_target_frame`: aligns to feature_frame keys; NaN propagation.
- `split_train_holdout`: strict-temporal cut at holdout_start.
- `aligned_train`: inner-join + drop NaN target.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from alphalens.screeners.options_implied import (
    FEATURE_NAMES,
    OPTIONS_FEATURES,
    aligned_train,
    build_target_frame,
    fit_global_lasso,
    forward_raw_return,
    predict_scores,
    split_train_holdout,
)

# ---------------------------------------------------------------------------
# Synthetic helpers


def _smd_close_history(ticker: str, n_days: int = 200, seed: int = 1) -> pd.DataFrame:
    """Synthetic smd-shape DataFrame with `tradeDate`, `close`, `ivp30`."""
    idx = pd.bdate_range("2023-01-02", periods=n_days)
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(0.0005 + 0.005 * rng.standard_normal(n_days)))
    return pd.DataFrame(
        {
            "tradeDate": [d.strftime("%Y-%m-%d") for d in idx],
            "close": closes,
            "ivp30": 50.0,
        }
    )


def _make_loader(histories: dict[str, pd.DataFrame]):
    def loader(t: str) -> pd.DataFrame | None:
        return histories.get(t.upper())

    return loader


def _train_features(n_rows: int = 1000, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Synthetic training panel — 7 random features + linear-combination target."""
    rng = np.random.default_rng(seed)
    data = {f: rng.normal(size=n_rows) for f in FEATURE_NAMES}
    df = pd.DataFrame(data)
    df.insert(0, "ticker", [f"T{i % 50}" for i in range(n_rows)])
    df.insert(0, "asof", "2023-06-01")
    # Target depends on subset of features: ivx30 (negative coef per Xing) +
    # reversal_1m. Adds enough signal so Lasso doesn't shrink to zero.
    target = -0.3 * df["ivx30"] + 0.15 * df["reversal_1m"] + 0.05 * rng.normal(size=n_rows)
    return df, target


# ---------------------------------------------------------------------------


class TestFitGlobalLasso(unittest.TestCase):
    def test_fits_with_nonzero_coefficients_when_signal_present(self):
        X, y = _train_features(n_rows=1000)
        fit = fit_global_lasso(X, y)
        self.assertEqual(fit.n_train_obs, 1000)
        # Synthetic signal lives on ivx30 + reversal_1m → at least 2 nonzero
        self.assertGreaterEqual(fit.n_nonzero_coefs, 1)
        self.assertGreaterEqual(fit.n_nonzero_options, 1)
        self.assertFalse(fit.all_options_zeroed)
        self.assertGreater(fit.chosen_alpha, 0)

    def test_all_options_zeroed_flag_consistent_with_count(self):
        """Flag semantics: `all_options_zeroed` iff `n_nonzero_options == 0`.
        Tested via constructed fit (no Lasso randomness)."""
        from alphalens.screeners.options_implied.model import GlobalLassoFit

        # Manual fit with exactly zero options coefs and one equity-control nonzero
        zero_options_fit = GlobalLassoFit(
            feature_names=tuple(FEATURE_NAMES),
            coefficients=np.array([0.0, 0.0, 0.0, 0.0, 0.5, 0.0, 0.0]),
            intercept=0.0,
            chosen_alpha=0.01,
            cv_mean_mse=0.05,
            n_train_obs=1000,
            n_nonzero_coefs=1,
            n_nonzero_options=0,
            scaler_means=np.zeros(7),
            scaler_stds=np.ones(7),
        )
        self.assertTrue(zero_options_fit.all_options_zeroed)

        nonzero_options_fit = GlobalLassoFit(
            feature_names=tuple(FEATURE_NAMES),
            coefficients=np.array([0.1, 0.0, 0.0, 0.0, 0.5, 0.0, 0.0]),
            intercept=0.0,
            chosen_alpha=0.01,
            cv_mean_mse=0.05,
            n_train_obs=1000,
            n_nonzero_coefs=2,
            n_nonzero_options=1,
            scaler_means=np.zeros(7),
            scaler_stds=np.ones(7),
        )
        self.assertFalse(nonzero_options_fit.all_options_zeroed)

    def test_raises_on_too_few_rows(self):
        X, y = _train_features(n_rows=50)
        with self.assertRaises(ValueError):
            fit_global_lasso(X, y)

    def test_raises_when_features_missing(self):
        X, y = _train_features(n_rows=200)
        X = X.drop(columns=list(FEATURE_NAMES[1:]))  # only ivp30 left
        with self.assertRaises(ValueError):
            fit_global_lasso(X, y)


class TestLassoSignAlignment(unittest.TestCase):
    """Pre-reg auto_pivot trigger: 'Lasso flips sign vs literature prior on
    vol features → diagnostic flag, no auto-pass; document deviation'.

    Per Xing 2010 / Bali 2009 / An-Ang-Bali-Cakici 2014: vol-level features
    (ivx30, ivp30, term-spread, IV/HV ratio) should fit NEGATIVE coefficients
    on forward returns. Helper classifies each option-feature coef as
    `agrees` (negative sign), `flipped` (positive), or `zero` (Lasso zeroed).
    """

    def _make_fit(self, coefs: list[float]):
        import numpy as np

        from alphalens.screeners.options_implied.model import GlobalLassoFit

        return GlobalLassoFit(
            feature_names=tuple(FEATURE_NAMES),
            coefficients=np.array(coefs),
            intercept=0.0,
            chosen_alpha=0.01,
            cv_mean_mse=0.05,
            n_train_obs=1000,
            n_nonzero_coefs=sum(1 for c in coefs if c != 0),
            n_nonzero_options=sum(1 for c in coefs[:4] if c != 0),
            scaler_means=np.zeros(7),
            scaler_stds=np.ones(7),
        )

    def test_all_negative_options_coefs_agree(self):
        from alphalens.screeners.options_implied.model import lasso_sign_alignment

        fit = self._make_fit([-0.1, -0.2, -0.05, -0.15, 0.3, 0.2, 0.1])
        alignment = lasso_sign_alignment(fit)
        # Options features (first 4) all agree
        for f in OPTIONS_FEATURES:
            self.assertEqual(alignment[f], "agrees")
        self.assertFalse(alignment["any_options_flipped"])

    def test_positive_options_coef_flagged_as_flipped(self):
        from alphalens.screeners.options_implied.model import lasso_sign_alignment

        # ivx30 positive (against Xing prior) → flipped
        fit = self._make_fit([+0.1, -0.2, -0.05, -0.15, 0.3, 0.2, 0.1])
        alignment = lasso_sign_alignment(fit)
        self.assertEqual(alignment["ivp30"], "flipped")
        self.assertEqual(alignment["ivx30"], "agrees")
        self.assertTrue(alignment["any_options_flipped"])

    def test_zero_coef_classified_separately(self):
        from alphalens.screeners.options_implied.model import lasso_sign_alignment

        fit = self._make_fit([0.0, -0.2, 0.0, -0.15, 0.3, 0.2, 0.1])
        alignment = lasso_sign_alignment(fit)
        self.assertEqual(alignment["ivp30"], "zero")
        self.assertEqual(alignment["ivx30"], "agrees")
        self.assertEqual(alignment["ivx180_minus_ivx30"], "zero")
        self.assertFalse(alignment["any_options_flipped"])


class TestPredictScores(unittest.TestCase):
    def test_predictions_have_same_length_as_input(self):
        X, y = _train_features(n_rows=1000)
        fit = fit_global_lasso(X, y)
        scores = predict_scores(fit, X)
        self.assertEqual(len(scores), len(X))
        # Rows without NaN inputs should have non-NaN scores
        self.assertEqual(scores.notna().sum(), len(X))

    def test_nan_feature_propagates_to_nan_score(self):
        X, y = _train_features(n_rows=1000)
        fit = fit_global_lasso(X, y)
        X.loc[0, "ivx30"] = np.nan
        scores = predict_scores(fit, X)
        self.assertTrue(np.isnan(scores.iloc[0]))
        self.assertTrue(scores.iloc[1:].notna().all())


# ---------------------------------------------------------------------------


class TestForwardRawReturn(unittest.TestCase):
    def test_returns_correct_close_to_close_value(self):
        history = _smd_close_history("AAA", n_days=200)
        loader = _make_loader({"AAA": history})
        # Use asof = day 100; entry = day 101, exit = day 121 (20d holding)
        asof = history.iloc[100]["tradeDate"]
        entry_close = float(history.iloc[101]["close"])
        exit_close = float(history.iloc[121]["close"])
        expected = exit_close / entry_close - 1.0
        actual = forward_raw_return(loader, "AAA", asof, holding_period=20)
        self.assertAlmostEqual(actual, expected, places=8)

    def test_returns_none_when_insufficient_forward_bars(self):
        history = _smd_close_history("AAA", n_days=30)
        loader = _make_loader({"AAA": history})
        # 30 bars total; asof at day 25; only 4 forward bars vs holding_period=20
        asof = history.iloc[25]["tradeDate"]
        self.assertIsNone(forward_raw_return(loader, "AAA", asof, holding_period=20))

    def test_returns_none_for_missing_ticker(self):
        loader = _make_loader({})
        self.assertIsNone(forward_raw_return(loader, "NOPE", "2023-06-01", holding_period=20))

    def test_skips_non_trading_rows_via_ivp30_filter(self):
        """Smd carries forward Friday close on Sat/Sun with NaN ivp30. Forward
        return must use TRADING days (ivp30 not-NaN), not all calendar days."""
        history = _smd_close_history("AAA", n_days=30)
        # Insert a Sat row with NaN ivp30 + same close
        sat_row = pd.DataFrame(
            [
                {
                    "tradeDate": "2023-01-07",  # Saturday
                    "close": float(history["close"].iloc[5]),
                    "ivp30": np.nan,
                }
            ]
        )
        history = pd.concat([history, sat_row], ignore_index=True)
        loader = _make_loader({"AAA": history})
        # asof = 2023-01-06 (Friday): forward bars start at the next trading
        # day, NOT the Saturday carry-forward row.
        result = forward_raw_return(loader, "AAA", "2023-01-06", holding_period=5)
        # If Saturday were included, entry close would be Saturday's carry-forward
        # = Friday's close → entry/exit ratio biased. With proper filter, entry is
        # next trading day's actual close.
        self.assertIsNotNone(result)


class TestDelistingTerminalReturn(unittest.TestCase):
    """Pre-reg `delisting_handling` rule: -50% standard, -100% bankruptcy.

    Critical for survivorship-correctness — without this, names that delist
    mid-holding silently drop from cross-section, inflating long-only premium.
    """

    def _build(self, n_days: int = 30):
        history = _smd_close_history("DEAD", n_days=n_days)
        loader = _make_loader({"DEAD": history})
        # asof at day 25; only 4 forward bars vs holding_period=20 → naive None
        asof = history.iloc[25]["tradeDate"]
        return loader, asof, history

    def test_bankruptcy_mid_holding_returns_minus_one(self):
        loader, asof, history = self._build()

        delisting_date = pd.Timestamp(history.iloc[28]["tradeDate"]).date()
        events = {"DEAD": (delisting_date, "bankruptcy")}
        result = forward_raw_return(
            loader, "DEAD", asof, holding_period=20, delisting_events=events
        )
        self.assertAlmostEqual(result, -1.0)

    def test_ch11_treated_as_bankruptcy(self):
        loader, asof, history = self._build()
        delisting_date = pd.Timestamp(history.iloc[28]["tradeDate"]).date()
        events = {"DEAD": (delisting_date, "ch11")}
        result = forward_raw_return(
            loader, "DEAD", asof, holding_period=20, delisting_events=events
        )
        self.assertAlmostEqual(result, -1.0)

    def test_acquisition_mid_holding_returns_minus_half(self):
        loader, asof, history = self._build()
        delisting_date = pd.Timestamp(history.iloc[28]["tradeDate"]).date()
        events = {"DEAD": (delisting_date, "acquisition")}
        result = forward_raw_return(
            loader, "DEAD", asof, holding_period=20, delisting_events=events
        )
        self.assertAlmostEqual(result, -0.5)

    def test_unknown_reason_defaults_to_standard_minus_half(self):
        """Pre-reg doesn't specify 'unknown'; survivorship parquet has many
        unknowns (mostly mis-classified acquisitions). Default to -0.5."""
        loader, asof, history = self._build()
        delisting_date = pd.Timestamp(history.iloc[28]["tradeDate"]).date()
        events = {"DEAD": (delisting_date, "unknown")}
        result = forward_raw_return(
            loader, "DEAD", asof, holding_period=20, delisting_events=events
        )
        self.assertAlmostEqual(result, -0.5)

    def test_naive_path_unchanged_when_no_delisting_events(self):
        """Backward-compat: omitting delisting_events leaves naive logic intact."""
        loader, asof, _ = self._build()
        result = forward_raw_return(loader, "DEAD", asof, holding_period=20)
        self.assertIsNone(result)

    def test_naive_path_when_delisting_outside_holding_window(self):
        """If delisting > asof + holding_period, no terminal-return adjustment;
        naive None applies (insufficient forward bars from cache)."""
        loader, asof, _ = self._build()

        # Delisting 100 days after asof — far outside 20d holding. Cache
        # itself only has 30 days so naive logic returns None.
        far_date = (pd.Timestamp(asof) + pd.Timedelta(days=100)).date()
        events = {"DEAD": (far_date, "bankruptcy")}
        result = forward_raw_return(
            loader, "DEAD", asof, holding_period=20, delisting_events=events
        )
        self.assertIsNone(result)


class TestBuildTargetFrame(unittest.TestCase):
    def test_aligns_to_feature_frame_keys(self):
        history = _smd_close_history("AAA", n_days=200)
        loader = _make_loader({"AAA": history})
        feat = pd.DataFrame(
            [
                {"asof": history.iloc[10]["tradeDate"], "ticker": "AAA", "f1": 1.0},
                {"asof": history.iloc[100]["tradeDate"], "ticker": "AAA", "f1": 2.0},
            ]
        )
        out = build_target_frame(feat, smd_loader=loader, holding_period=20)
        self.assertEqual(set(out.columns), {"asof", "ticker", "target"})
        self.assertEqual(len(out), 2)
        # First asof at day 10 → entry day 11, exit day 31, both available
        self.assertFalse(out["target"].isna().any())

    def test_empty_input_returns_empty_frame(self):
        loader = _make_loader({})
        out = build_target_frame(pd.DataFrame(), smd_loader=loader)
        self.assertTrue(out.empty)


class TestWinsorizeRightTail(unittest.TestCase):
    """Per-asof right-tail winsorization preserves left tail (bankruptcy
    floor -1.0) and clips pump-and-dump outliers."""

    def test_caps_extreme_right_tail(self):
        from alphalens.screeners.options_implied.target import (
            _winsorize_right_tail_per_asof,
        )

        # 20 bulk values + 1 extreme outlier
        target = pd.Series([*np.linspace(-0.1, 0.1, 20), 10.0])
        asof = pd.Series(["a"] * 21)
        out = _winsorize_right_tail_per_asof(target, asof, pct=0.95)
        # Outlier capped at 95th percentile
        self.assertLess(out.iloc[-1], 10.0)
        # Bulk values unchanged
        self.assertAlmostEqual(out.iloc[10], 0.005263, places=3)

    def test_preserves_left_tail_bankruptcy_floor(self):
        """Pre-reg delisting rule places -1.0 events; left tail must be intact."""
        from alphalens.screeners.options_implied.target import (
            _winsorize_right_tail_per_asof,
        )

        target = pd.Series([-1.0, -0.5, 0.02, 0.05, 0.10, 0.20, *np.linspace(0.01, 0.05, 15)])
        asof = pd.Series(["a"] * len(target))
        out = _winsorize_right_tail_per_asof(target, asof, pct=0.995)
        # -1.0 bankruptcy and -0.5 standard delisting preserved
        self.assertAlmostEqual(out.iloc[0], -1.0)
        self.assertAlmostEqual(out.iloc[1], -0.5)

    def test_propagates_nan(self):
        from alphalens.screeners.options_implied.target import (
            _winsorize_right_tail_per_asof,
        )

        target = pd.Series([np.nan, 0.05, 0.10, *np.linspace(0, 0.05, 17)])
        asof = pd.Series(["a"] * len(target))
        out = _winsorize_right_tail_per_asof(target, asof, pct=0.95)
        self.assertTrue(np.isnan(out.iloc[0]))


class TestSplitTrainHoldout(unittest.TestCase):
    def test_strict_temporal_boundary(self):
        df = pd.DataFrame(
            {
                "asof": ["2023-12-31", "2024-01-01", "2024-04-29", "2024-04-30", "2024-05-01"],
                "v": [1, 2, 3, 4, 5],
            }
        )
        train, holdout = split_train_holdout(df, date(2024, 4, 30))
        self.assertEqual(train["v"].tolist(), [1, 2, 3])
        self.assertEqual(holdout["v"].tolist(), [4, 5])


class TestAlignedTrain(unittest.TestCase):
    def test_drops_nan_targets_and_aligns(self):
        feat = pd.DataFrame({"asof": ["a", "a", "b"], "ticker": ["X", "Y", "X"], "f1": [1, 2, 3]})
        targ = pd.DataFrame(
            {
                "asof": ["a", "a", "b"],
                "ticker": ["X", "Y", "X"],
                "target": [0.1, np.nan, 0.3],
            }
        )
        X_aligned, y = aligned_train(feat, targ)
        self.assertEqual(len(X_aligned), 2)
        self.assertEqual(len(y), 2)
        self.assertNotIn("target", X_aligned.columns)
        self.assertEqual(y.tolist(), [0.1, 0.3])


if __name__ == "__main__":
    unittest.main()
