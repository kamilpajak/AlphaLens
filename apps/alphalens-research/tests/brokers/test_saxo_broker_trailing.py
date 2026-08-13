"""Hermetic tests for the native trailing-BUY-stop adapter (PR-T2a).

The V1 entry-trailing executor (docs/research/entry_trailing_design_2026_08_12.md
§2 verdict, §4b probe facts): a standalone native trailing BUY stop whose trigger
the BROKER ratchets down following new lows (zero client amends), plus the G1
gap-clamp StopLimit BUY. Both mirror the existing standalone-stop / market-order
flow: canonical-side check -> ALLOW_ORDERS gate -> precheck -> single POST ->
_handle_placement_response. NO caller is wired yet (inert until PR-T2b); this
file pins the adapter surface in isolation.

Wire facts are the SIM+LIVE-probed §4b P1/P7 set: OrderType=TrailingStopIfTraded
with the REQUIRED TrailingStopDistanceToMarket + TrailingStopStep fields and a
MANDATORY DayOrder OrderDuration (G1 — no trailing order lives through an
overnight gap); the StopLimit BUY carries StopLimitPrice. Precheck LIES about
price/type semantics (§4b P2/P6), so a wrong-side rejection is classified on the
POST response only, never on a green precheck.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker
from broker_contract.contract import (
    BrokerCapabilityError,
    OrderRejectedError,
    PlacedOrder,
)

_ALLOW = {ALLOW_ORDERS_ENV: "1"}
_ACCOUNTS = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}
# NOTE: SupportedOrderTypes carries TrailingStopIfTraded (§4b P1) AND StopLimit
# (§4b P7) — the two order shapes this capability places.
_DETAILS_KO = {
    "Uic": 307,
    "AssetType": "Stock",
    "Format": {"Decimals": 2, "OrderDecimals": 2},
    "TickSizeScheme": {
        "DefaultTickSize": 0.01,
        "Elements": [{"HighPrice": 0.9999, "TickSize": 0.0001}],
    },
    "SupportedOrderTypes": [
        "Limit",
        "Market",
        "Stop",
        "StopIfTraded",
        "StopLimit",
        "TrailingStopIfTraded",
    ],
}


class _StubTrailingClient:
    """Minimal stub SaxoClient for the trailing-stop surface (records calls)."""

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        precheck_response: tuple[int, dict[str, Any]] = (200, {"PreCheckResult": "Ok"}),
        place_response: tuple[int, dict[str, Any]] = (200, {"OrderId": "T-900"}),
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


def _make(stub: _StubTrailingClient) -> tuple[SaxoBroker, _StubTrailingClient]:
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


class TestSupportsTrailingStopProtocol(unittest.TestCase):
    def test_supports_trailing_stop_is_runtime_checkable_protocol(self):
        from broker_contract.contract import SupportsTrailingStop

        # A trivial object with BOTH methods structurally satisfies the Protocol.
        class _Both:
            def place_trailing_stop(
                self, uic, side, qty, trailing_distance, trailing_step, request_id=None
            ):
                return None

            def place_stop_limit(self, uic, side, qty, stop_price, limit_price, request_id=None):
                return None

        self.assertIsInstance(_Both(), SupportsTrailingStop)

        class _OnlyTrail:  # missing place_stop_limit
            def place_trailing_stop(
                self, uic, side, qty, trailing_distance, trailing_step, request_id=None
            ):
                return None

        self.assertNotIsInstance(_OnlyTrail(), SupportsTrailingStop)

        class _Neither:
            pass

        self.assertNotIsInstance(_Neither(), SupportsTrailingStop)

    def test_saxo_broker_conforms_to_the_capability(self):
        # Discoverability: the real adapter is narrowed exactly like the other
        # capabilities (isinstance against the @runtime_checkable Protocol).
        from broker_contract.contract import SupportsTrailingStop

        broker, _ = _make(_StubTrailingClient())
        self.assertIsInstance(broker, SupportsTrailingStop)


class TestTrailingStopBody(unittest.TestCase):
    def test_body_is_trailing_buy_with_distance_step_dayorder(self):
        broker, stub = _make(_StubTrailingClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_trailing_stop(
                uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
            )
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body, "a standalone trailing stop carries NO Orders array")
        self.assertNotIn("OrderPrice", body, "a trailing stop carries NO absolute price")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "TrailingStopIfTraded")
        self.assertEqual(body["BuySell"], "Buy")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["TrailingStopDistanceToMarket"], 0.05)
        self.assertEqual(body["TrailingStopStep"], 0.01)
        self.assertEqual(
            body["OrderDuration"],
            {"DurationType": "DayOrder"},
            "G1: trailing orders are DayOrder-only so none lives through an overnight gap",
        )
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "T-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_successful_post_returns_the_order_id(self):
        stub = _StubTrailingClient(place_response=(201, {"OrderId": "T-4242"}))
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_trailing_stop(
                uic=307, side="BUY", qty=1, trailing_distance=0.05, trailing_step=0.01
            )
        self.assertEqual(placed.entry_order_id, "T-4242")
        self.assertEqual(placed.exit_order_ids, ())

    def test_explicit_request_id_reused_as_external_reference(self):
        broker, stub = _make(_StubTrailingClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_trailing_stop(
                uic=307,
                side="BUY",
                qty=2,
                trailing_distance=0.05,
                trailing_step=0.01,
                request_id="rid-RIVN-entry-fire-1",
            )
        body, request_id = stub.place_calls[0]
        self.assertEqual(request_id, "rid-RIVN-entry-fire-1")
        self.assertEqual(body["ExternalReference"], "rid-RIVN-entry-fire-1")

    def test_request_id_defaults_to_uuid(self):
        broker, stub = _make(_StubTrailingClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_trailing_stop(
                uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
            )
        _, request_id = stub.place_calls[0]
        uuid.UUID(request_id)


class TestStopLimitBody(unittest.TestCase):
    def test_body_is_stop_limit_buy_with_stoplimitprice(self):
        broker, stub = _make(_StubTrailingClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_stop_limit(
                uic=307, side="BUY", qty=2, stop_price=16.00, limit_price=16.05
            )
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body, "the ceiling-clamp StopLimit has no bracket parent")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "StopLimit")
        self.assertEqual(body["BuySell"], "Buy")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["OrderPrice"], 16.00, "OrderPrice is the stop trigger")
        self.assertEqual(body["StopLimitPrice"], 16.05, "StopLimitPrice caps the fill (G1)")
        self.assertEqual(
            body["OrderDuration"],
            {"DurationType": "DayOrder"},
            "G1: the ceiling-clamp order is DayOrder-only too",
        )
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "T-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_explicit_request_id_reused_as_external_reference(self):
        broker, stub = _make(_StubTrailingClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_stop_limit(
                uic=307,
                side="BUY",
                qty=2,
                stop_price=16.00,
                limit_price=16.05,
                request_id="rid-RIVN-entry-clamp-1",
            )
        body, request_id = stub.place_calls[0]
        self.assertEqual(request_id, "rid-RIVN-entry-clamp-1")
        self.assertEqual(body["ExternalReference"], "rid-RIVN-entry-clamp-1")


class TestTrailingStopSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubTrailingClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env, clear=True):
                with self.assertRaises(BrokerCapabilityError) as ctx:
                    broker.place_trailing_stop(
                        uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
                    )
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
                with self.assertRaises(BrokerCapabilityError):
                    broker.place_stop_limit(
                        uic=307, side="BUY", qty=2, stop_price=16.00, limit_price=16.05
                    )
        self.assertEqual(stub.precheck_calls, [], "gate must fire before precheck")
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_saxo_form_side_rejected_before_any_http(self):
        # Incident 2026-07: a probe passed the Saxo-form side and the else-Buy
        # fallback silently bought stock. Any non-canonical side raises BEFORE
        # any client call.
        broker, stub = _make(_StubTrailingClient())
        for bad in ("Sell", "Buy", "sell", "buy", ""):
            with self.subTest(side=bad), mock.patch.dict("os.environ", _ALLOW):
                with self.assertRaises(ValueError) as ctx:
                    broker.place_trailing_stop(
                        uic=307, side=bad, qty=2, trailing_distance=0.05, trailing_step=0.01
                    )
                self.assertIn("'BUY'", str(ctx.exception))
                self.assertIn("'SELL'", str(ctx.exception))
                with self.assertRaises(ValueError):
                    broker.place_stop_limit(
                        uic=307, side=bad, qty=2, stop_price=16.00, limit_price=16.05
                    )
        self.assertEqual(stub.precheck_calls, [], "an invalid side must never precheck")
        self.assertEqual(stub.place_calls, [], "an invalid side must never POST")

    def test_unsupported_trailing_type_rejected_pre_post(self):
        no_trail = dict(_DETAILS_KO)
        no_trail["SupportedOrderTypes"] = ["Limit", "Market", "StopIfTraded", "StopLimit"]
        broker, stub = _make(_StubTrailingClient(details=no_trail))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_trailing_stop(
                    uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
                )
        self.assertIn("TrailingStopIfTraded", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")

    def test_unsupported_stop_limit_type_rejected_pre_post(self):
        no_sl = dict(_DETAILS_KO)
        no_sl["SupportedOrderTypes"] = ["Limit", "Market", "StopIfTraded", "TrailingStopIfTraded"]
        broker, stub = _make(_StubTrailingClient(details=no_sl))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_stop_limit(
                    uic=307, side="BUY", qty=2, stop_price=16.00, limit_price=16.05
                )
        self.assertIn("StopLimit", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")

    def test_nonpositive_trailing_distance_rejected_pre_post(self):
        broker, stub = _make(_StubTrailingClient())
        for bad in (0.0, -0.01, float("nan"), float("inf")):
            with self.subTest(distance=bad), mock.patch.dict("os.environ", _ALLOW):
                with self.assertRaises(OrderRejectedError):
                    broker.place_trailing_stop(
                        uic=307, side="BUY", qty=2, trailing_distance=bad, trailing_step=0.01
                    )
        self.assertEqual(stub.place_calls, [], "a garbage distance must never POST")

    def test_wrong_side_post_response_raises_typed_error(self):
        # §4b P2/P6: a BUY stop below the market returns OnWrongSideOfMarket on
        # the POST. The method must raise the typed OrderRejectedError (NOT
        # return a silent None). Precheck is green here (it LIES about wrong-side
        # semantics), so the classification comes from the POST response.
        stub = _StubTrailingClient(
            precheck_response=(200, {"PreCheckResult": "Ok"}),
            place_response=(
                400,
                {"ErrorInfo": {"ErrorCode": "OnWrongSideOfMarket", "Message": "wrong side"}},
            ),
        )
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                placed = broker.place_trailing_stop(
                    uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
                )
                self.assertIsNone(placed, "wrong-side must raise, never return")
        self.assertIn("OnWrongSideOfMarket", str(ctx.exception))

    def test_green_precheck_is_not_trusted_as_placement_success(self):
        # §4b P2/P6: precheck LIES (passes wrong-side bodies the real POST
        # rejects). A green precheck must NOT short-circuit the POST — the real
        # placement runs and its rejection is what surfaces.
        stub = _StubTrailingClient(
            precheck_response=(200, {"PreCheckResult": "Ok"}),
            place_response=(
                400,
                {"ErrorInfo": {"ErrorCode": "OnWrongSideOfMarket", "Message": "wrong side"}},
            ),
        )
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError):
                broker.place_trailing_stop(
                    uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
                )
        self.assertEqual(len(stub.place_calls), 1, "a green precheck must NOT skip the real POST")

    def test_failed_precheck_still_blocks_the_post(self):
        # A structured NON-Ok precheck (not the lying wrong-side case) blocks the
        # POST exactly like every other placer.
        stub = _StubTrailingClient(
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
                broker.place_trailing_stop(
                    uic=307, side="BUY", qty=2, trailing_distance=0.05, trailing_step=0.01
                )
        self.assertEqual(stub.precheck_calls[0].get("FieldGroups"), ["Costs"])
        self.assertEqual(stub.place_calls, [], "a failed precheck must block the real POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
