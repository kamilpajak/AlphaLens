"""Phase B — 5d-forward excess-return target generator.

Per `docs/research/preregistration/params_multi_source_two_stage_2026_04_30.json`
target_definition: 5-trading-day-forward close-to-close excess return over RF.

Convention (mirrors `HistoryStore.forward_return`):
- Score is computed using bars `<= asof`.
- Entry at the FIRST bar after `asof` (close[asof+1bd]).
- Exit at the bar `holding_period` steps later (close[asof+1bd + holding_period]).
- Total return = exit/entry - 1.
- Excess = total return - cumulative RF over the same window.

Rationale (per pre-reg):
- Do NOT pre-residualize on Carhart; preserve information. Carhart attribution
  is post-hoc on portfolio returns (see Gu-Kelly-Xiu 2024 update + ADR 0007).
- Returns NaN if the ticker has insufficient forward bars (e.g. delisted
  mid-holding). Survivorship-correct stores keep delisted bars; truncation
  before delisting yields valid forward windows for the bars that exist.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from alphalens_pipeline.data.store.history import HistoryStore

DEFAULT_HOLDING = 5


def _cumulative_rf_over_window(
    rf_series: pd.Series, entry_ts: pd.Timestamp, exit_ts: pd.Timestamp
) -> float | None:
    """Sum of daily RF returns over (entry_ts, exit_ts] — exclusive at entry,
    inclusive at exit.

    The price leg compounds 5 daily returns from entry close to exit close
    (close[entry+1bd]/close[entry]−1, …, close[exit]/close[exit−1bd]−1). Those
    5 daily returns are timestamped at days entry_ts+1bd … exit_ts. The RF
    window must match — same 5 timestamps. A naive ``loc[entry_ts:exit_ts]``
    inclusive slice would include 6 RF bars and bias the excess return.

    Daily RF is a per-day arithmetic rate; cumulative arithmetic sum matches
    the convention used in `attribution.factor_analysis.run_regression`
    (subtracts RF day-by-day on the LHS).

    Returns None if any RF bar in the window is missing.
    """
    if rf_series.empty:
        return None
    mask = (rf_series.index > entry_ts) & (rf_series.index <= exit_ts)
    rf_window = rf_series.loc[mask]
    if rf_window.empty:
        return None
    if rf_window.isna().any():
        return None
    return float(rf_window.sum())


def forward_excess_return(
    history_store: HistoryStore,
    ticker: str,
    asof: date,
    rf_series: pd.Series,
    holding_period: int = DEFAULT_HOLDING,
) -> float | None:
    """5d-forward excess-over-RF for `ticker` entering after `asof`.

    Mirrors `HistoryStore.forward_return` for the price leg, then subtracts
    cumulative daily RF over the same calendar window.

    Returns None if:
    - ticker unknown
    - fewer than holding_period+1 forward bars available (delisting mid-hold,
      end of history)
    - entry price not strictly positive
    - RF series missing a bar in the window
    """
    try:
        df = history_store.full(ticker)
    except KeyError:
        return None
    if df.empty:
        return None
    ts_asof = pd.Timestamp(asof)
    future = df.loc[df.index > ts_asof]
    if len(future) < holding_period + 1:
        return None
    entry_price = float(future.iloc[0]["close"])
    exit_price = float(future.iloc[holding_period]["close"])
    if entry_price <= 0.0:
        return None
    raw_return = exit_price / entry_price - 1.0

    entry_ts = future.index[0]
    exit_ts = future.index[holding_period]
    rf_cum = _cumulative_rf_over_window(rf_series, entry_ts, exit_ts)
    if rf_cum is None:
        return None
    return float(raw_return - rf_cum)


def build_target_frame(
    feature_frame: pd.DataFrame,
    *,
    history_store: HistoryStore,
    rf_series: pd.Series,
    holding_period: int = DEFAULT_HOLDING,
) -> pd.DataFrame:
    """Compute targets for every (asof, ticker) pair in `feature_frame`.

    Returns a DataFrame with columns ``asof, ticker, target`` aligned to the
    feature-frame keys. Rows where forward bars are insufficient have
    ``target = NaN`` and are dropped by callers prior to fitting.
    """
    if feature_frame.empty:
        return pd.DataFrame(columns=["asof", "ticker", "target"])

    keys = feature_frame[["asof", "ticker"]].drop_duplicates()
    targets: list[float] = []
    for _, row in keys.iterrows():
        t = forward_excess_return(
            history_store,
            row["ticker"],
            row["asof"],
            rf_series,
            holding_period=holding_period,
        )
        targets.append(np.nan if t is None else t)
    out = keys.copy()
    out["target"] = targets
    return out


def split_train_holdout(
    df: pd.DataFrame,
    holdout_start: date,
    *,
    asof_col: str = "asof",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Strict-temporal split. Rows with asof < holdout_start → train; else holdout.

    Used identically on feature and target frames so per-row alignment holds.
    """
    if df.empty:
        return df.iloc[0:0].copy(), df.iloc[0:0].copy()
    boundary = pd.Timestamp(holdout_start)
    asof_ts = pd.to_datetime(df[asof_col])
    train = df.loc[asof_ts < boundary].copy()
    holdout = df.loc[asof_ts >= boundary].copy()
    return train.reset_index(drop=True), holdout.reset_index(drop=True)


