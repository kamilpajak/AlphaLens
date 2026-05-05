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
import math  # math.sqrt used downstream
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

from alphalens.screeners.options_implied.features import (
    US_PRIMARY_EXCHANGES,
    _check_adv,
    _check_optionable,
    _coerce_numeric_columns,
)
from alphalens.screeners.options_volume.pc_abnormal_volume import (
    compute_abnormal_pcr_series,
)

logger = logging.getLogger(__name__)

FEATURE_COLUMNS: tuple[str, ...] = (
    "abnormal_pcr",
    "reversal_1m",
    "momentum_6m",
    "rv_30d",
)

_MIN_TRAILING_TRADING_DAYS = 127  # 126 for momentum_6m + 1 for current
_MIN_PRICE = 1.0


def _filter_us_primary(history: pd.DataFrame) -> pd.DataFrame:
    if "exchange" not in history.columns:
        return _coerce_numeric_columns(history.copy())
    mask = history["exchange"].astype(str).isin(US_PRIMARY_EXCHANGES)
    return _coerce_numeric_columns(history.loc[mask].copy())


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


def _compute_equity_controls(sliced: pd.DataFrame) -> dict[str, float] | None:
    """log_marketCap, reversal_1m, momentum_6m, rv_30d from the last close + marketCap."""
    closes = sliced["close"].to_numpy(dtype=float)
    n = len(closes)
    if n < _MIN_TRAILING_TRADING_DAYS:
        return None
    if not np.all(np.isfinite(closes[-_MIN_TRAILING_TRADING_DAYS:])):
        return None
    if closes[-1] <= 0:
        return None

    close_now = closes[-1]
    close_21 = closes[-22]
    close_126 = closes[-127]
    if close_21 <= 0 or close_126 <= 0:
        return None

    reversal_1m = -((close_now / close_21) - 1.0)
    momentum_6m = (close_21 / close_126) - 1.0

    if n < 31:
        return None
    log_rets = np.diff(np.log(closes[-31:]))
    if len(log_rets) < 30 or not np.all(np.isfinite(log_rets)):
        return None
    rv_30d = float(np.std(log_rets, ddof=1)) * math.sqrt(252.0)

    return {
        "reversal_1m": reversal_1m,
        "momentum_6m": momentum_6m,
        "rv_30d": rv_30d,
    }


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
        history = smd_loader(ticker)
        if history is None or history.empty:
            continue
        history = _filter_us_primary(history)
        if history.empty or "close" not in history.columns or "tradeDate" not in history.columns:
            continue

        # Trading-day rows (close non-NaN), sorted ascending.
        history = history.loc[history["close"].notna()].copy()
        if history.empty:
            continue
        history = history.sort_values("tradeDate").reset_index(drop=True)
        if "optVolPut" not in history.columns or "optVolCall" not in history.columns:
            continue

        # PIT-correct abnormal-pcr series over the ticker's whole history.
        abnormal_pcr = compute_abnormal_pcr_series(history["optVolPut"], history["optVolCall"])
        history["_abnormal_pcr"] = abnormal_pcr.to_numpy()
        history["_tradeDate_ts"] = pd.to_datetime(history["tradeDate"])

        for asof, asof_dt in zip(asof_strs, asof_ts, strict=True):
            sliced = history.loc[history["_tradeDate_ts"] <= asof_dt]
            if sliced.empty or len(sliced) < 2:
                continue

            prev_row = sliced.iloc[-2]
            if not _check_optionable(prev_row):
                continue

            if not _check_adv(sliced, min_dollar=adv_min_dollar):
                continue

            last_row = sliced.iloc[-1]
            close_now = last_row.get("close")
            if pd.isna(close_now) or close_now < _MIN_PRICE:
                continue

            abnormal_pcr_t = last_row["_abnormal_pcr"]
            if pd.isna(abnormal_pcr_t):
                continue

            controls = _compute_equity_controls(sliced)
            if controls is None:
                continue

            rows.append(
                {
                    "asof": asof,
                    "ticker": ticker.upper(),
                    "abnormal_pcr": float(abnormal_pcr_t),
                    **controls,
                }
            )

    if not rows:
        return pd.DataFrame(columns=("asof", "ticker", *FEATURE_COLUMNS))
    return pd.DataFrame(rows)
