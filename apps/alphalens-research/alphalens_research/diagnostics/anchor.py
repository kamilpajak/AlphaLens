"""CAR anchor selection for the selection diagnostic (Faza -1 HALT gate).

The legacy anchor is the prior-session CLOSE for both the stock and the SPY
leg. The arrival-VWAP anchor measures the return an ARRIVAL entry actually
earns: the stock leg starts at its arrival 30-min VWAP (the stamped
``reference_close``) and the SPY leg at its arrival OPEN (the same-window
market leg). This subtracts the stock's overnight gap, which the
prior-close anchor silently hands to "unfilled" names that gapped up.
"""

from __future__ import annotations

ANCHOR_PRIOR_CLOSE = "prior_close"
ANCHOR_ARRIVAL_VWAP = "arrival_vwap"
ANCHOR_MODES = (ANCHOR_PRIOR_CLOSE, ANCHOR_ARRIVAL_VWAP)


def event_anchor(
    mode: str,
    *,
    prior_close_stock: float | None,
    prior_close_spy: float | None,
    arrival_vwap_stock: float | None,
    arrival_open_spy: float | None,
) -> tuple[float | None, float | None]:
    """Return ``(stock_anchor, spy_anchor)`` for one event under ``mode``.

    ``None`` legs are propagated unchanged; the caller's CAR routine already
    treats a ``None``/non-positive anchor as an incomputable window.
    """
    if mode == ANCHOR_PRIOR_CLOSE:
        return prior_close_stock, prior_close_spy
    if mode == ANCHOR_ARRIVAL_VWAP:
        return arrival_vwap_stock, arrival_open_spy
    raise ValueError(f"unknown anchor mode: {mode!r} (expected one of {ANCHOR_MODES})")
