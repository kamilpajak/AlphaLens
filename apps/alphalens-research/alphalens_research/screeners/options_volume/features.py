"""PIT-correct feature joiner for the P/C abnormal-volume scorer.

Pre-registered as ``pc_abnormal_volume_retrospective_pre_2018_2026_05_05``
(sha256 ``03ddf4b7906ed07049bbb74dcdd599afa29abda1e8c4f6551a1876c78e45e689``).

Reads from the iVolatility smd cache and produces, per (ticker, asof), five
columns: ``[abnormal_pcr, log_marketCap, reversal_1m, momentum_6m, rv_30d]``.

Universe filters (mirroring v9D / options_implied):
- Exchange in US-primary set
- ADV (20-day median dollar volume) ≥ ``adv_min_dollar``
- Underlying close ≥ $1
- Dynamic optionable filter on t-1
- ≥ 127 trailing trading days for momentum_6m baseline
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import pandas as pd

from alphalens_research.screeners.options_implied.features import (
    _check_adv,
    _check_optionable,
    _compute_equity_controls,
    _filter_us_primary,
)
from alphalens_research.screeners.options_volume.pc_abnormal_volume import (
    compute_abnormal_pcr_series,
)

logger = logging.getLogger(__name__)

FEATURE_COLUMNS: tuple[str, ...] = (
    "abnormal_pcr",
    "reversal_1m",
    "momentum_6m",
    "rv_30d",
)

_MIN_PRICE = 1.0


def _slice_pit(history: pd.DataFrame, asof: str) -> pd.DataFrame:
    """Rows with ``tradeDate <= asof`` AND non-NaN trading flag.

    Uses ``close`` for the trading-day flag (rather than ivp30 like
    options_implied) — this scorer does not depend on IV-level fields, so a
    valid close is the right anchor. Sorted ascending; idx reset.
    """
    if history.empty or "tradeDate" not in history.columns:
        return history
    asof_ts = pd.Timestamp(asof)
    sliced = history.loc[pd.to_datetime(history["tradeDate"]) <= asof_ts]
    if "close" in sliced.columns:
        sliced = sliced.loc[sliced["close"].notna()]
    return sliced.sort_values("tradeDate").reset_index(drop=True)


def _prepare_ticker_history(
    history: pd.DataFrame | None,
) -> pd.DataFrame | None:
    """One-shot per-ticker preprocessing; returns None if the ticker is ineligible.

    Filters to US-primary exchanges, drops NaN-close rows, sorts by tradeDate,
    and pre-computes the abnormal-pcr series + tradeDate_ts column for fast
    per-asof slicing. Returns the prepared DataFrame or None when any
    structural prerequisite fails.
    """
    if history is None or history.empty:
        return None
    history = _filter_us_primary(history)
    if history.empty or "close" not in history.columns or "tradeDate" not in history.columns:
        return None
    history = history.loc[history["close"].notna()].copy()
    if history.empty:
        return None
    history = history.sort_values("tradeDate").reset_index(drop=True)
    if "optVolPut" not in history.columns or "optVolCall" not in history.columns:
        return None
    abnormal_pcr = compute_abnormal_pcr_series(history["optVolPut"], history["optVolCall"])
    history["_abnormal_pcr"] = abnormal_pcr.to_numpy()
    history["_tradeDate_ts"] = pd.to_datetime(history["tradeDate"])
    return history


def _emit_row_for_asof(
    history: pd.DataFrame,
    asof_dt: pd.Timestamp,
    *,
    adv_min_dollar: float,
) -> dict | None:
    """Build the feature row for one (history, asof). Returns None on filter failure."""
    sliced = history.loc[history["_tradeDate_ts"] <= asof_dt]
    if sliced.empty or len(sliced) < 2:
        return None
    if not _check_optionable(sliced.iloc[-2]):
        return None
    if not _check_adv(sliced, min_dollar=adv_min_dollar):
        return None
    last_row = sliced.iloc[-1]
    close_now = last_row.get("close")
    if pd.isna(close_now) or close_now < _MIN_PRICE:
        return None
    abnormal_pcr_t = last_row["_abnormal_pcr"]
    if pd.isna(abnormal_pcr_t):
        return None
    controls = _compute_equity_controls(sliced)
    if controls is None:
        return None
    return {"abnormal_pcr": float(abnormal_pcr_t), **controls}


def build_feature_frame(
    *,
    smd_loader: Callable[[str], pd.DataFrame | None],
    universe: Sequence[str],
    asof_dates: Sequence[str],
    adv_min_dollar: float = 2_000_000.0,
) -> pd.DataFrame:
    """Long-format frame with ``[asof, ticker, *FEATURE_COLUMNS]`` per (asof, ticker).

    The abnormal_pcr time-series is computed once per ticker over the ticker's
    full history (PIT-correct via shift+rolling internally), then sliced at
    each asof. Equity controls are computed on the asof slice from the close
    series and the last-row marketCap.
    """
    asof_strs = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in asof_dates]
    asof_ts = [pd.Timestamp(d) for d in asof_strs]

    rows: list[dict] = []
    for ticker in universe:
        prepared = _prepare_ticker_history(smd_loader(ticker))
        if prepared is None:
            continue
        for asof, asof_dt in zip(asof_strs, asof_ts, strict=True):
            row = _emit_row_for_asof(prepared, asof_dt, adv_min_dollar=adv_min_dollar)
            if row is None:
                continue
            rows.append({"asof": asof, "ticker": ticker.upper(), **row})

    if not rows:
        return pd.DataFrame(columns=("asof", "ticker", *FEATURE_COLUMNS))
    return pd.DataFrame(rows)
