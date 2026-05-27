"""Geometry-safe entry/TP ladder construction.

Separates "idea" (candidate levels with tags) from "geometry" (enforced
monotonicity, minimum spacing, minimum stop-distance). This kills the
pathological ``max()`` inversion where, in a downtrend, a moving-average
"support" can sit ABOVE the close (design memo §7.1, zen §3).
"""

from __future__ import annotations

from collections.abc import Sequence

_MIN_SPACING_MULT = 0.5  # tiers/tranches must be >= 0.5*ATR apart
_MIN_STOP_DIST_MULT = 0.5  # an entry tier must sit >= 0.5*ATR above the stop
_R_MULTIPLE_FALLBACK = (2.0, 3.0, 4.0)  # TP fallback when no overhead resistance


def build_entry_tiers(
    close: float,
    atr: float,
    candidates: Sequence[tuple[float, str]],
    stop: float,
    *,
    max_tiers: int = 3,
    min_spacing_mult: float = _MIN_SPACING_MULT,
    min_stop_dist_mult: float = _MIN_STOP_DIST_MULT,
) -> list[tuple[float, str]]:
    """Pick <=``max_tiers`` monotone-descending entry tiers from candidates.

    ``candidates`` are ``(price, tag)`` pairs (supports, MAs, fallbacks).
    Filters to prices strictly below ``close``, sorts nearest-first, and
    enforces:
      - ``price - stop >= min_stop_dist_mult*ATR`` (CRITICAL: keeps the
        equal-risk divisor away from zero — zen §3);
      - spacing ``prev - price >= min_spacing_mult*ATR`` (no clustered fills).

    Returns ``[(price, tag)]`` strictly descending; may be 0..max_tiers long
    (graceful degradation).
    """
    if atr <= 0:
        return []
    spacing = min_spacing_mult * atr
    min_stop_dist = min_stop_dist_mult * atr

    ordered = sorted(((float(p), t) for p, t in candidates if p < close), key=lambda x: -x[0])
    chosen: list[tuple[float, str]] = []
    for price, tag in ordered:
        if len(chosen) >= max_tiers:
            break
        if price - stop < min_stop_dist:
            continue  # too close to (or below) the stop — discard before sizing
        if chosen and (chosen[-1][0] - price) < spacing:
            continue  # too close to the previous tier
        chosen.append((price, tag))
    return chosen


def build_tp_tranches(
    close: float,
    atr: float,
    resistances: Sequence[float],
    blended_entry: float,
    stop: float,
    *,
    max_tranches: int = 3,
    min_spacing_mult: float = _MIN_SPACING_MULT,
) -> list[tuple[float, float, str]]:
    """Pick <=``max_tranches`` ascending take-profit targets.

    Prefers real overhead resistance zones (> close); when none exist
    (breakout into new highs), falls back to ATR R-multiples off the
    blended entry. Returns ``[(target, r_multiple, tag)]`` ascending.
    ``r_multiple`` uses ``R = blended_entry - stop`` (1R risk).
    """
    if atr <= 0:
        return []
    spacing = min_spacing_mult * atr
    risk = blended_entry - stop
    if risk <= 0:
        return []

    def _r(target: float) -> float:
        return (target - blended_entry) / risk

    above = sorted(float(p) for p in resistances if p > close)
    chosen: list[tuple[float, float, str]] = []
    for target in above:
        if len(chosen) >= max_tranches:
            break
        if chosen and (target - chosen[-1][0]) < spacing:
            continue
        chosen.append((target, _r(target), "overhead resistance"))

    if not chosen:
        # No overhead structure — use volatility R-multiples as a fallback tag.
        for mult in _R_MULTIPLE_FALLBACK[:max_tranches]:
            target = blended_entry + mult * risk
            if target > close:
                chosen.append((target, mult, f"{mult:g}R volatility target"))

    return chosen


__all__ = ["build_entry_tiers", "build_tp_tranches"]
