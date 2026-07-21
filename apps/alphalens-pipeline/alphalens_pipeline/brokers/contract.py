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
    """Broker rejected an order (carries the broker reason string).

    ``error_code`` is the verbatim vendor error code (Saxo
    ``ErrorInfo.ErrorCode``) when the adapter could attach it at the boundary
    (set in ``SaxoBroker._precheck_or_raise``); ``None`` when the rejection
    carried no structured code. Safety branches classify on this STRUCTURED
    code (see :func:`_is_sell_orders_already_exist` /
    :func:`_is_too_far_from_entry`), never by parsing the message string —
    string parsing is brittle and rots silently.
    """

    def __init__(self, *args: object, error_code: str | None = None) -> None:
        super().__init__(*args)
        self.error_code = error_code


class BrokerCapabilityError(BrokerError):
    """Method not supported by this broker or in this increment (e.g. P1 placement)."""


# Float-quantity comparison tolerance (saxo-oco memo). Owned share quantities
# are whole numbers on the wire but arrive as floats; a bare ``>=`` on two
# floats can flicker (e.g. ``45.9999999`` vs ``46.0``). Every protection
# comparison on a quantity uses this epsilon instead of a bare operator so the
# reconciler never places/cancels on a phantom sub-share difference.
_QTY_EPS = 0.5


def _is_sell_orders_already_exist(e: BrokerError) -> bool:
    """True iff ``e`` is Saxo's ``SellOrdersAlreadyExistForOwnedContracts``.

    The structured discriminator for Bug B (double-sell): a lone TP / an
    existing sell commitment on the uic means the executor must defer and
    retry next tick, not crash. Classified on the attached ``error_code``,
    never on the message string.
    """
    return (
        isinstance(e, OrderRejectedError)
        and e.error_code == "SellOrdersAlreadyExistForOwnedContracts"
    )


def _is_too_far_from_entry(e: BrokerError) -> bool:
    """True iff ``e`` is Saxo's ``TooFarFromEntryOrder`` rejection.

    A wide disaster stop rejected as a bracket child (ADR 0013 T7) — the
    signal that the exit belongs on a standalone position-level order, not an
    OCO bracket child.
    """
    return isinstance(e, OrderRejectedError) and e.error_code == "TooFarFromEntryOrder"


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
    # Per-uic protection accounting (saxo-oco memo §4.1) — additive, defaulted
    # so existing constructions and a second broker stay source-compatible; the
    # frozen base ``Broker`` Protocol is untouched. Mapped in ``_to_order_state``
    # from fields Saxo already returns (no new HTTP surface).
    uic: int | None = None  # row["Uic"]
    side: Literal["BUY", "SELL"] | None = None  # from row["BuySell"]
    order_type: str | None = None  # Saxo OpenOrderType ("StopIfTraded" | "Limit" | ...)
    amount: float | None = None  # row["Amount"] — RESTING qty, NOT filled
    external_reference: str | None = None  # row["ExternalReference"]


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


@runtime_checkable
class SupportsStandaloneStop(Protocol):
    """Extension capability: post-fill disaster-stop placement outside any bracket.

    NOT on the base :class:`Broker` Protocol (which stays frozen — see the
    class docstring) — only adapters implementing the MVP's Option-B
    standalone-stop flow (Saxo: a StandAlone StopIfTraded, no bracket parent,
    placed AFTER the entry fills) support it. The typed variant of the
    capability-protocol pattern in ``brokers/reconcile.py``
    (``SupportsOrderResolution`` / ``SupportsFillCrossCheck``): callers
    ``isinstance``-narrow a ``Broker`` to this Protocol rather than widening
    the base contract for one vendor's capability.
    """

    def place_standalone_stop(
        self,
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        request_id: str | None = None,
    ) -> PlacedOrder: ...


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
    "SupportsStandaloneStop",
]
