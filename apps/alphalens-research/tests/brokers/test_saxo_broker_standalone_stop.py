"""Hermetic tests for SaxoBroker.place_standalone_stop (Option-B standalone stop).

The disaster stop is NEVER a bracket child — placed as its own POST with NO
Orders array and no parent (relation=StandAlone; SIM-validated 2026-07-20: KO
qty 2 Sell @61.36, OrderId 5039296412). Pins body shape, ALLOW_ORDERS gate,
precheck-before-POST, realized (float) qty, empty-exits PlacedOrder.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    BrokerCapabilityError,
    OrderRejectedError,
    PlacedOrder,
)
from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker

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


class _StubStopClient:
    """Minimal stub SaxoClient for the standalone-stop surface (records calls)."""

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        precheck_response: tuple[int, dict[str, Any]] = (200, {"PreCheckResult": "Ok"}),
        place_response: tuple[int, dict[str, Any]] = (200, {"OrderId": "S-900"}),
    ):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []

    def get_accounts(self) -> dict[str, Any]:
        return _ACCOUNTS

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        return dict(self.details)

    def precheck_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.precheck_calls.append(body)
        return self.precheck_response

    def place_order(self, body: dict[str, Any], *, request_id: str) -> tuple[int, dict[str, Any]]:
        self.place_calls.append((body, request_id))
        return self.place_response


def _make(stub: _StubStopClient) -> tuple[SaxoBroker, _StubStopClient]:
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


class TestStandaloneStopBody(unittest.TestCase):
    def test_body_has_no_orders_array_and_is_stopiftraded(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body, "a standalone stop carries NO Orders array (no parent)")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "StopIfTraded")
        self.assertEqual(body["BuySell"], "Sell")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["OrderPrice"], 61.36)
        self.assertEqual(body["OrderDuration"], {"DurationType": "GoodTillCancel"})
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "S-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_buy_side_stop_mirrors(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="BUY", qty=2, stop_price=61.36)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["BuySell"], "Buy")

    def test_amount_is_realized_qty_float_not_planned(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="SELL", qty=2.0, stop_price=61.36)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["Amount"], 2.0)

    def test_request_id_is_uuid_and_reused_as_external_reference(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        body, request_id = stub.place_calls[0]
        uuid.UUID(request_id)
        self.assertEqual(body["ExternalReference"], request_id)

    def test_explicit_request_id_is_used_as_x_request_id_and_external_reference(self):
        # HIGH-1: a deterministic caller-supplied request_id lets a crash-window
        # re-POST hit Saxo's 15 s x-request-id dedup instead of minting a fresh
        # uuid and placing a SECOND live stop.
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(
                uic=307, side="SELL", qty=2, stop_price=61.36, request_id="rid-KO-stop"
            )
        body, request_id = stub.place_calls[0]
        self.assertEqual(request_id, "rid-KO-stop")
        self.assertEqual(body["ExternalReference"], "rid-KO-stop")


class TestStandaloneStopSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubStopClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as ctx:
                        broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.precheck_calls, [], "gate must fire before precheck")
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_precheck_runs_with_costs_and_blocks_on_not_ok(self):
        stub = _StubStopClient(
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
                broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        self.assertEqual(len(stub.precheck_calls), 1)
        self.assertEqual(stub.precheck_calls[0].get("FieldGroups"), ["Costs"])
        self.assertEqual(stub.place_calls, [], "a failed precheck must block the real POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))

    def test_unsupported_stop_type_rejected_pre_post(self):
        no_stop = dict(_DETAILS_KO)
        no_stop["SupportedOrderTypes"] = ["Limit", "Market"]
        broker, stub = _make(_StubStopClient(details=no_stop))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        self.assertIn("StopIfTraded", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")


if __name__ == "__main__":
    unittest.main()
