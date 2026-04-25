"""Stateless macro signal functions for Tactical Sector Rotation (Layer 2e).

Every signal is a pure function of input pd.Series → output pd.Series. No I/O,
no caching, no external clients — the caller supplies the data (from FREDClient
or HistoryStore). Tests use inline fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def yield_curve_slope(dgs10: pd.Series, dgs2: pd.Series) -> pd.Series:
    """Slope = 10y yield − 2y yield, aligned on intersection of date indexes."""
    aligned = pd.concat([dgs10, dgs2], axis=1, join="inner")
    aligned.columns = ["dgs10", "dgs2"]
    return (aligned["dgs10"] - aligned["dgs2"]).rename("yield_curve_slope")


def vix_decile(vix: pd.Series, *, lookback: int = 252) -> pd.Series:
    """Percentile rank of current VIX vs trailing lookback window, in [0, 1].

    First ``lookback - 1`` observations are NaN. A value equal to the highest in
    its window ranks 1.0; equal to the lowest ranks ``1/lookback``.
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    return (
        vix.rolling(lookback)
        .apply(
            lambda w: (w.rank(method="max").iloc[-1]) / len(w),
            raw=False,
        )
        .rename("vix_decile")
    )


def trailing_return_spread(leader: pd.Series, laggard: pd.Series, *, lookback: int) -> pd.Series:
    """Trailing cumulative return of leader minus laggard over `lookback` bars.

    Used for QQQ/IWM momentum spread and IVW/IVE value/growth spread.
    NaN for the first ``lookback`` observations (insufficient history).
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    aligned = pd.concat([leader, laggard], axis=1, join="inner")
    aligned.columns = ["lead", "lag"]
    lead_ret = aligned["lead"] / aligned["lead"].shift(lookback) - 1.0
    lag_ret = aligned["lag"] / aligned["lag"].shift(lookback) - 1.0
    return (lead_ret - lag_ret).rename("trailing_return_spread")


@dataclass(frozen=True)
class SignalSet:
    yield_curve_slope: pd.Series
    vix_decile: pd.Series
    qqq_iwm_spread: pd.Series

    def as_of(self, date: pd.Timestamp) -> dict[str, float]:
        """Snapshot signal values on a given date (returns NaN-safe floats)."""
        return {
            "yield_curve_slope": float(self.yield_curve_slope.get(date, float("nan"))),
            "vix_decile": float(self.vix_decile.get(date, float("nan"))),
            "qqq_iwm_spread": float(self.qqq_iwm_spread.get(date, float("nan"))),
        }


def build_signal_set(
    *,
    dgs10: pd.Series,
    dgs2: pd.Series,
    vix: pd.Series,
    qqq_close: pd.Series,
    iwm_close: pd.Series,
    vix_lookback: int = 252,
    spread_lookback: int = 126,
) -> SignalSet:
    return SignalSet(
        yield_curve_slope=yield_curve_slope(dgs10, dgs2),
        vix_decile=vix_decile(vix, lookback=vix_lookback),
        qqq_iwm_spread=trailing_return_spread(qqq_close, iwm_close, lookback=spread_lookback),
    )
