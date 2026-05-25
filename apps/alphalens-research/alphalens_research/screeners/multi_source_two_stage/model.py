"""Phase B — regime-conditional Lasso with nested expanding-window CV.

Per `docs/research/preregistration/params_multi_source_two_stage_2026_04_30.json`
stage2_conditional_alpha:
- One Lasso (L1) model per VIX-quartile regime (4 models).
- λ selected via 3-fold expanding-window CV on the regime's train_pool with
  60-trading-day embargo.
- max_lambda_grid_points = 25.

Embargo intuition: the target is a 5-day-forward return; without an embargo
between the train fold's last asof and the validation fold's first asof, the
two folds share overlapping forward windows and validation MSE is biased. A
60-day embargo > 5d horizon × 12 (cushion for slow-decaying autocorrelation).

Pipeline = StandardScaler → Lasso. Standardization within each fold (the
scaler is fit on train data only). All features are pre-locked in the
pre-registration; this module does NOT add or drop features.

ML hygiene:
- np.random.default_rng(seed) for any randomness (Lasso is deterministic
  given α and warm-start setting; we set random_state=0 for reproducibility).
- Numerical NaNs in features are imputed to the column train-median per regime
  before scaling. Records with NaN target are dropped by the caller before
  fit_two_stage is invoked.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd
from sklearn.linear_model import Lasso
from sklearn.preprocessing import StandardScaler

from alphalens_research.screeners.multi_source_two_stage.features import (
    FEATURE_NAMES,
    REGIME_LABELS,
)

logger = logging.getLogger(__name__)

# Frozen by pre-registration — see params_multi_source_two_stage_2026_04_30.json.
N_FOLDS_DEFAULT = 3
EMBARGO_DAYS_DEFAULT = 60
LAMBDA_GRID_POINTS_DEFAULT = 25
LAMBDA_MIN_RATIO = 1e-3  # lambda_min / lambda_max
RANDOM_STATE = 0

_MISSING_ASOF_MSG = "features_train must include an 'asof' column"


@dataclass(frozen=True)
class RegimeFit:
    """Bundle of standardization + fitted Lasso for a single regime."""

    regime: str
    feature_names: tuple[str, ...]
    scaler: StandardScaler
    model: Lasso
    feature_medians: np.ndarray  # column train-medians for NaN imputation
    chosen_alpha: float
    cv_mean_mse: float
    cv_alpha_grid: np.ndarray
    cv_mean_mse_grid: np.ndarray
    n_train_obs: int
    n_nonzero_coefs: int

    def predict(self, features_df: pd.DataFrame) -> np.ndarray:
        """Predict on a feature frame whose columns include FEATURE_NAMES."""
        X = _prepare_X(features_df, self.feature_names, self.feature_medians)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            Xs = self.scaler.transform(X)
            return self.model.predict(Xs)


def _prepare_X(df: pd.DataFrame, feature_names: tuple[str, ...], medians: np.ndarray) -> np.ndarray:
    """Extract features in `feature_names` order, impute NaNs to `medians`,
    return float64 array.
    """
    arr = df.loc[:, list(feature_names)].to_numpy(dtype=float).copy()
    if arr.size == 0:
        return arr
    nan_mask = np.isnan(arr)
    if nan_mask.any():
        # broadcast medians (shape [F]) over rows
        med_b = np.broadcast_to(medians, arr.shape).copy()
        arr[nan_mask] = med_b[nan_mask]
    return arr


def _expanding_splits_with_embargo(
    asof_series: pd.Series,
    n_folds: int,
    embargo_days: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window CV splits over a sorted-asof sample with calendar-day
    embargo between train end and validation start.

    Implementation:
    - Sort indices by asof.
    - Partition the sorted index into `n_folds + 1` blocks. Block 0 = warm-up
      (always train). Blocks 1..n_folds are successive validation folds; train
      = all-prior-blocks minus rows whose asof is within `embargo_days` of the
      validation block's first asof.
    - Returns ``[(train_idx, val_idx), ...]`` of length n_folds. Folds with
      empty train or validation are skipped (logged).
    """
    if asof_series.empty:
        return []
    sorted_idx = asof_series.sort_values().index.to_numpy()
    asof_sorted = pd.to_datetime(asof_series.loc[sorted_idx]).reset_index(drop=True)
    n = len(sorted_idx)
    if n < n_folds + 1:
        return []
    block_size = n // (n_folds + 1)
    if block_size < 1:
        return []

    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for k in range(1, n_folds + 1):
        val_lo = k * block_size
        val_hi = (k + 1) * block_size if k < n_folds else n
        val_pos = np.arange(val_lo, val_hi)
        train_pos_full = np.arange(0, val_lo)
        val_first = asof_sorted.iloc[val_lo]
        embargo_cutoff = val_first - pd.Timedelta(days=embargo_days)
        keep_mask = asof_sorted.iloc[train_pos_full].to_numpy() < embargo_cutoff
        train_pos = train_pos_full[keep_mask]
        if len(train_pos) == 0 or len(val_pos) == 0:
            logger.warning(
                "skipping fold k=%d: train=%d val=%d (embargo too aggressive?)",
                k,
                len(train_pos),
                len(val_pos),
            )
            continue
        splits.append((sorted_idx[train_pos], sorted_idx[val_pos]))
    return splits


