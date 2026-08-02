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
