"""The take-profit cost gate: does a tranche's own level clear its own round
trip, plus the stated edge buffer (spec sections 2, 5.2 and 8.1)?

ENGINE module: stdlib and this package only.

This is a REWRITE of ``live_exit_engine._exit_clears_cost`` and the
``costs.min_profitable_exit_price`` it delegates to, not an import of them, and
the rewrite is forced twice over. Those functions live in
``alphalens_pipeline.brokers.automanager``, which the engine's import
allow-list does not admit (spec section 3.1); and the fee card and
``FX_ROUND_TRIP_RATE`` cannot be lifted into ``broker_contract`` either, which
a test pins on purpose. What keeps the copy honest is a golden table computed
from the daemon's own function, in ``tests/intent_replay/test_cost_gate.py``.

**The notional is the INSTRUMENT's currency, and #1592 is what made that
possible.** The walk holds a tranche's size in the ACCOUNT currency, because a
rung's share of the budget divided by a limit price is account currency per
instrument price. ``Fx.to_shares`` turns that into a share count, and the
product with the entry price is then the notional the fee card is written
against -- which matters because ``min_commission`` is an instrument-currency
magnitude, so comparing it against an account-currency notional priced two
currencies at once. The conversion and the FX leg are both read off the stated
facts (section 5.2.1); nothing here inherits a production constant.

What this module still does NOT carry:

* **No whole-share lattice.** The daemon prices the WHOLE-SHARE notional; this
  prices the stated budget. Above the fee card's knee
  (``min_commission / commission_rate``) the fee is pure ad valorem and the two
  thresholds agree to full precision. Below it the per-fill minimum binds
  unequally and this threshold comes out too LOW, so the replay fires tranches
  the daemon declines: measured 1.05 bps at a budget of 1000 against 950 whole
  shares' worth, 15.24 at 250 against 210, 38.10 at 100 against 84. That gap has
  no currency in it and survives this issue unchanged.

The stance on unusable data is the daemon's and is the opposite of the stop
decision's: ``clears_cost`` FAILS OPEN. Refusing an exit on a number nothing
can price would strand a position with no take-profit path, while the disaster
stop still guards the downside.
"""

from __future__ import annotations

import math
from typing import Final

from intent_replay.config import Costs

__all__ = ["clears_cost", "min_profitable_exit_price", "round_trip_fee_bps"]

_BPS_PER_UNIT: Final = 1e4


def round_trip_fee_bps(notional: float, costs: Costs) -> float:
    """Buy plus sell commission plus the FX leg for ``notional`` (the
    INSTRUMENT's currency), in bps of that notional.

    A non-positive ``notional`` answers ``0.0`` rather than dividing — the
    daemon's own arm, and it is reachable here without a degenerate input: two
    positive numbers can multiply to zero by underflow. The FX term sits INSIDE
    that guard rather than beside it, as the daemon's does.

    The grouping is the daemon's: the two round trips are summed and the sum is
    divided once. In bps the FX term is the stated rate exactly, at every
    notional, because the notional cancels — which is why one stated fraction
    prices the leg (section 5.2.1).
    """
    if notional <= 0:
        return 0.0
    ad_valorem = costs.commission_rate.value * notional
    per_fill = (
        max(costs.min_commission.value, ad_valorem) if costs.min_commission_applies else ad_valorem
    )
    commission_round_trip = 2.0 * per_fill
    fx_round_trip = _fx_round_trip(notional, costs)
    return (commission_round_trip + fx_round_trip) / notional * _BPS_PER_UNIT


def _fx_round_trip(notional: float, costs: Costs) -> float:
    """The stated conversion cost of one round trip, or ``0.0``.

    ``applies`` is derived from the two currency codes, so this arm cannot be
    reached by a caller declaring a conversion the codes do not show
    (section 5.2.1).
    """
    fx = costs.fx
    if not fx.applies or fx.round_trip_cost_rate is None:
        return 0.0
    return fx.round_trip_cost_rate * notional


def min_profitable_exit_price(*, entry_price: float, units: float, costs: Costs) -> float | None:
    """The lowest exit price that clears the round trip plus the STATED edge, or
    ``None`` when the answer is not representable.

    ``units`` is the tranche's size in the ACCOUNT currency; the conversion to
    shares happens here, once, so every caller states one unit and the module
    docstring states which.

    ``None`` means UNREPRESENTABLE, never "no threshold": the result is guarded
    as well as the inputs, because the notional reaches a float extreme from
    both ends and only one of them shows in the inputs. It can overflow to
    infinity, making the fee ``inf / inf`` and the threshold ``NaN``; it can
    also VANISH while staying finite, at which point the per-fill minimum
    divided by it overflows and the threshold is infinite. Both make every
    ``price >= threshold`` comparison False, which would invert the fail-open
    policy :func:`clears_cost` states.
    """
    for value in (entry_price, units):
        if not math.isfinite(value) or value <= 0.0:
            return None
    cost_bps = round_trip_fee_bps(costs.fx.to_shares(units) * entry_price, costs)
    threshold = entry_price * (1.0 + (cost_bps + costs.exit_edge_min_bps.value) / _BPS_PER_UNIT)
    if not math.isfinite(threshold):
        return None
    return threshold


def clears_cost(*, price: float, entry_price: float, units: float, costs: Costs) -> bool:
    """Whether selling ``units`` at ``price`` clears the threshold. Fails OPEN on
    an unpriceable tranche (see the module docstring)."""
    threshold = min_profitable_exit_price(entry_price=entry_price, units=units, costs=costs)
    return threshold is None or price >= threshold