def _lambda_grid(X: np.ndarray, y: np.ndarray, n_points: int, min_ratio: float) -> np.ndarray:
    """Glmnet-style log-spaced grid from λ_max (smallest λ that zeros all coefs)
    down to λ_min = λ_max × min_ratio.

    λ_max = ‖X' y‖_∞ / n   (caller has already standardized X). The matmul
    runs under `np.errstate(...)` because numpy 2.3 + Apple Accelerate raises
    spurious "divide by zero" RuntimeWarnings on benign matmul calls; the
    actual numerical output is correct.
    """
    n = max(1, X.shape[0])
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        lam_max = float(np.max(np.abs(X.T @ y))) / n
    if lam_max <= 0 or not np.isfinite(lam_max):
        lam_max = 1.0
    lam_min = lam_max * min_ratio
    return np.logspace(np.log10(lam_max), np.log10(lam_min), num=n_points)


def _fit_single_regime(
    feat_df: pd.DataFrame,
    y: pd.Series,
    asof: pd.Series,
    regime: str,
    n_folds: int,
    embargo_days: int,
    lambda_grid_points: int,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
) -> RegimeFit | None:
    """Nested-CV Lasso fit on a single regime's training subsample.

    Returns None if the regime has insufficient data (fewer than n_folds+1
    distinct asof dates after embargo).
    """
    if len(feat_df) == 0:
        logger.warning("regime %s has 0 train rows — skipping", regime)
        return None

    # All-NaN columns produce nanmedian's "All-NaN slice" warning; that's a
    # signal-not-error path — fall back to 0 so downstream imputation still runs.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        medians = np.nanmedian(feat_df[list(feature_names)].to_numpy(dtype=float), axis=0)
    medians = np.nan_to_num(medians, nan=0.0)
    X_full = _prepare_X(feat_df, feature_names, medians)
    y_full = y.to_numpy(dtype=float)

    splits = _expanding_splits_with_embargo(asof, n_folds, embargo_days)
    if not splits:
        logger.warning(
            "regime %s: no valid CV splits (n=%d) — skipping",
            regime,
            len(feat_df),
        )
        return None

    grid_scaler = StandardScaler().fit(X_full)
    Xs_full = cast(np.ndarray, grid_scaler.transform(X_full))
    lam_grid = _lambda_grid(Xs_full, y_full, lambda_grid_points, LAMBDA_MIN_RATIO)

    fold_mses = np.zeros((len(splits), len(lam_grid)), dtype=float)
    # Wrap fit/predict in errstate to suppress numpy 2.3 + Apple Accelerate
    # spurious "divide by zero in matmul" warnings (output is correct).
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for fi, (train_idx, val_idx) in enumerate(splits):
            train_pos = feat_df.index.get_indexer(cast(pd.Index, train_idx))
            val_pos = feat_df.index.get_indexer(cast(pd.Index, val_idx))
            # Fold-local scaler so val standardization uses train-only stats.
            local_scaler = StandardScaler().fit(X_full[train_pos])
            Xs_train_local = local_scaler.transform(X_full[train_pos])
            Xs_val_local = local_scaler.transform(X_full[val_pos])
            y_train = y_full[train_pos]
            y_val = y_full[val_pos]
            for li, lam in enumerate(lam_grid):
                model = Lasso(
                    alpha=lam,
                    max_iter=10_000,
                    random_state=RANDOM_STATE,
                    selection="cyclic",
                )
                model.fit(Xs_train_local, y_train)
                preds = model.predict(Xs_val_local)
                fold_mses[fi, li] = float(np.mean((preds - y_val) ** 2))
    mean_mse = fold_mses.mean(axis=0)
    best_li = int(np.argmin(mean_mse))
    best_alpha = float(lam_grid[best_li])

    final_model = Lasso(
        alpha=best_alpha,
        max_iter=10_000,
        random_state=RANDOM_STATE,
        selection="cyclic",
    )
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        final_model.fit(Xs_full, y_full)
    n_nonzero = int(np.sum(np.abs(final_model.coef_) > 1e-12))

    logger.info(
        "regime %s: n=%d, λ chosen=%.4g (idx %d/%d), CV MSE=%.6g, nonzero coefs=%d",
        regime,
        len(feat_df),
        best_alpha,
        best_li,
        len(lam_grid) - 1,
        float(mean_mse[best_li]),
        n_nonzero,
    )

    return RegimeFit(
        regime=regime,
        feature_names=feature_names,
        scaler=grid_scaler,
        model=final_model,
        feature_medians=medians,
        chosen_alpha=best_alpha,
        cv_mean_mse=float(mean_mse[best_li]),
        cv_alpha_grid=lam_grid,
        cv_mean_mse_grid=mean_mse,
        n_train_obs=len(feat_df),
        n_nonzero_coefs=n_nonzero,
    )


