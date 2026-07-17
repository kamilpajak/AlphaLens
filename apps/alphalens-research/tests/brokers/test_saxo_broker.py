"""Hermetic tests for the SaxoBroker contract adapter (``brokers/saxo/broker.py``).

The adapter is tested against a stub SaxoClient carrying canned Saxo OpenAPI
payload fixtures (portfolio ``/port/v1/*`` + reference ``/ref/v1/instruments``
shapes). Pins:

- payload -> ``AccountSnapshot`` / ``Position`` / ``InstrumentRef`` translation
- resolve: exact-symbol match, zero-match and ambiguity ->
  ``InstrumentNotFoundError``, unmapped MIC -> ``InstrumentNotFoundError``
- FIFO instrument cache: a second resolve makes no HTTP call
- ``Saxo*Error`` -> ``Broker*Error`` translation at the adapter boundary
  (vendor exceptions never escape ``brokers/saxo/``)
- multi-account without ``SAXO_ACCOUNT_KEY`` fails loudly

The P2 order surface has its own suite (``test_saxo_broker_orders.py``); the
stub here carries just enough stateful order book for the shared
conformance-mixin lifecycle pin.

Also runs the shared :class:`BrokerConformanceMixin` against SaxoBroker so the
real adapter proves the same behavioral contract as any future broker.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    Broker,
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitError,
    InstrumentNotFoundError,
    InstrumentRef,
    Position,
)
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import (
    SaxoAuthError,
    SaxoError,
    SaxoRateLimitError,
)

from tests.brokers.test_broker_contract import BrokerConformanceMixin

_CLIENT_INFO = {"ClientKey": "CK-1", "DefaultAccountKey": "AK-1"}

_ACCOUNTS_SINGLE = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}

_ACCOUNTS_MULTI = {
    "Data": [
        {"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"},
        {"AccountKey": "AK-2", "AccountId": "16371ABC", "Currency": "PLN"},
    ]
}

_BALANCES = {
    "Currency": "USD",
    "CashBalance": 10_000.0,
    "TotalValue": 12_500.5,
    "MarginAvailableForTrading": 9_000.0,
}

_POSITIONS = {
    "Data": [
        {
            "PositionId": "5001",
            "PositionBase": {
                "Amount": 10.0,
                "OpenPrice": 50.0,
                "Uic": 211,
                "AssetType": "Stock",
            },
            "PositionView": {"MarketValue": 510.0, "ProfitLossOnTrade": 10.0},
            "DisplayAndFormat": {"Symbol": "KO:xnys", "Description": "Coca-Cola Co."},
        },
        {
            # SIM NoAccess quote: no PositionView block at all — market_value
            # and unrealized_pnl must map to None, not crash.
            "PositionId": "5002",
            "PositionBase": {
                "Amount": -5.0,
                "OpenPrice": 100.0,
                "Uic": 212,
                "AssetType": "Stock",
            },
            "DisplayAndFormat": {"Symbol": "AAPL:xnas"},
        },
    ]
}

_SEARCH_RESULTS: dict[str, dict[str, Any]] = {
    "KO": {
        "Data": [
            {"Symbol": "KO:xnys", "Identifier": 211, "AssetType": "Stock"},
            {"Symbol": "KOF:xnys", "Identifier": 999, "AssetType": "Stock"},
        ]
    },
    "AAPL": {"Data": [{"Symbol": "AAPL:xnas", "Identifier": 212, "AssetType": "Stock"}]},
    "AMBIG": {
        "Data": [
            {"Symbol": "AMBIG:xnys", "Identifier": 1, "AssetType": "Stock"},
            {"Symbol": "AMBIG:xnys", "Identifier": 2, "AssetType": "Stock"},
        ]
    },
}


class _StubSaxoClient:
    """Duck-typed stand-in for SaxoClient (the adapter only calls these five)."""

    def __init__(
        self,
        *,
        accounts: dict[str, Any] | None = None,
        fail_with: Exception | None = None,
    ):
        self.accounts = accounts or _ACCOUNTS_SINGLE
        self.fail_with = fail_with
        self.search_calls: list[tuple[str, str | None]] = []

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get_client_info(self) -> dict[str, Any]:
        self._maybe_fail()
        return dict(_CLIENT_INFO)

    def get_accounts(self) -> dict[str, Any]:
        self._maybe_fail()
        return self.accounts

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        self._maybe_fail()
        assert client_key == "CK-1"
        return dict(_BALANCES)

    def get_positions(self, client_key: str) -> dict[str, Any]:
        self._maybe_fail()
        return _POSITIONS

    def search_instruments(
        self,
        keywords: str,
        *,
        asset_types: str = "Stock",
        exchange_id: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        self.search_calls.append((keywords, exchange_id))
        return _SEARCH_RESULTS.get(keywords.upper(), {"Data": []})

    # ----- minimal stateful order surface (conformance-mixin lifecycle) -----

    _DETAILS = {
        "Format": {"Decimals": 2, "OrderDecimals": 2},
        "TickSizeScheme": {"DefaultTickSize": 0.01, "Elements": []},
        "SupportedOrderTypes": ["Limit", "Market", "StopIfTraded"],
    }

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        self._maybe_fail()
        return dict(self._DETAILS)

    def precheck_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        return 200, {"PreCheckResult": "Ok"}

    def place_order(self, body: dict[str, Any], *, request_id: str) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        self._order_seq = getattr(self, "_order_seq", 0) + 1
        entry_id = f"E-{self._order_seq}"
        children = [{"OrderId": f"{entry_id}-x{i}"} for i in range(len(body.get("Orders") or []))]
        self._orders = getattr(self, "_orders", {})
        self._orders[entry_id] = {
            "OrderId": entry_id,
            "Status": "Working",
            "Amount": body["Amount"],
            "FillAmount": 0.0,
            "Uic": body["Uic"],
        }
        return 201, {"OrderId": entry_id, "Orders": children}

    def cancel_order_ids(self, order_ids: str, *, account_key: str) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        self._orders = getattr(self, "_orders", {})
        self._orders.pop(order_ids, None)
        return 200, {}

    def get_open_orders(self) -> dict[str, Any]:
        self._maybe_fail()
        return {"Data": list(getattr(self, "_orders", {}).values())}

    def get_order_status(self, client_key: str, order_id: str) -> dict[str, Any] | None:
        self._maybe_fail()
        return getattr(self, "_orders", {}).get(order_id)


def _make_broker(**kw: Any) -> SaxoBroker:
    return SaxoBroker(_StubSaxoClient(), **kw)  # type: ignore[arg-type]


class TestAccountSnapshotTranslation(unittest.TestCase):
    def test_single_account_maps_balances(self):
        snapshot = _make_broker().get_account()

        self.assertIsInstance(snapshot, AccountSnapshot)
        self.assertEqual(snapshot.account_id, "AK-1")
        self.assertEqual(snapshot.currency, "USD")
        self.assertEqual(snapshot.cash, 10_000.0)
        self.assertEqual(snapshot.total_value, 12_500.5)
        self.assertEqual(snapshot.margin_available, 9_000.0)
        self.assertIsNotNone(snapshot.asof.tzinfo)

    def test_multi_account_without_selector_fails_loudly(self):
        broker = SaxoBroker(_StubSaxoClient(accounts=_ACCOUNTS_MULTI))  # type: ignore[arg-type]
        with self.assertRaises(BrokerError) as ctx:
            broker.get_account()
        self.assertIn("SAXO_ACCOUNT_KEY", str(ctx.exception))

    def test_multi_account_with_selector_picks_the_key(self):
        broker = SaxoBroker(
            _StubSaxoClient(accounts=_ACCOUNTS_MULTI),  # type: ignore[arg-type]
            account_key="AK-2",
        )
        self.assertEqual(broker.get_account().account_id, "AK-2")

    def test_selector_not_in_accounts_fails_loudly(self):
        broker = SaxoBroker(_StubSaxoClient(), account_key="AK-MISSING")  # type: ignore[arg-type]
        with self.assertRaises(BrokerError):
            broker.get_account()


class TestPositionsTranslation(unittest.TestCase):
    def test_positions_map_to_contract_dataclasses(self):
        positions = _make_broker().get_positions()

        self.assertEqual(len(positions), 2)
        first, second = positions
        self.assertIsInstance(first, Position)
        self.assertEqual(first.position_id, "5001")
        self.assertEqual(first.quantity, 10.0)
        self.assertEqual(first.avg_price, 50.0)
        self.assertEqual(first.market_value, 510.0)
        self.assertEqual(first.unrealized_pnl, 10.0)
        self.assertEqual(first.instrument.ticker, "KO")
        self.assertEqual(first.instrument.exchange_mic, "XNYS")
        self.assertEqual(first.instrument.broker_instrument_id, "211")
        self.assertEqual(first.instrument.broker_symbol, "KO:xnys")

        # SIM NoAccess quote (no PositionView) -> None, and short qty stays signed.
        self.assertEqual(second.quantity, -5.0)
        self.assertIsNone(second.market_value)
        self.assertIsNone(second.unrealized_pnl)


class TestInstrumentResolution(unittest.TestCase):
    def test_exact_symbol_match_wins_over_prefix_sibling(self):
        broker = _make_broker()
        ref = broker.resolve_instrument("KO", "XNYS")

        self.assertIsInstance(ref, InstrumentRef)
        self.assertEqual(ref.ticker, "KO")
        self.assertEqual(ref.exchange_mic, "XNYS")
        self.assertEqual(ref.broker_instrument_id, "211")
        self.assertEqual(ref.broker_symbol, "KO:xnys")
        self.assertEqual(ref.asset_type, "Stock")

    def test_search_passes_saxo_exchange_id_for_mic(self):
        client = _StubSaxoClient()
        SaxoBroker(client).resolve_instrument("AAPL", "XNAS")  # type: ignore[arg-type]
        self.assertEqual(client.search_calls, [("AAPL", "NASDAQ")])

    def test_zero_match_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError):
            _make_broker().resolve_instrument("NOPE", "XNYS")

    def test_ambiguous_match_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            _make_broker().resolve_instrument("AMBIG", "XNYS")
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_unmapped_mic_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            _make_broker().resolve_instrument("KO", "XTKS")
        self.assertIn("XTKS", str(ctx.exception))

    def test_cache_hit_skips_http(self):
        client = _StubSaxoClient()
        broker = SaxoBroker(client)  # type: ignore[arg-type]

        first = broker.resolve_instrument("KO", "XNYS")
        second = broker.resolve_instrument("KO", "XNYS")

        self.assertEqual(first, second)
        self.assertEqual(len(client.search_calls), 1, "second resolve must hit the cache")

    def test_cache_is_fifo_bounded(self):
        client = _StubSaxoClient()
        broker = SaxoBroker(client, cache_size=1)  # type: ignore[arg-type]

        broker.resolve_instrument("KO", "XNYS")
        broker.resolve_instrument("AAPL", "XNAS")  # evicts KO (FIFO, size 1)
        broker.resolve_instrument("KO", "XNYS")

        self.assertEqual(len(client.search_calls), 3, "evicted entry must re-fetch")


class TestErrorTranslationBoundary(unittest.TestCase):
    def test_saxo_errors_translate_to_broker_errors(self):
        cases = [
            (SaxoAuthError("401"), BrokerAuthError),
            (SaxoRateLimitError("429"), BrokerRateLimitError),
            (SaxoError("boom"), BrokerError),
        ]
        for saxo_exc, broker_exc_type in cases:
            with self.subTest(saxo=type(saxo_exc).__name__):
                broker = SaxoBroker(_StubSaxoClient(fail_with=saxo_exc))  # type: ignore[arg-type]
                with self.assertRaises(broker_exc_type) as ctx:
                    broker.get_account()
                # The vendor exception must never escape raw.
                self.assertNotIsInstance(ctx.exception, SaxoError)

    def test_translation_covers_positions_and_resolve(self):
        broker = SaxoBroker(_StubSaxoClient(fail_with=SaxoAuthError("401")))  # type: ignore[arg-type]
        with self.assertRaises(BrokerAuthError):
            broker.get_positions()
        with self.assertRaises(BrokerAuthError):
            broker.resolve_instrument("KO", "XNYS")


class TestSaxoBrokerConformance(BrokerConformanceMixin, unittest.TestCase):
    unknown_ticker = "NOPE"

    def make_broker(self) -> Broker:
        return _make_broker()


if __name__ == "__main__":
    unittest.main()
