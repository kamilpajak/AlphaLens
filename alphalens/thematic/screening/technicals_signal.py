"""Layer 4 signal 4 — technicals (RSI, MA50 distance, ATR, volume Z-score).

Pure-numpy primitives over a per-ticker OHLCV DataFrame (yfinance schema:
columns ``[open, high, low, close, volume]``). Wraps everything in
``score_technicals(ticker, asof, loader)`` which delegates the actual
OHLCV fetch to a caller-provided ``loader`` — same pattern as the FCFF
``feature_fetcher`` indirection.

The orchestrator can either preload yfinance OHLCV via
:func:`alphalens.data.alt_data.yfinance_cache.load_cached_histories` and
pass an in-memory lookup, or compose a live :func:`yf.Ticker.history`
call.

Each metric returns ``None`` when history is too short. The summary string
is always returned (never None) so it can render in the parquet without
NaN handling.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_RSI_PERIOD = 14
_MA_PERIOD = 50
_ATR_PERIOD = 14
_VOL_PERIOD = 20


def _compute_rsi(close: pd.Series, *, period: int = _RSI_PERIOD) -> float | None:
    """Wilder's RSI; returns last value or None when history < period+1."""
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    last_avg_loss = float(avg_loss.iloc[-1])
    if last_avg_loss == 0:
        return 100.0
    rs = float(avg_gain.iloc[-1]) / last_avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ma_distance_pct(close: pd.Series, *, period: int = _MA_PERIOD) -> float | None:
    """Percent distance of last close above the trailing simple MA."""
    if len(close) < period:
        return None
    ma = close.rolling(period).mean().iloc[-1]
    last = float(close.iloc[-1])
    if not np.isfinite(ma) or ma == 0:
        return None
    return 100.0 * (last - float(ma)) / float(ma)


def _compute_atr_pct(ohlcv: pd.DataFrame, *, period: int = _ATR_PERIOD) -> float | None:
    """Wilder's ATR normalised to last close (returns ``%`` of price)."""
    if len(ohlcv) < period + 1:
        return None
    high = ohlcv["high"].astype(float)
    low = ohlcv["low"].astype(float)
    close = ohlcv["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    last = float(close.iloc[-1])
    if not np.isfinite(atr) or last <= 0:
        return None
    return 100.0 * float(atr) / last


def _volume_zscore(volume: pd.Series, *, period: int = _VOL_PERIOD) -> float | None:
    """Z-score of the latest volume vs trailing ``period``-day distribution."""
    if len(volume) < period:
        return None
    window = volume.iloc[-period:].astype(float)
    mu = window.mean()
    sigma = window.std(ddof=0)
    if sigma == 0:
        return 0.0
    return float((float(volume.iloc[-1]) - mu) / sigma)


def _format_summary(metrics: dict[str, float | None]) -> str:
    """One-line human-readable summary for the parquet."""
    rsi = metrics.get("rsi")
    ma = metrics.get("ma50_distance_pct")
    atr = metrics.get("atr_pct")
    vz = metrics.get("volume_zscore")
    if all(v is None for v in (rsi, ma, atr, vz)):
        return "no data"
    parts = []
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if ma is not None:
        sign = "+" if ma >= 0 else ""
        parts.append(f"MA50 {sign}{ma:.1f}%")
    if atr is not None:
        parts.append(f"ATR {atr:.1f}%")
    if vz is not None:
        parts.append(f"volZ {vz:.1f}")
    return " / ".join(parts)


def score_technicals_from_frame(ohlcv: pd.DataFrame) -> dict[str, float | None | str]:
    """Compute the 4 technical metrics + a summary string from an OHLCV frame.

    The OHLCV frame must have ``[open, high, low, close, volume]`` columns
    (lowercase, yfinance-cache convention). Returns ``None`` for each metric
    when the window is too short. Summary is always a string.
    """
    if ohlcv is None or ohlcv.empty or "close" not in ohlcv.columns:
        empty = {
            "rsi": None,
            "ma50_distance_pct": None,
            "atr_pct": None,
            "volume_zscore": None,
        }
        return {**empty, "summary": _format_summary(empty)}

    close = ohlcv["close"].astype(float)
    volume = ohlcv.get("volume", pd.Series(dtype=float)).astype(float)
    metrics = {
        "rsi": _compute_rsi(close),
        "ma50_distance_pct": _ma_distance_pct(close),
        "atr_pct": _compute_atr_pct(ohlcv),
        "volume_zscore": _volume_zscore(volume) if not volume.empty else None,
    }
    return {**metrics, "summary": _format_summary(metrics)}


def score_technicals(
    *,
    ticker: str,
    asof: dt.date,
    loader: Callable[[str, dt.date], pd.DataFrame],
) -> dict[str, float | None | str]:
    """Top-level entry point — delegates OHLCV fetch to ``loader``.

    ``loader(ticker, asof)`` should return an OHLCV DataFrame ending at or
    before ``asof``; the caller picks the lookback window. The function
    coerces an empty / failed load into a no-data result rather than raising.
    """
    try:
        ohlcv = loader(ticker, asof)
    except Exception as exc:
        logger.warning("technicals loader failed for %s: %s", ticker, exc)
        ohlcv = pd.DataFrame()
    return score_technicals_from_frame(ohlcv)


__all__ = ["score_technicals", "score_technicals_from_frame"]
