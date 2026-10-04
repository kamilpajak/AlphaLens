"""#1514 end to end: a trailed stop keeps its level through a take-profit fire.

Driven through the real ``run_once`` tick against the fake broker. A pick with a
two-tranche ladder and a declared trail runs up, the trail raises the stop from
44.00 to 55.994, and TP1 sells half. Before #1514 the protection pass then put
the remaining stop back at 44.00 on several paths, and the ratchet kept it there
until price made a new high. Each case below is one of those paths.
"""

from __future__ import annotations

import os
import unittest

from alphalens_pipeline.brokers.automanager import stop_journal as sj
from broker_contract.contract import PlacedOrder
from broker_contract.sizing import TpTranchePlan
from broker_contract.trade_intent.schema import TrailingStop

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from tests.brokers.automanager.acceptance.world import ManagerWorld
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

_ENTRY, _PLAN_STOP = 50.0, 44.0  # 1R = 6, the trail arms at 53
_RUN_UP = 59.99  # trail target = 50 + 0.6 * (59.99 - 50) = 55.994
_TRAILED = 55.994
_TP1 = 60.0
_PULLBACK = 58.0  # below the old peak: the trail cannot re-raise a lost level here
_DECLARED_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)
_LADDER = (
    TpTranchePlan(tranche_index=0, target_price=_TP1, tranche_frac=0.5, r_multiple=1.0, tag="tp1"),
    TpTranchePlan(tranche_index=1, target_price=90.0, tranche_frac=0.5, r_multiple=2.0, tag="tp2"),
)


class _LaggingBroker(FakeBroker):
    """A fake whose reads can trail its writes, the way Saxo's can.

    ``positions_lag``: a market SELL is accepted but the positions read keeps
    the old quantity until ``settle()``. ``stale_orders_after_next_amend``: the
    orders read returns the pre-amend snapshot once after the next amend."""

    def __init__(self) -> None:
        super().__init__()
        self.positions_lag = False
        self.stale_orders_after_next_amend = False
        self._unsettled: list[tuple[int, float]] = []
        self._stale_snapshot: list[object] | None = None

    def place_market_order(self, uic, side, qty, request_id=None):  # type: ignore[override]
        if self.positions_lag and side == "SELL":
            self._seq += 1
            self._unsettled.append((uic, float(qty)))
            return PlacedOrder(entry_order_id=f"mkt-{self._seq}", exit_order_ids=())
        return super().place_market_order(uic, side, qty, request_id)

    def amend_stop_amount(self, uic, order_id, side, order_type, new_qty, stop_price, request_id):  # type: ignore[override]
        if self.stale_orders_after_next_amend:
            self._stale_snapshot = list(super().list_working_sell_orders())
            self.stale_orders_after_next_amend = False
        return super().amend_stop_amount(
            uic, order_id, side, order_type, new_qty, stop_price, request_id
        )

    def list_working_sell_orders(self):  # type: ignore[override]
        if self._stale_snapshot is not None:
            snapshot, self._stale_snapshot = self._stale_snapshot, None
            return snapshot  # type: ignore[return-value]
        return super().list_working_sell_orders()

    def settle(self) -> None:
        unsettled, self._unsettled = self._unsettled, []
        lag, self.positions_lag = self.positions_lag, False
        for uic, qty in unsettled:
            super().place_market_order(uic, "SELL", qty)
        self.positions_lag = lag


class _TrailedPosition(IsolatedHomeTestCase):
    amend_enabled = True

    def setUp(self) -> None:
        super().setUp()
        self.world = ManagerWorld(self)
        self.broker = _LaggingBroker()
        self.world.broker = self.broker
        self.world.live_exits_are_enabled()
        if self.amend_enabled:
            self.world.amend_is_enabled()
        else:
            # The flag gates only the resize arms; the trail still PATCHes
            # because the production broker implements the amend capability.
            os.environ.pop("ALPHALENS_BROKER_AMEND_ENABLED", None)
            self.world._amend_enabled = True
        self.uic = self.broker.uic_of("KO")
        self.broker.set_position("KO", 100, avg_price=_ENTRY)
        self.world._seed_plan("KO", stop=_PLAN_STOP, take_profit=None, exit_policy=_DECLARED_TRAIL)
        sj._append_standalone_stop_journal(
            sj._build_tranche_plan_line(
                uic=self.uic, tp_tranches=_LADDER, reference_qty=100, stop_price=_PLAN_STOP
            )
        )
        self.world.has_resting_stop("KO", shares=100, price=_PLAN_STOP)
        self.world.price_rises_to("KO", _RUN_UP)
        self.world.run_tick()
        self.assertEqual(self._stops(), [(100.0, _TRAILED)])  # the trail fired

    def _stops(self) -> list[tuple[float | None, float | None]]:
        return [
            (o.amount, None if o.resting_price is None else round(o.resting_price, 3))
            for o in self.broker.list_working_sell_orders()
            if o.uic == self.uic and o.order_type == "StopIfTraded"
        ]

    def _fire_tp1_then_pull_back(self) -> None:
        self.world.price_is("KO", _TP1)
        self.world.run_tick()
        for _ in range(3):
            self.broker.settle()
            self.world.price_is("KO", _PULLBACK)
            self.world.run_tick()

    def assert_one_stop_at_the_trailed_level(self) -> None:
        self.assertEqual(self.world.owned("KO"), 50)
        self.assertEqual(self._stops(), [(50.0, _TRAILED)])


class TestWithAmendsOn(_TrailedPosition):
    def test_reads_that_agree(self) -> None:
        self._fire_tp1_then_pull_back()
        self.assert_one_stop_at_the_trailed_level()

    def test_a_positions_read_that_lags_the_sell(self) -> None:
        self.broker.positions_lag = True
        self._fire_tp1_then_pull_back()
        self.assert_one_stop_at_the_trailed_level()

    def test_a_stale_orders_read_after_the_take_profit_amend(self) -> None:
        self.broker.stale_orders_after_next_amend = True
        self._fire_tp1_then_pull_back()
        self.assert_one_stop_at_the_trailed_level()

    def test_a_later_entry_tier_fills_after_the_trail(self) -> None:
        self.broker.set_position("KO", 150, avg_price=_ENTRY)
        self.world.price_is("KO", _PULLBACK)
        self.world.run_tick()
        self.assertEqual(self._stops(), [(150.0, _TRAILED)])

    def test_a_cancelled_stop_comes_back_at_the_plan_and_says_so(self) -> None:
        stop_id = next(o.order_id for o in self.broker.list_working_sell_orders())
        self.broker.cancel_order(stop_id)
        self.world.price_is("KO", _PULLBACK)
        self.world.run_tick()
        self.assertEqual(self._stops(), [(100.0, _PLAN_STOP)])
        self.assertTrue(
            any("not restored" in alert and "55.99" in alert for alert in self.world.alerts),
            self.world.alerts,
        )


class TestWithAmendsOff(_TrailedPosition):
    amend_enabled = False

    def test_a_positions_read_that_lags_the_sell(self) -> None:
        self.broker.positions_lag = True
        self._fire_tp1_then_pull_back()
        self.assert_one_stop_at_the_trailed_level()


if __name__ == "__main__":
    unittest.main()