def aligned_train_targets(
    feature_train: pd.DataFrame, target_train: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series]:
    """Inner-join features and targets on (asof, ticker), drop NaNs in either.

    Returns ``(features_aligned, target_series)`` ready for Lasso fit.
    Both share an integer index post reset_index for sklearn compatibility.
    """
    if feature_train.empty or target_train.empty:
        return feature_train.iloc[0:0].copy(), pd.Series(dtype=float)
    merged = feature_train.merge(target_train, on=["asof", "ticker"], how="inner")
    keep = merged.dropna(subset=["target"]).reset_index(drop=True)
    feat_cols = [c for c in keep.columns if c not in {"target"}]
    return keep[feat_cols], keep["target"].astype(float)


def rank_transform_per_asof(
    y: pd.Series,
    asof: pd.Series,
    *,
    min_slice_size: int = 3,
) -> pd.Series:
    """Per-asof percentile-rank transform centered at zero.

    For each asof slice with at least `min_slice_size` non-NaN values:
      1. Compute average-method rank within the slice (handles ties).
      2. Divide by slice size, subtract 0.5.

    Result is bounded in [-0.5 + 1/(2n), +0.5] for asof slices of size n,
    with NaN inputs propagating to NaN outputs and slices smaller than
    `min_slice_size` returning all NaN. Used by `fit_global(target_transform=
    'rank')` to align Lasso L1 + MSE inner solver with Spearman rank-IC
    objective (per v5 pre-reg
    `params_alt_data_screener_v3_2026_05_01.json`).

    Mathematical equivalence: minimizing MSE between predictions and rank-
    transformed y is equivalent (up to constant) to maximizing Pearson
    correlation between predictions and ranks within each asof slice, which
    is the definition of Spearman rank correlation.
    """
    if len(y) != len(asof):
        raise ValueError(f"y and asof must have same length; got {len(y)} vs {len(asof)}")
    if y.empty:
        return y.copy().astype(float)

    out = pd.Series(np.nan, index=y.index, dtype=float)
    df = pd.DataFrame({"y": y.values, "asof": asof.values}, index=y.index)
    for asof_val, slice_df in df.groupby("asof", sort=False):
        valid = slice_df["y"].dropna()
        if len(valid) < min_slice_size:
            continue
        # Average-method rank within slice (handles ties as average)
        ranks = valid.rank(method="average")
        n = len(valid)
        normalised = ranks / n - 0.5
        out.loc[valid.index] = normalised
    return out


__all__ = [
    "DEFAULT_HOLDING",
    "aligned_train_targets",
    "build_target_frame",
    "forward_excess_return",
    "rank_transform_per_asof",
    "split_train_holdout",
]
