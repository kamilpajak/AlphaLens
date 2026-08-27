"""Pure price-level computations for the ATR bracket exit geometry.

Stdlib-only (``math`` + typing). No I/O, no broker/replay dependency — every
function here is a total, side-effect-free mapping from primitive inputs to
either a price tuple or ``None`` on a degenerate / missing input.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def ceiling_from_52w_high(
    trade_setup: Mapping[str, Any] | None, pct_off_52w_high: float | None
) -> float | None:
    """Reconstruct the trailing 52w-high price from the brief's distance column.

    ``technical_pct_off_52w_high`` is ``100 * (last - peak) / peak`` (<= 0 by
    construction; 0 = at the high), so ``peak = asof_close / (1 + pct/100)``.
    Returns ``None`` (-> UNCAPPED TP, memo §4.2) when the pct or the setup's
    ``asof_close`` is missing / non-finite / degenerate — a missing 52w history
    is coverage, not a null.
    """
    if trade_setup is None or pct_off_52w_high is None:
        return None
    try:
        pct = float(pct_off_52w_high)
        asof_close = float(trade_setup.get("asof_close"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pct) or not math.isfinite(asof_close) or asof_close <= 0:
        return None
    denom = 1.0 + pct / 100.0
    if denom <= 0:
        return None
    return asof_close / denom


def atr_bracket_levels(
    blended: float,
    atr: float,
    *,
    stop_atr_mult: float,
    tp_atr_mult: float,
    tp_floor_frac: float,
    ceiling_price: float | None = None,
) -> tuple[float, float] | None:
    """Compute the (stop, tp) pair for a symmetric ATR bracket exit.

    Returns ``None`` for any degenerate input: a non-finite / non-positive
    ``atr``, a non-positive risk (``stop_atr_mult <= 0``), a bracket stop
    at/below zero (ATR wider than ~1/stop_atr_mult of the entry), or a
    ceiling at/below the cost floor (bracket not constructible). A ``None`` /
    non-finite ``ceiling_price`` leaves the TP uncapped. The function
    self-guards ``atr`` so future direct callers cannot poison the arithmetic
    into NaN levels; the current feedback callpath still pre-validates it.
    """
    if not math.isfinite(atr) or atr <= 0:
        return None
    if stop_atr_mult <= 0:
        return None
    bracket_stop = blended - stop_atr_mult * atr
    if bracket_stop <= 0:
        return None
    tp_floor = blended * (1.0 + tp_floor_frac)
    tp = max(tp_floor, blended + tp_atr_mult * atr)
    if ceiling_price is not None and math.isfinite(ceiling_price):
        if ceiling_price <= tp_floor:
            return None
        tp = min(tp, ceiling_price)
    return bracket_stop, tp


def chandelier_target(peak: float, atr: float, *, k: float) -> float | None:
    """Trailing-stop level for a long: ``peak - k*atr`` (ratchets up via the
    caller's peak). Returns ``None`` on any degenerate input or a non-positive
    target — never a bad stop."""
    for value in (peak, atr):
        if not math.isfinite(value) or value <= 0:
            return None
    target = peak - k * atr
    if not math.isfinite(target) or target <= 0:
        return None
    return target


def fractional_giveback_target(entry: float, peak: float, *, frac: float) -> float | None:
    """Trailing-stop level for a long that gives back at most ``1 - frac`` of the
    open gain: ``max(entry, entry + frac*(peak - entry))`` (ratchets up via the
    caller's peak). Unlike :func:`chandelier_target` the distance to the peak is
    a FRACTION of the gain, not an ATR offset, so it widens as the gain grows —
    this is the ``be_0p5r_trail0p6`` lens formula. Floors at ``entry`` so a
    direct call with ``peak < entry`` still returns a break-even stop, never a
    loosen. Returns ``None`` on any degenerate price or a ``frac`` outside
    ``(0, 1]`` — never a bad stop."""
    for value in (entry, peak):
        if not math.isfinite(value) or value <= 0:
            return None
    if not math.isfinite(frac) or frac <= 0.0 or frac > 1.0:
        return None
    return max(entry, entry + frac * (peak - entry))


def clamp_reanchor_target(
    prior_stop: float,
    proposed_target: float,
    *,
    anchor_price: float,
    min_distance_frac: float,
) -> float | None:
    """Economic safety envelope for a reanchored disaster stop (memo section 3.1).

    ``prior_stop`` is the placement-time planned disaster stop (the brief
    disaster floor). Returns ``None`` = "do NOT reanchor — leave the resting stop
    where it is" on any degenerate input or when the target would drop below
    ``prior_stop``. NOTE: this enforces NEVER-BELOW-BRIEF-FLOOR, not
    never-loosen-vs-the-current-live-stop (``OrderState`` carries no stop price).
    The min-distance floor caps how close the stop may sit to ``anchor_price``
    (a too-close proposal is pushed FARTHER from price); it is chosen so it never
    binds the 1.5x-ATR policy and exists mainly for a future stochastic policy.
    """
    for value in (prior_stop, proposed_target, anchor_price):
        if not math.isfinite(value) or value <= 0:
            return None
    floor_price = anchor_price * (1.0 - min_distance_frac)
    # The min() pushes the stop AWAY from anchor_price (down / farther from
    # market) — always safe, it only ever buys more room before the stop
    # triggers. For the trail arm (anchor_price=last_price) this caps the
    # stop just below the live market (the OnWrongSideOfMarket guard). It is
    # the CALLER's ratchet on this function's CLAMPED return value (never
    # below the last confirmed trailed level, see position_manager.py
    # _maybe_trail) that guarantees the placed stop stays monotone-up —
    # this clamp alone does not.
    target = min(proposed_target, floor_price)
    if target < prior_stop:
        return None
    return target
