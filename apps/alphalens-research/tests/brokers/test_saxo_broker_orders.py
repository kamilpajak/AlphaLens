"""Hermetic tests for SaxoBroker order placement / status / cancel (P2).

Everything runs against a canned-fixture stub client — no network. Pins the
safety rails and translation duties the design memo §P2 freezes:

- ``ALPHALENS_BROKER_ALLOW_ORDERS=1`` env gate blocks placement LOUDLY before
  any client call (cancel deliberately ungated — remediation must always work)
- precheck runs before EVERY real POST and a non-Ok result blocks placement
- ``ManualOrder=false`` pinned on parent and both children
- GTD entry duration is DATE-ONLY, computed on the venue's exchange calendar
- tick-size quantization (nearest) with a hard fail past the bps cap
- 200 -> ``PlacedOrder`` with exits in request order (TP first, then SL)
- 400 carrying an OrderId -> auto-cancel repair, then ``OrderRejectedError``
- 202 TradeNotCompleted -> ``BrokerError`` carrying the OrderId + reconcile
  instructions (no automatic action — state genuinely unknown)
- rejection bodies (ModelState / ErrorInfo) -> ``OrderRejectedError`` verbatim
- absent order -> ``OrderStatus.UNKNOWN`` honestly (never guessed FILLED)
"""

from __future__ import annotations

import datetime as dt
import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    BracketOrderRequest,
    BrokerCapabilityError,
    BrokerError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
)
from alphalens_pipeline.brokers.saxo import broker as broker_module
from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker
from alphalens_pipeline.paper.calendar import advance_trading_sessions

_ALLOW = {ALLOW_ORDERS_ENV: "1"}

_CLIENT_INFO = {"ClientKey": "CK-1"}
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

_PLACED_OK = (
    201,
    {"OrderId": "E-100", "Orders": [{"OrderId": "T-101"}, {"OrderId": "S-102"}]},
)

_ORDER_ROW_WORKING = {
    "OrderId": "E-100",
    "Status": "Working",
    "Amount": 10.0,
    "FillAmount": 0.0,
    "Uic": 307,
    "AssetType": "Stock",
}


def _instrument(ticker: str = "KO", mic: str = "XNYS", uic: str = "307") -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker,
        exchange_mic=mic,
        asset_type="Stock",
        broker_instrument_id=uic,
        broker_symbol=f"{ticker.lower()}:{mic.lower()}",
    )


def _request(**overrides: Any) -> BracketOrderRequest:
    defaults: dict[str, Any] = {
        "instrument": _instrument(),
        "side": "BUY",
        "quantity": 10,
        "entry_limit": 50.0,
        "stop_loss": 45.0,
        "take_profit": 60.0,
        "entry_ttl_days": 5,
        "client_request_id": str(uuid.uuid4()),
    }
    defaults.update(overrides)
    return BracketOrderRequest(**defaults)


class _StubOrderClient:
    """Stub SaxoClient for the order surface — records calls, cans responses."""

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        precheck_response: tuple[int, dict[str, Any]] = (200, {"PreCheckResult": "Ok"}),
        place_response: tuple[int, dict[str, Any]] = _PLACED_OK,
        order_rows: dict[str, dict[str, Any]] | None = None,
        cancel_response: tuple[int, dict[str, Any]] = (200, {}),
    ):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.order_rows = order_rows if order_rows is not None else {}
        self.cancel_response = cancel_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    def get_client_info(self) -> dict[str, Any]:
        return dict(_CLIENT_INFO)

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
        self.cancel_calls.append((order_ids, account_key))
        return self.cancel_response

    def get_open_orders(self) -> dict[str, Any]:
        return {"Data": list(self.order_rows.values())}

    def get_order_status(self, client_key: str, order_id: str) -> dict[str, Any] | None:
        return self.order_rows.get(order_id)


def _make_broker(client: _StubOrderClient | None = None) -> tuple[SaxoBroker, _StubOrderClient]:
    stub = client or _StubOrderClient()
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


class TestAllowOrdersEnvGate(unittest.TestCase):
    def test_gate_blocks_loudly_before_any_client_call(self):
        broker, stub = _make_broker()
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as ctx:
                        broker.place_bracket_order(_request())
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
                self.assertIn("no order was sent", str(ctx.exception).lower())
        self.assertEqual(stub.precheck_calls, [], "gate must fire before precheck")
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_cancel_is_deliberately_ungated(self):
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", {}, clear=True):
            broker.cancel_order("E-100")  # must not raise the capability gate
        self.assertEqual(stub.cancel_calls, [("E-100", "AK-1")])


