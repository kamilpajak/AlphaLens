"""The ONE round-trip transaction-cost model, as a pure stdlib-only leaf.

Extracted from ``alphalens_pipeline.brokers.automanager.control_loop`` (issue
#1112) so the placement-time fee floor and the exit-time cost gate cannot drift
apart: one fee model, two consumers. Nothing here reads the environment, the
clock or a broker.

The model (broker sizing design memo §4), calibrated on the Saxo LIVE Polish
schedule for a US venue:

    fee_rt(N) = 2 x max(MIN_COMMISSION_USD, COMMISSION_RATE x N)
                + (FX_ROUND_TRIP_RATE x N if a conversion applies else 0)

expressed in bps of ``N``. The per-fill minimum dominates small notionals: one
share at about $60 pays roughly 384 bps round trip, which is what turned the
2026-08-24 SMG round trip into a -380 bps loss on a flat gross P&L.
"""

from __future__ import annotations

import math

MIN_COMMISSION_USD = 1.0
"""Per-fill commission minimum. A USD figure — only meaningful when the notional
is denominated in USD too (see ``min_commission_applies``)."""

COMMISSION_RATE = 0.0008
"""Ad-valorem commission per fill (0.08%)."""

FX_ROUND_TRIP_RATE = 0.0050
"""FX conversion cost over the round trip (0.25% per conversion, two legs)."""

_BPS_PER_UNIT = 10_000.0


def round_trip_fee_bps(
    notional: float, *, fx_applies: bool, min_commission_applies: bool = True
) -> float:
    """The estimated round-trip fee for ``notional`` (instrument currency), in
    bps of that notional.

    ``fx_applies`` is ``True`` iff the account currency differs from the
    instrument currency, which adds the FX round-trip leg.
    ``min_commission_applies`` gates the per-fill USD minimum: it is only
    meaningful when ``notional`` is USD-denominated; a non-USD instrument gets
    the ad-valorem rate alone.

    A non-positive ``notional`` returns ``0.0`` — a caller's cap comparison then
    stays inert rather than dividing by zero.
    """
    if notional <= 0:
        return 0.0
    ad_valorem = COMMISSION_RATE * notional
    per_fill = max(MIN_COMMISSION_USD, ad_valorem) if min_commission_applies else ad_valorem
    commission_round_trip = 2.0 * per_fill
    fx_round_trip = FX_ROUND_TRIP_RATE * notional if fx_applies else 0.0
    return (commission_round_trip + fx_round_trip) / notional * _BPS_PER_UNIT


EXIT_EDGE_MIN_BPS = 50.0
"""The DECLARED minimum edge, in bps, a position must clear ON TOP of round-trip
cost (issue #1112, ``E_min`` in the issue's condition
``T(s) > realised_average(s) + round_trip_cost + E_min``).

This is a declared value, NOT one derived from the fee. *Optimal Transaction
Filters Under Transitory Trading Opportunities* shows that a filter set exactly
equal to round-trip cost is suboptimal: at the break-even point the expected net
gain is zero while the variance is not, so the filter has to sit strictly wider
than the fee. The literature gives no closed form for this strategy's
opportunity process, so the width is a judgement call recorded here rather than
a formula. 50 bps is the starting value; changing it is a strategy change and
belongs in a pre-registered measurement, not in a bug fix.
"""

COST_GATE_FX_APPLIES = True
"""Whether the #1112 cost gates charge the FX round-trip leg. DECLARED, because
neither gate knows the account currency: the exit engine holds only uic /
tranches / qty / stop, and the entry watch line carries the tier, not the
account. The live account is PLN and every first-cohort instrument is USD, so a
conversion always applies today."""

COST_GATE_MIN_COMMISSION_APPLIES = True
"""Whether the #1112 cost gates apply the per-fill USD minimum. DECLARED for the
same reason, and for the same first US-only cohort. It is the conservative
choice: the minimum can only RAISE the required edge, so a non-USD venue would
see the gates refuse a little too eagerly, never too late. Placement-side
(``control_loop._check_fee_floor``) does know the instrument currency and gates
on it there; when a non-USD venue (XWAR / XTKS) actually opens, these two
gates need the same currency fact threaded through instead of this constant."""


def min_profitable_exit_price(
    *,
    entry_price: float,
    qty: float,
    fx_applies: bool = COST_GATE_FX_APPLIES,
    min_commission_applies: bool = COST_GATE_MIN_COMMISSION_APPLIES,
) -> float | None:
    """The lowest exit price that clears round-trip cost plus
    :data:`EXIT_EDGE_MIN_BPS` on a position of ``qty`` shares bought at
    ``entry_price`` — the ONE threshold both #1112 gates compare against.

    Shared on purpose: the arm-time gate
    (:func:`~alphalens_pipeline.brokers.automanager.entry_trail_geometry.arms_inside_exit_region`)
    and the exit-time gate
    (``live_exit_engine._exit_clears_cost``) must draw the same line, or the rail
    submits an entry whose own take-profit it will later refuse to fire.

    ``None`` (never raises) on any degenerate input — a non-finite or
    non-positive ``entry_price`` / ``qty``. Callers fail OPEN on ``None``: a
    gate that silently refuses on unusable data would stop the rail, which is
    worse than the defect it prevents.

    NOTE the threshold depends on ``qty`` through the per-fill USD minimum: a
    smaller notional pays proportionally more, so a small tranche needs a wider
    move than a large one at the same entry.
    """
    for value in (entry_price, qty):
        if not math.isfinite(value) or value <= 0.0:
            return None
    cost_bps = round_trip_fee_bps(
        qty * entry_price,
        fx_applies=fx_applies,
        min_commission_applies=min_commission_applies,
    )
    return entry_price * (1.0 + (cost_bps + EXIT_EDGE_MIN_BPS) / _BPS_PER_UNIT)


__all__ = [
    "COMMISSION_RATE",
    "COST_GATE_FX_APPLIES",
    "COST_GATE_MIN_COMMISSION_APPLIES",
    "EXIT_EDGE_MIN_BPS",
    "FX_ROUND_TRIP_RATE",
    "MIN_COMMISSION_USD",
    "min_profitable_exit_price",
    "round_trip_fee_bps",
]
