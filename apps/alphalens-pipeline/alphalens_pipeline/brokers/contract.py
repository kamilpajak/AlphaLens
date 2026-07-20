"""Broker-agnostic surface: dataclasses, the ``Broker`` Protocol, error taxonomy.

ZERO vendor imports — a second broker (IBKR, ...) implements this contract
without touching any Saxo code, and consumers (CLI, future reconciler) branch
on nothing vendor-specific. Semantics a Protocol cannot pin (what an adapter
must actually DO) live in the shared conformance mixin at
``apps/alphalens-research/tests/brokers/test_broker_contract.py``.

YAGNI cuts (deliberate, per the design memo): NO streaming, NO quotes/prices,
NO order modify (cancel+replace in P2 if needed), NO portfolio analytics,
NO multi-account handles. In P1 only the four reads are implemented by
``SaxoBroker``; the placement/status/cancel SIGNATURES are frozen here now so
P2 does not churn the contract — adapters raise :class:`BrokerCapabilityError`
until their increment lands.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Error taxonomy — each broker adapter translates its vendor errors at the
# boundary; vendor exceptions never escape the adapter package.
# --------------------------------------------------------------------------


class BrokerError(RuntimeError):
    """Base: permanent / unclassified broker failure."""


class BrokerAuthError(BrokerError):
    """Credentials invalid or expired — no retry, operator action required."""


class BrokerRateLimitError(BrokerError):
    """Throttle exhausted after retries — soft-fail eligible."""


class InstrumentNotFoundError(BrokerError):
    """Instrument resolution failed for (ticker, exchange_mic) — miss or ambiguity."""


class OrderRejectedError(BrokerError):
    """Broker rejected an order (carries the broker reason string)."""


class BrokerCapabilityError(BrokerError):
    """Method not supported by this broker or in this increment (e.g. P1 placement)."""


# --------------------------------------------------------------------------
# Contract dataclasses (frozen — execution records are facts, not state).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentRef:
    """A broker-resolved instrument handle.

    ``exchange_mic`` follows the ISO 10383 convention already used by
    ``alphalens_pipeline.paper.calendar`` (e.g. ``"XNYS"``), keeping the
    multi-venue extension seam (ticker->exchange routing) consistent.

    ``currency`` is the AUTHORITATIVE instrument trading currency stamped by
    the adapter from the broker's own instrument data at resolve time (Saxo:
    the search row's ``CurrencyCode`` — zero extra API calls), NEVER inferred
    from the MIC. ``""`` means not-stamped (e.g. a best-effort reverse lookup
    from a position row); ``resolve_instrument`` implementations must either
    stamp a real ISO code or refuse — sizing consumers treat ``""`` as a
    refusal, not a guess. (FX-leg design memo, additive field — the Broker
    Protocol itself stays frozen.)
    """

    ticker: str  # plain ticker, e.g. "KO"
    exchange_mic: str  # ISO 10383 MIC, e.g. "XNYS"
    asset_type: str  # "Stock" for now
    broker_instrument_id: str  # opaque broker handle; Saxo: str(Uic)
    broker_symbol: str  # broker display symbol, e.g. "KO:xnys"
    currency: str = ""  # ISO currency code, e.g. "PLN"; "" = not stamped


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str  # opaque broker key; Saxo: AccountKey
    currency: str
    cash: float
    total_value: float
    margin_available: float | None
    asof: dt.datetime  # UTC


@dataclass(frozen=True)
class Position:
    instrument: InstrumentRef
    quantity: float  # signed quantity; positive is long
    avg_price: float
    market_value: float | None  # None when broker quote unavailable (SIM NoAccess)
    unrealized_pnl: float | None
    position_id: str  # Saxo: PositionId (needed later to attach exits, EoD netting)


class OrderStatus(enum.Enum):
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderState:
    order_id: str
    status: OrderStatus
    instrument: InstrumentRef | None
    filled_quantity: float
    raw_status: str  # broker-native string, for diagnostics; never branched on by consumers


@dataclass(frozen=True)
class BracketOrderRequest:
    """ONE entry + server-side OCO exits — the unit a ladder decomposes into (P2)."""

    instrument: InstrumentRef
    side: Literal["BUY", "SELL"]
    quantity: int
    entry_limit: float
    stop_loss: float | None  # disaster stop
    take_profit: float | None
    entry_ttl_days: int  # TRADING days (trade_setup.order_ttl_days); exits GoodTillCancel
    client_request_id: str  # uuid4; idempotency/dedup token (Saxo x-request-id)


@dataclass(frozen=True)
class PlacedOrder:
    entry_order_id: str
    exit_order_ids: tuple[str, ...]


# --------------------------------------------------------------------------
# The Protocol.
# --------------------------------------------------------------------------


@runtime_checkable
class Broker(Protocol):
    """Broker-agnostic execution surface (ADR 0014).

    ``typing.Protocol`` (not ABC) per house composition-over-inheritance
    style: an adapter conforms structurally, without importing this module
    into its inheritance chain.
    """

    name: str  # "saxo", "ibkr", ...

    def get_account(self) -> AccountSnapshot: ...

    def get_positions(self) -> list[Position]: ...

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef: ...

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder: ...

    def get_order(self, order_id: str) -> OrderState: ...

    def list_open_orders(self) -> list[OrderState]: ...

    def cancel_order(self, order_id: str) -> None: ...


__all__ = [
    "AccountSnapshot",
    "BracketOrderRequest",
    "Broker",
    "BrokerAuthError",
    "BrokerCapabilityError",
    "BrokerError",
    "BrokerRateLimitError",
    "InstrumentNotFoundError",
    "InstrumentRef",
    "OrderRejectedError",
    "OrderState",
    "OrderStatus",
    "PlacedOrder",
    "Position",
]
