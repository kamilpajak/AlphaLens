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

Two things this module deliberately does NOT carry:

* **No FX term.** The daemon adds ``FX_ROUND_TRIP_RATE`` (0.0050, exactly
  50 bps of any notional) when a conversion applies. No configuration key
  states that rate and section 2.1 forbids inheriting a production constant, so
  ``fx_applies: true`` is refused in ``config`` and never reaches this module.
  Pricing it is #1592.
* **No whole-share lattice.** The daemon prices the WHOLE-SHARE notional; this
  prices the stated budget. Above the fee card's knee
  (``min_commission / commission_rate``) the fee is pure ad valorem and the two
  thresholds agree to full precision. Below it the per-fill minimum binds
  unequally and this threshold comes out too LOW, so the replay fires tranches
  the daemon declines: measured 1.05 bps at a budget of 1000 against 950 whole
  shares' worth, 15.24 at 250 against 210, 38.10 at 100 against 84.

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
    """Buy plus sell commission for ``notional``, in bps of that notional.

    A non-positive ``notional`` answers ``0.0`` rather than dividing — the
    daemon's own arm, and it is reachable here without a degenerate input: two
    positive numbers can multiply to zero by underflow.
    """
    if notional <= 0:
        return 0.0
    ad_valorem = costs.commission_rate.value * notional
    per_fill = (
        max(costs.min_commission.value, ad_valorem) if costs.min_commission_applies else ad_valorem
    )
    return 2.0 * per_fill / notional * _BPS_PER_UNIT


def min_profitable_exit_price(*, entry_price: float, units: float, costs: Costs) -> float | None:
    """The lowest exit price that clears the round trip plus the STATED edge, or
    ``None`` when the answer is not representable.

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
    cost_bps = round_trip_fee_bps(units * entry_price, costs)
    threshold = entry_price * (1.0 + (cost_bps + costs.exit_edge_min_bps.value) / _BPS_PER_UNIT)
    if not math.isfinite(threshold):
        return None
    return threshold


def clears_cost(*, price: float, entry_price: float, units: float, costs: Costs) -> bool:
    """Whether selling ``units`` at ``price`` clears the threshold. Fails OPEN on
    an unpriceable tranche (see the module docstring)."""
    threshold = min_profitable_exit_price(entry_price=entry_price, units=units, costs=costs)
    return threshold is None or price >= threshold
