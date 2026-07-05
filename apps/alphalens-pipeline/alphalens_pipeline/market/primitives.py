"""Stateless pure primitives for the market-state signal (PR-0).

Every function is a pure function of pandas input → pandas output: no I/O, no
store, no network, no stamping. The PR-1 ``market_state.classify`` composes
these into the discrete regime label. Tests use inline fixtures (see
``test_market_primitives.py``).

Design notes:
- ``rolling_quantile_rank`` generalizes the ``data.macro.signals.vix_decile``
  idiom (trailing percentile rank in [0, 1]) to any series — this is how the
  ATR% vol axis is made self-normalizing across regimes.
- ``atr`` uses Wilder smoothing (RMA = ``ewm(alpha=1/window, adjust=False)``).
- ``bollinger_keltner_squeeze`` is the TTM "squeeze" flag: the Bollinger Bands
  sitting entirely inside the Keltner Channel — a volatility-compression signal.
"""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over a trailing ``window`` of observations."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series.rolling(window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """Exponential moving average (``span=window, adjust=False`` → seeds on first obs)."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series.ewm(span=window, adjust=False).mean()


def normalized_slope(series: pd.Series, *, window: int) -> pd.Series:
    """Fractional change over ``window`` bars: ``(s_t − s_{t−window}) / s_t``.

    Normalizing by the current value makes the slope scale-free and comparable
    across price levels. The first ``window`` values are NaN. Assumes a positive
    series (prices); a zero value yields ``inf``/``NaN`` at that point.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    return (series - series.shift(window)) / series


def rolling_quantile_rank(series: pd.Series, *, lookback: int = 252) -> pd.Series:
    """Percentile rank of each value vs its trailing ``lookback`` window, in [0, 1].

    Generalizes ``data.macro.signals.vix_decile``. The first ``lookback − 1``
    observations are NaN. A value equal to the highest in its window ranks 1.0;
    equal to the lowest ranks ``1 / lookback``.

    Performance: O(n·lookback) via a rolling ``apply``; fine for typical daily
    histories (~7500 bars × lookback 252), not pandas-vectorised.
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    return series.rolling(lookback).apply(
        lambda w: w.rank(method="max").iloc[-1] / len(w), raw=False
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder true range: ``max(high−low, |high−prev_close|, |low−prev_close|)``.

    The first bar has no prior close, so its true range is ``high−low``. An
    empty input returns an empty series (rather than raising on the first-bar
    assignment below).
    """
    if len(high) == 0:
        return pd.Series(dtype=float)
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    tr = ranges.max(axis=1)
    # First bar: no prior close → the two gap terms are NaN; fall back to high−low.
    tr.iloc[0] = (high - low).iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, *, window: int = 14) -> pd.Series:
    """Average true range via Wilder smoothing (RMA = ``ewm(alpha=1/window)``)."""
    if window <= 0:
        raise ValueError("window must be positive")
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / window, adjust=False).mean()


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, *, window: int = 14) -> pd.Series:
    """ATR as a fraction of close — a scale-free realized-volatility proxy.

    Assumes positive ``close`` (prices); a zero close yields ``inf``/``NaN``.
    """
    return atr(high, low, close, window=window) / close


def bollinger_keltner_squeeze(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    bb_window: int = 20,
    bb_k: float = 2.0,
    kc_window: int = 20,
    kc_mult: float = 1.5,
) -> pd.Series:
    """TTM squeeze flag: True when the Bollinger Bands sit entirely inside the
    Keltner Channel (volatility compression). Warmup bars are False.

    Bollinger: ``sma(close, bb_window) ± bb_k · std(close, bb_window)`` (population
    std, ddof=0). Keltner: ``ema(close, kc_window) ± kc_mult · atr(kc_window)``.
    """
    bb_mid = sma(close, bb_window)
    bb_std = close.rolling(bb_window).std(ddof=0)
    bb_upper = bb_mid + bb_k * bb_std
    bb_lower = bb_mid - bb_k * bb_std

    kc_mid = ema(close, kc_window)
    kc_atr = atr(high, low, close, window=kc_window)
    kc_upper = kc_mid + kc_mult * kc_atr
    kc_lower = kc_mid - kc_mult * kc_atr

    squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    # Comparisons against warmup NaNs already yield False; coerce to a clean bool dtype.
    return squeeze.fillna(False).astype(bool)
