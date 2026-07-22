"""Hermetic tests for SaxoBroker.place_oco_exit (Stage 2 rung-2 OCO exit, DARK).

The rung-2 upgrade posts a standalone OCO EXIT pair (no entry parent) on a
filled long: one ``Limit`` take-profit leg + one far ``StopIfTraded`` disaster
leg, both SELL, both ``Amount == owned``, ``OrderRelation:"Oco"``. Live SIM
probe (PR #885, Q1/Q2 TRUE) proved the ACCEPTED body is the SIBLINGS-ARRAY form
``{"AccountKey": key, "Orders": [limit_leg, stop_leg]}`` — the sell side commits
owned ONCE (no ``SellOrdersAlreadyExistForOwnedContracts``) and the far stop
escapes ``TooFarFromEntryOrder`` while OCO-linked.

Pins: siblings-array body shape (no top-level order fields), both legs
``Amount == owned``, per-leg ``ExternalReference`` derived from ``request_id``,
the degenerate-ordering guard (stop must sit below tp), the ALLOW_ORDERS gate,
and the ``PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))``
response shape. The wide-stop 15% child-distance guard
(``_validate_price_relations``) MUST NOT run — the OCO exit is precisely that
escape.
"""

from __future__ import annotations

import unittest
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

# A live-shaped siblings-array place response: two accepted legs, no top-level
# OrderId (the OCO group has no parent order).
_OCO_201_TWO_LEGS = (
    201,
    {"Orders": [{"OrderId": "L-1"}, {"OrderId": "S-2"}]},
)


class _StubOcoClient:
    """Minimal stub SaxoClient for the OCO-exit surface (records calls)."""

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        precheck_response: tuple[int, dict[str, Any]] = (200, {"PreCheckResult": "Ok"}),
        place_response: tuple[int, dict[str, Any]] = _OCO_201_TWO_LEGS,
    ):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []
        self.cancel_calls: list[str] = []

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

    def cancel_order_ids(self, order_ids: str, *, account_key: str) -> tuple[int, dict[str, Any]]:
        # Partial-acceptance / OCO-sibling cleanup DELETE (records the call).
        self.cancel_calls.append(order_ids)
        return (200, {})


def _make(stub: _StubOcoClient) -> tuple[SaxoBroker, _StubOcoClient]:
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


def _place(broker: SaxoBroker, **kwargs: Any) -> PlacedOrder:
    defaults: dict[str, Any] = {
        "uic": 307,
        "side": "SELL",
        "qty": 46,
        "stop_price": 45.0,
        "take_profit": 64.0,
        "request_id": "crid-A-0",
    }
    defaults.update(kwargs)
    with mock.patch.dict("os.environ", _ALLOW):
        return broker.place_oco_exit(**defaults)


