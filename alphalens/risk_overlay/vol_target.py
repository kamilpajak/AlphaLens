"""Volatility-targeting overlay (Moreira & Muir 2017, Journal of Finance).

Scale daily portfolio exposure by ``target_vol / realized_vol_t-1``, where
realized_vol is a rolling-window estimate of the strategy's own returns.
A ``max_leverage`` cap prevents the multiplier from blowing up when
realized vol approaches zero.

Causality contract: ``scale[t]`` is computed from ``returns[< t]`` only.
This is enforced by ``_RollingStdEstimator.estimate``, which slices on
``index < asof`` (strict) before aggregating. Tests guard the contract.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class RealizedVolEstimator(Protocol):
    """Strict-history vol estimator. ``None`` when history is too short."""

    def estimate(self, returns: pd.Series, asof: pd.Timestamp) -> float | None: ...


class _RollingStdEstimator:
    """Rolling sample standard deviation, annualised by ``periods_per_year``."""

    def __init__(self, lookback: int, periods_per_year: int):
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        self.lookback = lookback
        self.periods_per_year = periods_per_year

    def estimate(self, returns: pd.Series, asof: pd.Timestamp) -> float | None:
        history = returns.loc[returns.index < asof]
        if len(history) < self.lookback:
            return None
        window = history.iloc[-self.lookback :]
        return float(window.std(ddof=1) * math.sqrt(self.periods_per_year))


class VolTargeter:
    """Scale portfolio returns to a target annualised volatility.

    Parameters
    ----------
    target_vol
        Annualised vol target (e.g. ``0.10`` for 10%).
    lookback
        Rolling window for the realised-vol estimate, expressed in
        rebalance periods (NOT trading days). Caller passes whatever
        cadence the engine runs on. Per Moreira-Muir 2017 the canonical
        choice tracks ~1 month of returns at the rebalance grain.
    periods_per_year
        Rebalances per year (252/stride for the engine's calendar).
    max_leverage
        Cap on the multiplier. Default 1.5 mirrors Moreira-Muir 2017's
        unconditional-variance target. Cap is the only thing keeping the
        multiplier finite when realised vol → 0.
    """

    def __init__(
        self,
        *,
        target_vol: float,
        lookback: int,
        periods_per_year: int,
        max_leverage: float = 1.5,
    ):
        if target_vol <= 0:
            raise ValueError("target_vol must be positive")
        if max_leverage <= 0:
            raise ValueError("max_leverage must be positive")
        self.target_vol = float(target_vol)
        self.max_leverage = float(max_leverage)
        self._estimator = _RollingStdEstimator(lookback, periods_per_year)

    def scale_factor(self, returns: pd.Series, asof: pd.Timestamp) -> float:
        rv = self._estimator.estimate(returns, asof)
        if rv is None or rv <= 0:
            return 1.0
        raw = self.target_vol / rv
        return min(raw, self.max_leverage)

    def scale_series(self, returns: pd.Series) -> pd.Series:
        """Per-period scale factors aligned to ``returns.index``."""
        if returns.empty:
            return pd.Series([], dtype=float, index=returns.index, name="scale")
        return pd.Series(
            [self.scale_factor(returns, ts) for ts in returns.index],
            index=returns.index,
            name="scale",
        )


def apply_vol_target(returns: pd.Series, targeter: VolTargeter) -> pd.Series:
    """Apply vol-targeting to a portfolio-returns Series.

    For each timestamp ``t`` this computes ``scale[t]`` from
    ``returns[< t]`` and yields ``scaled[t] = scale[t] * returns[t]``.
    Insufficient-history points pass through (scale = 1.0).
    """
    if returns.empty:
        return returns.copy()
    scales = targeter.scale_series(returns)
    return (returns * scales).rename(returns.name or "portfolio")
