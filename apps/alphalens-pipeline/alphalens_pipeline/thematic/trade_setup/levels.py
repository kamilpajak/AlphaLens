"""ZigZag-ATR swing detection + support/resistance clustering.

Pure functions over numpy/list inputs. Parameters are fixed globally (no
per-symbol tuning — multiple-testing discipline). These levels are
reference/coordination zones, not predictive signals (design memo §2).
"""

from __future__ import annotations

from collections.abc import Sequence

# Global, fixed parameter (no per-symbol tuning). The swing-detection
# threshold multiplier lives in ``builder`` where the OHLCV is assembled.
_CLUSTER_RADIUS_MULT = 0.5  # swing prices within 0.5*ATR collapse into one zone


def detect_swing_points(
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    threshold: float,
) -> list[tuple[int, float, str]]:
    """ZigZag pivots: confirm a swing once price reverses by >= ``threshold``.

    Returns ``[(index, price, kind)]`` with ``kind`` in ``{"H", "L"}`` in
    chronological order. ``threshold`` is an absolute price move (caller
    passes ``mult * ATR``). Deterministic single pass; ``direction`` starts
    undetermined and locks on the first threshold breach.
    """
    n = len(highs)
    if n < 2 or threshold <= 0:
        return []

    pivots: list[tuple[int, float, str]] = []
    direction = 0  # +1 = tracking a high (up-swing), -1 = tracking a low, 0 = undetermined
    hi, hi_idx = highs[0], 0
    lo, lo_idx = lows[0], 0

    for i in range(1, n):
        if direction == 1:
            if highs[i] >= hi:
                hi, hi_idx = highs[i], i
            elif hi - lows[i] >= threshold:
                pivots.append((hi_idx, hi, "H"))
                direction = -1
                lo, lo_idx = lows[i], i
        elif direction == -1:
            if lows[i] <= lo:
                lo, lo_idx = lows[i], i
            elif highs[i] - lo >= threshold:
                pivots.append((lo_idx, lo, "L"))
                direction = 1
                hi, hi_idx = highs[i], i
        else:  # undetermined — track both extremes, lock on first breach
            if highs[i] >= hi:
                hi, hi_idx = highs[i], i
            if lows[i] <= lo:
                lo, lo_idx = lows[i], i
            if hi - lows[i] >= threshold:
                pivots.append((hi_idx, hi, "H"))
                direction = -1
                lo, lo_idx = lows[i], i
            elif highs[i] - lo >= threshold:
                pivots.append((lo_idx, lo, "L"))
                direction = 1
                hi, hi_idx = highs[i], i

    return pivots


def cluster_prices(prices: Sequence[float], *, radius: float) -> list[float]:
    """Merge prices within ``radius`` into zone centers (mean of the cluster).

    Returns zone centers sorted ascending. A cluster grows while the next
    sorted price is within ``radius`` of the running cluster mean.
    """
    pts = sorted(float(p) for p in prices)
    if not pts:
        return []
    if radius <= 0:
        return pts

    zones: list[float] = []
    bucket = [pts[0]]
    for p in pts[1:]:
        center = sum(bucket) / len(bucket)
        if p - center <= radius:
            bucket.append(p)
        else:
            zones.append(sum(bucket) / len(bucket))
            bucket = [p]
    zones.append(sum(bucket) / len(bucket))
    return zones


def support_resistance(
    close: float,
    pivots: Sequence[tuple[int, float, str]],
    atr: float,
    *,
    cluster_radius_mult: float = _CLUSTER_RADIUS_MULT,
) -> tuple[list[float], list[float]]:
    """Split clustered pivots into supports (< close) and resistances (> close).

    Returns ``(supports, resistances)`` where supports are sorted DESCENDING
    (nearest-below first) and resistances ascending (nearest-above first).
    """
    radius = atr * cluster_radius_mult
    low_prices = [p for (_, p, kind) in pivots if kind == "L" and p < close]
    high_prices = [p for (_, p, kind) in pivots if kind == "H" and p > close]
    supports = sorted(cluster_prices(low_prices, radius=radius), reverse=True)
    resistances = cluster_prices(high_prices, radius=radius)
    return supports, resistances


__all__ = [
    "cluster_prices",
    "detect_swing_points",
    "support_resistance",
]