class TestOcoBodyCommitsOwnedOnce(unittest.TestCase):
    def test_siblings_array_two_sell_legs_amount_owned_once(self):
        broker, stub = _make(_StubOcoClient())
        _place(broker, qty=46, stop_price=45.0, take_profit=64.0)
        body, _ = stub.place_calls[0]

        # Top-level is the siblings envelope ONLY: AccountKey + Orders, and NO
        # top-level order fields (no parent order commits owned a second time).
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertIn("Orders", body)
        self.assertNotIn("Uic", body)
        self.assertNotIn("OrderType", body)
        self.assertNotIn("Amount", body)
        self.assertNotIn("BuySell", body)

        legs = body["Orders"]
        self.assertEqual(len(legs), 2, "exactly two OCO legs")
        for leg in legs:
            self.assertEqual(leg["BuySell"], "Sell")
            self.assertEqual(leg["Amount"], 46, "each leg commits owned ONCE")
            self.assertEqual(leg["OrderRelation"], "Oco")
            self.assertEqual(leg["Uic"], 307)
            self.assertEqual(leg["AssetType"], "Stock")
            self.assertEqual(leg["OrderDuration"], {"DurationType": "GoodTillCancel"})
            self.assertIs(leg["ManualOrder"], False)

        by_type = {leg["OrderType"]: leg for leg in legs}
        self.assertEqual(set(by_type), {"Limit", "StopIfTraded"}, "one Limit + one StopIfTraded")
        self.assertEqual(by_type["Limit"]["OrderPrice"], 64.0, "Limit @ take_profit")
        self.assertEqual(by_type["StopIfTraded"]["OrderPrice"], 45.0, "StopIfTraded @ stop_price")

    def test_per_leg_external_reference_derived_from_request_id(self):
        broker, stub = _make(_StubOcoClient())
        _place(broker, request_id="crid-A-3")
        body, header_request_id = stub.place_calls[0]
        # The place POST x-request-id header is the base request_id (deterministic
        # → Saxo 15 s dedup catches a same-size crash-retry).
        self.assertEqual(header_request_id, "crid-A-3")
        by_type = {leg["OrderType"]: leg for leg in body["Orders"]}
        self.assertEqual(by_type["StopIfTraded"]["ExternalReference"], "crid-A-3-stop")
        self.assertEqual(by_type["Limit"]["ExternalReference"], "crid-A-3-tp")

    def test_wide_stop_escapes_child_distance_guard(self):
        # A ~30% wide disaster stop (the whole reason the OCO exit exists) must
        # NOT be rejected by the bracket 15% child-distance guard.
        broker, stub = _make(_StubOcoClient())
        _place(broker, stop_price=45.0, take_profit=64.0)  # stop ~30% below tp
        self.assertEqual(len(stub.place_calls), 1, "wide stop must reach the POST, not be blocked")

    def test_prechecks_with_costs_before_post(self):
        broker, stub = _make(_StubOcoClient())
        _place(broker)
        self.assertEqual(len(stub.precheck_calls), 1)
        self.assertEqual(stub.precheck_calls[0].get("FieldGroups"), ["Costs"])


class TestOcoPlacementResponse(unittest.TestCase):
    def test_happy_path_returns_both_exit_ids_no_entry(self):
        broker, _ = _make(_StubOcoClient())
        placed = _place(broker)
        self.assertIsInstance(placed, PlacedOrder)
        self.assertEqual(placed.entry_order_id, "", "an OCO exit pair has no entry order")
        self.assertCountEqual(placed.exit_order_ids, ("L-1", "S-2"))
        self.assertEqual(len(placed.exit_order_ids), 2)

    def test_leg_ids_ordered_stop_then_tp_via_external_reference(self):
        # When Saxo echoes per-leg ExternalReference, the tuple is (stop_id, tp_id).
        echoed = (
            201,
            {
                "Orders": [
                    {"OrderId": "L-1", "ExternalReference": "crid-A-0-tp"},
                    {"OrderId": "S-2", "ExternalReference": "crid-A-0-stop"},
                ]
            },
        )
        broker, _ = _make(_StubOcoClient(place_response=echoed))
        placed = _place(broker, request_id="crid-A-0")
        self.assertEqual(placed.exit_order_ids, ("S-2", "L-1"), "(stop_id, tp_id)")

    def test_leg_ids_ordered_by_ordertype_when_no_external_reference(self):
        # Q7 fallback: Saxo omits the per-leg ExternalReference echo. The
        # (stop_id, tp_id) tuple MUST be resolved by OrderType (Limit -> tp,
        # StopIfTraded -> stop), NOT by response array order — the request body is
        # [limit_leg, stop_leg], so array order would swap the pair.
        no_echo = (
            201,
            {
                "Orders": [
                    {"OrderId": "L-1", "OrderType": "Limit"},  # tp leg, listed FIRST
                    {"OrderId": "S-2", "OrderType": "StopIfTraded"},  # stop leg
                ]
            },
        )
        broker, _ = _make(_StubOcoClient(place_response=no_echo))
        placed = _place(broker, request_id="crid-A-0")
        self.assertEqual(
            placed.exit_order_ids, ("S-2", "L-1"), "(stop_id, tp_id) resolved by OrderType"
        )

    def test_response_with_no_legs_raises_brokererror(self):
        # A 2xx with an empty Orders array is a failure, not a silent no-op.
        from alphalens_pipeline.brokers.contract import BrokerError

        broker, stub = _make(_StubOcoClient(place_response=(201, {"Orders": []})))
        with self.assertRaises(BrokerError):
            _place(broker)
        self.assertEqual(stub.cancel_calls, [], "no accepted leg → nothing to clean up")

    def test_single_leg_2xx_cleans_up_the_stranded_leg_and_raises(self):
        # A half-accepted OCO (one leg only) must cancel the stranded leg (the
        # sibling cascade cleans the rest) and raise — never return a lone leg.
        from alphalens_pipeline.brokers.contract import BrokerError

        broker, stub = _make(_StubOcoClient(place_response=(201, {"Orders": [{"OrderId": "L-1"}]})))
        with self.assertRaises(BrokerError) as ctx:
            _place(broker)
        self.assertNotIsInstance(ctx.exception, PlacedOrder)
        self.assertEqual(stub.cancel_calls, ["L-1"], "the stranded leg must be cancelled")

    def test_202_raises_brokererror_no_silent_placement(self):
        from alphalens_pipeline.brokers.contract import BrokerError

        broker, _ = _make(_StubOcoClient(place_response=(202, {"Orders": [{"OrderId": "X-1"}]})))
        with self.assertRaises(BrokerError) as ctx:
            _place(broker)
        self.assertIn("202", str(ctx.exception))

    def test_reject_with_no_accepted_leg_raises_order_rejected(self):
        reject = (
            400,
            {"ErrorInfo": {"ErrorCode": "SomeReject", "Message": "no"}},
        )
        broker, stub = _make(_StubOcoClient(place_response=reject))
        with self.assertRaises(OrderRejectedError) as ctx:
            _place(broker)
        self.assertIn("SomeReject", str(ctx.exception))
        self.assertEqual(stub.cancel_calls, [], "no accepted leg → no cleanup DELETE")

    def test_reject_after_accepting_a_leg_cleans_up_and_raises(self):
        reject = (
            400,
            {"OrderId": "A-1", "ErrorInfo": {"ErrorCode": "SomeReject", "Message": "no"}},
        )
        broker, stub = _make(_StubOcoClient(place_response=reject))
        with self.assertRaises(OrderRejectedError) as ctx:
            _place(broker)
        self.assertIn("A-1", str(ctx.exception))
        self.assertEqual(stub.cancel_calls, ["A-1"], "the accepted leg must be cancelled")


