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
_MA200_PERIOD = 200
_ATR_PERIOD = 14
_VOL_PERIOD = 20
_FIFTY_TWO_WEEK_PERIOD = 252  # trading days in ~1 year
_MA200_SLOPE_LOOKBACK_DAYS = 20  # slope = % change in MA200 over last 20 trading days


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


def _ma_slope_pct_per_day(
    close: pd.Series,
    *,
    period: int = _MA200_PERIOD,
    lookback: int = _MA200_SLOPE_LOOKBACK_DAYS,
) -> float | None:
    """Slope of the MA over the last ``lookback`` trading days as % per day.

    Positive = uptrending MA (long-term trend support). Negative = downtrend.
    Per Perplexity research: a meaningful "promotion" requires not just
    distance below the MA but an upward-sloping MA — otherwise the stock
    is a falling knife, not a mean-reversion candidate.
    """
    if len(close) < period + lookback:
        return None
    ma_series = close.rolling(period).mean().dropna()
    if len(ma_series) < lookback + 1:
        return None
    end_val = float(ma_series.iloc[-1])
    start_val = float(ma_series.iloc[-1 - lookback])
    if not np.isfinite(end_val) or not np.isfinite(start_val) or start_val == 0:
        return None
    return 100.0 * (end_val - start_val) / start_val / lookback


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


def _pct_off_52w_high(close: pd.Series, *, period: int = _FIFTY_TWO_WEEK_PERIOD) -> float | None:
    """Last close vs trailing 52-week high, as a signed percent.

    0.0 means at the high; negative means below. Captures "off-peak"
    drawdown — the price-action analog to a "mega promotion" intuition
    that's not visible in fundamentals.
    """
    if len(close) < period:
        return None
    window = close.iloc[-period:].astype(float)
    peak = float(window.max())
    last = float(close.iloc[-1])
    if not np.isfinite(peak) or peak <= 0:
        return None
    return 100.0 * (last - peak) / peak


def _pct_off_52w_low(close: pd.Series, *, period: int = _FIFTY_TWO_WEEK_PERIOD) -> float | None:
    """Last close vs trailing 52-week low, as a signed percent.

    0.0 means at the low; positive means above. High value = far above
    52w trough (possible parabolic / mean-reversion risk).
    """
    if len(close) < period:
        return None
    window = close.iloc[-period:].astype(float)
    trough = float(window.min())
    last = float(close.iloc[-1])
    if not np.isfinite(trough) or trough <= 0:
        return None
    return 100.0 * (last - trough) / trough


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
    pct_high = metrics.get("pct_off_52w_high")
    pct_low = metrics.get("pct_off_52w_low")
    ma200 = metrics.get("ma200_distance_pct")
    ma200_slope = metrics.get("ma200_slope_pct_per_day")
    if all(v is None for v in (rsi, ma, atr, vz, pct_high, pct_low, ma200, ma200_slope)):
        return "no data"
    parts = []
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if ma is not None:
        sign = "+" if ma >= 0 else ""
        parts.append(f"MA50 {sign}{ma:.1f}%")
    if ma200 is not None:
        sign = "+" if ma200 >= 0 else ""
        slope_tag = ""
        if ma200_slope is not None:
            slope_tag = f" (slope {'+' if ma200_slope >= 0 else ''}{ma200_slope:.3f}%/d)"
        parts.append(f"MA200 {sign}{ma200:.1f}%{slope_tag}")
    if pct_high is not None:
        sign = "+" if pct_high >= 0 else ""
        parts.append(f"52w high {sign}{pct_high:.1f}%")
    if pct_low is not None:
        sign = "+" if pct_low >= 0 else ""
        parts.append(f"52w low {sign}{pct_low:.1f}%")
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
            "pct_off_52w_high": None,
            "pct_off_52w_low": None,
            "ma200_distance_pct": None,
            "ma200_slope_pct_per_day": None,
        }
        return {**empty, "summary": _format_summary(empty)}

    close = ohlcv["close"].astype(float)
    volume = ohlcv.get("volume", pd.Series(dtype=float)).astype(float)
    metrics = {
        "rsi": _compute_rsi(close),
        "ma50_distance_pct": _ma_distance_pct(close),
        "atr_pct": _compute_atr_pct(ohlcv),
        "volume_zscore": _volume_zscore(volume) if not volume.empty else None,
        "pct_off_52w_high": _pct_off_52w_high(close),
        "pct_off_52w_low": _pct_off_52w_low(close),
        "ma200_distance_pct": _ma_distance_pct(close, period=_MA200_PERIOD),
        "ma200_slope_pct_per_day": _ma_slope_pct_per_day(close),
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