def fit_two_stage(
    features_train: pd.DataFrame,
    target_train: pd.Series,
    *,
    n_folds: int = N_FOLDS_DEFAULT,
    embargo_days: int = EMBARGO_DAYS_DEFAULT,
    lambda_grid_points: int = LAMBDA_GRID_POINTS_DEFAULT,
) -> dict[str, RegimeFit]:
    """Fit one Lasso per VIX-quartile regime via nested expanding-window CV.

    Parameters
    ----------
    features_train
        DataFrame with at minimum [asof, ticker, regime] + FEATURE_NAMES columns.
        Already restricted to train_pool (asof < holdout_start) and to rows with
        non-NaN target.
    target_train
        Series aligned by index to `features_train` rows; values are 5d-forward
        excess returns.

    Returns
    -------
    dict[str, RegimeFit]
        Mapping regime label → fitted bundle. Regimes with insufficient data
        are absent from the returned mapping (callers handle by predicting
        a neutral score for those rows).
    """
    if "regime" not in features_train.columns:
        raise KeyError("features_train must include a 'regime' column")
    if "asof" not in features_train.columns:
        raise KeyError(_MISSING_ASOF_MSG)
    if len(features_train) != len(target_train):
        raise ValueError(
            f"features ({len(features_train)}) and target ({len(target_train)}) lengths must match"
        )

    df = features_train.copy()
    df["__y__"] = target_train.values
    df = df.dropna(subset=["__y__"]).reset_index(drop=True)

    fits: dict[str, RegimeFit] = {}
    for regime in REGIME_LABELS:
        sub = df.loc[df["regime"] == regime].copy()
        if sub.empty:
            logger.warning("regime %s has no train observations", regime)
            continue
        sub_feat = sub.drop(columns=["__y__"])
        sub_y = sub["__y__"]
        sub_asof = sub["asof"]
        fit = _fit_single_regime(
            sub_feat,
            sub_y,
            sub_asof,
            regime,
            n_folds=n_folds,
            embargo_days=embargo_days,
            lambda_grid_points=lambda_grid_points,
        )
        if fit is not None:
            fits[regime] = fit
    return fits


