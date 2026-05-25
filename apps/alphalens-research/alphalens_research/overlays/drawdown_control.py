"""Drawdown-control overlay (de-lever-only step function on equity curve).

Sibling to ``vol_target.VolTargeter`` in ADR 0007's Layer 4. Where vol-target
sizes by realised vol and may *lever up* (cap=1.5 per Moreira-Muir 2017),
drawdown-control sizes by realised drawdown from peak and may *only de-lever*
(cap=1.0). Designed after vol-target v1 FAIL'd by levering into a no-alpha
drawdown — leverage is a footgun on a base whose alpha hasn't been confirmed.

Rule (default thresholds — frozen in pre-reg ``v10_drawdown_overlay_…``):
  - weight = 1.0 when current drawdown from rolling peak ≤ 5%
  - weight = 0.5 when drawdown ∈ (5%, 10%]
  - weight = 0.0 when drawdown > 10%
  - re-arming the full-exposure regime requires the equity curve to recover
    within ``recovery_band_pct`` of the prior peak (default 2%) — prevents
    whipsaw at the band boundary.

Causality contract (same as VolTargeter): ``scale[t]`` is computed from
``returns[< t]`` only — equity curve and rolling peak both end at ``t-1``.
``scale_series`` enforces this with a single ``shift(1)``.

No predictive features (no macro inputs, no factor data). Output ∈ [0, 1].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DrawdownControlConfig:
    """Step-function thresholds + recovery band. Pre-reg should freeze these."""

    light_dd: float = 0.05
    """Drawdown ≤ this → full exposure (weight=1.0)."""

    heavy_dd: float = 0.10
    """Drawdown ≥ this → zero exposure (weight=0.0).

    Drawdowns in ``(light_dd, heavy_dd)`` get half exposure (weight=0.5)."""

    half_weight: float = 0.5
    """Mid-band exposure level (between light_dd and heavy_dd)."""

    recovery_band_pct: float = 0.02
    """Re-arm full exposure when equity within this fraction of the rolling peak."""

    peak_lookback: int = 0
    """Rolling-peak window in periods. 0 = expanding peak (since-inception max).

    Non-zero values use a rolling window of this many *rebalance* periods
    (NOT trading days), matching the unit convention of VolTargeter.lookback."""

    def __post_init__(self) -> None:
        if not 0 < self.light_dd < self.heavy_dd:
            raise ValueError(f"require 0 < light_dd ({self.light_dd}) < heavy_dd ({self.heavy_dd})")
        if not 0.0 <= self.half_weight <= 1.0:
            raise ValueError(f"half_weight must be in [0, 1], got {self.half_weight}")
        if not 0.0 <= self.recovery_band_pct < 1.0:
            raise ValueError(f"recovery_band_pct must be in [0, 1), got {self.recovery_band_pct}")
        if self.peak_lookback < 0:
            raise ValueError(f"peak_lookback must be ≥ 0, got {self.peak_lookback}")


class DrawdownControlOverlay:
    """De-lever-only equity-curve drawdown overlay. Output ∈ [0, 1]."""

    def __init__(self, config: DrawdownControlConfig | None = None):
        self.config = config or DrawdownControlConfig()

    def scale_series(self, returns: pd.Series) -> pd.Series:
        """Per-period exposure multiplier aligned to ``returns.index``.

        ``scale[t]`` is derived from the equity curve through ``t-1`` only
        (single ``shift(1)`` enforces this). Insufficient-history points
        yield 1.0 (identity). The hysteresis logic — once weight drops, it
        does not recover until equity is within ``recovery_band_pct`` of
        the prior peak — is applied iteratively in a single forward pass
        on the historical curve.
        """
        if returns.empty:
            return pd.Series([], dtype=float, index=returns.index, name="scale")

        cfg = self.config
        # Equity curve: cumprod of (1+r). NaN returns treated as 0 contribution
        # to the curve so the rolling peak doesn't break, but they are *not*
        # forward-filled in the input — caller is responsible for input quality.
        equity = (1.0 + returns.fillna(0.0)).cumprod()

        # Rolling (or expanding) peak through to time t.
        if cfg.peak_lookback == 0:
            peak = equity.cummax()
        else:
            peak = equity.rolling(cfg.peak_lookback, min_periods=1).max()

        # Drawdown is non-negative: 1 - equity/peak.
        drawdown = 1.0 - (equity / peak)

        # Apply hysteresis: once weight drops below 1.0, it stays below 1.0
        # until equity is within recovery_band_pct of the rolling peak. This
        # is the realistic regime — without it the overlay whipsaws at the
        # band boundary.
        weights = pd.Series(1.0, index=returns.index, dtype=float)
        in_drawdown_regime = False
        recovery_threshold = 1.0 - cfg.recovery_band_pct
        for t, dd in zip(drawdown.index, drawdown.to_numpy(), strict=False):
            weight, in_drawdown_regime = _weight_for_timestep(
                dd=float(dd),
                equity_t=float(equity.loc[t]),
                peak_t=float(peak.loc[t]),
                in_drawdown_regime=in_drawdown_regime,
                recovery_threshold=recovery_threshold,
                cfg=cfg,
            )
            weights.loc[t] = weight

        # Causality: scale[t] uses returns[<t], so shift the trigger forward.
        # First period has no prior history → scale=1.0 (identity).
        return weights.shift(1).fillna(1.0).rename("scale")


def _weight_for_timestep(
    *,
    dd: float,
    equity_t: float,
    peak_t: float,
    in_drawdown_regime: bool,
    recovery_threshold: float,
    cfg: DrawdownControlConfig,
) -> tuple[float, bool]:
    """Compute (weight, new_in_drawdown_regime) for a single timestep.

    Extracted from ``DrawdownControlOverlay.scale_series`` to lower
    cognitive complexity; behavior identical to the inline implementation.
    """
    if in_drawdown_regime:
        ratio = equity_t / peak_t if peak_t > 0 else 1.0
        if ratio >= recovery_threshold:
            in_drawdown_regime = False
    if not in_drawdown_regime and dd <= cfg.light_dd:
        return 1.0, False
    if dd >= cfg.heavy_dd:
        return 0.0, True
    # Either above light_dd or currently inside drawdown regime → half weight.
    return cfg.half_weight, True


def apply_drawdown_control(returns: pd.Series, overlay: DrawdownControlOverlay) -> pd.Series:
    """Apply drawdown-control overlay to a portfolio-returns Series.

    For each timestamp ``t`` this computes ``scale[t]`` from ``returns[<t]``
    and yields ``scaled[t] = scale[t] * returns[t]``.
    """
    if returns.empty:
        return returns.copy()
    scales = overlay.scale_series(returns)
    return (returns * scales).rename(str(returns.name) if returns.name else "portfolio")
