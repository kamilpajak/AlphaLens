"""Live take-profit engine (the 'brain') — decide + execute TP-tranche exits.

Wired into the daemon tick via ``control_loop._run_live_exits_pass`` (behind
``ALPHALENS_LIVE_MARKET_EXITS``). Everything here is driven by an injected
PriceFeed + a broker satisfying :class:`LiveExitBroker`, and is exercised
hermetically.

Safe sequence (SIM-probed 2026-08-05, netting account): shrink the standalone SL
by the tranche FIRST, THEN market-sell the tranche (a sell while the SL commits
full owned is rejected SellOrdersAlreadyExistForOwnedContracts); sell <= live
owned so the position can never flip short.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from broker_contract.contract import (
    _QTY_EPS,
    OrderState,
    SupportsAmendStop,
    SupportsMarketOrders,
    SupportsNettedPositionReads,
)
from broker_contract.price_feed import PriceFeed, PricePoint
from broker_contract.quantity import QuantityLattice, quantize_down
from broker_contract.sizing import TpTranchePlan

from alphalens_pipeline.brokers.automanager.costs import (
    COST_GATE_FX_APPLIES,
    COST_GATE_MIN_COMMISSION_APPLIES,
    min_profitable_exit_price,
    round_trip_fee_bps,
)
from alphalens_pipeline.brokers.automanager.costs import EXIT_EDGE_MIN_BPS as _EXIT_EDGE_MIN_BPS
from alphalens_pipeline.brokers.automanager.labels import tp_label_from_tag
from alphalens_pipeline.brokers.automanager.position_manager import _sole_standalone_stop
from alphalens_pipeline.brokers.execution import assert_rail_lattice

logger = logging.getLogger(__name__)


@runtime_checkable
class LiveExitBroker(
    SupportsAmendStop, SupportsMarketOrders, SupportsNettedPositionReads, Protocol
):
    """The engine's stated broker requirement set (#1141) — narrowed ONCE by the
    caller (``control_loop._run_live_exits_pass``) before any engine call, and
    guaranteed at boot by ``build_default_deps`` whenever
    ``ALPHALENS_LIVE_MARKET_EXITS=1``.

    Deliberately NOT the full frozen :class:`~broker_contract.contract.Broker`:
    the pass + engine together touch exactly these six methods (the engine
    itself calls five; ``get_long_positions`` is the pass's position read), and
    demanding ``place_bracket_order`` etc. would make the pass-level
    ``isinstance`` refuse a broker for abilities the engine never uses.
    Composition over a blanket base is the same capability-protocol pattern the
    parents follow.

    CPython runtime-checkable notes, both load-bearing: (1) ``isinstance`` uses
    ``inspect.getattr_static``, so a mock/fake must set every capability method
    EXPLICITLY — attributes a bare ``Mock.__getattr__`` fabricates on first
    access are invisible to the check (this bit the LIVE-composition stub in
    this very PR); (2) ``runtime_checkable`` re-derives the check from the
    COMBINED member set rather than composing the parents' checks, which is
    equivalent only while every parent stays method-only — keep data members
    out of the parent capabilities.
    """

    def cancel_order(self, order_id: str) -> None: ...

    def list_working_sell_orders(self) -> list[OrderState]: ...


_PRICE_EPS = 1e-9  # a long tranche fires when price >= target (within eps)
_BPS_PER_UNIT = 10_000.0

EXIT_EDGE_MIN_BPS = _EXIT_EDGE_MIN_BPS
"""Re-exported from :mod:`alphalens_pipeline.brokers.automanager.costs` — the declared ``E_min``
buffer, shared with the arm-time gate so the two cannot drift apart."""


def tranche_tag(index: int) -> str:
    return f"tp{index + 1}"


@dataclass(frozen=True)
class TrancheExit:
    tag: str
    # FLOAT, and the annotation is the part that changed rather than the value.
    # `qty` is `min(planned, available)`, and `available` now comes from
    # `quantize_down`, which returns the venue's own float. The old `int` was a
    # lie the moment the cap applied: `min(10, 6.0)` is `6.0`. Float is also the
    # contract type everywhere else on this rail (`Broker.place_market_order`,
    # `Position.quantity`, `OrderState.filled_quantity`), and the LIVE journal
    # already records float quantities Saxo accepted (`qty: 1.0`, `qty: 8.0`),
    # so this is the existing payload class, not a new one.
    qty: float
    target_price: float


@dataclass(frozen=True)
class TrancheExitResult:
    """Outcome of one ``execute_tranche_exit`` call. ``sell_order_id`` is the
    market-SELL order id ``Broker.place_market_order`` returned (None when
    ``sold`` is False). Captured so a LATER offline reconciler can join the
    broker's actual fill by order id -- see ``mark_tranche_fired``'s "OUT OF
    SCOPE" note; this type carries the join key, not the fill itself."""

    sold: bool
    sell_order_id: str | None


def _is_decidable_price(price: Any) -> bool:
    """Whether ``price`` is a quote an exit decision may be taken on: a real,
    finite, strictly positive number.

    Kept separate from the cost gate on purpose — the cost gate is a judgement
    about EDGE and fails open on missing data, while this is a judgement about
    whether there is a price at all, and fails closed.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return False
    return math.isfinite(price) and price > 0.0


def _is_real_quantity(qty: Any) -> bool:
    """Whether ``qty`` is an owned-share count this rail may do arithmetic on.

    The quantity sibling of :func:`_is_decidable_price`, and it exists for the
    same reason that one does: ``round()`` is a sharp edge. ``round(nan)``
    raises ``ValueError``, ``round(inf)`` raises ``OverflowError`` and ``None``
    has no ``__round__`` — and none of those is a ``BrokerError``, so they
    escape ``_run_live_exits_pass``'s only ``except`` clause. The statement
    immediately after it in the tick is the protection pass, the never-naked
    backstop that re-asserts SL == owned, so an escaping exception does not
    just lose one exit: it starves protection for that tick.

    Non-positive is not an error, just nothing to sell — the callers already
    treat it that way, and folding it in here keeps one predicate.
    """
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        return False
    return math.isfinite(qty) and qty > 0.0


def _exit_clears_cost(
    *, price: float, target_price: float, qty: float, realised_entry: float | None, tag: str
) -> bool:
    """Whether selling ``qty`` shares at ``price`` clears round-trip cost plus
    the declared :data:`EXIT_EDGE_MIN_BPS` buffer, measured from the REALISED
    entry (issue #1112 step 2).

    The threshold is :func:`~alphalens_pipeline.brokers.automanager.costs.min_profitable_exit_price`,
    evaluated at THIS TRANCHE's quantity.

    READING RULE — the arm-time gate calls the same function, but at the
    quantity of the position it opens, so the two bars differ whenever the two
    quantities do: a smaller exit quantity pays proportionally more of the
    per-fill USD minimum and therefore needs a HIGHER price. An armed tier is
    not automatically an exit this gate will fire. They coincide only while the
    exit plan is one tranche selling the whole position, which
    :func:`~alphalens_pipeline.brokers.automanager.costs.single_full_position_tranche_violation`
    enforces at arm time.

    FAILS OPEN — an unknown realised entry (``None``, the SIM ``NoAccess``
    non-positive sentinel, or a NaN) returns ``True`` and logs. This is the
    OPPOSITE of ``position_manager._maybe_reanchor``'s fail-closed stance on the
    same field, deliberately: refusing to re-anchor a stop leaves the brief's
    own stop in place, whereas refusing an exit strands a live position with no
    take-profit path. The disaster stop is a separate resting broker order this
    engine never touches, so it still guards the downside either way.
    """
    if realised_entry is None or not math.isfinite(realised_entry) or realised_entry <= 0.0:
        logger.warning(
            "tranche %s: realised entry unknown (%r) — cost gate skipped, firing as before",
            tp_label_from_tag(tag),
            realised_entry,
        )
        return True
    required_price = min_profitable_exit_price(entry_price=realised_entry, qty=qty)
    if required_price is None or price >= required_price:
        return True
    # Refused. Restate the same threshold in bps, which is the shape an operator
    # reads the journal in (the decision above is the price comparison).
    cost_bps = round_trip_fee_bps(
        qty * realised_entry,
        fx_applies=COST_GATE_FX_APPLIES,
        min_commission_applies=COST_GATE_MIN_COMMISSION_APPLIES,
    )
    edge_bps = (price / realised_entry - 1.0) * _BPS_PER_UNIT
    logger.warning(
        "tranche %s refused (inside cost): realised entry %.4f, target %.4f, bid %.4f, "
        "edge %.1f bps < round-trip cost %.1f bps + E_min %.1f bps",
        tp_label_from_tag(tag),
        realised_entry,
        target_price,
        price,
        edge_bps,
        cost_bps,
        EXIT_EDGE_MIN_BPS,
    )
    return False


def plan_tranche_exits(
    *,
    price: float,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    owned: float,
    already_fired: frozenset[str],
    lattice: QuantityLattice,
    realised_entry: float | None = None,
) -> list[TrancheExit]:
    """Which not-yet-fired tranches a LONG at ``price`` should realize now.

    ``reference_qty`` is the tranche-sizing base (the intended/peak filled
    position); tranche qty = round(reference_qty * tranche_frac), cumulatively
    clamped so the batch never exceeds live ``owned``. Order preserved.

    ``realised_entry`` is the position's realised average entry price
    (``Position.avg_price``). When supplied, a tranche whose distance from it is
    inside round-trip cost plus :data:`EXIT_EDGE_MIN_BPS` is refused and logged
    rather than fired — issue #1112, the 2026-08-24 SMG round trip that took
    -380 bps net on a flat gross P&L. Defaulted so the pure-decision callers
    that have no position in hand keep their existing behaviour; the live
    caller (:func:`run_live_exits`) always passes it.

    A non-finite or non-positive ``price`` plans NOTHING. That is about a wrong
    ACTION, not a crash: ``price >= target`` is true for infinity, so before this
    guard an infinite bid touched EVERY tranche at once and the cost gate passed
    them all. A NaN bid did the same whenever the realised entry was unknown,
    which is exactly the branch :func:`_exit_clears_cost` fails open on.
    Defence in depth — the production feeds already withhold such a quote (see
    :func:`run_live_exits`), so no live caller reaches this today.
    """
    # BEFORE the input guards, deliberately. Those return `[]` for a malformed
    # price or quantity, which would skip the assertion for that position and
    # leave the lattice unchecked exactly when something is already wrong.
    assert_rail_lattice(lattice)
    if not _is_decidable_price(price):
        logger.warning("live exits: price %r is not decidable — no tranche planned", price)
        return []
    if not _is_real_quantity(owned):
        logger.warning("live exits: owned %r is not a real quantity — no tranche planned", owned)
        return []
    if not _is_real_quantity(reference_qty):
        # `owned` was guarded in #1126; this is its sibling in the same
        # expression, and the one that comes off DISK. `fold_tranche_plans`
        # parses a journal line's reference_qty with a bare `float()`, and JSON
        # spells non-finite values `NaN` / `Infinity`, so both survive the fold.
        # `round(nan)` raises ValueError and `round(inf)` OverflowError —
        # neither is a BrokerError, so neither is caught by
        # `_run_live_exits_pass`, and the statement after it in the tick is the
        # never-naked protection pass. One malformed line would starve
        # protection for EVERY position, not just the one it describes.
        logger.warning(
            "live exits: reference_qty %r is not a real quantity — no tranche planned",
            reference_qty,
        )
        return []
    available = quantize_down(owned, lattice)
    out: list[TrancheExit] = []
    for i, t in enumerate(tp_tranches):
        tag = tranche_tag(i)
        if tag in already_fired:
            continue
        if price + _PRICE_EPS < t.target_price:
            continue  # target not touched
        qty = min(round(reference_qty * t.tranche_frac), available)
        if qty <= 0:
            continue
        if realised_entry is not None and not _exit_clears_cost(
            price=price,
            target_price=t.target_price,
            qty=qty,
            realised_entry=realised_entry,
            tag=tag,
        ):
            # STOP the batch, never skip to the deeper tranche. The threshold
            # scales with the tranche's own notional (the per-fill USD minimum
            # weighs more on a small tranche), so a deeper LARGER tranche at a
            # HIGHER target can clear while this one does not. Firing it would
            # advance already_fired and the stop-shrink accounting past an
            # unfired shallower tranche, exiting the ladder out of order.
            break
        out.append(TrancheExit(tag=tag, qty=qty, target_price=t.target_price))
        available -= qty
    return out


def execute_tranche_exit(
    broker: LiveExitBroker,
    *,
    uic: int,
    exit: TrancheExit,
    sl_leg: OrderState,
    stop_price: float,
    request_ref: str,
    lattice: QuantityLattice,
) -> TrancheExitResult:
    """Realize ONE tranche: free the tranche from the standalone SL, THEN market
    sell it. Re-snapshots live owned first (never sell more than owned → cannot
    flip short). Returns a ``TrancheExitResult`` (``sold`` True iff the sell was
    sent, carrying the SELL order id for later fill-joining). Callers MUST hold
    a per-uic lock so this never races the never-naked reconcile.

    The target SL size is derived from the LIVE owned snapshot (``owned - qty`` =
    remaining), NOT from ``sl_leg.amount`` — the latter is stale across a
    multi-tranche batch. ``sl_leg`` supplies only the order id / side / type.
    When nothing remains (a full close), the SL is CANCELLED, not amended to zero
    (Saxo rejects a zero-qty amend). If the market sell fails AFTER the SL was
    shrunk/cancelled, the position is briefly under-covered — the never-naked
    reconcile pass re-covers it next tick (the wired caller's backstop).
    """
    # This function CANCELS and AMENDS stops, so it refuses a lattice it cannot
    # reason about on its own rather than trusting that a planner ran first. It
    # is module-level and callable directly; a guard that fires on one entry
    # point only is a guard that can be walked past.
    assert_rail_lattice(lattice)
    live = broker.get_positions_by_uic(uic)
    if not _is_real_quantity(live.quantity):
        # Same stance as the planner: a live read that degrades to None / nan /
        # inf must end this tranche, never raise past the pass boundary and
        # starve the protection pass that follows it in the tick.
        logger.warning(
            "tranche %s uic %s: live quantity %r is not a real quantity — no sell",
            tp_label_from_tag(exit.tag),
            uic,
            live.quantity,
        )
        return TrancheExitResult(sold=False, sell_order_id=None)
    owned = max(live.quantity, 0.0)
    # Quantized ONCE. Computing it twice invites the two uses to drift apart,
    # and the second one decides whether the disaster stop is amended or
    # cancelled — the exact arithmetic this change exists to make trustworthy.
    sellable = quantize_down(owned, lattice)
    qty = min(exit.qty, sellable)
    if qty <= 0:
        logger.info(
            "tranche %s uic %s: position gone (owned=%.2f) — no sell",
            tp_label_from_tag(exit.tag),
            uic,
            owned,
        )
        return TrancheExitResult(sold=False, sell_order_id=None)
    new_sl_qty = max(sellable - qty, 0.0)
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
    placed = broker.place_market_order(
        uic, "SELL", qty, request_id=f"{request_ref}-{exit.tag}-sell"
    )
    sell_order_id = placed.entry_order_id or None
    logger.info(
        "tranche %s uic %s: SL qty -> %.0f, market-sold %d",
        tp_label_from_tag(exit.tag),
        uic,
        new_sl_qty,
        qty,
    )
    return TrancheExitResult(sold=True, sell_order_id=sell_order_id)


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


def _fire_telemetry(
    point: PricePoint, exit: TrancheExit, *, sell_order_id: str | None
) -> dict[str, Any]:
    """Map the decision-instant quote + the fired tranche to the decision-side
    execution-quality telemetry dict. Pure — no I/O, no broker read.

    ``decision_bid`` is the executable side for selling a long; keeping the full
    bid/ask/mid/spread lets a downstream consumer compute implementation
    shortfall against ANY decision convention once the FILL price is captured in
    a later increment (see ``mark_tranche_fired``). ``sell_order_id`` is the join
    key a later offline reconciler uses against the broker's own fill audit
    trail (``cs/v1/audit/orderactivities``) — NOT the fill price itself, which is
    still a separate increment. Field names are a stable contract — a downstream
    reader keys off them verbatim."""
    return {
        "decision_bid": point.bid,
        "decision_ask": point.ask,
        "decision_mid": point.mid,
        "spread_abs": point.ask - point.bid,
        "target_price": exit.target_price,
        "qty": exit.qty,
        "event_time": point.event_time.isoformat() if point.event_time is not None else None,
        "source": point.source,
        "sell_order_id": sell_order_id,
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


@dataclass(frozen=True)
class FiredTranche:
    """One tranche this pass actually market-sold (#1219) — the facts the caller
    needs to render an operator alert. The engine stays notification-port-free
    (mirroring ``entry_trail_watcher``'s alert-intent pattern): control_loop owns
    delivery, message text and the uic -> ticker mapping."""

    uic: int
    tag: str
    qty: float
    sell_order_id: str | None


def run_live_exits(
    broker: LiveExitBroker,
    feed: PriceFeed,
    managed: list[ManagedExit],
    *,
    lattice: QuantityLattice,
) -> list[FiredTranche]:
    """One live-exit pass over managed positions. Stale/absent price -> veto (skip).
    Returns one :class:`FiredTranche` per tranche fired (fired count = its length).

    ``broker`` is the engine's own :class:`LiveExitBroker` requirement set — the
    caller (``control_loop._run_live_exits_pass``) ``isinstance``-narrows before
    calling, so a broker missing any of the six methods never reaches this
    function and the historical per-uic ``getattr`` dance is gone (#1141).

    A point whose BID is not a finite positive number vetoes the uic, BEFORE any
    comparison or ladder activation. The motivating case is a wrong action, not
    a crash: an infinite bid satisfies ``price >= target`` for every tranche, so
    the engine used to sell the whole ladder in one pass.

    Defence in depth, not a reachable live defect today: both production feeds
    already withhold such a quote, by different mechanisms —
    ``yfinance_price_feed`` checks ``isfinite`` / ``> 0`` before it builds the
    point, ``saxo_live_price_feed`` builds the point and then returns it only if
    ``price_feed.is_fresh`` passes, which vetoes a non-finite, non-positive or
    crossed side. Neither is a rule this engine owns, so it states its own.
    """
    # Once per pass, before any per-position work: a lattice the rail cannot
    # reason about ends the pass rather than being re-checked per position.
    assert_rail_lattice(lattice)
    fired: list[FiredTranche] = []
    for m in managed:
        point = feed.latest(m.uic)
        if point is None:
            continue  # stream-health veto
        if not _is_decidable_price(point.bid):
            logger.warning(
                "uic %s: bid %r is not a decidable price — skipping live exits this pass",
                m.uic,
                point.bid,
            )
            continue
        live = broker.get_positions_by_uic(m.uic)
        legs = tuple(broker.list_working_sell_orders())
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
            lattice=lattice,
            # The realised entry the #1112 cost gate measures from — already in
            # hand from the position read above, so no extra broker I/O.
            realised_entry=live.avg_price,
        )
        for ex in exits:
            result = execute_tranche_exit(
                broker,
                uic=m.uic,
                exit=ex,
                sl_leg=sl,
                stop_price=m.stop_price,
                request_ref=f"u{m.uic}",
                lattice=lattice,
            )
            if result.sold:
                telemetry = _fire_telemetry(point, ex, sell_order_id=result.sell_order_id)
                mark_tranche_fired(m.uic, ex.tag, telemetry=telemetry)
                fired.append(
                    FiredTranche(
                        uic=m.uic,
                        tag=ex.tag,
                        qty=ex.qty,
                        sell_order_id=result.sell_order_id,
                    )
                )
    return fired
