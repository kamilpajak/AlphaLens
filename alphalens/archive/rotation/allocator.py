"""Overlay allocator: core weights + macro regime tilts → target allocation.

Clamping rules (pre-committed, never overridden after IS lock):
- Every ticker's tilt magnitude is clamped to ``|max_tilt|`` independently.
- After clamping, if tilts don't net to zero, the residual is redistributed
  pro-rata across *untilted* core positions to keep total weight = 1.0.
- If the resulting allocation would have a negative weight on any ticker,
  the whole rebalance fails with ``AllocationError`` (caller must widen core
  or tighten ``max_tilt``).
"""

from __future__ import annotations

from collections.abc import Mapping

from alphalens.data.macro.scorer import MacroRegime

_WEIGHT_TOL = 1e-9


class AllocationError(ValueError):
    pass


class OverlayAllocator:
    def __init__(self, *, core_weights: Mapping[str, float], max_tilt: float):
        total = sum(core_weights.values())
        if abs(total - 1.0) > _WEIGHT_TOL:
            raise AllocationError(f"core_weights must sum to 1.0 (got {total:.9f})")
        if max_tilt <= 0:
            raise AllocationError(f"max_tilt must be positive (got {max_tilt})")
        self._core = dict(core_weights)
        self._max_tilt = float(max_tilt)

    def apply(self, regime: MacroRegime) -> dict[str, float]:
        # Clamp each tilt independently.
        clamped: dict[str, float] = {}
        for ticker in self._core:
            raw = float(regime.tilt_sum.get(ticker, 0.0))
            clamped[ticker] = max(-self._max_tilt, min(self._max_tilt, raw))

        # Residual = deficit that must be spread across untilted (or lightly
        # tilted) positions to keep sum = 1.0.
        residual = -sum(clamped.values())
        if abs(residual) > _WEIGHT_TOL:
            self._spread_residual(clamped, residual)

        weights = {t: self._core[t] + delta for t, delta in clamped.items()}
        for t, w in weights.items():
            if w < -_WEIGHT_TOL:
                raise AllocationError(
                    f"allocation made {t} weight negative ({w:.6f}); widen core or tighten max_tilt"
                )
            weights[t] = max(0.0, w)  # clamp numerical noise
        return weights

    def _spread_residual(self, clamped: dict[str, float], residual: float) -> None:
        """Distribute residual pro-rata across core positions that are untilted."""
        untilted = [t for t, d in clamped.items() if abs(d) < _WEIGHT_TOL]
        if not untilted:
            # Fall back: spread proportionally across *all* core positions
            untilted = list(clamped.keys())
        base_sum = sum(self._core[t] for t in untilted)
        if base_sum < _WEIGHT_TOL:
            raise AllocationError("cannot spread residual: no untilted core weight")
        for t in untilted:
            clamped[t] += residual * (self._core[t] / base_sum)
