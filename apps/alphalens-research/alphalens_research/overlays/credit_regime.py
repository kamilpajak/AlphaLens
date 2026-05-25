"""Credit-regime risk overlay (Layer 4) — HY OAS z-score → exposure.

Sibling to ``vol_target.py`` (Moreira-Muir 2017). Linear interpolation
between exposure 1.0 at z<=-1 (full equity) and 0.5 at z>=+1 (half cash
at risk-free). Strict-history contract — z-score at asof t uses spread
observations < t only.

Hyperparameters locked to literature pre-look:
- Lookback 252 BD (1y rolling) — Frazzini-Pedersen 2014 BAB convention.
- z thresholds ±1 (1-sigma) — Frazzini-Pedersen 2014 + Asness-Frazzini-
  Pedersen 2019 QMJ regime-gate convention.
- Min exposure 0.5 — Moreira-Muir 2017 vol-target with relaxed floor.

Pre-committed contingency: if Phase A overlay sanity check on TRAIN
fails (correlation between spread_z and forward 21d market return is
non-monotone or sign-drifts across decade-windows), DROP this overlay
from PRIMARY hypothesis and run pure-Layer-2 long-only safe-decile.
"""

from __future__ import annotations

import pandas as pd
from alphalens_pipeline.data.macro.signals import hy_oas_z_from_series

_NEUTRAL_EXPOSURE = 1.0
_MIN_EXPOSURE_AT_PLUS_ONE_Z = 0.5
_INTERP_AT_ZERO = 0.75


def credit_exposure_from_z(
    spread_z: float | None,
    *,
    low_z: float = -1.0,
    high_z: float = +1.0,
    full_exposure: float = _NEUTRAL_EXPOSURE,
    min_exposure: float = _MIN_EXPOSURE_AT_PLUS_ONE_Z,
) -> float:
    """Linear-interp exposure ∈ [min_exposure, full_exposure].

    ``z <= low_z`` → full_exposure. ``z >= high_z`` → min_exposure.
    Linear in between. ``None`` (warmup / insufficient history) →
    full_exposure (neutral).
    """
    if spread_z is None:
        return full_exposure
    if spread_z <= low_z:
        return full_exposure
    if spread_z >= high_z:
        return min_exposure
    # Linear interp from (low_z, full) to (high_z, min)
    span = high_z - low_z
    frac = (spread_z - low_z) / span
    return full_exposure + frac * (min_exposure - full_exposure)


class CreditRegimeOverlay:
    """HY OAS z-score-driven exposure gate.

    Caller supplies the spread Series (e.g. FRED ``BAMLH0A0HYM2``) once
    at construction; ``exposure(asof)`` queries strict-history z-score
    and maps via :func:`credit_exposure_from_z`.

    Parameters
    ----------
    spread_series
        Time-indexed pd.Series of HY OAS in percent (e.g. 4.5 for 450bp).
    lookback
        Rolling lookback for z-score in observations (default 252).
    low_z, high_z
        Linear-interp thresholds (default ±1).
    min_exposure, full_exposure
        Exposure floor/ceiling (default 0.5 / 1.0).
    """

    def __init__(
        self,
        *,
        spread_series: pd.Series,
        lookback: int = 252,
        low_z: float = -1.0,
        high_z: float = +1.0,
        min_exposure: float = _MIN_EXPOSURE_AT_PLUS_ONE_Z,
        full_exposure: float = _NEUTRAL_EXPOSURE,
    ):
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if low_z >= high_z:
            raise ValueError("low_z must be strictly less than high_z")
        if min_exposure < 0.0 or min_exposure > full_exposure:
            raise ValueError("0 <= min_exposure <= full_exposure")
        self._spread = spread_series
        self._lookback = lookback
        self._low_z = low_z
        self._high_z = high_z
        self._min_exposure = min_exposure
        self._full_exposure = full_exposure

    def exposure(self, asof: pd.Timestamp) -> float:
        """Strict-history exposure at ``asof``.

        Uses spread observations < asof to compute z-score. Falls back to
        ``full_exposure`` (neutral) on insufficient history.
        """
        z = hy_oas_z_from_series(self._spread, asof, lookback=self._lookback)
        return credit_exposure_from_z(
            z,
            low_z=self._low_z,
            high_z=self._high_z,
            full_exposure=self._full_exposure,
            min_exposure=self._min_exposure,
        )

    def exposure_series(self, dates: pd.DatetimeIndex) -> pd.Series:
        """Vectorised over ``dates`` — convenience for backtest drivers."""
        return pd.Series([self.exposure(d) for d in dates], index=dates, name="credit_exposure")
