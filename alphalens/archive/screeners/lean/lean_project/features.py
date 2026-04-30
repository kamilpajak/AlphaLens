"""Feature primitives used by the MVP1 rule-based scorer.

Pure pandas/numpy — no Lean/AlgorithmImports/etc. Designed to be testable on
the host and callable from inside the Lean algorithm (main.py) identically.

Convention: each function takes the full `close`/`volume` series (oldest to
newest, last element = most recent bar) and returns a single scalar for the
current bar. Insufficient history raises `ValueError`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd


def _as_series(values: Sequence[float] | pd.Series) -> pd.Series:
    if isinstance(values, pd.Series):
        return values
    return pd.Series(list(values))


def _require_length(series: pd.Series, needed: int, feature: str) -> None:
    if len(series) < needed:
        raise ValueError(f"{feature}: need at least {needed} bars, got {len(series)}")


def rate_of_change(close: Sequence[float] | pd.Series, window: int) -> float:
    """Rate of change over `window` bars: (today / today-window) - 1."""
    s = _as_series(close)
    _require_length(s, window + 1, "rate_of_change")
    past = float(s.iloc[-window - 1])
    current = float(s.iloc[-1])
    if past <= 0.0:
        return float("nan")
    return current / past - 1.0


def sma(close: Sequence[float] | pd.Series, window: int) -> float:
    """Simple moving average of the last `window` bars."""
    s = _as_series(close)
    _require_length(s, window, "sma")
    return float(s.iloc[-window:].mean())


def volume_surprise(volume: Sequence[float] | pd.Series, window: int) -> float:
    """Today's volume divided by the trailing `window`-day average (>1 = above avg)."""
    s = _as_series(volume)
    _require_length(s, window + 1, "volume_surprise")
    avg = float(s.iloc[-window - 1 : -1].mean())
    if avg <= 0.0:
        return float("nan")
    return float(s.iloc[-1]) / avg


def distance_to_high(close: Sequence[float] | pd.Series, window: int) -> float:
    """How far below the `window`-day high the last close is.

    0 means at/above the high; 1 means infinitely far below. The formula is
    `1 - close[-1] / max(close[-window:])`. Clipped to [0, 1].
    """
    s = _as_series(close)
    _require_length(s, window, "distance_to_high")
    window_high = float(s.iloc[-window:].max())
    if window_high <= 0.0:
        return float("nan")
    dist = 1.0 - float(s.iloc[-1]) / window_high
    return float(max(0.0, min(1.0, dist)))


def breakout(
    close: Sequence[float] | pd.Series,
    volume: Sequence[float] | pd.Series,
    window: int,
    volume_multiple: float,
) -> bool:
    """True if today's close exceeds the rolling `window`-day high AND today's
    volume is at least `volume_multiple` * trailing average.
    """
    c = _as_series(close)
    v = _as_series(volume)
    _require_length(c, window + 1, "breakout")
    _require_length(v, window + 1, "breakout")
    # Prior window's max (exclude today) — we want a "new" high.
    prior_high = float(c.iloc[-window - 1 : -1].max())
    prior_vol_avg = float(v.iloc[-window - 1 : -1].mean())
    today_close = float(c.iloc[-1])
    today_vol = float(v.iloc[-1])
    if prior_vol_avg <= 0.0 or math.isnan(prior_high):
        return False
    return today_close > prior_high and today_vol >= volume_multiple * prior_vol_avg


def trend_strength(
    close: Sequence[float] | pd.Series,
    sma_short: int,
    sma_medium: int,
    sma_long: int,
) -> float:
    """Return 1.0 if close > SMA(short) > SMA(medium) > SMA(long); 0.0 if fully inverted.

    Scores partial stacks proportionally in 1/3 steps.
    """
    s = _as_series(close)
    if sma_short >= sma_medium or sma_medium >= sma_long:
        raise ValueError("expected sma_short < sma_medium < sma_long")
    _require_length(s, sma_long, "trend_strength")
    c = float(s.iloc[-1])
    sma_s = sma(s, sma_short)
    sma_m = sma(s, sma_medium)
    sma_l = sma(s, sma_long)
    checks = [c > sma_s, sma_s > sma_m, sma_m > sma_l]
    return sum(1 for ok in checks if ok) / 3.0


def dollar_volume_average(
    close: Sequence[float] | pd.Series,
    volume: Sequence[float] | pd.Series,
    window: int,
) -> float:
    """Trailing `window`-day average dollar volume (close * volume)."""
    c = _as_series(close).astype(float)
    v = _as_series(volume).astype(float)
    _require_length(c, window, "dollar_volume_average")
    _require_length(v, window, "dollar_volume_average")
    dv = (c * v).iloc[-window:]
    return float(dv.mean())


def z_score(values: Sequence[float]) -> np.ndarray:
    """Cross-sectional z-score. NaNs kept as NaN; zero variance returns zeros."""
    arr = np.asarray(list(values), dtype=float)
    mean = np.nanmean(arr)
    std = np.nanstd(arr)
    if std <= 0.0 or math.isnan(std):
        return np.zeros_like(arr)
    return (arr - mean) / std