class TestPrecheckGate(unittest.TestCase):
    def test_precheck_runs_before_place_and_blocks_on_not_ok(self):
        broker, stub = _make_broker(
            _StubOrderClient(
                precheck_response=(
                    200,
                    {
                        "PreCheckResult": "Error",
                        "ErrorInfo": {"ErrorCode": "OrderValueToSmall", "Message": "too small"},
                    },
                )
            )
        )
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request())
        self.assertEqual(len(stub.precheck_calls), 1)
        self.assertEqual(stub.place_calls, [], "a failed precheck must block the real POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))

    def test_precheck_body_requests_costs_field_group(self):
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_bracket_order(_request())
        self.assertEqual(stub.precheck_calls[0].get("FieldGroups"), ["Costs"])
        self.assertNotIn(
            "FieldGroups",
            stub.place_calls[0][0],
            "FieldGroups is precheck-only, never sent on the real POST",
        )


class TestPlacementBody(unittest.TestCase):
    def _place(self, request: BracketOrderRequest) -> tuple[dict[str, Any], _StubOrderClient]:
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_bracket_order(request)
        body, _ = stub.place_calls[0]
        return body, stub

    def test_body_manual_order_false_everywhere(self):
        body, _ = self._place(_request())
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(len(body["Orders"]), 2)
        for child in body["Orders"]:
            self.assertIs(child["ManualOrder"], False)

    def test_body_core_fields_and_child_shapes(self):
        request = _request()
        body, _ = self._place(request)

        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["Amount"], 10)
        self.assertEqual(body["BuySell"], "Buy")
        self.assertEqual(body["OrderType"], "Limit")
        self.assertEqual(body["OrderPrice"], 50.0)
        self.assertEqual(body["ExternalReference"], request.client_request_id)

        tp, sl = body["Orders"]
        self.assertEqual(
            (tp["OrderType"], tp["OrderPrice"], tp["BuySell"]), ("Limit", 60.0, "Sell")
        )
        self.assertEqual(
            (sl["OrderType"], sl["OrderPrice"], sl["BuySell"]), ("StopIfTraded", 45.0, "Sell")
        )
        for child in (tp, sl):
            self.assertEqual(child["Amount"], 10, "Amount must be identical across all three")
            self.assertEqual(child["AccountKey"], "AK-1")
            self.assertEqual(child["OrderDuration"], {"DurationType": "GoodTillCancel"})

    def test_gtd_date_only_from_exchange_calendar(self):
        fixed_today = dt.date(2026, 7, 10)  # a Friday
        with mock.patch.object(broker_module, "_today", return_value=fixed_today):
            body, _ = self._place(_request(entry_ttl_days=5))

        duration = body["OrderDuration"]
        self.assertEqual(duration["DurationType"], "GoodTillDate")
        self.assertIs(duration["ExpirationDateContainsTime"], False)
        expected = advance_trading_sessions(fixed_today, 5, exchange="XNYS")
        self.assertEqual(duration["ExpirationDateTime"], expected.isoformat())

    def test_stop_only_bracket_omits_tp_child(self):
        body, _ = self._place(_request(take_profit=None))
        (sl,) = body["Orders"]
        self.assertEqual(sl["OrderType"], "StopIfTraded")

    def test_sell_entry_mirrors_child_side(self):
        # SELL geometry: stop above entry, tp below (relation validator enforces it)
        body, _ = self._place(_request(side="SELL", stop_loss=55.0, take_profit=45.0))
        self.assertEqual(body["BuySell"], "Sell")
        for child in body["Orders"]:
            self.assertEqual(child["BuySell"], "Buy")


