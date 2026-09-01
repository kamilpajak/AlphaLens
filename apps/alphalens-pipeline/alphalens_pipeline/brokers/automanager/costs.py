"""The ONE round-trip transaction-cost model for THIS rail, as a pure leaf
(stdlib plus the stdlib-only ``broker_contract.constants``, for the shared
share-quantity precision).

Extracted from ``control_loop`` (issue #1112) so the placement-time fee floor
and the exit-time cost gate cannot drift apart: one fee model, several
consumers. Nothing here reads the environment, the clock or a broker.

WHY THIS LIVES PIPELINE-SIDE (issue #1122, decided 2026-08-25). An earlier
revision of #1116 put this module in ``broker_contract``. That package states
its own rule in ``fx.py`` — "the ADAPTER reports, never the contract decides" —
and the numbers below are Saxo's, not the system's. Measured before deciding:
NOTHING inside ``broker_contract`` imported this module; every real consumer
(``control_loop``, ``entry_trail_geometry``, ``live_exit_engine``) sits in THIS
package, so the shared layer was never needed to hold it. A second adapter with
a different schedule now inherits nothing from here by accident. The capability
seam sketched in #1122 stays available if one ever arrives; it is deliberately
not built for a single adapter.

The model (broker sizing design memo §4), calibrated on the Saxo LIVE Polish
schedule for a US venue:

    fee_rt(N) = 2 x max(MIN_COMMISSION_USD, COMMISSION_RATE x N)
                + (FX_ROUND_TRIP_RATE x N if a conversion applies else 0)

expressed in bps of ``N``. The per-fill minimum dominates small notionals: one
share at about $60 pays roughly 384 bps round trip, which is what turned the
2026-08-24 SMG round trip into a -380 bps loss on a flat gross P&L.

Read the module in three parts, because they are three DIFFERENT kinds of fact
and only the first two are about Saxo:

1. vendor economics — ``MIN_COMMISSION_USD`` / ``COMMISSION_RATE`` /
   ``FX_ROUND_TRIP_RATE``, and the two ``COST_GATE_*`` declarations that stand
   in for a currency the gates cannot see;
2. pure arithmetic — ``round_trip_fee_bps``, ``min_profitable_exit_price``,
   ``single_full_position_tranche_violation``,
   ``apportioned_coverage_violation``; these hold for any schedule;
3. one declared STRATEGY parameter — ``EXIT_EDGE_MIN_BPS``. It is not a broker
   fact and not derivable from one. It is kept beside the cost model because
   the two only ever appear together, in one formula; changing it is a strategy
   change (see its own docstring), not a fee correction.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from broker_contract.constants import QTY_PRECISION

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
    ``entry_price``.

    READING RULE — this is ONE cost function, NOT one threshold. Both #1112
    gates call it, but at DIFFERENT quantities: the arm-time gate
    (:func:`~alphalens_pipeline.brokers.automanager.entry_trail_geometry.arms_inside_exit_region`)
    prices the position it is about to open, the exit-time gate
    (``live_exit_engine._exit_clears_cost``) prices the tranche it is about to
    sell. The per-fill USD minimum makes the required move depend on the priced
    quantity, so the two bars only coincide when the two quantities do.
    Measured at an entry of 60.00: 60.8000 at 10 shares, 61.2667 at 3 shares,
    62.6000 at 1 share. The smaller the exit quantity, the HIGHER the exit-time
    bar — so an armed tier is not automatically an exit the gate will fire.

    What makes the arm-time pricing honest depends on which exit plan governs
    the watch. A GEOMETRY plan is pinned to one tranche selling the whole
    position (:func:`single_full_position_tranche_violation`), so the quantity
    the arm gate prices is the quantity the exit will sell. A BRIEF-ladder plan
    (breakeven_trail, since #1183) is multi-tranche by construction, so the arm
    gate prices its FIRST tranche's apportioned share count instead
    (``control_loop._brief_plan_arm_refusal``) — the bar it draws is the bar the
    exit gate will draw for that same tranche.

    ``None`` (never raises) on any degenerate input — a non-finite or
    non-positive ``entry_price`` / ``qty``. Callers fail OPEN on ``None``: a
    gate that silently refuses on unusable data would stop the rail, which is
    worse than the defect it prevents.
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


def single_full_position_tranche_violation(
    *, tranche_quantities: Sequence[float], position_qty: float
) -> str | None:
    """Why this exit plan breaks the contract the #1112 GEOMETRY arm gate
    depends on, or ``None`` when it holds.

    The contract, scoped to geometry-policy plans (a watch with an APPLIED
    geometry target): the exit plan must be exactly ONE active tranche that
    sells the WHOLE position. The arm gate prices the round trip at the
    quantity of the position it opens; the exit gate prices it at the quantity
    of the tranche it sells. Those two bars only coincide while the two
    quantities do (see :func:`min_profitable_exit_price`). Brief-ladder plans
    (no applied geometry) are multi-tranche by design and answer to
    :func:`apportioned_coverage_violation` instead.

    Measured 2026-08-25: every ``tranche_plan`` record on the LIVE rail
    (SMG 2026-08-19, uic 23474, ETSY 2026-08-18) carries one ``geometry``
    tranche at ``tranche_frac`` 1.0, so the contract holds today. It is checked
    here rather than assumed because restoring multi-tranche take-profits would
    silently make the arm gate's pricing optimistic.

    A tranche counts as ACTIVE when its quantity exceeds
    :data:`~broker_contract.constants.QTY_PRECISION` — the broker's own share
    precision, not a local float epsilon. A zero-sized or sub-precision tranche
    is something the broker could never sell, so it is not counted.

    Returns a human-readable reason; on numeric input it never raises (a
    non-numeric quantity is a programming error and is left to raise). The
    caller decides what loudly means — this rail refuses the arm and alerts. It
    must NOT silently fall back to a smaller pricing quantity, merge the
    tranches, or keep whole-position pricing.
    """
    if not math.isfinite(position_qty) or position_qty <= 0.0:
        return f"position quantity is not usable ({position_qty!r})"
    if any(not math.isfinite(q) for q in tranche_quantities):
        return f"exit plan carries a non-finite tranche quantity ({list(tranche_quantities)!r})"
    active = [q for q in tranche_quantities if q > QTY_PRECISION]
    if len(active) != 1:
        return (
            f"exit plan has {len(active)} active tranche(s), the arm gate prices "
            f"exactly 1 selling the whole position"
        )
    if abs(active[0] - position_qty) > QTY_PRECISION:
        return (
            f"exit plan's only tranche ({active[0]:g}) does not sell the whole position "
            f"({position_qty:g})"
        )
    return None


def apportioned_coverage_violation(
    *, tranche_quantities: Sequence[float], reference_qty: float
) -> str | None:
    """Why this multi-tranche exit plan, AFTER whole-share apportionment, does
    not sell the whole position — or ``None`` when it does (issue #1112,
    breakeven_trail follow-up).

    The brief-ladder sibling of :func:`single_full_position_tranche_violation`:
    where the geometry contract demands ONE tranche selling everything (so
    whole-position pricing at arm time stays conservative), this one demands
    that the apportioned tranche quantities SUM to the whole position — an
    un-covered remainder has no take-profit path and sits stop-only until the
    disaster stop or a trail rescue, which is the silent-defect shape the
    2026-08-31 seven-share NOV plan carried before apportionment.

    ``tranche_quantities`` are the whole-share counts
    ``live_exit_engine.apportion_tranche_quantities`` produced; a position that
    rounds below one share cannot be exited by any tranche and is refused
    outright. Same return contract as the sibling: a human-readable reason,
    never raises on numeric input, and the caller decides what loudly means.
    """
    if not math.isfinite(reference_qty) or reference_qty <= 0.0:
        return f"position quantity is not usable ({reference_qty!r})"
    if any(not math.isfinite(q) for q in tranche_quantities):
        return f"exit plan carries a non-finite tranche quantity ({list(tranche_quantities)!r})"
    whole_position = round(reference_qty)
    if whole_position < 1:
        return f"position of {reference_qty:g} share(s) rounds below one sellable share"
    covered = sum(tranche_quantities)
    if abs(covered - whole_position) > QTY_PRECISION:
        return (
            f"exit plan's apportioned tranches sell {covered:g} of {whole_position:g} "
            f"share(s) — the remainder would sit stop-only with no take-profit path"
        )
    return None


__all__ = [
    "COMMISSION_RATE",
    "COST_GATE_FX_APPLIES",
    "COST_GATE_MIN_COMMISSION_APPLIES",
    "EXIT_EDGE_MIN_BPS",
    "FX_ROUND_TRIP_RATE",
    "MIN_COMMISSION_USD",
    "QTY_PRECISION",
    "apportioned_coverage_violation",
    "min_profitable_exit_price",
    "round_trip_fee_bps",
    "single_full_position_tranche_violation",
]
