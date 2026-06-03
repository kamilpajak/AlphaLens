"""Tests for the broker-neutral OCO-ladder exit primitive (PR-1 foundation).

Covers:
  * The Alpaca adapter ``AlpacaClient.attach_exit_ladder`` decomposition —
    one OCO submit per tranche, capturing BOTH the parent (TP) id and the
    ``legs[0]`` (SL) id, returning one ``ExitLadderLeg`` per tranche.
  * Whole-share preservation (OCO/bracket require whole shares).
  * The empty-``legs`` failure path (cannot capture the SL id → raise).
  * Structural conformance of a pure-Python fake broker to ``BrokerClient``
    (``@runtime_checkable`` name check) + a positive control that a stub
    MISSING ``attach_exit_ladder`` does NOT pass.

The alpaca-py SDK is faked via ``sys.modules`` exactly as in
``tests.test_alpaca_client`` so no network / real SDK is needed. This repo
runs unittest (NOT pytest).
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_fake_alpaca(target: dict) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Install a fake ``alpaca.trading.{client,requests,enums}`` into the
    given ``sys.modules`` snapshot so the canonical client's lazy import
    resolves to the mock. Mirrors the helper in ``tests.test_alpaca_client``.
    """
    fake_alpaca = types.ModuleType("alpaca")
    fake_trading = types.ModuleType("alpaca.trading")
    fake_trading_client = types.ModuleType("alpaca.trading.client")
    fake_trading_requests = types.ModuleType("alpaca.trading.requests")
    fake_trading_enums = types.ModuleType("alpaca.trading.enums")
    fake_trading_stream = types.ModuleType("alpaca.trading.stream")

    fake_trading_client.TradingClient = MagicMock(name="TradingClient")
    fake_trading_stream.TradingStream = MagicMock(name="TradingStream")
    fake_trading_requests.LimitOrderRequest = MagicMock(name="LimitOrderRequest")
    fake_trading_requests.MarketOrderRequest = MagicMock(name="MarketOrderRequest")
    fake_trading_requests.StopOrderRequest = MagicMock(name="StopOrderRequest")
    fake_trading_requests.TakeProfitRequest = MagicMock(name="TakeProfitRequest")
    fake_trading_requests.StopLossRequest = MagicMock(name="StopLossRequest")
    fake_trading_requests.GetOrdersRequest = MagicMock(name="GetOrdersRequest")

    class _OrderSide:
        BUY = "OrderSide.BUY"
        SELL = "OrderSide.SELL"

    class _OrderClass:
        SIMPLE = "OrderClass.SIMPLE"
        BRACKET = "OrderClass.BRACKET"
        OCO = "OrderClass.OCO"
        OTO = "OrderClass.OTO"

    class _TimeInForce:
        GTC = "TimeInForce.GTC"
        DAY = "TimeInForce.DAY"

    fake_trading_enums.OrderSide = _OrderSide
    fake_trading_enums.OrderClass = _OrderClass
    fake_trading_enums.TimeInForce = _TimeInForce

    target["alpaca"] = fake_alpaca
    target["alpaca.trading"] = fake_trading
    target["alpaca.trading.client"] = fake_trading_client
    target["alpaca.trading.requests"] = fake_trading_requests
    target["alpaca.trading.enums"] = fake_trading_enums
    target["alpaca.trading.stream"] = fake_trading_stream
    fake_alpaca.trading = fake_trading
    fake_trading.client = fake_trading_client
    fake_trading.requests = fake_trading_requests
    fake_trading.enums = fake_trading_enums
    fake_trading.stream = fake_trading_stream

    return fake_trading_client, fake_trading_requests, fake_trading_enums


class _FakeAlpacaTestCase(unittest.TestCase):
    """Snapshots ``sys.modules`` so the fake SDK is restored on teardown;
    resets the canonical client singleton + SDK cache between tests.
    """

    def setUp(self):
        self._sys_modules_patcher = patch.dict("sys.modules")
        self._sys_modules_patcher.start()
        self.fake_client_mod, self.fake_requests_mod, self.fake_enums_mod = _install_fake_alpaca(
            sys.modules
        )
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()

    def tearDown(self):
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()
        self._sys_modules_patcher.stop()