class TestTickQuantization(unittest.TestCase):
    def test_prices_quantized_to_tick_nearest(self):
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_bracket_order(
                _request(entry_limit=50.0009, stop_loss=44.9991, take_profit=60.0004)
            )
        body, _ = stub.place_calls[0]
        self.assertEqual(body["OrderPrice"], 50.0)
        tp, sl = body["Orders"]
        self.assertEqual(tp["OrderPrice"], 60.0)
        self.assertEqual(sl["OrderPrice"], 45.0)

    def test_sub_dollar_band_uses_finer_tick(self):
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_bracket_order(
                _request(entry_limit=0.5001, stop_loss=0.4502, take_profit=None)
            )
        body, _ = stub.place_calls[0]
        self.assertEqual(body["OrderPrice"], 0.5001, "0.0001 tick band must be honored")

    def test_hard_fail_when_adjustment_exceeds_bps_cap(self):
        # 50.10 -> tick 0.25 would move the price 0.10 = ~20bps... use a coarse
        # tick so the nearest-tick adjustment clearly exceeds the 25 bps cap.
        coarse = dict(_DETAILS_KO)
        coarse["TickSizeScheme"] = {"DefaultTickSize": 1.0, "Elements": []}
        broker, stub = _make_broker(_StubOrderClient(details=coarse))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request(entry_limit=50.4))
        self.assertIn("tick", str(ctx.exception).lower())
        self.assertEqual(stub.place_calls, [], "a failed quantization must never POST")
        self.assertEqual(stub.precheck_calls, [], "validation runs before precheck")

    def test_unsupported_order_type_rejected_pre_post(self):
        no_stop = dict(_DETAILS_KO)
        no_stop["SupportedOrderTypes"] = ["Limit", "Market"]
        broker, stub = _make_broker(_StubOrderClient(details=no_stop))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request())
        self.assertIn("StopIfTraded", str(ctx.exception))
        self.assertEqual(stub.place_calls, [])


class TestPriceRelationValidation(unittest.TestCase):
    """Degenerate geometry must fail LOCALLY on the quantized prices — a
    stop at/above a BUY entry (or tp at/below it) never reaches the broker,
    not even its precheck (review finding, PR #840)."""

    def test_buy_stop_at_or_above_entry_rejected_before_any_call(self):
        for bad_stop in (50.0, 55.0):
            broker, stub = _make_broker()
            with mock.patch.dict("os.environ", _ALLOW):
                with self.assertRaises(OrderRejectedError) as ctx:
                    broker.place_bracket_order(_request(stop_loss=bad_stop))
            self.assertIn("stop_loss", str(ctx.exception))
            self.assertEqual(stub.place_calls, [])
            self.assertEqual(stub.precheck_calls, [])

    def test_buy_tp_at_or_below_entry_rejected_before_any_call(self):
        broker, stub = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request(take_profit=50.0))
        self.assertIn("take_profit", str(ctx.exception))
        self.assertEqual(stub.place_calls, [])
        self.assertEqual(stub.precheck_calls, [])