def predict_scores(
    fits: dict[str, RegimeFit],
    features_df: pd.DataFrame,
) -> pd.Series:
    """Apply the per-regime model based on each row's `regime` column.

    Rows whose regime has no fitted model receive NaN (caller filters before
    ranking). Output is a Series indexed by ``features_df.index`` so callers
    can attach back to (asof, ticker) keys however they like.
    """
    if "regime" not in features_df.columns:
        raise KeyError("features_df must include a 'regime' column")
    out = pd.Series(np.nan, index=features_df.index, dtype=float)
    for regime, fit in fits.items():
        mask = features_df["regime"] == regime
        if not mask.any():
            continue
        sub = features_df.loc[mask]
        out.loc[mask] = fit.predict(sub)
    return out


# ---------------------------------------------------------------------------
# v2 (multi_source_global_lasso_2026_04_30) — single global Lasso, no Stage 1.
# Reuses _fit_single_regime under the synthetic regime label "GLOBAL".
# Pre-reg: docs/research/preregistration/params_multi_source_global_lasso_2026_04_30.json


GLOBAL_REGIME_LABEL = "GLOBAL"


def fit_global(
    features_train: pd.DataFrame,
    target_train: pd.Series,
    *,
    n_folds: int = N_FOLDS_DEFAULT,
    embargo_days: int = EMBARGO_DAYS_DEFAULT,
    lambda_grid_points: int = LAMBDA_GRID_POINTS_DEFAULT,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
    target_transform: str = "none",
) -> RegimeFit | None:
    """Fit a single Lasso on the entire train pool, no regime conditioning.

    Pre-reg ablation of `fit_two_stage`: same procedure (nested expanding-
    window CV with 60d embargo, glmnet-style λ grid) applied ONCE on the
    full train pool instead of per-regime.

    Returns ``None`` if the train pool is empty or has no valid CV splits.
    """
    if "asof" not in features_train.columns:
        raise KeyError(_MISSING_ASOF_MSG)
    if len(features_train) != len(target_train):
        raise ValueError(
            f"features ({len(features_train)}) and target ({len(target_train)}) lengths must match"
        )

    df = features_train.copy()
    df["__y__"] = target_train.values
    df = df.dropna(subset=["__y__"]).reset_index(drop=True)
    if df.empty:
        logger.warning("fit_global: empty train pool after NaN-target drop")
        return None

    if target_transform == "rank":
        from alphalens_research.screeners.multi_source_two_stage.target import (
            rank_transform_per_asof,
        )

        # Per-asof rank-percentile transform centered at zero. Aligns Lasso
        # MSE inner solver with Spearman rank-IC objective per v5 pre-reg.
        df["__y__"] = rank_transform_per_asof(df["__y__"], df["asof"]).values
        # Drop rows where transform produced NaN (asof slice too small)
        df = df.dropna(subset=["__y__"]).reset_index(drop=True)
        if df.empty:
            logger.warning("fit_global: empty train pool after rank-transform NaN drop")
            return None
    elif target_transform != "none":
        raise ValueError(f"target_transform must be 'none' or 'rank'; got {target_transform!r}")

    feat = df.drop(columns=["__y__"])
    y = df["__y__"]
    return _fit_single_regime(
        feat,
        y,
        df["asof"],
        GLOBAL_REGIME_LABEL,
        n_folds=n_folds,
        embargo_days=embargo_days,
        lambda_grid_points=lambda_grid_points,
        feature_names=feature_names,
    )


