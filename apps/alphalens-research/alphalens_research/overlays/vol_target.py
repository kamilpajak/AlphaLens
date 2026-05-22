"""Volatility-targeting overlay (Moreira & Muir 2017, Journal of Finance).

Scale daily portfolio exposure by ``target_vol / realized_vol_t-1``, where
realized_vol is a rolling-window estimate of the strategy's own returns.
A ``max_leverage`` cap prevents the multiplier from blowing up when
realized vol approaches zero.

Causality contract: ``scale[t]`` is computed from ``returns[< t]`` only.
``scale_series`` enforces this with ``returns.rolling(L).std().shift(1)``
— the rolling window ending at ``t-1`` is the realised-vol estimate that
sets ``scale[t]``. Tests guard the contract directly.

Defensive fallbacks (all yield ``scale = 1.0`` = neutral identity scaling):
  - insufficient history (warmup periods)
  - NaN inside the rolling window (corrupt input)
  - realised vol exactly zero (degenerate state — logged at WARNING)
"""

from __future__ import annotations

import logging
import math
from typing import Protocol, runtime_checkable

import pandas as pd

logger = logging.getLogger(__name__)

# Tolerance for "is realised vol effectively zero?" — covers exact zero
# (constant returns) AND any FP-rounding residue from std on near-
# constant data. Below this, target/rv would balloon to ~1e10+ and
# clip to max_leverage anyway, but treating the regime as degenerate
# (scale=1.0, log a WARNING) is cleaner than amplifying noise.
_ZERO_VOL_TOL: float = 1e-12


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
        std = float(window.std(ddof=1) * math.sqrt(self.periods_per_year))
        if math.isnan(std):
            return None
        return std


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
        """Compute one scale factor (single ``asof``) — convenience for
        programmatic callers. Hot path is ``scale_series`` which is O(N)
        via vectorised rolling. Both honour the same fallback contract."""
        rv = self._estimator.estimate(returns, asof)
        if rv is None or math.isnan(rv) or rv <= _ZERO_VOL_TOL:
            if rv is not None and not math.isnan(rv) and abs(rv) <= _ZERO_VOL_TOL:
                logger.warning(
                    "vol_target.scale_factor: realised vol is effectively zero "
                    "(|rv|=%s <= %s) at asof=%s — degenerate state, falling "
                    "back to scale=1.0",
                    rv,
                    _ZERO_VOL_TOL,
                    asof,
                )
            return 1.0
        raw = self.target_vol / rv
        return min(raw, self.max_leverage)

    def scale_series(self, returns: pd.Series) -> pd.Series:
        """Vectorised per-period scale factors aligned to ``returns.index``.

        Computes ``rolling(lookback).std().shift(1)`` once and derives the
        per-period multiplier from that, rather than re-slicing history at
        every timestamp. Honours the same fallback contract as
        ``scale_factor``: insufficient history, NaN windows, and zero
        realised vol all yield 1.0; the zero-vol case logs a WARNING."""
        if returns.empty:
            return pd.Series([], dtype=float, index=returns.index, name="scale")

        L = self._estimator.lookback
        ppy = self._estimator.periods_per_year
        # Rolling std of the strategy's own returns; default min_periods=L
        # yields NaN for the first L-1 points (warmup) and for any window
        # that contains a NaN return (corrupt input).
        rv_unshifted = returns.rolling(L).std(ddof=1) * math.sqrt(ppy)
        # Strict less-than: scale[t] uses returns[<t], so shift the rolling
        # estimate forward by one period.
        rv = rv_unshifted.shift(1)

        # Distinguish degenerate near-zero vol from missing/insufficient
        # history. NaN in rv covers (a) warmup, (b) NaN inside the window.
        # Both collapse silently to scale=1.0 below. Near-zero vol (|rv|
        # within FP tolerance of 0) is a separate degenerate state worth
        # logging — `target/rv` would otherwise balloon to ~1e10+ and clip
        # to max_leverage, masking the absent signal.
        zero_vol_mask = rv.abs() <= _ZERO_VOL_TOL
        if zero_vol_mask.any():
            logger.warning(
                "vol_target.scale_series: realised vol is effectively zero "
                "(|rv| <= %s) at %d timestamps — degenerate state, falling "
                "back to scale=1.0 (not amplifying the zero-variance window)",
                _ZERO_VOL_TOL,
                int(zero_vol_mask.sum()),
            )

        # Avoid div-by-(near-)zero by routing degenerate-vol through NaN,
        # which is then filled by the final `fillna(1.0)`.
        rv_safe = rv.where(rv > _ZERO_VOL_TOL)
        raw = self.target_vol / rv_safe
        scaled = raw.clip(upper=self.max_leverage)
        return scaled.fillna(1.0).rename("scale")


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
