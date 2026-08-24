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


__all__ = [
    "COMMISSION_RATE",
    "FX_ROUND_TRIP_RATE",
    "MIN_COMMISSION_USD",
    "round_trip_fee_bps",
]