def predict_scores_global(fit: RegimeFit | None, features_df: pd.DataFrame) -> pd.Series:
    """Apply the single global model to all rows regardless of regime.

    `regime` column is ignored. If ``fit`` is None (training failed), returns
    a Series of NaN with the same index — caller filters before ranking.
    """
    if fit is None:
        return pd.Series(np.nan, index=features_df.index, dtype=float)
    return pd.Series(fit.predict(features_df), index=features_df.index, dtype=float)


# ---------------------------------------------------------------------------
# v9 (nonlinear_alt_data_v1_lightgbm_mse_2026_05_01) — LightGBM MSE on raw
# return target. Pre-reg:
# docs/research/preregistration/params_nonlinear_alt_data_v1_lightgbm_mse_2026_05_01.json


@dataclass(frozen=True)
class LightGBMFit:
    """Bundle of LightGBM model + metadata mimicking RegimeFit shape.

    Drivers can call .predict(features_df) like a RegimeFit; field semantics:
    - chosen_alpha: best n_estimators selected via CV early stopping
    - cv_mean_mse: mean fold MSE at chosen n_estimators
    - n_nonzero_coefs: count of features with feature_importances_ > 0
    """

    feature_names: tuple[str, ...]
    model: object  # LGBMRegressor (not strict-typed to avoid import cycle)
    feature_medians: np.ndarray
    chosen_alpha: float  # best n_estimators
    cv_mean_mse: float
    n_train_obs: int
    n_nonzero_coefs: int

    def predict(self, features_df: pd.DataFrame) -> np.ndarray:
        X = _prepare_X(features_df, self.feature_names, self.feature_medians)
        if X.size == 0:
            return np.array([])
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return self.model.predict(X)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class LightGBMConfig:
    """LightGBM hyperparameters (pre-registration locked; override only for tests)."""

    max_depth: int = 5
    num_leaves: int = 32
    min_child_samples: int = 500
    learning_rate: float = 0.05
    n_estimators_max: int = 500
    early_stopping_rounds: int = 20
    reg_alpha: float = 0.1
    reg_lambda: float = 0.1
    feature_fraction: float = 1.0
    bagging_fraction: float = 0.8
    bagging_freq: int = 1
    random_state: int = 42


