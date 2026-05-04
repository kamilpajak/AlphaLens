"""v7 Phase B — 20d-forward raw equity return target generator.

Per `params_v7_smd_options_implied_2026_05_02.json` target_definition:
  raw_target: "20-day forward equity return"
  transformation: "raw" (no rank transform unless empirically justified)
  horizon: "20d-forward"

Convention: at asof_t, score is computed using bars `<= asof`. Entry at the
first trading bar after asof_t (close[asof+1bd]); exit 20 trading days later
(close[asof+1bd + 20]). Total return = exit/entry - 1. Carhart attribution +
MDY-excess applied at portfolio level (post-hoc).

Survivorship-correctness: pre-reg `delisting_handling` rule mandates that
mid-holding delistings receive a TERMINAL return (NOT NaN drop):
  - bankruptcy / ch11 / fdic_receivership → -1.0 (full wipeout)
  - acquisition / voluntary / unknown → -0.5 (50% terminal)
This prevents the silent drop from cross-section that re-introduces
survivorship bias. Caller passes `delisting_events: dict[ticker -> (date, reason)]`
constructed from `~/.alphalens/survivorship/delisted_2021_2026.parquet`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd

DEFAULT_HOLDING = 20

SmdLoader = Callable[[str], pd.DataFrame | None]

# Pre-reg `delisting_handling` rule: terminal return based on reason.
# Bankruptcy-class reasons → full wipeout (-100%); others → standard -50%.
_BANKRUPTCY_REASONS = frozenset({"bankruptcy", "ch11", "chapter_11", "fdic_receivership"})
_BANKRUPTCY_TERMINAL_RETURN = -1.0
_DEFAULT_TERMINAL_RETURN = -0.5

DelistingEventsIndex = dict[str, tuple[date, str]]


def _terminal_return_for_reason(reason: str) -> float:
    """Map vendor reason string to pre-reg terminal-return value."""
    return (
        _BANKRUPTCY_TERMINAL_RETURN
        if reason.lower().strip() in _BANKRUPTCY_REASONS
        else _DEFAULT_TERMINAL_RETURN
    )


def load_delisting_events_index(parquet_path) -> DelistingEventsIndex:
    """Load `~/.alphalens/survivorship/delisted_2021_2026.parquet` into a
    ticker→(date, reason) dict. Caller passes path; returns empty dict if
    parquet missing.
    """
    import pathlib

    path = pathlib.Path(parquet_path)
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    out: DelistingEventsIndex = {}
    for _, row in df.iterrows():
        ticker = str(row["ticker"]).upper()
        d = row["delisted_date"]
        if hasattr(d, "date"):
            d = d.date()
        reason = str(row.get("reason", "unknown") or "unknown")
        # Most-recent delisting wins on collision (later parquet rows are
        # the more authoritative reclassifications).
        out[ticker] = (d, reason)
    return out


def forward_raw_return(
    smd_loader: SmdLoader,
    ticker: str,
    asof: str | date,
    *,
    holding_period: int = DEFAULT_HOLDING,
    delisting_events: DelistingEventsIndex | None = None,
) -> float | None:
    """20d-forward raw close-to-close return for `ticker` entering after `asof`.

    Reads close prices from the cached smd parquet (single-source vendor).
    Returns None if insufficient forward bars OR entry price ≤ 0.

    Pre-reg `delisting_handling` rule: when `delisting_events` is supplied
    AND the ticker has a delisting date inside the (asof, asof + holding_period]
    window, override the naive-None return with a TERMINAL value:
      - reason ∈ {bankruptcy, ch11, ...} → -1.0
      - reason ∈ {acquisition, voluntary, unknown, ...} → -0.5
    This prevents survivorship bias from silent mid-holding drops.
    """
    history = smd_loader(ticker)
    if history is None or history.empty or "close" not in history.columns:
        return _terminal_if_delisted(ticker, asof, holding_period, delisting_events)
    asof_ts = pd.Timestamp(asof)
    # Use canonical signal field (ivp30) to identify trading-day rows; smd
    # carries forward Friday close on Sat/Sun with NaN ivp30 (see
    # `features._slice_pit` for the same convention).
    if "ivp30" in history.columns:
        history = history.loc[history["ivp30"].notna()]
    if "tradeDate" not in history.columns:
        return None
    history = history.sort_values("tradeDate")
    future = history.loc[pd.to_datetime(history["tradeDate"]) > asof_ts]
    if len(future) < holding_period + 1:
        # Insufficient forward bars — check delisting rule before giving up.
        return _terminal_if_delisted(ticker, asof, holding_period, delisting_events)
    entry_price = float(future.iloc[0]["close"])
    exit_price = float(future.iloc[holding_period]["close"])
    if entry_price <= 0.0 or not np.isfinite(entry_price) or not np.isfinite(exit_price):
        return None
    return float(exit_price / entry_price - 1.0)


def _terminal_if_delisted(
    ticker: str,
    asof: str | date,
    holding_period: int,
    delisting_events: DelistingEventsIndex | None,
) -> float | None:
    """Apply pre-reg terminal-return rule when delisting falls inside the
    (asof, asof + holding_period business days] window. Returns None if the
    rule does not apply (no event, or delisting is outside the window).
    """
    if not delisting_events:
        return None
    info = delisting_events.get(ticker.upper())
    if info is None:
        return None
    delisted_date, reason = info
    asof_ts = pd.Timestamp(asof)
    delisted_ts = pd.Timestamp(delisted_date)
    # Holding window in business days; use bdate_range length as proxy for
    # trading-day distance (PIT-correct enough for terminal-return decisions).
    window_end = asof_ts + pd.tseries.offsets.BDay(holding_period)
    if asof_ts < delisted_ts <= window_end:
        return _terminal_return_for_reason(reason)
    return None


def build_target_frame(
    feature_frame: pd.DataFrame,
    *,
    smd_loader: SmdLoader,
    holding_period: int = DEFAULT_HOLDING,
    delisting_events: DelistingEventsIndex | None = None,
    winsorize_right_tail_pct: float | None = 0.995,
) -> pd.DataFrame:
    """Compute 20d-forward raw returns for every (asof, ticker) in feature_frame.

    Returns DataFrame [asof, ticker, target] aligned to feature_frame keys.
    NaN target = insufficient forward bars (drop before fitting). When
    `delisting_events` is supplied, mid-holding deaths receive pre-reg
    terminal returns (-50% / -100%) instead of NaN.

    `winsorize_right_tail_pct` (default 0.995) caps per-asof targets at the
    given percentile of that slice. Pre-reg empirical adjustment applied
    Phase A 2026-05-02: raw 20d-forward returns showed CV-MSE ~4.0 driven by
    extreme right-tail (pump-and-dump) outliers — Lasso CV picked α that
    zeroed all coefs. Right-tail-only winsorization preserves the -1.0
    bankruptcy floor mandated by pre-reg `delisting_handling` while bounding
    the pump-and-dump impact on Lasso loss. Pass None to disable.
    """
    if feature_frame.empty:
        return pd.DataFrame(columns=["asof", "ticker", "target"])
    keys = feature_frame[["asof", "ticker"]].drop_duplicates()
    targets: list[float] = []
    for _, row in keys.iterrows():
        r = forward_raw_return(
            smd_loader,
            row["ticker"],
            row["asof"],
            holding_period=holding_period,
            delisting_events=delisting_events,
        )
        targets.append(np.nan if r is None else r)
    out = keys.copy()
    out["target"] = targets

    if winsorize_right_tail_pct is not None and not out.empty:
        out["target"] = _winsorize_right_tail_per_asof(
            out["target"], out["asof"], pct=winsorize_right_tail_pct
        )
    return out


def _winsorize_right_tail_per_asof(
    target: pd.Series, asof: pd.Series, *, pct: float = 0.995
) -> pd.Series:
    """Per-asof right-tail winsorization: cap values above the `pct`-th quantile
    of the asof slice. Left tail untouched (preserves -1.0 bankruptcy floor).

    NaN inputs propagate; slices with <10 valid values skip winsorization.
    """
    if target.empty:
        return target
    df = pd.DataFrame({"target": target.values, "asof": asof.values}, index=target.index)
    out = df["target"].copy()
    for asof_val, slice_df in df.groupby("asof", sort=False):
        valid = slice_df["target"].dropna()
        if len(valid) < 10:
            continue
        cap = valid.quantile(pct)
        if not np.isfinite(cap):
            continue
        mask = (df["asof"] == asof_val) & (df["target"] > cap)
        out.loc[mask] = cap
    return out


def split_train_holdout(
    df: pd.DataFrame,
    holdout_start: date,
    *,
    asof_col: str = "asof",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Strict-temporal split. Rows with asof < holdout_start → train; else holdout."""
    if df.empty:
        return df.iloc[0:0].copy(), df.iloc[0:0].copy()
    boundary = pd.Timestamp(holdout_start)
    asof_ts = pd.to_datetime(df[asof_col])
    train = df.loc[asof_ts < boundary].copy()
    holdout = df.loc[asof_ts >= boundary].copy()
    return train.reset_index(drop=True), holdout.reset_index(drop=True)


def aligned_train(
    feature_train: pd.DataFrame, target_train: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series]:
    """Inner-join + drop NaN target. Returns (features, target) ready for Lasso."""
    if feature_train.empty or target_train.empty:
        return feature_train.iloc[0:0].copy(), pd.Series(dtype=float)
    merged = feature_train.merge(target_train, on=["asof", "ticker"], how="inner")
    keep = merged.dropna(subset=["target"]).reset_index(drop=True)
    feat_cols = [c for c in keep.columns if c not in {"target"}]
    return keep[feat_cols], keep["target"].astype(float)