def _oco_parent_stub(tp_id: str, sl_id: str) -> MagicMock:
    """Build the OCO submit return: a PARENT object that IS the take-profit
    limit (``parent.id``) carrying the stop-loss leg in ``parent.legs[0]``
    (``legs[0].id``) — the ground-truth Alpaca OCO shape.
    """
    sl_leg = MagicMock(name="sl_leg")
    sl_leg.id = sl_id
    parent = MagicMock(name="oco_parent")
    parent.id = tp_id
    parent.legs = [sl_leg]
    return parent


class TestAttachExitLadderDecomposition(_FakeAlpacaTestCase):
    def _build_client(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        return AlpacaClient(api_key="k", secret_key="s")

    def _trading_instance(self):
        return self.fake_client_mod.TradingClient.return_value

    def test_three_tranches_submit_three_oco_orders_and_return_three_legs(self):
        # Given a fake broker returning a distinct OCO parent per submit.
        from alphalens_pipeline.paper.broker import ExitTranche

        parents = [
            _oco_parent_stub("tp-1", "sl-1"),
            _oco_parent_stub("tp-2", "sl-2"),
            _oco_parent_stub("tp-3", "sl-3"),
        ]
        self._trading_instance().submit_order.side_effect = parents

        tranches = [
            ExitTranche(qty=4, take_profit_limit=120.0),
            ExitTranche(qty=3, take_profit_limit=130.0),
            ExitTranche(qty=2, take_profit_limit=140.0),
        ]

        # When attaching the exit ladder with a single disaster stop.
        client = self._build_client()
        legs = client.attach_exit_ladder(symbol="NVDA", tranches=tranches, stop_price=90.0)

        # Then exactly three OCO submits fired, each with order_class == OCO.
        self.assertEqual(self._trading_instance().submit_order.call_count, 3)
        self.assertEqual(self.fake_requests_mod.LimitOrderRequest.call_count, 3)
        for call in self.fake_requests_mod.LimitOrderRequest.call_args_list:
            self.assertEqual(call.kwargs["order_class"], self.fake_enums_mod.OrderClass.OCO)
            self.assertEqual(call.kwargs["side"], self.fake_enums_mod.OrderSide.SELL)
            self.assertIn("take_profit", call.kwargs)
            self.assertIn("stop_loss", call.kwargs)

        # And three ExitLadderLeg returned, ids drawn from parent.id (TP) and
        # parent.legs[0].id (SL), with index/qty/prices preserved.
        self.assertEqual(len(legs), 3)
        for i, (leg, tr) in enumerate(zip(legs, tranches, strict=True)):
            self.assertEqual(leg.tranche_index, i)
            self.assertEqual(leg.qty, tr.qty)
            self.assertEqual(leg.take_profit_limit, tr.take_profit_limit)
            self.assertEqual(leg.stop_price, 90.0)
            self.assertEqual(leg.tp_order_id, f"tp-{i + 1}")
            self.assertEqual(leg.sl_order_id, f"sl-{i + 1}")

    def test_order_ids_are_coerced_to_str_when_broker_returns_uuid(self):
        # Given a broker returning UUID objects for order ids (the real Alpaca
        # SDK does — order.id is a uuid.UUID, not a str).
        import uuid

        from alphalens_pipeline.paper.broker import ExitTranche

        tp_uuid, sl_uuid = uuid.uuid4(), uuid.uuid4()
        self._trading_instance().submit_order.side_effect = [_oco_parent_stub(tp_uuid, sl_uuid)]

        # When attaching a one-tranche ladder.
        legs = self._build_client().attach_exit_ladder(
            symbol="NVDA", tranches=[ExitTranche(qty=1, take_profit_limit=120.0)], stop_price=90.0
        )

        # Then the leg ids are plain str (ledger persists them as TEXT), not UUID.
        self.assertIsInstance(legs[0].tp_order_id, str)
        self.assertIsInstance(legs[0].sl_order_id, str)
        self.assertEqual(legs[0].tp_order_id, str(tp_uuid))
        self.assertEqual(legs[0].sl_order_id, str(sl_uuid))

    def test_each_oco_shares_the_same_stop_price(self):
        from alphalens_pipeline.paper.broker import ExitTranche

        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-1", "sl-1"),
            _oco_parent_stub("tp-2", "sl-2"),
        ]
        client = self._build_client()
        client.attach_exit_ladder(
            symbol="NVDA",
            tranches=[
                ExitTranche(qty=5, take_profit_limit=120.0),
                ExitTranche(qty=5, take_profit_limit=130.0),
            ],
            stop_price=88.0,
        )
        # Every StopLossRequest carries the same disaster stop price.
        for call in self.fake_requests_mod.StopLossRequest.call_args_list:
            self.assertEqual(call.kwargs["stop_price"], 88.0)
        self.assertEqual(self.fake_requests_mod.StopLossRequest.call_count, 2)

    def test_whole_shares_preserved_in_submitted_qty(self):
        # Given integer tranche qtys (OCO/bracket require WHOLE shares).
        from alphalens_pipeline.paper.broker import ExitTranche

        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-1", "sl-1"),
            _oco_parent_stub("tp-2", "sl-2"),
        ]
        client = self._build_client()
        client.attach_exit_ladder(
            symbol="NVDA",
            tranches=[
                ExitTranche(qty=7, take_profit_limit=120.0),
                ExitTranche(qty=3, take_profit_limit=130.0),
            ],
            stop_price=90.0,
        )
        submitted_qtys = [
            call.kwargs["qty"] for call in self.fake_requests_mod.LimitOrderRequest.call_args_list
        ]
        self.assertEqual(submitted_qtys, [7, 3])
        for q in submitted_qtys:
            self.assertIsInstance(q, int)

    def test_submitted_and_recorded_prices_are_tick_rounded(self):
        # Given a sub-penny TP and a sub-penny disaster stop (the deterministic
        # brief_trade_setup ladder emits full float precision).
        from alphalens_pipeline.paper.broker import ExitTranche

        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-1", "sl-1"),
        ]
        client = self._build_client()
        legs = client.attach_exit_ladder(
            symbol="NVDA",
            tranches=[ExitTranche(qty=4, take_profit_limit=69.22318381700624)],
            stop_price=58.117777,
        )
        # The broker request carries the tick-rounded prices...
        tp_req = self.fake_requests_mod.TakeProfitRequest.call_args_list[0]
        sl_req = self.fake_requests_mod.StopLossRequest.call_args_list[0]
        self.assertEqual(tp_req.kwargs["limit_price"], 69.22)
        self.assertEqual(sl_req.kwargs["stop_price"], 58.12)
        # ...and the recorded leg matches what was submitted (not the raw
        # intent), so a reconciler comparing a leg to the broker order sees no
        # sub-tick drift.
        self.assertEqual(legs[0].take_profit_limit, 69.22)
        self.assertEqual(legs[0].stop_price, 58.12)

    def test_symbol_and_time_in_force_passthrough(self):
        from alphalens_pipeline.paper.broker import ExitTranche

        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-1", "sl-1"),
        ]
        client = self._build_client()
        client.attach_exit_ladder(
            symbol="AMD",
            tranches=[ExitTranche(qty=4, take_profit_limit=120.0)],
            stop_price=90.0,
            time_in_force="gtc",
        )
        limit_call = self.fake_requests_mod.LimitOrderRequest.call_args_list[0]
        # symbol is routed unchanged onto the OCO request.
        self.assertEqual(limit_call.kwargs["symbol"], "AMD")
        # time_in_force='gtc' maps to the SDK TimeInForce.GTC enum.
        self.assertEqual(limit_call.kwargs["time_in_force"], self.fake_enums_mod.TimeInForce.GTC)

    def test_empty_tranches_raises_refuses_unprotected_position(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClientError

        client = self._build_client()
        # When attaching with no tranches — Then it raises and submits nothing
        # (an empty ladder would leave the held position with no stop).
        with self.assertRaises(AlpacaClientError) as cm:
            client.attach_exit_ladder(symbol="NVDA", tranches=[], stop_price=90.0)
        self.assertIn("empty", str(cm.exception).lower())
        self.assertEqual(self._trading_instance().submit_order.call_count, 0)

    def test_empty_legs_on_second_tranche_rolls_back_first_then_raises(self):
        # Given tranche #1 returns a valid OCO parent but tranche #2 comes back
        # with no legs (SL id uncapturable on the second submit).
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClientError
        from alphalens_pipeline.paper.broker import ExitTranche

        no_legs = MagicMock(name="oco_parent_no_legs")
        no_legs.id = "tp-2"
        no_legs.legs = []
        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-1", "sl-1"),
            no_legs,
        ]
        client = self._build_client()
        with self.assertRaises(AlpacaClientError) as cm:
            client.attach_exit_ladder(
                symbol="NVDA",
                tranches=[
                    ExitTranche(qty=4, take_profit_limit=120.0),
                    ExitTranche(qty=3, take_profit_limit=130.0),
                ],
                stop_price=90.0,
            )
        # The loop aborts on tranche #2; both submits fired.
        self.assertEqual(self._trading_instance().submit_order.call_count, 2)
        self.assertIn("#1", str(cm.exception))
        # ALL-OR-NOTHING: the no-legs parent (tp-2) AND the already-live tranche
        # #1 (tp-1) are both cancelled so nothing stays live at the broker.
        canceled = [c.args[0] for c in self._trading_instance().cancel_order_by_id.call_args_list]
        self.assertIn("tp-1", canceled)
        self.assertIn("tp-2", canceled)