def fit_lightgbm_mse_global(
    features_train: pd.DataFrame,
    target_train: pd.Series,
    *,
    n_folds: int = N_FOLDS_DEFAULT,
    embargo_days: int = EMBARGO_DAYS_DEFAULT,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
    config: LightGBMConfig | None = None,
) -> LightGBMFit | None:
    """Fit LightGBM MSE on full train pool with CV-tuned n_estimators.

    Per pre-reg `nonlinear_alt_data_v1_lightgbm_mse_2026_05_01`:
    all hyperparameters locked except n_estimators, which is selected via
    early stopping on 3-fold expanding-window CV with 60d embargo (mean
    optimal across folds → final n_estimators for full-train refit).

    Returns ``None`` if train pool is empty or has no valid CV splits.
    """
    from lightgbm import LGBMRegressor

    cfg = config or LightGBMConfig()

    if "asof" not in features_train.columns:
        raise KeyError(_MISSING_ASOF_MSG)
    if len(features_train) != len(target_train):
        raise ValueError(
            f"features ({len(features_train)}) and target ({len(target_train)}) lengths must match"
        )

    df = features_train.copy()
    df["__y__"] = target_train.values
    df = df.dropna(subset=["__y__"]).reset_index(drop=True)
    if df.empty:
        logger.warning("fit_lightgbm_mse_global: empty train pool after NaN-target drop")
        return None

    feat = df.drop(columns=["__y__"])
    y_full = df["__y__"].to_numpy(dtype=float)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        medians = np.nanmedian(feat[list(feature_names)].to_numpy(dtype=float), axis=0)
    medians = np.nan_to_num(medians, nan=0.0)
    X_full = _prepare_X(feat, feature_names, medians)

    splits = _expanding_splits_with_embargo(df["asof"], n_folds, embargo_days)
    if not splits:
        logger.warning(
            "fit_lightgbm_mse_global: no valid CV splits (n=%d)",
            len(df),
        )
        return None

    base_kwargs = {
        "objective": "regression",
        "max_depth": cfg.max_depth,
        "num_leaves": cfg.num_leaves,
        "min_child_samples": cfg.min_child_samples,
        "learning_rate": cfg.learning_rate,
        "reg_alpha": cfg.reg_alpha,
        "reg_lambda": cfg.reg_lambda,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "random_state": cfg.random_state,
        "verbose": -1,
        "n_jobs": -1,
    }

    fold_best_iters: list[int] = []
    fold_best_mses: list[float] = []
    for fi, (train_idx, val_idx) in enumerate(splits):
        train_pos = feat.index.get_indexer(cast(pd.Index, train_idx))
        val_pos = feat.index.get_indexer(cast(pd.Index, val_idx))
        X_tr = X_full[train_pos]
        X_vl = X_full[val_pos]
        y_tr = y_full[train_pos]
        y_vl = y_full[val_pos]
        model_cv = LGBMRegressor(n_estimators=cfg.n_estimators_max, **base_kwargs)
        from lightgbm import early_stopping, log_evaluation

        model_cv.fit(
            X_tr,
            y_tr,
            eval_set=[(X_vl, y_vl)],
            eval_metric="l2",
            callbacks=[
                early_stopping(stopping_rounds=cfg.early_stopping_rounds, verbose=False),
                log_evaluation(period=0),
            ],
        )
        best_iter = (
            int(model_cv.best_iteration_) if model_cv.best_iteration_ else cfg.n_estimators_max
        )
        best_mse = float(model_cv.best_score_["valid_0"]["l2"])
        fold_best_iters.append(best_iter)
        fold_best_mses.append(best_mse)
        logger.info(
            "lgbm fold %d/%d: best_iter=%d, val MSE=%.6g, n_train=%d, n_val=%d",
            fi + 1,
            len(splits),
            best_iter,
            best_mse,
            len(X_tr),
            len(X_vl),
        )

    final_n_estimators = int(np.round(np.mean(fold_best_iters)))
    final_n_estimators = max(1, final_n_estimators)
    cv_mean_mse = float(np.mean(fold_best_mses))

    final_model = LGBMRegressor(n_estimators=final_n_estimators, **base_kwargs)
    final_model.fit(X_full, y_full)
    importances = final_model.feature_importances_
    n_nonzero = int(np.sum(importances > 0))

    logger.info(
        "lgbm-mse global fit: n_train=%d, final n_estimators=%d (mean of %s), "
        "CV MSE=%.6g, nonzero importance feats=%d/%d",
        len(df),
        final_n_estimators,
        fold_best_iters,
        cv_mean_mse,
        n_nonzero,
        len(feature_names),
    )

    return LightGBMFit(
        feature_names=feature_names,
        model=final_model,
        feature_medians=medians,
        chosen_alpha=float(final_n_estimators),
        cv_mean_mse=cv_mean_mse,
        n_train_obs=len(df),
        n_nonzero_coefs=n_nonzero,
    )


def predict_scores_lightgbm(fit: LightGBMFit | None, features_df: pd.DataFrame) -> pd.Series:
    """Apply the fitted LightGBM model to all rows; mirrors predict_scores_global."""
    if fit is None:
        return pd.Series(np.nan, index=features_df.index, dtype=float)
    return pd.Series(fit.predict(features_df), index=features_df.index, dtype=float)


__all__ = [
    "EMBARGO_DAYS_DEFAULT",
    "GLOBAL_REGIME_LABEL",
    "LAMBDA_GRID_POINTS_DEFAULT",
    "N_FOLDS_DEFAULT",
    "LightGBMConfig",
    "LightGBMFit",
    "RegimeFit",
    "fit_global",
    "fit_lightgbm_mse_global",
    "fit_two_stage",
    "predict_scores",
    "predict_scores_global",
    "predict_scores_lightgbm",
]
