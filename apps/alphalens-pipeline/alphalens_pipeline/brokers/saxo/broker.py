"""SaxoBroker — adapts the canonical SaxoClient to the ``contract.Broker`` Protocol.

Translation duties (and nothing else — transport lives in ``client.py``):

- Saxo payloads -> the frozen contract dataclasses;
- ``Saxo*Error`` -> ``Broker*Error`` at the boundary (vendor exceptions never
  escape ``brokers/saxo/``);
- MIC -> Saxo ExchangeId mapping + exact-symbol instrument resolution with a
  FIFO-bounded in-process cache (sec_edgar_client cache pattern; no disk
  cache in P1);
- P2-gated methods (placement / order status / cancel) raise
  :class:`BrokerCapabilityError` — the contract SIGNATURES are frozen, the
  implementations land with ADR 0014 P2.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerAuthError,
    BrokerCapabilityError,
    BrokerError,
    BrokerRateLimitError,
    InstrumentNotFoundError,
    InstrumentRef,
    OrderState,
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

# Env var selecting one AccountKey when the Saxo profile has several accounts.
# With a single account the adapter auto-picks; with several and no selector
# it fails loudly rather than guessing.
ACCOUNT_KEY_ENV = "SAXO_ACCOUNT_KEY"

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

_P2_CAPABILITY_MSG = "order placement lands in P2 (ADR 0014); P1 SaxoBroker is reads-only on SIM"

_DEFAULT_INSTRUMENT_CACHE_SIZE = 256


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

    # ----- P2-gated methods (signatures frozen; implementations land in P2) -----

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        raise BrokerCapabilityError(_P2_CAPABILITY_MSG)

    def get_order(self, order_id: str) -> OrderState:
        raise BrokerCapabilityError(_P2_CAPABILITY_MSG)

    def list_open_orders(self) -> list[OrderState]:
        raise BrokerCapabilityError(_P2_CAPABILITY_MSG)

    def cancel_order(self, order_id: str) -> None:
        raise BrokerCapabilityError(_P2_CAPABILITY_MSG)

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
    "SaxoBroker",
    "create_saxo_broker_from_env",
]
