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


class _SwingTracker:
    """Mutable ZigZag state — tracks the running high/low extreme and emits a
    pivot when price reverses from that extreme by >= the threshold.

    The per-leg branches live in separate methods so no single function
    carries the whole state machine's cognitive load (each stays well under
    the Sonar S3776 ceiling). Behaviour is identical to one inlined loop.
    """

    def __init__(self, high0: float, low0: float) -> None:
        self.direction = 0  # +1 = up-leg (tracking a high), -1 = down-leg, 0 = undetermined
        self.hi, self.hi_idx = high0, 0
        self.lo, self.lo_idx = low0, 0
        self.pivots: list[tuple[int, float, str]] = []

    def _flip_to_down(self, low: float, idx: int) -> None:
        self.pivots.append((self.hi_idx, self.hi, "H"))
        self.direction = -1
        self.lo, self.lo_idx = low, idx

    def _flip_to_up(self, high: float, idx: int) -> None:
        self.pivots.append((self.lo_idx, self.lo, "L"))
        self.direction = 1
        self.hi, self.hi_idx = high, idx

    def _up_leg(self, high: float, low: float, idx: int, threshold: float) -> None:
        if high >= self.hi:
            self.hi, self.hi_idx = high, idx
        elif self.hi - low >= threshold:
            self._flip_to_down(low, idx)

    def _down_leg(self, high: float, low: float, idx: int, threshold: float) -> None:
        if low <= self.lo:
            self.lo, self.lo_idx = low, idx
        elif high - self.lo >= threshold:
            self._flip_to_up(high, idx)

    def _seed(self, high: float, low: float, idx: int, threshold: float) -> None:
        """Undetermined: track both extremes, lock direction on first breach."""
        if high >= self.hi:
            self.hi, self.hi_idx = high, idx
        if low <= self.lo:
            self.lo, self.lo_idx = low, idx
        if self.hi - low >= threshold:
            self._flip_to_down(low, idx)
        elif high - self.lo >= threshold:
            self._flip_to_up(high, idx)

    def step(self, high: float, low: float, idx: int, threshold: float) -> None:
        if self.direction == 1:
            self._up_leg(high, low, idx, threshold)
        elif self.direction == -1:
            self._down_leg(high, low, idx, threshold)
        else:
            self._seed(high, low, idx, threshold)


def detect_swing_points(
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    threshold: float,
) -> list[tuple[int, float, str]]:
    """ZigZag pivots: confirm a swing once price reverses by >= ``threshold``.

    Returns ``[(index, price, kind)]`` with ``kind`` in ``{"H", "L"}`` in
    chronological order. ``threshold`` is an absolute price move (caller
    passes ``mult * ATR``). Deterministic single pass; direction starts
    undetermined and locks on the first threshold breach.
    """
    n = len(highs)
    if n < 2 or threshold <= 0:
        return []
    tracker = _SwingTracker(highs[0], lows[0])
    for i in range(1, n):
        tracker.step(highs[i], lows[i], i, threshold)
    return tracker.pivots


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
