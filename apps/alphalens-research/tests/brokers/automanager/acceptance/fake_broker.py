"""A stateful in-memory fake broker for the acceptance suite.

It is deliberately NOT Saxo — it implements only the generic ``Broker`` Protocol
plus the three exit-capability Protocols (``SupportsStandaloneStop`` /
``SupportsOcoExit`` / ``SupportsAmendStop``). Running the real manager against it
proves two things at once: the guarantees hold, AND the manager never depends on
anything Saxo-specific.

Statefulness is the point: a placed stop becomes a resting SELL leg that the next
tick reads back as coverage, so steady-state invariants (never-naked, no-oversell)
are exercised across ticks exactly as they would be against a live broker.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from broker_contract.contract import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)

_QTY_EPS = 0.5  # float-qty tolerance, mirrors contract._QTY_EPS
_FIRST_UIC = 40000  # deterministic ticker -> uic assignment


class FakeBroker:
    """An in-memory broker whose reads reflect its own writes.

    Scenario code never touches this directly — the ManagerWorld DSL drives it.
    """

    name = "fake"

    def __init__(self, *, equity: float = 1_000_000.0) -> None:
        self._equity = equity
        self._uic_by_ticker: dict[str, int] = {}
        self._ticker_by_uic: dict[int, str] = {}
        self._next_uic = _FIRST_UIC
        self._positions: dict[int, Position] = {}
        self._orders: dict[str, OrderState] = {}
        self._oco_sibling: dict[str, str] = {}
        self._seq = 0

        # Fault-injection knobs (set by the DSL for the resilience scenarios).
        self.oco_reject_code: str | None = None
        self.stop_place_error: Exception | None = None
        self.failing_uics: set[int] = set()
        self.cancel_errors: dict[str, Exception] = {}
        # One-shot fault targeting ONLY place_market_order (INC-5 live-exits
        # coordination proof): lets a scenario shrink the standalone SL via a
        # SUCCESSFUL amend_stop_amount and then fail the very next market sell,
        # without failing_uics/stop_place_error also failing the amend.
        self.market_order_error: Exception | None = None

    # ----- instrument registry (ticker <-> uic) --------------------------------

    def uic_of(self, ticker: str) -> int:
        key = ticker.upper()
        if key not in self._uic_by_ticker:
            self._uic_by_ticker[key] = self._next_uic
            self._ticker_by_uic[self._next_uic] = key
            self._next_uic += 1
        return self._uic_by_ticker[key]

    def _instrument(self, ticker: str) -> InstrumentRef:
        return InstrumentRef(
            ticker=ticker.upper(),
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(self.uic_of(ticker)),
            broker_symbol=f"{ticker.upper()}:xnys",
            currency="USD",
        )

    # ----- test-only state mutators (used by the DSL, never by the manager) ----

    def set_position(self, ticker: str, shares: float, *, avg_price: float) -> None:
        uic = self.uic_of(ticker)
        if abs(shares) <= _QTY_EPS:
            self._positions.pop(uic, None)
            return
        self._positions[uic] = Position(
            instrument=self._instrument(ticker),
            quantity=float(shares),
            avg_price=float(avg_price),
            market_value=None,
            unrealized_pnl=None,
            position_id=f"pos-{uic}",
        )

    def add_resting_sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        *,
        order_type: str,
        relation: str | None = None,
    ) -> str:
        uic = self.uic_of(ticker)
        self._seq += 1
        order_id = f"resting-{self._seq}"
        self._orders[order_id] = OrderState(
            order_id=order_id,
            status=OrderStatus.WORKING,
            instrument=self._instrument(ticker),
            filled_quantity=0.0,
            raw_status="Working",
            uic=uic,
            side="SELL",
            order_type=order_type,
            amount=float(shares),
            external_reference=order_id,
            order_relation=relation,
        )
        return order_id

    def has_order(self, order_id: str) -> bool:
        return order_id in self._orders

    # ----- base Broker Protocol ------------------------------------------------

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="FAKE-ACC",
            currency="USD",
            cash=self._equity,
            total_value=self._equity,
            margin_available=None,
            asof=dt.datetime.now(dt.UTC),
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        return self._instrument(ticker)

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        self._seq += 1
        entry_id = f"entry-{self._seq}"
        self._orders[entry_id] = OrderState(
            order_id=entry_id,
            status=OrderStatus.WORKING,
            instrument=request.instrument,
            filled_quantity=0.0,
            raw_status="Working",
            uic=int(request.instrument.broker_instrument_id),
            side=request.side,
            order_type="Limit",
            amount=float(request.quantity),
            external_reference=request.client_request_id,
        )
        return PlacedOrder(entry_order_id=entry_id, exit_order_ids=())

    def get_order(self, order_id: str) -> OrderState:
        order = self._orders.get(order_id)
        if order is None:
            return OrderState(
                order_id=order_id,
                status=OrderStatus.UNKNOWN,
                instrument=None,
                filled_quantity=0.0,
                raw_status="",
            )
        return order

    def list_open_orders(self) -> list[OrderState]:
        return list(self._orders.values())

    def cancel_order(self, order_id: str) -> None:
        err = self.cancel_errors.get(order_id)
        if err is not None:
            raise err
        self._orders.pop(order_id, None)
        sibling = self._oco_sibling.pop(order_id, None)
        if sibling is not None:
            self._orders.pop(sibling, None)
            self._oco_sibling.pop(sibling, None)

    # ----- broker-state-truth protection reads ---------------------------------

    def get_long_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.quantity > _QTY_EPS]

    def get_positions_by_uic(self, uic: int) -> Position:
        pos = self._positions.get(uic)
        if pos is not None:
            return pos
        return Position(
            instrument=self._flat_instrument(uic),
            quantity=0.0,
            avg_price=0.0,
            market_value=None,
            unrealized_pnl=None,
            position_id=f"flat-{uic}",
        )

    def list_working_sell_orders(self) -> list[OrderState]:
        return [
            o for o in self._orders.values() if o.side == "SELL" and o.status == OrderStatus.WORKING
        ]

    # ----- exit capability Protocols -------------------------------------------

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        self._guard_write(uic)
        order_id = self._new_sell_leg(uic, side, qty, "StopIfTraded", request_id)
        return PlacedOrder(entry_order_id=order_id, exit_order_ids=())

    def place_oco_exit(
        self,
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        take_profit: float,
        request_id: str,
        position_id: str | None = None,
    ) -> PlacedOrder:
        self._guard_write(uic)
        if self.oco_reject_code is not None:
            raise OrderRejectedError(
                f"fake OCO reject for uic {uic}", error_code=self.oco_reject_code
            )
        stop_id = self._new_sell_leg(uic, side, qty, "StopIfTraded", f"{request_id}-stop", "Oco")
        tp_id = self._new_sell_leg(uic, side, qty, "Limit", f"{request_id}-tp", "Oco")
        self._oco_sibling[stop_id] = tp_id
        self._oco_sibling[tp_id] = stop_id
        return PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))

    def amend_stop_amount(
        self,
        uic: int,
        order_id: str,
        side: str,
        order_type: str,
        new_qty: float,
        stop_price: float,
        request_id: str,
    ) -> PlacedOrder:
        self._guard_write(uic)
        self._resize(order_id, new_qty)
        # OCO amend propagates to the sibling (both legs stay Amount-consistent).
        sibling = self._oco_sibling.get(order_id)
        if sibling is not None:
            self._resize(sibling, new_qty)
        return PlacedOrder(entry_order_id="", exit_order_ids=(order_id,))

    def place_market_order(
        self, uic: int, side: str, qty: float, request_id: str | None = None
    ) -> PlacedOrder:
        """In-memory market fill: BUY grows the netted position, SELL reduces it
        (clamped at flat — the netting account never flips short on an oversell).
        Deterministic order id; no resting order is created (a market order fills)."""
        if self.market_order_error is not None:
            err, self.market_order_error = self.market_order_error, None  # one-shot
            raise err
        self._guard_write(uic)
        current = self._positions.get(uic)
        held = current.quantity if current is not None else 0.0
        delta = float(qty) if side == "BUY" else -float(qty)
        new_qty = max(held + delta, 0.0)
        self._seq += 1
        order_id = f"mkt-{self._seq}"
        if new_qty <= _QTY_EPS:
            self._positions.pop(uic, None)
        elif current is not None:
            self._positions[uic] = dataclasses.replace(current, quantity=new_qty)
        else:
            ticker = self._ticker_by_uic.get(uic, f"UIC{uic}")
            self._positions[uic] = Position(
                instrument=self._instrument(ticker),
                quantity=new_qty,
                avg_price=0.0,
                market_value=None,
                unrealized_pnl=None,
                position_id=f"pos-{uic}",
            )
        return PlacedOrder(entry_order_id=order_id, exit_order_ids=())

    # ----- internals -----------------------------------------------------------

    def _flat_instrument(self, uic: int) -> InstrumentRef:
        ticker = self._ticker_by_uic.get(uic, f"UIC{uic}")
        return self._instrument(ticker)

    def _guard_write(self, uic: int) -> None:
        if uic in self.failing_uics:
            raise self.stop_place_error or BrokerError(f"fake broker failure on uic {uic}")
        if self.stop_place_error is not None:
            err, self.stop_place_error = self.stop_place_error, None  # one-shot
            raise err

    def _new_sell_leg(
        self,
        uic: int,
        side: str,
        qty: float,
        order_type: str,
        ref: str | None,
        relation: str | None = None,
    ) -> str:
        self._seq += 1
        order_id = f"leg-{self._seq}"
        self._orders[order_id] = OrderState(
            order_id=order_id,
            status=OrderStatus.WORKING,
            instrument=self._instrument(self._ticker_by_uic.get(uic, f"UIC{uic}")),
            filled_quantity=0.0,
            raw_status="Working",
            uic=uic,
            side=side,
            order_type=order_type,
            amount=float(qty),
            external_reference=ref,
            order_relation=relation,
        )
        return order_id

    def _resize(self, order_id: str, new_qty: float) -> None:
        order = self._orders.get(order_id)
        if order is not None:
            self._orders[order_id] = dataclasses.replace(order, amount=float(new_qty))
