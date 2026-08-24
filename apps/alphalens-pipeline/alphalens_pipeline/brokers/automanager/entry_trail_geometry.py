"""Pure trailing-entry order geometry (PR-T2b) — the entry-side sibling of the
exit ``clamp_reanchor_target`` (do NOT overload that one; memo §3 G1 note).

Design memo: ``docs/research/entry_trailing_design_2026_08_12.md`` (LOCKED
2026-08-12), §2 V1 verdict, §3 G1 (ceiling clamp) / G10 (coarse step), §4b probe
facts, §6 flag.

The V1 executor latches the TOUCH and places ONE native Saxo trailing-LIMIT BUY.
This leaf turns the market observation at that instant — the touch reference bid
and the running trough — plus the configured ``d_bps`` into the four order
parameters the adapter POSTs:

- ``order_price`` = the INITIAL trigger = ``reference * (1 + d)`` (probe: the
  requested trigger was ``bid + distance``; here ``distance = reference * d`` so
  the trigger is one distance above the bid, matching the RIVN/MARA probes).
- ``trailing_distance`` = ``reference * d`` — the absolute price distance the
  server keeps between the trigger and the falling low.
- ``trailing_step`` = a COARSE ratchet step (memo G10, ``~10%`` of the distance)
  so a violent tape does not generate one server ratchet per tick.
- ``ceiling_price`` = ``trough * (1 + d) * (1 + eps)`` — the G1 gap-through cap
  (the ``StopLimitPrice`` on the same native order) so an overnight gap / halt
  reopen can never fill above a fixed ceiling. Guarded to never sit below the
  trigger (they are equal at touch, where ``reference == trough``; the ``max``
  is defensive so the broker's directional clamp never rejects a valid arm).

NO tick alignment here — the Saxo adapter tick-aligns every price at placement
(probe fact 3, ``SaxoBroker._build_trailing_stop_body``). NO I/O, no clock, no
broker: a pure function returning ``None`` on any degenerate input, mirroring
``clamp_reanchor_target``'s finite-positive gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from broker_contract.costs import min_profitable_exit_price

_BPS_DENOMINATOR = 10_000
"""``d = d_bps / 10_000`` (50 bps -> 0.005) — mirrors ``entry_trail_watcher``."""

TRAILING_STEP_FRACTION = 0.10
"""Coarse ratchet step as a fraction of the trailing distance (memo G10:
``>= ~10% of d``). The adapter floors it to ``>= 1 tick`` at placement."""

CEILING_EPS_FRAC = 0.002
"""The ``eps`` buffer in ``trough*(1+d)*(1+eps)`` (memo §3 G1): the gap-through
fill may sit up to this fraction (20 bps) above the initial trigger before the
StopLimit ceiling refuses it — tight enough to bound a gap-up, loose enough that
tick rounding never drops the ceiling below the trigger."""


@dataclass(frozen=True)
class TrailingOrderGeometry:
    """The four native trailing-LIMIT parameters (pre-tick-alignment)."""

    order_price: float  # the initial trigger
    trailing_distance: float
    trailing_step: float
    ceiling_price: float  # the G1 StopLimitPrice ceiling


def compute_trailing_order_geometry(
    *, reference: float, trough: float, d_bps: int
) -> TrailingOrderGeometry | None:
    """The trailing-LIMIT geometry at the touch instant, or ``None`` on any
    degenerate input (non-finite / non-positive price, ``d_bps <= 0``).

    ``reference`` is the touch reference bid (the LIVE V1 probe's buy-side
    reference); ``trough`` is the running low (``== reference`` at the touch
    tick). ``d_bps`` is the configured trail distance in basis points."""
    if d_bps <= 0:
        return None
    for value in (reference, trough):
        if not math.isfinite(value) or value <= 0.0:
            return None
    d_frac = d_bps / _BPS_DENOMINATOR
    distance = reference * d_frac
    order_price = reference + distance
    step = distance * TRAILING_STEP_FRACTION
    # trough*(1+d) is the memo's ceiling base; the max() guards it to never fall
    # below the trigger (equal at touch — defensive against a trough < reference
    # input so the broker's ceiling >= trigger clamp never rejects a valid arm).
    ceiling_base = max(trough * (1.0 + d_frac), order_price)
    ceiling_price = ceiling_base * (1.0 + CEILING_EPS_FRAC)
    return TrailingOrderGeometry(
        order_price=order_price,
        trailing_distance=distance,
        trailing_step=step,
        ceiling_price=ceiling_price,
    )


def entry_fill_estimate(*, reference: float, trough: float, d_bps: int) -> float | None:
    """A realistic UPPER bound on the price this trail could fill at, or ``None``
    on any degenerate input (issue #1112 step 1).

    This is the armed order's own ``ceiling_price`` — the ``StopLimitPrice`` the
    broker enforces, so no fill of that order can print above it. The tier LIMIT
    is deliberately NOT used: on 2026-08-24 the SMG trail filled at 59.9261
    against a tier limit of 59.786017 (23 bps above it), because the broker's
    server-side trail ratcheted the trigger independently of our limit. A
    validity check on the nominal limit would have seen nothing wrong.
    """
    geo = compute_trailing_order_geometry(reference=reference, trough=trough, d_bps=d_bps)
    return None if geo is None else geo.ceiling_price


def arms_inside_exit_region(
    *, fill_estimate: float | None, exit_target: float | None, qty: float | None
) -> bool:
    """Whether arming this tier would open a position its own exit target cannot
    pay for (issue #1112: the LIVE SMG round trip of 2026-08-24, 62 seconds,
    -380 bps).

    The condition is the issue's Goal, not a bare price comparison:

        refuse unless   exit_target > fill_estimate + round_trip_cost + E_min

    so a target that sits above the fill but inside the round trip is refused
    too. Both #1112 gates measure that threshold with the SAME
    :func:`~broker_contract.costs.min_profitable_exit_price`, so a tier can
    never be armed on a target the exit gate would later refuse to fire.

    ``qty`` is the tier's share count — the cost model's per-fill USD minimum
    makes the required move depend on it (one share at about $60 pays roughly
    382 bps round trip).

    FAILS OPEN by design: a missing, non-finite or non-positive input returns
    ``False`` (arm as before). A gate that silently refuses every arm on
    degenerate data would stop the whole entry rail, which is worse than the
    defect it prevents; the caller logs whichever way it goes.
    """
    if fill_estimate is None or exit_target is None or qty is None:
        return False
    for value in (fill_estimate, exit_target, qty):
        if not math.isfinite(value) or value <= 0.0:
            return False
    required = min_profitable_exit_price(entry_price=fill_estimate, qty=qty)
    if required is None:
        return False
    return exit_target < required
