"""SaxoBroker — adapts the canonical SaxoClient to the ``contract.Broker`` Protocol.

Translation duties (and nothing else — transport lives in ``client.py``):

- Saxo payloads -> the frozen contract dataclasses;
- ``Saxo*Error`` -> ``Broker*Error`` at the boundary (vendor exceptions never
  escape ``brokers/saxo/``);
- MIC -> Saxo ExchangeId mapping + exact-symbol instrument resolution with a
  FIFO-bounded in-process cache (sec_edgar_client cache pattern; no disk
  cache in P1);
- P2 order surface: the ``ALPHALENS_BROKER_ALLOW_ORDERS`` env gate, pre-POST
  validation (tick-size quantization + SupportedOrderTypes), the precheck
  gate before EVERY real POST, single-shot 3-way bracket body build
  (``ManualOrder=false`` everywhere, date-only GTD entry via the exchange
  calendar, GTC exits), partial-acceptance auto-repair (400-with-OrderId ->
  cancel the entry, cascade cleans children), honest 202 handling, and
  order-state mapping (absent -> ``OrderStatus.UNKNOWN``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerAuthError,
    BrokerCapabilityError,
    BrokerError,
    BrokerRateLimitError,
    InstrumentNotFoundError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)
from alphalens_pipeline.brokers.saxo.client import (
    SaxoAuthError,
    SaxoClient,
    SaxoError,
    SaxoRateLimitError,
    get_default_saxo_client,
)
from alphalens_pipeline.paper.calendar import advance_trading_sessions

logger = logging.getLogger(__name__)

# Env var selecting one AccountKey when the Saxo profile has several accounts.
# With a single account the adapter auto-picks; with several and no selector
# it fails loudly rather than guessing.
ACCOUNT_KEY_ENV = "SAXO_ACCOUNT_KEY"

# Safety rail (design memo §P2): order PLACEMENT requires this env var set to
# "1"; checked at method entry, before precheck, before any POST. cancel_order
# is deliberately NOT gated — remediation (cleaning a naked entry after a 202
# or partial acceptance) must always work; reads stay ungated too.
ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"

# ISO 10383 MIC -> Saxo ExchangeId. Seeded from Saxo's /ref/v1/exchanges
# reference data; the SAXO_LIVE_TEST=1 probe (tests/live/test_saxo_live.py)
# verifies the codes against the real SIM gateway. Saxo display symbols carry
# the lowercase MIC as suffix ("KO:xnys"), which resolve exploits for the
# exact-symbol match below. Adding a venue = one entry here.
_MIC_TO_SAXO_EXCHANGE_ID: dict[str, str] = {
    "XNYS": "NYSE",
    "XNAS": "NASDAQ",
    "XWAR": "WSE",
}

_DEFAULT_INSTRUMENT_CACHE_SIZE = 256


def _today() -> dt.date:
    """UTC today — module-level so tests can pin the GTD calendar math."""
    return dt.datetime.now(dt.UTC).date()


@contextlib.contextmanager
def _translate_saxo_errors() -> Iterator[None]:
    """Adapter boundary: map the vendor taxonomy onto the contract taxonomy."""
    try:
        yield
    except SaxoAuthError as exc:
        raise BrokerAuthError(str(exc)) from exc
    except SaxoRateLimitError as exc:
        raise BrokerRateLimitError(str(exc)) from exc
    except SaxoError as exc:
        raise BrokerError(str(exc)) from exc


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _validate_price_relations(
    side: str,
    entry_q: float,
    stop_q: float | None,
    tp_q: float | None,
    *,
    symbol: str,
) -> None:
    """Reject degenerate bracket geometry LOCALLY, on the QUANTIZED prices.

    A BUY bracket requires ``stop < entry < tp`` (a SELL bracket the reverse).
    Saxo's precheck would reject these too, but locally the failure is
    deterministic, immediate, and names the offending leg — and no network
    call is spent on an order that can never be valid (review finding, PR #840).
    """
    buy = side == "BUY"
    if stop_q is not None and ((stop_q >= entry_q) if buy else (stop_q <= entry_q)):
        raise OrderRejectedError(
            f"{symbol}: degenerate bracket — stop_loss {stop_q} must be "
            f"{'below' if buy else 'above'} the entry {entry_q} for a {side}"
        )
    if tp_q is not None and ((tp_q <= entry_q) if buy else (tp_q >= entry_q)):
        raise OrderRejectedError(
            f"{symbol}: degenerate bracket — take_profit {tp_q} must be "
            f"{'above' if buy else 'below'} the entry {entry_q} for a {side}"
        )


class SaxoBroker:
    """Saxo implementation of the broker-agnostic ``contract.Broker`` Protocol."""

    name = "saxo"

    def __init__(
        self,
        client: SaxoClient,
        *,
        account_key: str | None = None,
        cache_size: int = _DEFAULT_INSTRUMENT_CACHE_SIZE,
    ):
        self._client = client
        self._account_key = account_key
        self._cache_size = max(cache_size, 1)
        # FIFO-bounded (ticker, mic) -> InstrumentRef cache: Uics are stable,
        # the working set is small (daily brief candidates), and FIFO keeps
        # eviction deterministic without LRU bookkeeping.
        self._instrument_cache: OrderedDict[tuple[str, str], InstrumentRef] = OrderedDict()

    # ----- reads (P1) -----

    def get_account(self) -> AccountSnapshot:
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            accounts: list[dict[str, Any]] = self._client.get_accounts().get("Data") or []
            account = self._select_account(accounts)
            account_key = str(account["AccountKey"])
            balances = self._client.get_balances(client_key, account_key=account_key)
        return AccountSnapshot(
            account_id=account_key,
            currency=str(balances["Currency"]),
            cash=float(balances["CashBalance"]),
            total_value=float(balances["TotalValue"]),
            margin_available=_opt_float(balances.get("MarginAvailableForTrading")),
            asof=dt.datetime.now(dt.UTC),
        )

    def get_positions(self) -> list[Position]:
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            rows: list[dict[str, Any]] = self._client.get_positions(client_key).get("Data") or []
        return [self._to_position(row) for row in rows]

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        ticker = ticker.upper()
        exchange_mic = exchange_mic.upper()
        cache_key = (ticker, exchange_mic)
        cached = self._instrument_cache.get(cache_key)
        if cached is not None:
            return cached

        exchange_id = _MIC_TO_SAXO_EXCHANGE_ID.get(exchange_mic)
        if exchange_id is None:
            raise InstrumentNotFoundError(
                f"no Saxo ExchangeId mapping for MIC {exchange_mic!r}; supported: "
                f"{sorted(_MIC_TO_SAXO_EXCHANGE_ID)} (add the venue to "
                "_MIC_TO_SAXO_EXCHANGE_ID after verifying via /ref/v1/exchanges)"
            )

        with _translate_saxo_errors():
            payload = self._client.search_instruments(ticker, exchange_id=exchange_id)
        rows: list[dict[str, Any]] = payload.get("Data") or []
        expected_symbol = f"{ticker}:{exchange_mic}".lower()
        matches = [row for row in rows if str(row.get("Symbol", "")).lower() == expected_symbol]
        if not matches:
            raise InstrumentNotFoundError(
                f"no Saxo instrument with symbol {expected_symbol!r} for "
                f"({ticker}, {exchange_mic}); search returned "
                f"{[row.get('Symbol') for row in rows]}"
            )
        if len(matches) > 1:
            raise InstrumentNotFoundError(
                f"ambiguous Saxo resolution for ({ticker}, {exchange_mic}): "
                f"{len(matches)} rows share symbol {expected_symbol!r}"
            )
        row = matches[0]
        ref = InstrumentRef(
            ticker=ticker,
            exchange_mic=exchange_mic,
            asset_type=str(row.get("AssetType", "Stock")),
            broker_instrument_id=str(row["Identifier"]),
            broker_symbol=str(row["Symbol"]),
        )
        self._cache_instrument(cache_key, ref)
        return ref

    # ----- orders (P2) -----

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        """Place ONE single-shot 3-way bracket (entry + order-attached exits).

        Safety order of operations: (1) the ``ALPHALENS_BROKER_ALLOW_ORDERS``
        env gate — checked before ANY client call; (2) pre-POST validation
        (SupportedOrderTypes + tick-size quantization with the bps hard-fail);
        (3) the precheck gate (validates, places nothing); (4) the ONE real
        POST with ``x-request-id = client_request_id``. Precheck reserves
        nothing, so the real POST can still fail — handled in
        :meth:`_handle_placement_response` (partial-acceptance auto-repair,
        honest 202).
        """
        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            raise BrokerCapabilityError(
                f"order placement is disabled: set {ALLOW_ORDERS_ENV}=1 to allow "
                "SIM order submission (design memo §P2 safety rail). "
                "No order was sent."
            )
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_bracket_body(request, account_key)
            self._precheck_or_raise(body, request)
            status, payload = self._client.place_order(body, request_id=request.client_request_id)
            return self._handle_placement_response(status, payload, request, account_key)

    def precheck_bracket_order(self, request: BracketOrderRequest) -> dict[str, Any]:
        """Validate + precheck WITHOUT placing (the CLI dry-run path).

        Runs the same pre-POST validation and ``/trade/v2/orders/precheck``
        call as :meth:`place_bracket_order` but never POSTs the real order —
        so it is deliberately NOT behind the ``ALPHALENS_BROKER_ALLOW_ORDERS``
        gate. Returns the precheck payload (EstimatedCashRequired / costs /
        PreCheckResult); raises :class:`OrderRejectedError` on a non-Ok
        result exactly like the live path would.
        """
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_bracket_body(request, account_key)
            return self._precheck_or_raise(body, request)

    def get_order(self, order_id: str) -> OrderState:
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            payload = self._client.get_order_status(client_key, order_id)
        if not payload or not payload.get("OrderId"):
            # The open-orders endpoint drops filled/cancelled/expired orders:
            # absent is honestly UNKNOWN (FILLED vs CANCELLED needs the audit/
            # ENS activity log — P3 scope, see design memo §P2).
            return OrderState(
                order_id=order_id,
                status=OrderStatus.UNKNOWN,
                instrument=None,
                filled_quantity=0.0,
                raw_status="",
            )
        return self._to_order_state(payload)

    def list_open_orders(self) -> list[OrderState]:
        with _translate_saxo_errors():
            rows: list[dict[str, Any]] = self._client.get_open_orders().get("Data") or []
        return [self._to_order_state(row) for row in rows]

    def cancel_order(self, order_id: str) -> None:
        """Cancel an order. Deliberately NOT behind the placement env gate.

        Cancelling an unfilled entry silently cascades to its related-order
        children — one DELETE cleans the whole bracket (never delete children
        first). Cancellation can fail while an order is locked pre-execution;
        that surfaces as :class:`BrokerError` and the caller retries.
        """
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            status, payload = self._client.cancel_order_ids(order_id, account_key=account_key)
        if status >= 400:
            raise BrokerError(
                f"cancel of order {order_id} failed with HTTP {status}: {payload} "
                "(the order may be locked pre-execution — retry)"
            )

    # ----- internals -----

    def _select_account(self, accounts: list[dict[str, Any]]) -> dict[str, Any]:
        if not accounts:
            raise BrokerError("Saxo returned no accounts for this client")
        if self._account_key is not None:
            for account in accounts:
                if str(account.get("AccountKey")) == self._account_key:
                    return account
            raise BrokerError(
                f"account key {self._account_key!r} not among the client's accounts "
                f"({[a.get('AccountKey') for a in accounts]}); check {ACCOUNT_KEY_ENV}"
            )
        if len(accounts) == 1:
            return accounts[0]
        raise BrokerError(
            f"client has {len(accounts)} accounts "
            f"({[a.get('AccountKey') for a in accounts]}); set {ACCOUNT_KEY_ENV} "
            "to pick one"
        )

    def _to_position(self, row: dict[str, Any]) -> Position:
        base: dict[str, Any] = row.get("PositionBase") or {}
        view: dict[str, Any] = row.get("PositionView") or {}
        display: dict[str, Any] = row.get("DisplayAndFormat") or {}
        symbol = str(display.get("Symbol", ""))
        ticker, _, mic_suffix = symbol.partition(":")
        return Position(
            instrument=InstrumentRef(
                ticker=ticker.upper(),
                exchange_mic=mic_suffix.upper(),
                asset_type=str(base.get("AssetType", "Stock")),
                broker_instrument_id=str(base.get("Uic", "")),
                broker_symbol=symbol,
            ),
            quantity=float(base["Amount"]),
            avg_price=float(base["OpenPrice"]),
            market_value=_opt_float(view.get("MarketValue")),
            unrealized_pnl=_opt_float(view.get("ProfitLossOnTrade")),
            position_id=str(row.get("PositionId", "")),
        )

    def _resolve_account_key(self) -> str:
        accounts: list[dict[str, Any]] = self._client.get_accounts().get("Data") or []
        return str(self._select_account(accounts)["AccountKey"])

    @staticmethod
    def _tick_size_for(price: float, details: dict[str, Any]) -> float:
        """The instrument tick size applicable at ``price``.

        Preference order: the price-banded ``TickSizeScheme`` -> a flat
        ``TickSize`` -> ``10^-decimals`` from the display format.
        """
        scheme = details.get("TickSizeScheme")
        if isinstance(scheme, dict):
            elements = sorted(
                (e for e in scheme.get("Elements") or [] if e.get("HighPrice") is not None),
                key=lambda e: float(e["HighPrice"]),
            )
            for element in elements:
                if price <= float(element["HighPrice"]):
                    return float(element["TickSize"])
            default = scheme.get("DefaultTickSize")
            if default is not None:
                return float(default)
        flat = details.get("TickSize")
        if flat is not None:
            return float(flat)
        fmt: dict[str, Any] = details.get("Format") or {}
        decimals = int(fmt.get("OrderDecimals", fmt.get("Decimals", details.get("Decimals", 2))))
        return 10.0**-decimals

    def _quantize_price(self, price: float, details: dict[str, Any], *, label: str) -> float:
        """Nearest-tick quantization with the config-versioned bps hard-fail.

        The quantization must be a ROUNDING, not a silent price change: if the
        nearest tick moves the price by more than the (call-time read) policy
        cap, the placement fails loudly instead of drifting the geometry.
        """
        if price <= 0:
            raise OrderRejectedError(f"{label} must be > 0, got {price}")
        tick = self._tick_size_for(price, details)
        quantized = round(round(price / tick) * tick, 10)
        adjustment_bps = abs(quantized - price) / price * 1e4
        max_bps = execution_policy._MAX_TICK_ADJUSTMENT_BPS
        if adjustment_bps > max_bps:
            raise OrderRejectedError(
                f"{label}={price} needs a tick adjustment of {adjustment_bps:.1f} bps "
                f"(tick {tick}) — exceeds the {max_bps} bps cap; the instrument's "
                "tick scheme disagrees with the setup's price scale"
            )
        return quantized

    def _build_bracket_body(self, request: BracketOrderRequest, account_key: str) -> dict[str, Any]:
        """Single-shot 3-way bracket body for ``POST /trade/v2/orders``.

        Amount identical across parent and children (Saxo hard rule);
        ``ManualOrder`` pinned from the execution policy on all three;
        ``AccountKey`` explicit on children per docs guidance; entry duration
        is date-only GTD computed ``entry_ttl_days`` TRADING days ahead on the
        venue's exchange calendar; exits are GoodTillCancel so they outlive
        the entry's TTL.
        """
        instrument = request.instrument
        details = self._client.get_instrument_details(
            instrument.broker_instrument_id, instrument.asset_type
        )
        supported = details.get("SupportedOrderTypes") or []
        if supported and "Limit" not in supported:
            raise OrderRejectedError(
                f"instrument {instrument.broker_symbol} does not support Limit "
                f"orders (SupportedOrderTypes={supported})"
            )
        stop_type = execution_policy._STOP_ORDER_TYPE
        if request.stop_loss is not None and supported and stop_type not in supported:
            raise OrderRejectedError(
                f"instrument {instrument.broker_symbol} does not support {stop_type} "
                f"orders (SupportedOrderTypes={supported})"
            )

        manual_order = execution_policy._MANUAL_ORDER
        exit_duration = {"DurationType": execution_policy._EXIT_DURATION}
        side = "Buy" if request.side == "BUY" else "Sell"
        opposite = "Sell" if request.side == "BUY" else "Buy"

        entry_q = self._quantize_price(request.entry_limit, details, label="entry_limit")
        tp_q = (
            self._quantize_price(request.take_profit, details, label="take_profit")
            if request.take_profit is not None
            else None
        )
        stop_q = (
            self._quantize_price(request.stop_loss, details, label="stop_loss")
            if request.stop_loss is not None
            else None
        )
        _validate_price_relations(
            request.side, entry_q, stop_q, tp_q, symbol=instrument.broker_symbol
        )
        expiry = advance_trading_sessions(
            _today(), request.entry_ttl_days, exchange=instrument.exchange_mic
        )

        children: list[dict[str, Any]] = []
        if request.take_profit is not None:
            children.append(
                {
                    "Amount": request.quantity,
                    "BuySell": opposite,
                    "OrderType": "Limit",
                    "OrderPrice": tp_q,
                    "OrderDuration": dict(exit_duration),
                    "ManualOrder": manual_order,
                    "AccountKey": account_key,
                }
            )
        if request.stop_loss is not None:
            children.append(
                {
                    "Amount": request.quantity,
                    "BuySell": opposite,
                    "OrderType": stop_type,
                    "OrderPrice": stop_q,
                    "OrderDuration": dict(exit_duration),
                    "ManualOrder": manual_order,
                    "AccountKey": account_key,
                }
            )

        body: dict[str, Any] = {
            "Uic": int(instrument.broker_instrument_id),
            "AssetType": instrument.asset_type,
            "AccountKey": account_key,
            "Amount": request.quantity,
            "BuySell": side,
            "OrderType": "Limit",
            "OrderPrice": entry_q,
            "OrderDuration": {
                "DurationType": "GoodTillDate",
                "ExpirationDateTime": expiry.isoformat(),
                "ExpirationDateContainsTime": False,
            },
            "ManualOrder": manual_order,
            # Correlation ONLY (echoed in responses/order objects); the real
            # idempotency lives in the x-request-id header (15s dedup window).
            "ExternalReference": request.client_request_id,
        }
        if children:
            body["Orders"] = children
        return body

    def _precheck_or_raise(
        self, body: dict[str, Any], request: BracketOrderRequest
    ) -> dict[str, Any]:
        """POST /trade/v2/orders/precheck; non-Ok blocks the real POST."""
        precheck_body = dict(body)
        precheck_body["FieldGroups"] = ["Costs"]
        status, payload = self._client.precheck_order(precheck_body)
        error_info = payload.get("ErrorInfo")
        result = payload.get("PreCheckResult")
        if status >= 400 or error_info or (result is not None and result != "Ok"):
            raise OrderRejectedError(
                f"precheck rejected bracket {request.client_request_id} "
                f"({request.instrument.broker_symbol}): status={status} "
                f"PreCheckResult={result!r} ErrorInfo={error_info!r} — nothing was placed"
            )
        return payload

    @staticmethod
    def _find_order_id(payload: dict[str, Any]) -> str | None:
        """An OrderId anywhere in an error body means live orders exist."""
        order_id = payload.get("OrderId")
        if order_id:
            return str(order_id)
        for child in payload.get("Orders") or []:
            if isinstance(child, dict) and child.get("OrderId"):
                return str(child["OrderId"])
        return None

    @staticmethod
    def _rejection_detail(payload: dict[str, Any]) -> str:
        error_info = payload.get("ErrorInfo")
        if isinstance(error_info, dict):
            return f"{error_info.get('ErrorCode')}: {error_info.get('Message')}"
        if payload.get("ErrorCode"):
            return f"{payload.get('ErrorCode')}: {payload.get('Message')}"
        model_state = payload.get("ModelState")
        if isinstance(model_state, dict):
            parts = [
                f"{field}: {'; '.join(str(m) for m in messages)}"
                if isinstance(messages, list)
                else f"{field}: {messages}"
                for field, messages in sorted(model_state.items())
            ]
            return " | ".join(parts)
        return str(payload)

    def _handle_placement_response(
        self,
        status: int,
        payload: dict[str, Any],
        request: BracketOrderRequest,
        account_key: str,
    ) -> PlacedOrder:
        if status in (200, 201):
            order_id = payload.get("OrderId")
            if not order_id:
                raise BrokerError(
                    f"Saxo placement returned HTTP {status} without an OrderId "
                    f"for bracket {request.client_request_id}: {payload!r}"
                )
            exit_ids = tuple(
                str(child["OrderId"])
                for child in payload.get("Orders") or []
                if isinstance(child, dict) and child.get("OrderId")
            )
            return PlacedOrder(entry_order_id=str(order_id), exit_order_ids=exit_ids)

        if status == 202:
            order_id = self._find_order_id(payload)
            raise BrokerError(
                f"Saxo 202 TradeNotCompleted for bracket {request.client_request_id}: "
                f"entry order {order_id} is likely LIVE while its related exits were "
                "CANCELLED by Saxo (naked-entry hazard). No automatic action taken — "
                "order state is genuinely unknown for ~seconds. Reconcile: poll "
                "'alphalens broker orders', then 'alphalens broker cancel "
                f"{order_id}' if the entry is unwanted."
            )

        # 4xx rejection path. Sequential acceptance (master -> TP -> SL) means
        # an OrderId in the error body = live orders exist -> auto-repair by
        # cancelling the entry (cascade removes any placed child), then raise.
        detail = self._rejection_detail(payload)
        live_order_id = self._find_order_id(payload)
        if live_order_id:
            cleanup = self._repair_partial_acceptance(live_order_id, account_key)
            raise OrderRejectedError(
                f"Saxo rejected bracket {request.client_request_id} with HTTP {status} "
                f"AFTER accepting order {live_order_id} ({detail}); cleanup: {cleanup}"
            )
        raise OrderRejectedError(
            f"Saxo rejected bracket {request.client_request_id} with HTTP {status}: {detail}"
        )

    def _repair_partial_acceptance(self, order_id: str, account_key: str) -> str:
        try:
            status, payload = self._client.cancel_order_ids(order_id, account_key=account_key)
        except SaxoError as exc:
            logger.error("partial-acceptance cleanup DELETE failed for %s: %s", order_id, exc)
            return f"cancel of {order_id} FAILED ({exc}) — reconcile via 'alphalens broker orders'"
        if status >= 400:
            logger.error(
                "partial-acceptance cleanup for %s returned HTTP %d: %r", order_id, status, payload
            )
            return (
                f"cancel of {order_id} FAILED with HTTP {status} — reconcile via "
                "'alphalens broker orders'"
            )
        return f"entry order {order_id} cancelled (children cascade)"

    def _instrument_from_uic(self, uic: Any) -> InstrumentRef | None:
        """Best-effort reverse lookup through the resolve cache; None on miss."""
        if uic is None:
            return None
        uic_str = str(uic)
        for ref in self._instrument_cache.values():
            if ref.broker_instrument_id == uic_str:
                return ref
        return None

    def _to_order_state(self, row: dict[str, Any]) -> OrderState:
        filled = float(row.get("FillAmount") or 0.0)
        return OrderState(
            order_id=str(row.get("OrderId", "")),
            status=OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.WORKING,
            instrument=self._instrument_from_uic(row.get("Uic")),
            filled_quantity=filled,
            raw_status=str(row.get("Status", "")),
        )

    def _cache_instrument(self, key: tuple[str, str], ref: InstrumentRef) -> None:
        if key not in self._instrument_cache and len(self._instrument_cache) >= self._cache_size:
            self._instrument_cache.popitem(last=False)  # FIFO eviction
        self._instrument_cache[key] = ref


def create_saxo_broker_from_env() -> SaxoBroker:
    """Registry factory: default SaxoClient + optional ``SAXO_ACCOUNT_KEY``.

    Construction errors (e.g. missing ``SAXO_SIM_TOKEN``) surface as the
    contract taxonomy — the vendor ``SaxoAuthError`` must not escape even
    from the factory.
    """
    with _translate_saxo_errors():
        client = get_default_saxo_client()
    return SaxoBroker(client, account_key=os.environ.get(ACCOUNT_KEY_ENV) or None)


__all__ = [
    "ACCOUNT_KEY_ENV",
    "ALLOW_ORDERS_ENV",
    "SaxoBroker",
    "create_saxo_broker_from_env",
]
