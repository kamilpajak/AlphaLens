"""Live take-profit engine (the 'brain') — decide + execute TP-tranche exits.

INERT: no live caller wires this into the daemon tick yet (INC-2 supplies the
real price feed; a later increment wires it). Everything here is driven by an
injected PriceFeed + the Broker capabilities, and is exercised hermetically.

Safe sequence (SIM-probed 2026-08-05, netting account): shrink the standalone SL
by the tranche FIRST, THEN market-sell the tranche (a sell while the SL commits
full owned is rejected SellOrdersAlreadyExistForOwnedContracts); sell <= live
owned so the position can never flip short.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from broker_contract.sizing import TpTranchePlan

logger = logging.getLogger(__name__)

_PRICE_EPS = 1e-9  # a long tranche fires when price >= target (within eps)


def tranche_tag(index: int) -> str:
    return f"tp{index + 1}"


@dataclass(frozen=True)
class TrancheExit:
    tag: str
    qty: int
    target_price: float


def plan_tranche_exits(
    *,
    price: float,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    owned: float,
    already_fired: frozenset[str],
) -> list[TrancheExit]:
    """Which not-yet-fired tranches a LONG at ``price`` should realize now.

    ``reference_qty`` is the tranche-sizing base (the intended/peak filled
    position); tranche qty = round(reference_qty * tranche_pct), cumulatively
    clamped so the batch never exceeds live ``owned``. Order preserved.
    """
    available = round(owned)
    out: list[TrancheExit] = []
    for i, t in enumerate(tp_tranches):
        tag = tranche_tag(i)
        if tag in already_fired:
            continue
        if price + _PRICE_EPS < t.target_price:
            continue  # target not touched
        qty = min(round(reference_qty * t.tranche_pct), available)
        if qty <= 0:
            continue
        out.append(TrancheExit(tag=tag, qty=qty, target_price=t.target_price))
        available -= qty
    return out