class TestAttachExitLadderAllOrNothing(_FakeAlpacaTestCase):
    """ALL-OR-NOTHING (Q2): a mid-ladder failure must cancel every OCO group
    already placed this call before re-raising, so a failed attach leaves
    NOTHING live at the broker (matching the zero ledger rows the caller
    persists on failure → a clean, duplicate-free retry)."""

    def _build_client(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        return AlpacaClient(api_key="k", secret_key="s")

    def _trading_instance(self):
        return self.fake_client_mod.TradingClient.return_value

    def test_third_tranche_submit_raises_cancels_two_placed_groups(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClientError
        from alphalens_pipeline.paper.broker import ExitTranche

        # Tranches #0 and #1 place valid OCO groups; tranche #2's submit raises.
        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-0", "sl-0"),
            _oco_parent_stub("tp-1", "sl-1"),
            RuntimeError("alpaca 403: rejected mid-ladder"),
        ]
        client = self._build_client()
        with self.assertRaises(AlpacaClientError):
            client.attach_exit_ladder(
                symbol="NVDA",
                tranches=[
                    ExitTranche(qty=4, take_profit_limit=120.0),
                    ExitTranche(qty=3, take_profit_limit=130.0),
                    ExitTranche(qty=2, take_profit_limit=140.0),
                ],
                stop_price=90.0,
            )
        # The two already-placed OCO parents were cancelled (rollback).
        canceled = [c.args[0] for c in self._trading_instance().cancel_order_by_id.call_args_list]
        self.assertEqual(sorted(canceled), ["tp-0", "tp-1"])

    def test_rollback_cancel_failure_does_not_mask_original_error(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClientError
        from alphalens_pipeline.paper.broker import ExitTranche

        self._trading_instance().submit_order.side_effect = [
            _oco_parent_stub("tp-0", "sl-0"),
            RuntimeError("original mid-ladder error"),
        ]
        # The rollback cancel ALSO fails — must not mask the original error.
        self._trading_instance().cancel_order_by_id.side_effect = RuntimeError("cancel also failed")
        client = self._build_client()
        with self.assertRaises(AlpacaClientError) as cm:
            client.attach_exit_ladder(
                symbol="NVDA",
                tranches=[
                    ExitTranche(qty=4, take_profit_limit=120.0),
                    ExitTranche(qty=3, take_profit_limit=130.0),
                ],
                stop_price=90.0,
            )
        # The propagated error is the ORIGINAL mid-ladder failure, not the
        # cancel failure.
        self.assertIn("original mid-ladder error", str(cm.exception))

    def test_empty_legs_parent_raises_cannot_capture_sl_id(self):
        # Given a parent with NO legs (SL id uncapturable).
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClientError
        from alphalens_pipeline.paper.broker import ExitTranche

        parent = MagicMock(name="oco_parent_no_legs")
        parent.id = "tp-1"
        parent.legs = []
        self._trading_instance().submit_order.return_value = parent

        client = self._build_client()
        # When attaching — Then it raises rather than silently dropping the SL.
        with self.assertRaises(AlpacaClientError) as cm:
            client.attach_exit_ladder(
                symbol="NVDA",
                tranches=[ExitTranche(qty=4, take_profit_limit=120.0)],
                stop_price=90.0,
            )
        self.assertIn("stop-loss", str(cm.exception).lower())


class TestExitTrancheWholeShareInvariant(unittest.TestCase):
    """The whole-share + positivity invariant is venue-independent and is
    enforced at the intent layer (``ExitTranche.__post_init__``), not deferred
    to an Alpaca 422.
    """

    def test_whole_int_qty_accepted(self):
        from alphalens_pipeline.paper.broker import ExitTranche

        self.assertEqual(ExitTranche(qty=4, take_profit_limit=120.0).qty, 4)

    def test_fractional_qty_rejected(self):
        from alphalens_pipeline.paper.broker import ExitTranche

        with self.assertRaises(TypeError):
            ExitTranche(qty=4.0, take_profit_limit=120.0)
        with self.assertRaises(TypeError):
            ExitTranche(qty=4.5, take_profit_limit=120.0)

    def test_zero_and_negative_qty_rejected(self):
        from alphalens_pipeline.paper.broker import ExitTranche

        with self.assertRaises(ValueError):
            ExitTranche(qty=0, take_profit_limit=120.0)
        with self.assertRaises(ValueError):
            ExitTranche(qty=-3, take_profit_limit=120.0)

    def test_bool_qty_rejected(self):
        # bool is an int subclass; True must NOT masquerade as a 1-share qty.
        from alphalens_pipeline.paper.broker import ExitTranche

        with self.assertRaises(TypeError):
            ExitTranche(qty=True, take_profit_limit=120.0)


class TestBrokerClientConformanceForExitLadder(unittest.TestCase):
    """A pure-Python fake implementing ``attach_exit_ladder`` satisfies the
    protocol; a stub missing it fails the runtime_checkable name check.
    """

    def test_fake_broker_with_attach_exit_ladder_passes_isinstance(self):
        from alphalens_pipeline.paper.broker import BrokerClient, ExitLadderLeg

        class _FullBroker:
            def submit_limit_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_stop_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_market_order(self, **kw):  # pragma: no cover - shape only
                ...

            def get_account(self):  # pragma: no cover - shape only
                ...

            def get_position(self, symbol):  # pragma: no cover - shape only
                ...

            def get_order(self, order_id):  # pragma: no cover - shape only
                ...

            def cancel_order(self, order_id):  # pragma: no cover - shape only
                ...

            def list_open_orders(self):  # pragma: no cover - shape only
                ...

            def list_positions(self):  # pragma: no cover - shape only
                ...

            def attach_exit_ladder(
                self, *, symbol, tranches, stop_price, time_in_force="gtc"
            ) -> list[ExitLadderLeg]:  # pragma: no cover - shape only
                return []

        self.assertIsInstance(_FullBroker(), BrokerClient)

    def test_stub_missing_attach_exit_ladder_fails_isinstance(self):
        from alphalens_pipeline.paper.broker import BrokerClient

        class _PartialBroker:
            def submit_limit_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_stop_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_market_order(self, **kw):  # pragma: no cover - shape only
                ...

            def get_account(self):  # pragma: no cover - shape only
                ...

            def get_position(self, symbol):  # pragma: no cover - shape only
                ...

            def get_order(self, order_id):  # pragma: no cover - shape only
                ...

            def cancel_order(self, order_id):  # pragma: no cover - shape only
                ...

            def list_open_orders(self):  # pragma: no cover - shape only
                ...

            def list_positions(self):  # pragma: no cover - shape only
                ...

            # Deliberately NO attach_exit_ladder.

        self.assertFalse(isinstance(_PartialBroker(), BrokerClient))


if __name__ == "__main__":
    unittest.main()
