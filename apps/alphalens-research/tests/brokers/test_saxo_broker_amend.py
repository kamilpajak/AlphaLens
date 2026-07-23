"""Hermetic tests for SaxoBroker.amend_stop_amount (Stage 3 PATCH resize, DARK).

The amend primitive resizes a SINGLE resting standalone StopIfTraded IN PLACE
via ``PATCH /trade/v2/orders`` (Q8, SIM-precheck-clean): it re-applies
``Amount == live-owned`` to an existing OrderId, so the resting order never
leaves the book (no cancel, no naked window) and a cross-tick re-emit is
absolute-target idempotent (set Amount=owned twice = owned).

Pins: the Q8 body shape (OrderId + AssetType + AccountKey + OrderType +
OrderPrice + OrderDuration + Amount + BuySell + ManualOrder; NO
ExternalReference, NO PositionId, NO precheck), the ALLOW_ORDERS gate FIRST,
the 200-same-OrderId ``PlacedOrder(entry_order_id="",
exit_order_ids=(order_id,))`` response, the 202-ambiguous ``BrokerError`` (leave
the resting order alone), the non-2xx ``OrderRejectedError``, and the
``SupportsAmendStop`` runtime_checkable narrowing.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    BrokerCapabilityError,
    BrokerError,
    OrderRejectedError,
    PlacedOrder,
    SupportsAmendStop,
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

# A live-shaped amend response: PATCH echoes the SAME OrderId it resized.
_AMEND_200_SAME = (200, {"OrderId": "S-900"})


class _StubAmendClient:
    """Minimal stub SaxoClient for the amend surface (records calls).

    Deliberately has NO ``precheck_order`` / ``place_order`` — the amend path
    must never precheck and never POST; a call to either would AttributeError
    and fail the test loudly.
    """

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        amend_response: tuple[int, dict[str, Any]] = _AMEND_200_SAME,
    ):
        self.details = details or _DETAILS_KO
        self.amend_response = amend_response
        self.amend_calls: list[tuple[dict[str, Any], str]] = []

    def get_accounts(self) -> dict[str, Any]:
        return _ACCOUNTS

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        return dict(self.details)

    def amend_order(
        self, patch_body: dict[str, Any], *, request_id: str
    ) -> tuple[int, dict[str, Any]]:
        self.amend_calls.append((patch_body, request_id))
        return self.amend_response


def _make(stub: _StubAmendClient) -> tuple[SaxoBroker, _StubAmendClient]:
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


def _amend(broker: SaxoBroker, **kwargs: Any) -> PlacedOrder:
    defaults: dict[str, Any] = {
        "uic": 307,
        "order_id": "S-900",
        "side": "SELL",
        "order_type": "StopIfTraded",
        "new_qty": 7,
        "stop_price": 45.0,
        "request_id": "crid-A-amend-3",
    }
    defaults.update(kwargs)
    with mock.patch.dict("os.environ", _ALLOW):
        return broker.amend_stop_amount(**defaults)


class TestAmendBody(unittest.TestCase):
    def test_builds_q8_body(self):
        broker, stub = _make(_StubAmendClient())
        _amend(broker, uic=307, order_id="S-900", new_qty=7, stop_price=45.0)
        body, header_request_id = stub.amend_calls[0]

        self.assertEqual(body["OrderId"], "S-900", "amend targets the resting OrderId")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "StopIfTraded")
        self.assertEqual(body["OrderPrice"], 45.0, "stop_price quantized to the tick")
        self.assertEqual(body["OrderDuration"], {"DurationType": "GoodTillCancel"})
        self.assertEqual(body["Amount"], 7, "resize to the new (absolute) target qty")
        self.assertEqual(body["BuySell"], "Sell")
        self.assertIs(body["ManualOrder"], False)
        # request_id rides the header only (x-request-id dedup key), NOT the body.
        self.assertEqual(header_request_id, "crid-A-amend-3")

    def test_body_has_no_external_reference_and_no_position_id(self):
        broker, stub = _make(_StubAmendClient())
        _amend(broker)
        body, _ = stub.amend_calls[0]
        self.assertNotIn("ExternalReference", body, "amend preserves the resting order's own ref")
        self.assertNotIn("PositionId", body, "reduce-only PositionId refuted (Stage 3)")

    def test_buy_side_maps_to_buy(self):
        broker, stub = _make(_StubAmendClient())
        _amend(broker, side="BUY")
        body, _ = stub.amend_calls[0]
        self.assertEqual(body["BuySell"], "Buy")

    def test_falsy_order_type_defaults_to_stopiftraded(self):
        # Belt: a resting OCO child stop can echo ``OrderType: None`` from a naive
        # ``/orders/me`` read (Stage-3.5 write-side gotcha), and the mapped OrderState
        # can then carry a falsy order_type. Both "" and None must default to
        # "StopIfTraded" so the single amend-body choke point always PATCHes a valid
        # stop order (covers both the standalone and the OCO-leg callers).
        for falsy in ("", None):
            with self.subTest(order_type=falsy):
                broker, stub = _make(_StubAmendClient())
                _amend(broker, order_type=falsy)
                body, _ = stub.amend_calls[0]
                self.assertEqual(
                    body["OrderType"], "StopIfTraded", "falsy order_type defaults to StopIfTraded"
                )

    def test_present_order_type_is_preserved(self):
        broker, stub = _make(_StubAmendClient())
        _amend(broker, order_type="Stop")
        body, _ = stub.amend_calls[0]
        self.assertEqual(
            body["OrderType"], "Stop", "a present order_type is passed through unchanged"
        )

    def test_never_prechecks(self):
        # The stub has no precheck_order; reaching it would AttributeError. The
        # amend path has no PATCH precheck endpoint (spec) — assert only amend
        # was called.
        broker, stub = _make(_StubAmendClient())
        _amend(broker)
        self.assertEqual(len(stub.amend_calls), 1, "exactly one PATCH, no precheck")


class TestAmendResponse(unittest.TestCase):
    def test_200_same_orderid_returns_placed_order(self):
        broker, _ = _make(_StubAmendClient(amend_response=(200, {"OrderId": "S-900"})))
        placed = _amend(broker, order_id="S-900")
        self.assertIsInstance(placed, PlacedOrder)
        self.assertEqual(placed.entry_order_id, "", "an amend of an exit has no entry")
        self.assertEqual(placed.exit_order_ids, ("S-900",), "the preserved resting OrderId")

    def test_201_same_orderid_also_accepted(self):
        broker, _ = _make(_StubAmendClient(amend_response=(201, {"OrderId": "S-900"})))
        placed = _amend(broker, order_id="S-900")
        self.assertEqual(placed.exit_order_ids, ("S-900",))

    def test_2xx_different_orderid_raises_brokererror_ambiguous(self):
        # A 2xx that echoes a DIFFERENT OrderId is ambiguous — never claim the
        # resize landed on the intended order.
        broker, _ = _make(_StubAmendClient(amend_response=(200, {"OrderId": "OTHER-1"})))
        with self.assertRaises(BrokerError) as ctx:
            _amend(broker, order_id="S-900")
        self.assertNotIsInstance(ctx.exception, OrderRejectedError)
        self.assertIn("S-900", str(ctx.exception))

    def test_2xx_missing_orderid_raises_brokererror_ambiguous(self):
        broker, _ = _make(_StubAmendClient(amend_response=(200, {})))
        with self.assertRaises(BrokerError) as ctx:
            _amend(broker, order_id="S-900")
        self.assertNotIsInstance(ctx.exception, OrderRejectedError)

    def test_202_raises_brokererror_leaves_resting_order(self):
        broker, _ = _make(_StubAmendClient(amend_response=(202, {"OrderId": "S-900"})))
        with self.assertRaises(BrokerError) as ctx:
            _amend(broker, order_id="S-900")
        self.assertNotIsInstance(ctx.exception, OrderRejectedError)
        self.assertIn("202", str(ctx.exception))

    def test_non_2xx_raises_order_rejected(self):
        reject = (400, {"ErrorInfo": {"ErrorCode": "OrderNotFound", "Message": "gone"}})
        broker, _ = _make(_StubAmendClient(amend_response=reject))
        with self.assertRaises(OrderRejectedError) as ctx:
            _amend(broker, order_id="S-900")
        self.assertIn("OrderNotFound", str(ctx.exception))

    def test_non_2xx_attaches_structured_error_code(self):
        reject = (400, {"ErrorInfo": {"ErrorCode": "OrderNotFound", "Message": "gone"}})
        broker, _ = _make(_StubAmendClient(amend_response=reject))
        with self.assertRaises(OrderRejectedError) as ctx:
            _amend(broker, order_id="S-900")
        self.assertEqual(ctx.exception.error_code, "OrderNotFound")


class TestAmendSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubAmendClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as ctx:
                        broker.amend_stop_amount(
                            uic=307,
                            order_id="S-900",
                            side="SELL",
                            order_type="StopIfTraded",
                            new_qty=7,
                            stop_price=45.0,
                            request_id="crid-A-amend-3",
                        )
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.amend_calls, [], "gate must fire before any PATCH")

    def test_non_finite_stop_price_rejected_before_patch(self):
        broker, stub = _make(_StubAmendClient())
        with self.assertRaises(OrderRejectedError):
            _amend(broker, stop_price=float("nan"))
        self.assertEqual(stub.amend_calls, [], "a garbage price must never PATCH")


class TestSupportsAmendStopProtocol(unittest.TestCase):
    def test_saxo_broker_satisfies_runtime_checkable_protocol(self):
        broker, _ = _make(_StubAmendClient())
        self.assertIsInstance(broker, SupportsAmendStop)

    def test_object_without_amend_method_is_not_an_instance(self):
        class _NoAmend:
            pass

        self.assertNotIsInstance(_NoAmend(), SupportsAmendStop)


if __name__ == "__main__":
    unittest.main()
