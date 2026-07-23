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
import dataclasses
import datetime as dt
import logging
import os
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any, Literal, cast

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.contract import (
    _QTY_EPS,
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
from alphalens_pipeline.paper.fx import FxRateQuote

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


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _order_side(value: Any) -> Literal["BUY", "SELL"] | None:
    """Saxo ``BuySell`` -> the contract's canonical order side, ``None`` otherwise."""
    if value == "Buy":
        return "BUY"
    if value == "Sell":
        return "SELL"
    return None


def _oco_leg_ref(request_id: str, leg: str) -> str:
    """Per-leg ``ExternalReference`` derived from the OCO base ``request_id``.

    ``<request_id>-stop`` / ``<request_id>-tp``. Kept local to the adapter (the
    automanager's ``_exit_stop_ref`` / ``_exit_tp_ref`` are NOT imported here —
    that would create a broker -> automanager -> broker import cycle). The
    executor passes a base ``request_id`` (derived from the entry crid + resize
    generation); the same-suffix scheme keeps the leg refs deterministic for
    x-request-id dedup and broker-state correlation.
    """
    return f"{request_id}-{leg}"


def _position_uic(position: Position) -> int | None:
    """The uic a Position belongs to (``broker_instrument_id`` is ``str(Uic)``)."""
    try:
        return int(position.instrument.broker_instrument_id)
    except (TypeError, ValueError):
        return None


def _validate_price_relations(
    side: str,
    entry_q: float,
    stop_q: float | None,
    tp_q: float | None,
    *,
    symbol: str,
) -> None:
    """Reject degenerate + too-far bracket geometry LOCALLY, on the QUANTIZED
    prices.

    A BUY bracket requires ``stop < entry < tp`` (a SELL bracket the reverse).
    Saxo's precheck would reject the ORDERING violations too, but locally the
    failure is deterministic, immediate, and names the offending leg — and no
    network call is spent on an order that can never be valid (review finding,
    PR #840).

    The second guard is a child-DISTANCE fail-fast: a wide whole-ladder
    disaster stop (ADR 0013 T7) sits 20-30% below each tier entry, far beyond
    Saxo's bracket child-distance band, so Saxo 400s it per-leg with
    ``TooFarFromEntryOrder`` while the single-order precheck stays FALSE-GREEN.
    Any child (stop OR take-profit) more than
    :data:`execution._MAX_CHILD_DISTANCE_FRAC` from the entry is rejected here
    — before any network call, on BOTH the precheck and place paths — with
    guidance that a wide disaster stop belongs on a STANDALONE position-level
    order (Option B), not an OCO bracket child. This is an EARLY ARCHITECTURAL
    guard, not Saxo's authority: Saxo's real child-distance cap is
    instrument-specific and undocumented (tighter than this band), so an
    in-band child may still be rejected server-side. Design:
    ``docs/research/saxo_wide_stop_bracket_design_2026_07_20.md``.
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
    limit_frac = execution_policy._MAX_CHILD_DISTANCE_FRAC
    for child_label, child_q in (("stop_loss", stop_q), ("take_profit", tp_q)):
        if child_q is None:
            continue
        dist_frac = abs(entry_q - child_q) / entry_q
        if dist_frac > limit_frac:
            raise OrderRejectedError(
                f"{symbol}: {child_label} child {child_q} is {dist_frac * 100:.1f}% "
                f"from the entry {entry_q}, beyond the {limit_frac * 100:.0f}% bracket "
                "child-distance fail-fast band — a wide disaster stop must be placed "
                "as a STANDALONE position-level order (Option B), not an OCO bracket "
                "child. This is an early architectural guard, not Saxo's authority: "
                "Saxo's own child-distance limit is instrument-specific and "
                "undocumented (tighter than this band), so it can 400 an in-band child "
                "with TooFarFromEntryOrder "
                "(saxo_wide_stop_bracket_design_2026_07_20)."
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
        currency = str(row.get("CurrencyCode") or "").upper()
        if not currency:
            # Authoritative instrument currency comes ONLY from Saxo's own
            # instrument data — never MIC-inferred, never guessed (FX-leg
            # memo §4.3 item 4). A row without CurrencyCode is a refusal.
            raise InstrumentNotFoundError(
                f"Saxo search row for {expected_symbol!r} carries no CurrencyCode — "
                "refusing to resolve without an authoritative instrument currency "
                "(currency is never inferred from the MIC)"
            )
        ref = InstrumentRef(
            ticker=ticker,
            exchange_mic=exchange_mic,
            asset_type=str(row.get("AssetType", "Stock")),
            broker_instrument_id=str(row["Identifier"]),
            broker_symbol=str(row["Symbol"]),
            currency=currency,
        )
        self._cache_instrument(cache_key, ref)
        return ref

    def get_fx_rate(self, base: str, quote: str) -> FxRateQuote:
        """One FX spot snapshot for sizing (vendor CAPABILITY, not Protocol).

        Deliberately NOT a ``contract.Broker`` member — the frozen Protocol
        stays currency-naive; the CLI reaches this through the established
        ``getattr`` capability pattern and refuses cross-currency sizing for
        brokers without it. Resolution: ``/ref/v1/currencypairs`` from the
        BASE side (one-directional listing), FxSpot Keywords search fallback;
        then ONE ``/trade/v1/infoprices`` FxSpot read. The quote is reported
        VERBATIM (Mid + Bid/Ask + PriceTypes + MarketState + a local UTC
        fetch timestamp) — policy acceptance lives in
        ``execution.build_fx_conversion``, not in the adapter.
        """
        base = base.upper()
        quote = quote.upper()
        with _translate_saxo_errors():
            uic = self._resolve_fx_pair_uic(base, quote)
            payload = self._client.get_fx_infoprice(uic)
        quote_block: dict[str, Any] = payload.get("Quote") or {}
        market_state = quote_block.get("MarketState") or payload.get("MarketState")
        return FxRateQuote(
            base_currency=base,
            quote_currency=quote,
            mid=_opt_float(quote_block.get("Mid")),
            bid=_opt_float(quote_block.get("Bid")),
            ask=_opt_float(quote_block.get("Ask")),
            price_type_bid=_opt_str(quote_block.get("PriceTypeBid")),
            price_type_ask=_opt_str(quote_block.get("PriceTypeAsk")),
            market_state=_opt_str(market_state),
            source=f"saxo-fxspot-uic-{uic}-mid",
            asof=dt.datetime.now(dt.UTC),
        )

    def _resolve_fx_pair_uic(self, base: str, quote: str) -> str:
        """FX pair symbol (e.g. ``EURPLN``) -> Uic; refusal on any ambiguity.

        The currencypairs listing is one-directional — always looked up from
        the base side. A pair absent there falls back to an exact-symbol
        FxSpot Keywords search; anything else raises
        :class:`InstrumentNotFoundError` (never a guessed pair, never an
        inverted rate).
        """
        pair_symbol = f"{base}{quote}"
        listing = self._client.get_currency_pairs()
        for row in listing.get("Data") or []:
            symbol = str(row.get("CurrencyPair") or row.get("Symbol") or "").upper()
            row_uic = row.get("Uic", row.get("Identifier"))
            if symbol == pair_symbol and row_uic is not None:
                return str(row_uic)
        search = self._client.search_instruments(pair_symbol, asset_types="FxSpot")
        matches = [
            row
            for row in search.get("Data") or []
            if str(row.get("Symbol", "")).upper() == pair_symbol
            and row.get("Identifier") is not None
        ]
        if len(matches) == 1:
            return str(matches[0]["Identifier"])
        raise InstrumentNotFoundError(
            f"FX pair {base}->{quote} unresolvable: not listed under the base side in "
            f"/ref/v1/currencypairs and the FxSpot keyword search returned "
            f"{len(matches)} exact matches for {pair_symbol!r} — refusing to size "
            "(no fallback rate, no inverted lookup)"
        )

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
            self._precheck_or_raise(
                body,
                label=f"bracket {request.client_request_id} ({request.instrument.broker_symbol})",
            )
            status, payload = self._client.place_order(body, request_id=request.client_request_id)
            return self._handle_placement_response(
                status, payload, request.client_request_id, account_key
            )

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
            return self._precheck_or_raise(
                body,
                label=f"bracket {request.client_request_id} ({request.instrument.broker_symbol})",
            )

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        """Place ONE Option-B standalone StopIfTraded (no bracket parent).

        The disaster stop is NEVER a bracket child (MVP memo §Place): placed
        here AFTER the entry fills, sized to REALIZED filled qty (Risk 2). Same
        safety order as place_bracket_order: ALLOW_ORDERS gate first, then
        precheck, then ONE POST with x-request-id = client_request_id. No Orders
        array + no parent => relation=StandAlone, no TooFarFromEntryOrder
        (SIM-validated 2026-07-20, OrderId 5039296412). exit_order_ids is empty.

        ``request_id`` is the x-request-id / ExternalReference. Pass a DETERMINISTIC
        value (the auto-manager derives it from the entry client_request_id) so a
        crash-window re-POST hits Saxo's 15 s x-request-id dedup instead of placing
        a second live stop; omit it (None) to mint a fresh uuid4 per call.
        """
        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            raise BrokerCapabilityError(
                f"order placement is disabled: set {ALLOW_ORDERS_ENV}=1 to allow "
                "SIM order submission (design memo §P2 safety rail). No order was sent."
            )
        client_request_id = request_id or str(uuid.uuid4())
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_standalone_stop_body(
                uic, side, qty, stop_price, client_request_id, account_key
            )
            self._precheck_or_raise(body, label=f"standalone-stop Uic {uic} {client_request_id}")
            status, payload = self._client.place_order(body, request_id=client_request_id)
            return self._handle_placement_response(status, payload, client_request_id, account_key)

    def _build_standalone_stop_body(
        self,
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        client_request_id: str,
        account_key: str,
    ) -> dict[str, Any]:
        """Option-B standalone StopIfTraded body — no Orders array, no entry."""
        asset_type = "Stock"  # MVP scope: single-name equities only
        details = self._client.get_instrument_details(uic, asset_type)
        supported = details.get("SupportedOrderTypes") or []
        stop_type = execution_policy._STOP_ORDER_TYPE
        if supported and stop_type not in supported:
            raise OrderRejectedError(
                f"instrument Uic {uic} does not support {stop_type} orders "
                f"(SupportedOrderTypes={supported})"
            )
        stop_q = self._quantize_price(stop_price, details, label="stop_price")
        return {
            "Uic": int(uic),
            "AssetType": asset_type,
            "AccountKey": account_key,
            "Amount": qty,
            "BuySell": "Sell" if side == "SELL" else "Buy",
            "OrderType": stop_type,
            "OrderPrice": stop_q,
            "OrderDuration": {"DurationType": execution_policy._EXIT_DURATION},
            "ManualOrder": execution_policy._MANUAL_ORDER,
            "ExternalReference": client_request_id,
        }

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
        """Place ONE standalone OCO exit pair (rung-2 upgrade, saxo-oco memo §4.4).

        Two SELL legs — a near ``Limit`` take-profit MASTER + a far
        ``StopIfTraded`` disaster stop nested in the master's ``Orders`` array,
        both ``Amount == qty``, ``OrderRelation:"Oco"``, ``ManualOrder`` on both —
        under ONE POST. LIVE-VALIDATED real-POST shape (SIM 2026-07-22): the sell
        side commits ``qty`` ONCE (no ``SellOrdersAlreadyExistForOwnedContracts``)
        and the far stop escapes ``TooFarFromEntryOrder`` while OCO-linked. The
        pure siblings-array ``{AccountKey, Orders:[a,b]}`` body prechecks clean
        (Q1/Q2, PR #885) but the REAL POST rejects it — precheck is lenient on
        the ``ManualOrder``/master requirement, the live POST is strict.

        Same safety order as :meth:`place_standalone_stop`: the ALLOW_ORDERS gate
        FIRST (before any client call), then precheck, then ONE POST with
        x-request-id = ``request_id``. Deliberately does NOT run
        :func:`_validate_price_relations` — the 15% child-distance guard is an
        entry-child fail-fast, and the OCO exit is precisely the wide-stop escape
        (fact #3/#4); only a degenerate-ordering check remains
        (``_build_oco_exit_body`` rejects a stop that is not below the tp).

        ``request_id`` is a DETERMINISTIC base ref: it is the POST x-request-id
        (a same-size crash-retry hits Saxo's 15 s dedup) and the two per-leg
        ``ExternalReference`` values are ``<request_id>-stop`` / ``<request_id>-tp``.
        ``position_id`` is accepted but unused in Stage 2 (reduce-only is Stage 3).
        Returns ``PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))``.
        """
        _ = position_id  # Stage 3 (reduce-only linkage); accepted, unused here.
        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            raise BrokerCapabilityError(
                f"order placement is disabled: set {ALLOW_ORDERS_ENV}=1 to allow "
                "SIM order submission (design memo §P2 safety rail). No order was sent."
            )
        stop_ref = _oco_leg_ref(request_id, "stop")
        tp_ref = _oco_leg_ref(request_id, "tp")
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_oco_exit_body(
                uic, side, qty, stop_price, take_profit, account_key, stop_ref, tp_ref
            )
            self._precheck_or_raise(body, label=f"oco-exit Uic {uic} {request_id}")
            status, payload = self._client.place_order(body, request_id=request_id)
            return self._handle_oco_placement_response(
                status, payload, request_id, account_key, stop_ref, tp_ref
            )

    def _build_oco_exit_body(
        self,
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        take_profit: float,
        account_key: str,
        stop_ref: str,
        tp_ref: str,
    ) -> dict[str, Any]:
        """Siblings-array OCO exit body: two SELL legs, no top-level order.

        Both legs ``Amount == qty`` (Q3a makes owned-sized safe: a cash account
        cannot oversell), ``OrderRelation:"Oco"``, GoodTillCancel, ``ManualOrder``
        pinned from the execution policy. One ``Limit`` @ quantized take-profit,
        one ``StopIfTraded`` @ quantized stop. NO ``_validate_price_relations``
        (the wide-stop escape) — only a degenerate-ordering guard.
        """
        asset_type = "Stock"  # MVP scope: single-name equities only
        details = self._client.get_instrument_details(uic, asset_type)
        supported = details.get("SupportedOrderTypes") or []
        stop_type = execution_policy._STOP_ORDER_TYPE
        for required in ("Limit", stop_type):
            if supported and required not in supported:
                raise OrderRejectedError(
                    f"instrument Uic {uic} does not support {required} orders "
                    f"(SupportedOrderTypes={supported})"
                )
        stop_q = self._quantize_price(stop_price, details, label="stop_price")
        tp_q = self._quantize_price(take_profit, details, label="take_profit")
        # Degenerate-ordering guard ONLY (a long SELL OCO needs stop < tp). The
        # wide child-distance guard is deliberately skipped — the OCO exit is the
        # escape for a disaster stop 15-30% away (saxo-oco memo §4.4).
        if not stop_q < tp_q:
            raise OrderRejectedError(
                f"degenerate OCO exit Uic {uic}: stop {stop_q} must be strictly below "
                f"take_profit {tp_q} for a long SELL OCO pair"
            )
        buy_sell = "Sell" if side == "SELL" else "Buy"
        exit_duration = {"DurationType": execution_policy._EXIT_DURATION}
        manual_order = execution_policy._MANUAL_ORDER

        def _leg(order_type: str, price: float, ref: str) -> dict[str, Any]:
            return {
                "Uic": int(uic),
                "AssetType": asset_type,
                "AccountKey": account_key,
                "Amount": qty,
                "BuySell": buy_sell,
                "OrderType": order_type,
                "OrderPrice": price,
                "OrderDuration": dict(exit_duration),
                "ManualOrder": manual_order,
                "OrderRelation": "Oco",
                "ExternalReference": ref,
            }

        # Real-POST OCO shape (SIM-validated 2026-07-22): a TOP-LEVEL order (the
        # Limit take-profit master) with the StopIfTraded sibling nested in its
        # ``Orders`` array, ``ManualOrder`` + ``OrderRelation:"Oco"`` on BOTH. The
        # pure siblings-array ``{AccountKey, Orders:[a,b]}`` envelope PRECHECKS clean
        # but the REAL POST rejects it (HTTP 400 IllegalRequest — ManualOrder must be
        # on all orders / no master); precheck is lenient, the live POST is strict.
        # Both legs still ``Amount == qty`` -> the mutually-exclusive pair commits
        # owned ONCE (Q1). ``_leg`` stamps AccountKey + ManualOrder + OrderRelation.
        limit_leg = _leg("Limit", tp_q, tp_ref)  # master / take-profit
        stop_leg = _leg(stop_type, stop_q, stop_ref)  # OCO sibling / disaster stop
        limit_leg["Orders"] = [stop_leg]
        return limit_leg

    def _handle_oco_placement_response(
        self,
        status: int,
        payload: dict[str, Any],
        request_id: str,
        account_key: str,
        stop_ref: str,
        tp_ref: str,
    ) -> PlacedOrder:
        """Parse the top-level-master + nested-child OCO place response.

        The real-POST OCO body is a Limit take-profit MASTER (top-level OrderId)
        with the StopIfTraded sibling nested in ``Orders`` (SIM-validated), so the
        two ids are deterministic by structure: ``tp_id`` = the top-level OrderId,
        ``stop_id`` = the child OrderId. 200/201 with BOTH present ->
        ``PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))``. A 2xx
        missing either leg is a half-accepted OCO — cancel the stranded leg (sibling
        cascade cleans the rest) and raise, never return a lone leg. 202 and any
        reject raise, so the executor keeps the rung-1 stop live on the degrade path.
        ``stop_ref`` / ``tp_ref`` are unused for parsing now (structure is fixed) but
        kept on the signature for the caller's symmetry.
        """
        del stop_ref, tp_ref  # parsing is structural now, not ref-matched
        if status in (200, 201):
            master_id = payload.get("OrderId")  # the Limit / take-profit master
            children = [
                child
                for child in payload.get("Orders") or []
                if isinstance(child, dict) and child.get("OrderId")
            ]
            if not master_id or not children:
                live_order_id = self._find_order_id(payload)
                cleanup = (
                    self._repair_partial_acceptance(live_order_id, account_key)
                    if live_order_id
                    else "no accepted leg to clean up"
                )
                raise BrokerError(
                    f"Saxo OCO placement for {request_id} returned HTTP {status} without "
                    f"both master + child legs ({payload!r}); cleanup: {cleanup}"
                )
            tp_id = str(master_id)  # top-level master = the Limit take-profit leg
            stop_id = str(children[0]["OrderId"])  # nested child = the StopIfTraded leg
            return PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))
        if status == 202:
            live_order_id = self._find_order_id(payload)
            raise BrokerError(
                f"Saxo 202 TradeNotCompleted for OCO {request_id}: leg {live_order_id} "
                "may be LIVE while its OCO sibling was CANCELLED by Saxo (naked-exit "
                "hazard). No automatic action taken — order state is genuinely unknown "
                "for ~seconds. Reconcile via 'alphalens broker orders'."
            )
        detail = self._rejection_detail(payload)
        live_order_id = self._find_order_id(payload)
        if live_order_id:
            cleanup = self._repair_partial_acceptance(live_order_id, account_key)
            raise OrderRejectedError(
                f"Saxo rejected OCO {request_id} with HTTP {status} "
                f"AFTER accepting leg {live_order_id} ({detail}); cleanup: {cleanup}"
            )
        raise OrderRejectedError(f"Saxo rejected OCO {request_id} with HTTP {status}: {detail}")

    def get_order(self, order_id: str) -> OrderState:
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            payload = self._client.get_order_status(client_key, order_id)
        # LIVE-CALIBRATED 2026-07-17: the single-order endpoint answers with a
        # COLLECTION envelope ({"__count": N, "Data": [entry + its related
        # children]}), the entry not necessarily first — select the row whose
        # OrderId matches. A flat dict (older shape) still passes through.
        if payload and "Data" in payload:
            rows = payload.get("Data") or []
            payload = next((row for row in rows if str(row.get("OrderId")) == str(order_id)), None)
        if not payload or not payload.get("OrderId"):
            # The open-orders endpoint drops filled/cancelled/expired orders:
            # absent is honestly UNKNOWN. Terminal resolution (FILLED vs
            # CANCELLED vs REJECTED vs EXPIRED) is the P3 audit-log
            # capability — see :meth:`resolve_order_outcome`.
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

    # ----- broker-state-truth protection reads (saxo-oco memo §4.1) -----
    #
    # Thin, mapping-only filters over the already-fetched read payloads (no new
    # HTTP surface). The per-tick protection pass derives status purely from
    # these — never from a journal line.

    def get_long_positions(self) -> list[Position]:
        """Strictly-long positions, ONE per uic (netted qty > _QTY_EPS); flat and
        short dropped.

        Saxo can return a single position as several same-uic lots (seen live on
        SIM: two separate Market buys stayed as distinct lots). The protection
        view keys by uic, so the lots MUST be summed here — otherwise they
        overwrite each other and the stop is sized to one lot, leaving the rest
        of the position naked. Mirrors the per-uic summing in
        ``get_positions_by_uic``. The netted Position keeps the first lot's
        non-quantity fields (``avg_price`` is NOT a weighted average) — only
        ``quantity`` drives protection sizing, same as ``get_positions_by_uic``.
        Positions whose uic cannot be parsed are passed through individually
        (they cannot be keyed, and the protection view skips them anyway).
        """
        by_uic: dict[int, Position] = {}
        no_uic: list[Position] = []
        for pos in self.get_positions():
            uic = _position_uic(pos)
            if uic is None:
                no_uic.append(pos)
                continue
            existing = by_uic.get(uic)
            by_uic[uic] = (
                pos
                if existing is None
                else cast(
                    Position,
                    dataclasses.replace(existing, quantity=existing.quantity + pos.quantity),
                )
            )
        return [p for p in (*by_uic.values(), *no_uic) if p.quantity > _QTY_EPS]

    def list_working_sell_orders(self) -> list[OrderState]:
        """Live SELL legs still committing owned qty (WORKING / PARTIALLY_FILLED)."""
        return [
            o
            for o in self.list_open_orders()
            if o.side == "SELL" and o.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)
        ]

    def get_positions_by_uic(self, uic: int) -> Position:
        """Netted Position for one uic (summed signed qty across lots).

        The execute-time owned re-check reads this immediately before placing a
        protective stop so it never oversells or plants a stop on a uic that
        has already gone flat. When the uic carries no live lot it returns a
        zero-qty sentinel Position (never ``None``) so callers branch on the
        quantity, not on presence.
        """
        matching = [p for p in self.get_positions() if _position_uic(p) == uic]
        if not matching:
            return Position(
                instrument=InstrumentRef(
                    ticker="",
                    exchange_mic="",
                    asset_type="Stock",
                    broker_instrument_id=str(uic),
                    broker_symbol="",
                ),
                quantity=0.0,
                avg_price=0.0,
                market_value=None,
                unrealized_pnl=None,
                position_id="",
            )
        netted_qty = sum(p.quantity for p in matching)
        # dataclasses.replace is typed to return the generic DataclassInstance, so
        # narrow it back to Position for the annotated return (S5886).
        return cast(Position, dataclasses.replace(matching[0], quantity=netted_qty))

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

    # ----- P3 terminal resolution + fill cross-check (vendor CAPABILITY) -----
    #
    # Deliberately NOT part of the frozen ``contract.Broker`` Protocol: only
    # Saxo can honor audit-log resolution today. The vendor-agnostic
    # reconciler reaches these through the ``SupportsOrderResolution`` /
    # ``SupportsFillCrossCheck`` extension Protocols in
    # ``brokers/reconcile.py`` (typed variant of the existing
    # ``getattr(broker, "precheck_bracket_order", None)`` CLI precedent);
    # brokers lacking them degrade to UNRESOLVED(capability_absent) — never
    # a guessed terminal state.

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        """Classify a DISAPPEARED order's terminal outcome from the audit log.

        One GET ``/cs/v1/audit/orderactivities?OrderId=...&EntryType=Last``
        (audit store, 2+yr documented retention; ENS is NOT used — hard
        14-day cap). Classification is on the (Status, SubStatus) PAIR —
        live findings prove Status alone is insufficient (rejected
        placements surface as ``Placed``/``Rejected`` in a single row).

        Unresolvable cases return ``OrderStatus.UNKNOWN`` with a reason code
        in ``raw_status`` (``not_in_retention`` / ``fill_fields_unverified``
        / ``inconsistent_state`` / ``unrecognized``) — the mapping never
        guesses; the reconciler surfaces these as UNRESOLVED verdicts.
        """
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            payload = self._client.get_order_activities(
                client_key, order_id=order_id, entry_type="Last"
            )
        rows = [
            row
            for row in payload.get("Data") or []
            if str(row.get("OrderId", "")) in ("", str(order_id))
        ]
        if not rows:
            # Retention exceeded, or an id Saxo never knew.
            return self._unresolved_state(order_id, "not_in_retention")
        row = max(rows, key=lambda r: int(r.get("LogId") or 0))
        return self._classify_activity_row(order_id, row)

    _NON_TERMINAL_PAIRS = frozenset(
        {
            ("Placed", "Confirmed"),
            ("Placed", "Requested"),
            ("Fill", "Confirmed"),
            ("Fill", "Requested"),
            ("Cancelled", "Requested"),
        }
    )

    @staticmethod
    def _unresolved_state(order_id: str, raw_status: str) -> OrderState:
        return OrderState(
            order_id=order_id,
            status=OrderStatus.UNKNOWN,
            instrument=None,
            filled_quantity=0.0,
            raw_status=raw_status,
        )

    def _classify_activity_row(self, order_id: str, row: dict[str, Any]) -> OrderState:
        """(Status, SubStatus) PAIR -> terminal OrderStatus; UNKNOWN+reason otherwise."""
        status = str(row.get("Status", ""))
        sub_status = str(row.get("SubStatus", ""))
        pair = f"{status}/{sub_status}"
        diagnostics = self._activity_diagnostics(pair, row)

        # (1) SubStatus="Rejected" wins regardless of Status — live-verified:
        # rejected placements are a single Placed/Rejected audit row.
        if sub_status == "Rejected":
            return self._terminal_state(order_id, OrderStatus.REJECTED, diagnostics)
        # (2) FinalFill -> FILLED. The fill-field handling here was originally
        # DOC-SOURCED (zero fills existed on the live account at design time).
        # It is now verified against a REAL SIM FinalFill row captured
        # 2026-07-20 (first-fill experiment: FillAmount/FilledAmount==2.0,
        # ExecutionPrice/AveragePrice==82.09, ExternalReference==client_request_id) —
        # see tests/brokers/test_saxo_broker.py::TestFinalFillRealFixture, whose
        # fixture is byte-shaped from that row. Still assert presence rather
        # than fabricating 0 or full qty so a malformed row surfaces honestly.
        if status == "FinalFill":
            filled = row.get("FilledAmount", row.get("FillAmount"))
            try:
                filled_quantity = float(filled)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return self._unresolved_state(
                    order_id, f"fill_fields_unverified ({pair} row={row!r})"
                )
            return self._terminal_state(order_id, OrderStatus.FILLED, diagnostics, filled_quantity)
        # (3) Cancelled/Confirmed — live-verified terminal cancel row.
        if status == "Cancelled" and sub_status == "Confirmed":
            return self._terminal_state(order_id, OrderStatus.CANCELLED, diagnostics)
        # (4) Expired — doc-sourced (not live-producible on the SIM account).
        if status == "Expired":
            return self._terminal_state(order_id, OrderStatus.EXPIRED, diagnostics)
        # (5) A recognized NON-terminal last row for an order absent from the
        # open-orders view is an inconsistent state — surface, never guess.
        if (status, sub_status) in self._NON_TERMINAL_PAIRS:
            return self._unresolved_state(order_id, f"inconsistent_state ({diagnostics})")
        return self._unresolved_state(order_id, f"unrecognized ({diagnostics})")

    @staticmethod
    def _activity_diagnostics(pair: str, row: dict[str, Any]) -> str:
        """Raw ``Status/SubStatus`` pair plus LogId/ActivityTime for diagnostics."""
        parts = [pair]
        if row.get("LogId") is not None:
            parts.append(f"LogId={row['LogId']}")
        if row.get("ActivityTime"):
            parts.append(f"ActivityTime={row['ActivityTime']}")
        return " ".join(parts)

    def _terminal_state(
        self,
        order_id: str,
        status: OrderStatus,
        diagnostics: str,
        filled_quantity: float = 0.0,
    ) -> OrderState:
        return OrderState(
            order_id=order_id,
            status=status,
            instrument=None,
            filled_quantity=filled_quantity,
            raw_status=diagnostics,
        )

    def get_open_position_references(self) -> list[str]:
        """``ExternalReference`` values of OPEN positions (fill cross-check).

        The journal's ``client_request_id`` round-trips verbatim as
        ``ExternalReference`` on every audit row and position — a FILLED
        entry whose exit is still working shows up here.
        """
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            rows: list[dict[str, Any]] = self._client.get_positions(client_key).get("Data") or []
        references: list[str] = []
        for row in rows:
            base: dict[str, Any] = row.get("PositionBase") or {}
            reference = base.get("ExternalReference") or row.get("ExternalReference")
            if reference:
                references.append(str(reference))
        return references

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        """Flattened ``/port/v1/closedpositions`` rows (round-trip cross-check).

        Accepts BOTH live body shapes via the client wrapper; each returned
        row is the inner ``ClosedPosition`` dict when the envelope form is
        present, the raw row otherwise.
        """
        with _translate_saxo_errors():
            client_key = str(self._client.get_client_info()["ClientKey"])
            payload = self._client.get_closed_positions(client_key)
        rows: list[dict[str, Any]] = []
        for row in payload.get("Data") or []:
            inner = row.get("ClosedPosition") if isinstance(row, dict) else None
            rows.append(inner if isinstance(inner, dict) else row)
        return rows

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
                # Best-effort display stamp from DisplayAndFormat; "" (not a
                # guess) when the position row carries no Currency. Sizing
                # NEVER consumes this path — it goes through resolve_instrument,
                # which refuses instead of stamping "".
                currency=str(display.get("Currency") or "").upper(),
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

    def _precheck_or_raise(self, body: dict[str, Any], *, label: str) -> dict[str, Any]:
        """POST /trade/v2/orders/precheck; non-Ok blocks the real POST."""
        precheck_body = dict(body)
        precheck_body["FieldGroups"] = ["Costs"]
        status, payload = self._client.precheck_order(precheck_body)
        error_info = payload.get("ErrorInfo")
        result = payload.get("PreCheckResult")
        if status >= 400 or error_info or (result is not None and result != "Ok"):
            # Attach the verbatim Saxo ErrorCode so safety branches classify on
            # a STRUCTURED code (memo §4.2), never by parsing the message.
            error_code = error_info.get("ErrorCode") if isinstance(error_info, dict) else None
            raise OrderRejectedError(
                f"precheck rejected {label}: status={status} "
                f"PreCheckResult={result!r} ErrorInfo={error_info!r} — nothing was placed",
                error_code=error_code,
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
        request_id: str,
        account_key: str,
    ) -> PlacedOrder:
        if status in (200, 201):
            order_id = payload.get("OrderId")
            if not order_id:
                raise BrokerError(
                    f"Saxo placement returned HTTP {status} without an OrderId "
                    f"for {request_id}: {payload!r}"
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
                f"Saxo 202 TradeNotCompleted for {request_id}: "
                f"entry order {order_id} is likely LIVE while its related exits were "
                "CANCELLED by Saxo (naked-entry hazard). No automatic action taken — "
                "order state is genuinely unknown for ~seconds. Reconcile: poll "
                "'alphalens broker orders', then 'alphalens broker cancel "
                f"{order_id}' if the entry is unwanted."
            )
        detail = self._rejection_detail(payload)
        live_order_id = self._find_order_id(payload)
        if live_order_id:
            cleanup = self._repair_partial_acceptance(live_order_id, account_key)
            raise OrderRejectedError(
                f"Saxo rejected {request_id} with HTTP {status} "
                f"AFTER accepting order {live_order_id} ({detail}); cleanup: {cleanup}"
            )
        raise OrderRejectedError(f"Saxo rejected {request_id} with HTTP {status}: {detail}")

    def _repair_partial_acceptance(self, order_id: str, account_key: str) -> str:
        try:
            status, payload = self._client.cancel_order_ids(order_id, account_key=account_key)
        except SaxoError as exc:
            logger.exception("partial-acceptance cleanup DELETE failed for %s: %s", order_id, exc)
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
        uic_raw = row.get("Uic")
        return OrderState(
            order_id=str(row.get("OrderId", "")),
            status=OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.WORKING,
            instrument=self._instrument_from_uic(uic_raw),
            filled_quantity=filled,
            raw_status=str(row.get("Status", "")),
            # Per-uic accounting fields (memo §4.1) — mapping-only, no new HTTP.
            uic=int(uic_raw) if uic_raw is not None else None,
            side=_order_side(row.get("BuySell")),
            order_type=_opt_str(row.get("OpenOrderType")),
            amount=_opt_float(row.get("Amount")),
            external_reference=_opt_str(row.get("ExternalReference")),
            order_relation=_opt_str(row.get("OrderRelation")),
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
