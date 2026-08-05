"""Hermetic tests for the live-market market-order adapter (INC-1).

Market BUY (entry tranche) / market SELL (exit tranche) mirror the existing
standalone-stop flow: canonical-side check -> ALLOW_ORDERS gate -> precheck ->
single POST -> _handle_placement_response. No caller is wired yet (inert
until INC-3/4); this file pins the adapter surface in isolation.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker
from broker_contract.contract import BrokerCapabilityError, OrderRejectedError, PlacedOrder


class TestSupportsMarketOrdersProtocol(unittest.TestCase):
    def test_supports_market_orders_is_runtime_checkable_protocol(self):
        from broker_contract.contract import SupportsMarketOrders

        # A trivial object with the method structurally satisfies the Protocol.
        class _M:
            def place_market_order(self, uic, side, qty, request_id=None):
                return None

        self.assertIsInstance(_M(), SupportsMarketOrders)

        class _N:  # missing the method
            pass

        self.assertNotIsInstance(_N(), SupportsMarketOrders)


_ALLOW = {ALLOW_ORDERS_ENV: "1"}
_ACCOUNTS = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}
_DETAILS_KO = {
    "Uic": 307,
    "AssetType": "Stock",
    "Format": {"Decimals": 2, "OrderDecimals": 2},
    "TickSizeScheme": {
        "DefaultTickSize": 0.01,
        "Elements": [{"HighPrice": 0.9999, "TickSize": 0.0001}],
    },
    "SupportedOrderTypes": ["Limit", "Market", "Stop", "StopIfTraded", "StopLimit"],
}


class _StubMarketClient:
    def __init__(
        self,
        *,
        details=None,
        precheck_response=(200, {"PreCheckResult": "Ok"}),
        place_response=(200, {"OrderId": "M-900"}),
    ):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []

    def get_accounts(self):
        return _ACCOUNTS

    def get_instrument_details(self, uic, asset_type="Stock"):
        return dict(self.details)

    def precheck_order(self, body):
        self.precheck_calls.append(body)
        return self.precheck_response

    def place_order(self, body, *, request_id):
        self.place_calls.append((body, request_id))
        return self.place_response


def _make(stub):
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


class TestMarketOrderBody(unittest.TestCase):
    def test_body_is_market_no_price_dayorder_buy(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_market_order(uic=307, side="BUY", qty=2)
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body)
        self.assertNotIn("OrderPrice", body, "a market order carries NO price")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "Market")
        self.assertEqual(body["BuySell"], "Buy")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["OrderDuration"], {"DurationType": "DayOrder"})
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "M-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_sell_side_mirrors(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="SELL", qty=3)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["BuySell"], "Sell")
        self.assertEqual(body["Amount"], 3)

    def test_explicit_request_id_reused_as_external_reference(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="SELL", qty=2, request_id="rid-tp1")
        body, request_id = stub.place_calls[0]
        self.assertEqual(request_id, "rid-tp1")
        self.assertEqual(body["ExternalReference"], "rid-tp1")

    def test_request_id_defaults_to_uuid(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="BUY", qty=2)
        _, request_id = stub.place_calls[0]
        uuid.UUID(request_id)


class TestMarketOrderSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubMarketClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env, clear=True):
                with self.assertRaises(BrokerCapabilityError) as ctx:
                    broker.place_market_order(uic=307, side="BUY", qty=2)
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_saxo_form_side_rejected_before_any_http(self):
        broker, stub = _make(_StubMarketClient())
        for bad in ("Sell", "Buy", "sell", "buy", ""):
            with self.subTest(side=bad), mock.patch.dict("os.environ", _ALLOW):
                with self.assertRaises(ValueError) as ctx:
                    broker.place_market_order(uic=307, side=bad, qty=2)
                self.assertIn("'BUY'", str(ctx.exception))
                self.assertIn("'SELL'", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "invalid side must never POST")

    def test_unsupported_market_type_rejected_pre_post(self):
        no_mkt = dict(_DETAILS_KO)
        no_mkt["SupportedOrderTypes"] = ["Limit", "StopIfTraded"]
        broker, stub = _make(_StubMarketClient(details=no_mkt))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_market_order(uic=307, side="BUY", qty=2)
        self.assertIn("Market", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")

    def test_failed_precheck_blocks_post(self):
        stub = _StubMarketClient(
            precheck_response=(
                200,
                {
                    "PreCheckResult": "Error",
                    "ErrorInfo": {"ErrorCode": "OrderValueToSmall", "Message": "too small"},
                },
            )
        )
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_market_order(uic=307, side="BUY", qty=2)
        self.assertEqual(stub.place_calls, [], "failed precheck must block the POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