class TestOcoDegenerateOrdering(unittest.TestCase):
    def test_stop_not_below_tp_rejected_before_post(self):
        broker, stub = _make(_StubOcoClient())
        with self.assertRaises(OrderRejectedError):
            _place(broker, stop_price=64.0, take_profit=45.0)  # stop above tp
        self.assertEqual(stub.place_calls, [], "a degenerate OCO must never POST")

    def test_stop_equal_tp_rejected(self):
        broker, stub = _make(_StubOcoClient())
        with self.assertRaises(OrderRejectedError):
            _place(broker, stop_price=50.0, take_profit=50.0)
        self.assertEqual(stub.place_calls, [])


class TestOcoSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubOcoClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as ctx:
                        broker.place_oco_exit(
                            uic=307,
                            side="SELL",
                            qty=46,
                            stop_price=45.0,
                            take_profit=64.0,
                            request_id="crid-A-0",
                        )
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.precheck_calls, [], "gate must fire before precheck")
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_precheck_reject_blocks_post(self):
        stub = _StubOcoClient(
            precheck_response=(
                200,
                {
                    "PreCheckResult": "Error",
                    "ErrorInfo": {"ErrorCode": "OrderValueToSmall", "Message": "too small"},
                },
            )
        )
        broker, _ = _make(stub)
        with self.assertRaises(OrderRejectedError) as ctx:
            _place(broker)
        self.assertEqual(len(stub.precheck_calls), 1)
        self.assertEqual(stub.place_calls, [], "a failed precheck must block the real POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))

    def test_unsupported_order_type_rejected_pre_post(self):
        no_limit = dict(_DETAILS_KO)
        no_limit["SupportedOrderTypes"] = ["Market", "StopIfTraded"]
        broker, stub = _make(_StubOcoClient(details=no_limit))
        with self.assertRaises(OrderRejectedError) as ctx:
            _place(broker)
        self.assertIn("Limit", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")


if __name__ == "__main__":
    unittest.main()
