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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from broker_contract.contract import Broker, OrderState
from broker_contract.price_feed import PriceFeed, PricePoint
from broker_contract.sizing import TpTranchePlan

from alphalens_pipeline.brokers.automanager.position_manager import _sole_standalone_stop

logger = logging.getLogger(__name__)

_PRICE_EPS = 1e-9  # a long tranche fires when price >= target (within eps)
_QTY_EPS = 0.5  # share-qty tolerance (mirrors broker_contract.contract._QTY_EPS)


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


def execute_tranche_exit(
    broker: Broker,
    *,
    uic: int,
    exit: TrancheExit,
    sl_leg: OrderState,
    stop_price: float,
    request_ref: str,
) -> bool:
    """Realize ONE tranche: free the tranche from the standalone SL, THEN market
    sell it. Re-snapshots live owned first (never sell more than owned → cannot
    flip short). Returns True iff the sell was sent. Callers MUST hold a per-uic
    lock so this never races the never-naked reconcile.

    The target SL size is derived from the LIVE owned snapshot (``owned - qty`` =
    remaining), NOT from ``sl_leg.amount`` — the latter is stale across a
    multi-tranche batch. ``sl_leg`` supplies only the order id / side / type.
    When nothing remains (a full close), the SL is CANCELLED, not amended to zero
    (Saxo rejects a zero-qty amend). If the market sell fails AFTER the SL was
    shrunk/cancelled, the position is briefly under-covered — the never-naked
    reconcile pass re-covers it next tick (the wired caller's backstop).
    """
    live = broker.get_positions_by_uic(uic)
    owned = max(live.quantity, 0.0)
    qty = min(exit.qty, round(owned))
    if qty <= 0:
        logger.info("tranche %s uic %s: position gone (owned=%.2f) — no sell", exit.tag, uic, owned)
        return False
    new_sl_qty = max(round(owned) - qty, 0.0)
    # 1) free the tranche from the SL FIRST (a sell while the SL commits full
    #    owned is rejected SellOrdersAlreadyExistForOwnedContracts).
    if new_sl_qty <= _QTY_EPS:
        broker.cancel_order(sl_leg.order_id)  # full close — cancel, don't amend-to-zero
    else:
        broker.amend_stop_amount(
            uic=uic,
            order_id=sl_leg.order_id,
            side=sl_leg.side or "SELL",
            order_type=sl_leg.order_type or "StopIfTraded",
            new_qty=new_sl_qty,
            stop_price=stop_price,
            request_id=f"{request_ref}-{exit.tag}-amend",
        )
    # 2) market-sell the freed tranche.
    broker.place_market_order(uic, "SELL", qty, request_id=f"{request_ref}-{exit.tag}-sell")
    logger.info("tranche %s uic %s: SL qty -> %.0f, market-sold %d", exit.tag, uic, new_sl_qty, qty)
    return True


def fold_fired_tranches(lines: Iterable[Mapping[str, Any]]) -> dict[int, frozenset[str]]:
    """Fold append-only ``tranche_fired`` journal lines into per-uic tag sets.
    Non-``tranche_fired`` and malformed (missing uic/tag) lines are ignored."""
    acc: dict[int, set[str]] = {}
    for line in lines:
        if line.get("kind") != "tranche_fired":
            continue
        uic, tag = line.get("uic"), line.get("tag")
        if uic is None or not tag:
            continue
        acc.setdefault(int(uic), set()).add(str(tag))
    return {u: frozenset(t) for u, t in acc.items()}


def _fire_telemetry(point: PricePoint, exit: TrancheExit) -> dict[str, Any]:
    """Map the decision-instant quote + the fired tranche to the decision-side
    execution-quality telemetry dict. Pure — no I/O, no broker read.

    ``decision_bid`` is the executable side for selling a long; keeping the full
    bid/ask/mid/spread lets a downstream consumer compute implementation
    shortfall against ANY decision convention once the FILL price is captured in
    a later increment (see ``mark_tranche_fired``). Field names are a stable
    contract — a downstream reader keys off them verbatim."""
    return {
        "decision_bid": point.bid,
        "decision_ask": point.ask,
        "decision_mid": point.mid,
        "spread_abs": point.ask - point.bid,
        "target_price": exit.target_price,
        "qty": exit.qty,
        "event_time": point.event_time.isoformat() if point.event_time is not None else None,
        "source": point.source,
    }