class TestPlacementResponseHandling(unittest.TestCase):
    def test_place_maps_200_to_placed_order_tp_then_sl(self):
        broker, _ = _make_broker()
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_bracket_order(_request())
        self.assertIsInstance(placed, PlacedOrder)
        self.assertEqual(placed.entry_order_id, "E-100")
        self.assertEqual(placed.exit_order_ids, ("T-101", "S-102"))

    def test_400_with_order_id_triggers_cancel_repair_then_raises(self):
        # Sequential acceptance (master then TP then SL): a 400 body carrying
        # an OrderId means live orders exist — the broker must auto-DELETE the
        # entry (cascade removes any placed child) then raise.
        broker, stub = _make_broker(
            _StubOrderClient(
                place_response=(
                    400,
                    {
                        "OrderId": "E-100",
                        "ErrorInfo": {"ErrorCode": "TooFarFromEntryOrder", "Message": "SL bad"},
                    },
                )
            )
        )
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request())
        self.assertEqual(stub.cancel_calls, [("E-100", "AK-1")])
        message = str(ctx.exception)
        self.assertIn("TooFarFromEntryOrder", message)
        self.assertIn("cancel", message.lower())

    def test_400_with_order_id_and_failed_cleanup_still_raises_with_both_facts(self):
        stub = _StubOrderClient(
            place_response=(
                400,
                {"OrderId": "E-100", "ErrorInfo": {"ErrorCode": "X", "Message": "boom"}},
            ),
            cancel_response=(409, {"Message": "order locked"}),
        )
        broker, _ = _make_broker(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_bracket_order(_request())
        message = str(ctx.exception)
        self.assertIn("E-100", message)
        self.assertIn("failed", message.lower())

    def test_202_raises_broker_error_with_order_id_and_reconcile_instructions(self):
        broker, stub = _make_broker(_StubOrderClient(place_response=(202, {"OrderId": "E-100"})))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(BrokerError) as ctx:
                broker.place_bracket_order(_request())
        message = str(ctx.exception)
        self.assertIn("E-100", message)
        self.assertIn("broker orders", message)
        self.assertIn("broker cancel", message)
        self.assertEqual(stub.cancel_calls, [], "202 must take NO automatic action")

    def test_rejection_codes_translate_to_order_rejected_error(self):
        cases = [
            (
                400,
                {"ErrorInfo": {"ErrorCode": "OnWrongSideOfMarket", "Message": "wrong side"}},
                "OnWrongSideOfMarket",
            ),
            (
                400,
                {"ModelState": {"OrderPrice": ["Price not in tick increments"]}},
                "Price not in tick increments",
            ),
            (400, {"ErrorCode": "PriceExceedsAggressiveTolerance", "Message": "px"}, "Price"),
        ]
        for status, payload, expected_fragment in cases:
            with self.subTest(payload=payload):
                broker, _ = _make_broker(_StubOrderClient(place_response=(status, payload)))
                with mock.patch.dict("os.environ", _ALLOW):
                    with self.assertRaises(OrderRejectedError) as ctx:
                        broker.place_bracket_order(_request())
                self.assertIn(expected_fragment, str(ctx.exception))


class TestOrderReads(unittest.TestCase):
    def test_get_order_absent_maps_unknown(self):
        broker, _ = _make_broker(_StubOrderClient(order_rows={}))
        state = broker.get_order("E-GONE")
        self.assertIsInstance(state, OrderState)
        self.assertEqual(state.status, OrderStatus.UNKNOWN)
        self.assertEqual(state.order_id, "E-GONE")

    def test_get_order_working_and_partial_fill(self):
        rows = {
            "E-100": dict(_ORDER_ROW_WORKING),
            "E-200": {**_ORDER_ROW_WORKING, "OrderId": "E-200", "FillAmount": 4.0},
        }
        broker, _ = _make_broker(_StubOrderClient(order_rows=rows))

        working = broker.get_order("E-100")
        partial = broker.get_order("E-200")

        self.assertEqual(working.status, OrderStatus.WORKING)
        self.assertEqual(working.raw_status, "Working")
        self.assertEqual(partial.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(partial.filled_quantity, 4.0)

    def test_get_order_instrument_populated_from_resolve_cache(self):
        broker, _ = _make_broker(_StubOrderClient(order_rows={"E-100": _ORDER_ROW_WORKING}))
        # Without a cache entry the instrument is honestly None...
        self.assertIsNone(broker.get_order("E-100").instrument)
        # ...and once the resolve cache knows Uic 307 it is populated from it.
        ref = _instrument()
        broker._instrument_cache[("KO", "XNYS")] = ref
        self.assertEqual(broker.get_order("E-100").instrument, ref)

    def test_list_open_orders_translates(self):
        rows = {
            "E-100": dict(_ORDER_ROW_WORKING),
            "E-200": {**_ORDER_ROW_WORKING, "OrderId": "E-200", "FillAmount": 4.0},
        }
        broker, _ = _make_broker(_StubOrderClient(order_rows=rows))

        states = broker.list_open_orders()

        self.assertEqual({s.order_id for s in states}, {"E-100", "E-200"})
        by_id = {s.order_id: s for s in states}
        self.assertEqual(by_id["E-100"].status, OrderStatus.WORKING)
        self.assertEqual(by_id["E-200"].status, OrderStatus.PARTIALLY_FILLED)


class TestCancel(unittest.TestCase):
    def test_cancel_issues_single_delete_cascade_documented(self):
        # ONE DELETE on the entry cleans the whole bracket (children cascade);
        # the broker must never delete children first.
        broker, stub = _make_broker()
        broker.cancel_order("E-100")
        self.assertEqual(stub.cancel_calls, [("E-100", "AK-1")])

    def test_cancel_failure_translates_to_broker_error(self):
        broker, _ = _make_broker(
            _StubOrderClient(cancel_response=(409, {"Message": "order locked pre-execution"}))
        )
        with self.assertRaises(BrokerError) as ctx:
            broker.cancel_order("E-100")
        self.assertIn("E-100", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
