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

import dataclasses
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


@dataclasses.dataclass(frozen=True)
class VenueFeeCard:
    """One venue's commission schedule (#1238 PR 3). ``min_commission`` is
    denominated in the INSTRUMENT currency — the same currency as the notional
    the fee equation prices, so the model needs no conversion."""

    commission_rate: float
    min_commission: float
    label: str


US_FEE_CARD = VenueFeeCard(
    commission_rate=COMMISSION_RATE, min_commission=MIN_COMMISSION_USD, label="saxo-pl-classic-us"
)
"""Saxo LIVE Polish schedule, US venues: 0.08% min USD 1 per fill."""

WSE_FEE_CARD = VenueFeeCard(
    commission_rate=0.0012, min_commission=10.0, label="saxo-pl-classic-wse"
)
"""Saxo LIVE Polish schedule, WSE (GPW): 0.12% min PLN 10 per fill (Saxo PL
Classic tier, read from home.saxo/pl-pl 2026-09-02). ASSUMES the Classic tier —
the operator must confirm the account tier before the first LIVE GPW pick."""

_FEE_CARD_BY_INSTRUMENT_CURRENCY: dict[str, VenueFeeCard] = {
    "USD": US_FEE_CARD,
    "PLN": WSE_FEE_CARD,
}


def fee_card_for(instrument_currency: str | None) -> VenueFeeCard | None:
    """The venue fee card for an instrument currency, or ``None`` when this
    rail has no verified schedule for it (callers then keep the conservative
    legacy constants). Keyed by CURRENCY, not MIC: on this rail the currency
    identifies the venue schedule (USD ↔ US venues, PLN ↔ WSE), and the
    currency is the fact both gates already have stamped."""
    if not instrument_currency:
        return None
    return _FEE_CARD_BY_INSTRUMENT_CURRENCY.get(instrument_currency.upper())


def round_trip_fee_bps(
    notional: float,
    *,
    fx_applies: bool,
    min_commission_applies: bool = True,
    card: VenueFeeCard = US_FEE_CARD,
) -> float:
    """The estimated round-trip fee for ``notional`` (instrument currency), in
    bps of that notional, priced on ``card``'s schedule (default: the US card —
    byte-identical to the pre-#1238 model).

    ``fx_applies`` is ``True`` iff the account currency differs from the
    instrument currency, which adds the FX round-trip leg.
    ``min_commission_applies`` gates the per-fill minimum: with a matching
    ``card`` the minimum is denominated in the notional's own currency and the
    flag stays ``True``; the ``False`` arm survives for the legacy callers that
    cannot name the venue and drop the (USD) minimum for a non-USD notional.

    A non-positive ``notional`` returns ``0.0`` — a caller's cap comparison then
    stays inert rather than dividing by zero.
    """
    if notional <= 0:
        return 0.0
    ad_valorem = card.commission_rate * notional
    per_fill = max(card.min_commission, ad_valorem) if min_commission_applies else ad_valorem
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
"""LEGACY default for the #1112 cost gates' FX leg (#1238 PR 3 threaded the
REAL fact through: the gates now read the journal-stamped currency pair via
:func:`cost_gate_facts`). This constant survives as the conservative fallback
for records armed before the stamps existed — the pre-stamp cohort is USD on a
PLN account, so a conversion always applied there."""

COST_GATE_MIN_COMMISSION_APPLIES = True
"""LEGACY default for the #1112 cost gates' per-fill minimum, same fallback
role as :data:`COST_GATE_FX_APPLIES`. Conservative by construction: the
minimum can only RAISE the required edge, so an unstamped record is refused a
little too eagerly, never too late. Stamped records price the venue's own
minimum from its :class:`VenueFeeCard` instead."""


@dataclasses.dataclass(frozen=True)
class CostGateFacts:
    """The currency facts the #1112 gates price a round trip with (#1238 PR 3).

    Derived from the journal-stamped instrument/sizing currencies by
    :func:`cost_gate_facts`; :meth:`legacy` reproduces the conservative
    ``COST_GATE_*`` constants exactly, for records armed before the stamps
    existed and for any currency this rail has no verified fee card for."""

    fx_applies: bool
    min_commission_applies: bool
    card: VenueFeeCard

    @classmethod
    def legacy(cls) -> CostGateFacts:
        return cls(
            fx_applies=COST_GATE_FX_APPLIES,
            min_commission_applies=COST_GATE_MIN_COMMISSION_APPLIES,
            card=US_FEE_CARD,
        )


def cost_gate_facts(
    *, instrument_currency: str | None, sizing_currency: str | None
) -> CostGateFacts:
    """Turn the journal-stamped currency pair into gate facts.

    Both currencies known AND a verified fee card for the instrument currency
    → real facts: the FX leg applies iff the currencies differ, and the
    venue's own per-fill minimum applies (it is denominated in the notional's
    currency). Anything unknown → :meth:`CostGateFacts.legacy` — the
    conservative pre-#1238 constants, which can only over-refuse an exit,
    never under-charge it."""
    card = fee_card_for(instrument_currency)
    if card is None or not sizing_currency:
        return CostGateFacts.legacy()
    assert instrument_currency is not None  # fee_card_for(None) is None
    return CostGateFacts(
        fx_applies=instrument_currency.upper() != sizing_currency.upper(),
        min_commission_applies=True,
        card=card,
    )


def min_profitable_exit_price(
    *,
    entry_price: float,
    qty: float,
    facts: CostGateFacts | None = None,
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
    if facts is None:
        facts = CostGateFacts.legacy()
    cost_bps = round_trip_fee_bps(
        qty * entry_price,
        fx_applies=facts.fx_applies,
        min_commission_applies=facts.min_commission_applies,
        card=facts.card,
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

    DELIBERATE rounding-target mismatch: the apportionment sums to the plan's
    own DECLARED coverage (``round(reference_qty * sum(fracs))``) while this
    check compares against the WHOLE position (``round(reference_qty)``). That
    gap is the point — apportionment realizes a partial plan faithfully, and
    this contract is where a partial plan gets refused.
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