def mark_tranche_fired(uic: int, tag: str, *, telemetry: Mapping[str, Any] | None = None) -> None:
    """Append one ``tranche_fired`` marker (idempotency: a fired tranche never
    re-fires). Writes via the shared append-only standalone-stop journal seam.

    ``kind``/``uic``/``tag`` stay at the TOP LEVEL because ``fold_fired_tranches``
    keys idempotency off them; optional decision-side ``telemetry`` is nested
    under its own key so it can never collide with those. When ``telemetry`` is
    None the line is the historical bare 3-key shape (byte-identical for existing
    callers/journals).

    OUT OF SCOPE (decision-side only): the ACTUAL fill / execution price is NOT
    captured here. ``Broker.place_market_order`` returns no fill price, so
    implementation shortfall (fill − decision) is not yet computable; capturing
    the fill needs a follow-up broker read (``get_order`` /
    ``cs/v1/audit/orderactivities``) and is a SEPARATE increment. This function
    persists the half that is irrecoverable if not stamped at the fire instant
    (the provider bid/ask/mid at decision time); the fill side lives in the
    broker's own audit trail."""
    from alphalens_pipeline.brokers.automanager.control_loop import (
        _append_standalone_stop_journal,
    )

    line: dict[str, Any] = {"kind": "tranche_fired", "uic": int(uic), "tag": str(tag)}
    if telemetry is not None:
        line["telemetry"] = dict(telemetry)
    _append_standalone_stop_journal(line)


@dataclass(frozen=True)
class ManagedExit:
    uic: int
    tp_tranches: tuple[TpTranchePlan, ...]
    reference_qty: float
    stop_price: float
    already_fired: frozenset[str]


def run_live_exits(broker: Broker, feed: PriceFeed, managed: list[ManagedExit]) -> int:
    """One live-exit pass over managed positions. Stale/absent price -> veto (skip).
    INERT: no daemon caller yet. Returns the number of tranches fired.

    ``list_working_sell_orders`` is NOT part of the ``Broker`` Protocol
    (``broker_contract/contract.py``), so it is read via the same defensive
    ``getattr(broker, "list_working_sell_orders", None)`` convention
    ``control_loop.py`` already uses at two call sites -- an
    ``AttributeError`` here would escape the ``except BrokerError`` boundary
    and kill the whole tick.
    """
    fired = 0
    list_sells = getattr(broker, "list_working_sell_orders", None)
    for m in managed:
        point = feed.latest(m.uic)
        if point is None:
            continue  # stream-health veto
        if list_sells is None:
            logger.warning(
                "uic %s: broker has no list_working_sell_orders - skipping live exits this pass",
                m.uic,
            )
            continue
        live = broker.get_positions_by_uic(m.uic)
        legs = tuple(list_sells())
        legs = tuple(leg for leg in legs if leg.uic == m.uic)
        sl = _sole_standalone_stop(legs)
        if sl is None:
            logger.info("uic %s: no sole standalone SL — skipping live exits this pass", m.uic)
            continue
        exits = plan_tranche_exits(
            price=point.bid,  # selling a long: the executable side is the BID
            tp_tranches=m.tp_tranches,
            reference_qty=m.reference_qty,
            owned=live.quantity,
            already_fired=m.already_fired,
        )
        for ex in exits:
            if execute_tranche_exit(
                broker,
                uic=m.uic,
                exit=ex,
                sl_leg=sl,
                stop_price=m.stop_price,
                request_ref=f"u{m.uic}",
            ):
                mark_tranche_fired(m.uic, ex.tag, telemetry=_fire_telemetry(point, ex))
                fired += 1
    return fired
