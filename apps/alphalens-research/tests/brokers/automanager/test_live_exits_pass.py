"""Hermetic tests for the live TP-tranche exit tick phase (INC-5 Task 2):
``_build_managed_exits``, the ``ALPHALENS_LIVE_MARKET_EXITS`` flag, and
``_run_live_exits_pass`` -- behind the flag, default OFF.

With the flag OFF (or ALLOW_ORDERS off -- see the module docstring on
``_live_exits_orders_allowed``) the pass never builds a ``ManagedExit`` and
never calls ``run_live_exits`` -- a byte-identical no-op to today's tick."""

from __future__ import annotations

import datetime as dt
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from broker_contract.contract import InstrumentRef, Position
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker

_ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
_LIVE_EXITS_ENV = "ALPHALENS_LIVE_MARKET_EXITS"


def _tr(index: int, target: float, pct: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_pct=pct,
        r_multiple=1.0,
        tag=f"tp{index + 1}",
    )


def _mk_pos(*, uic: int, qty: float, ticker: str = "KO") -> Position:
    return Position(
        instrument=InstrumentRef(
            ticker=ticker,
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(uic),
            broker_symbol=f"{ticker}:xnys",
        ),
        quantity=qty,
        avg_price=15.0,
        market_value=None,
        unrealized_pnl=None,
        position_id=f"pos-{uic}",
    )


class _FakeFeed:
    def __init__(self, prices: dict[int, float | None]) -> None:
        self._p = prices

    def latest(self, uic: int) -> PricePoint | None:
        px = self._p.get(uic)
        return (
            None
            if px is None
            else PricePoint(uic=uic, price=px, asof=dt.datetime(2026, 8, 5, tzinfo=dt.UTC))
        )


def _deps(
    broker: object,
    *,
    alerts: list[str],
    live_exits_feed_factory: object = None,
) -> cl.LoopDeps:
    return cl.LoopDeps(
        broker=broker,  # type: ignore[arg-type]
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter(()),
        place_pick=lambda pick: False,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: cl.BrokerView(working_children={}),
        build_protection_view=cl.build_protection_view,
        execute_protection=lambda action, kill, report: None,
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: alerts.append(msg),  # noqa: PLW0108
        alert_throttled=lambda msg, reason: alerts.append(msg) or True,
        live_exits_feed_factory=live_exits_feed_factory,  # type: ignore[arg-type]
    )


class TestLiveMarketExitsEnabledGate(unittest.TestCase):
    def test_env_name_pinned(self) -> None:
        self.assertEqual(cl._LIVE_MARKET_EXITS_ENV, _LIVE_EXITS_ENV)

    def test_unset_defaults_to_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_LIVE_EXITS_ENV, None)
            self.assertFalse(cl._live_market_exits_enabled())

    def test_set_to_1_enables(self) -> None:
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1"}):
            self.assertTrue(cl._live_market_exits_enabled())

    def test_set_to_other_value_stays_disabled(self) -> None:
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "true"}):
            self.assertFalse(cl._live_market_exits_enabled())


class TestBuildManagedExits(unittest.TestCase):
    def test_a_uic_with_a_tranche_plan_and_a_live_long_becomes_one_managed_exit(self) -> None:
        pos = _mk_pos(uic=486, qty=100.0)
        tranches = (_tr(0, 16.0, 0.5),)
        tranche_plans = {486: (tranches, 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos], tranche_plans=tranche_plans, fired={}
        )
        self.assertEqual(len(managed), 1)
        m = managed[0]
        self.assertEqual(m.uic, 486)
        self.assertEqual(m.tp_tranches, tranches)
        self.assertEqual(m.reference_qty, 100.0)
        self.assertEqual(m.stop_price, 13.0)
        self.assertEqual(m.already_fired, frozenset())

    def test_a_uic_with_no_tranche_plan_is_skipped(self) -> None:
        pos = _mk_pos(uic=999, qty=50.0)
        managed = cl._build_managed_exits(long_positions=[pos], tranche_plans={}, fired={})
        self.assertEqual(managed, [])

    def test_fired_tags_flow_into_already_fired(self) -> None:
        pos = _mk_pos(uic=486, qty=50.0)
        tranche_plans = {486: ((_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)), 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos], tranche_plans=tranche_plans, fired={486: frozenset({"tp1"})}
        )
        self.assertEqual(managed[0].already_fired, frozenset({"tp1"}))

    def test_mixed_managed_and_skipped_positions(self) -> None:
        managed_pos = _mk_pos(uic=1, qty=10.0)
        skipped_pos = _mk_pos(uic=2, qty=20.0)
        tranche_plans = {1: ((_tr(0, 5.0, 1.0),), 10.0, 4.0)}
        managed = cl._build_managed_exits(
            long_positions=[managed_pos, skipped_pos], tranche_plans=tranche_plans, fired={}
        )
        self.assertEqual([m.uic for m in managed], [1])


class _JournalCase(unittest.TestCase):
    """Base case wiring a temp STANDALONE_STOP_JOURNAL_PATH per test."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        journal = Path(self._tmp.name) / "standalone_stops.jsonl"
        patch = mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal)
        patch.start()
        self.addCleanup(patch.stop)

    def _seed_tranche_plan(
        self,
        uic: int,
        *,
        tranches: tuple[TpTranchePlan, ...],
        reference_qty: float,
        stop_price: float,
    ) -> None:
        cl._append_standalone_stop_journal(
            cl._build_tranche_plan_line(
                uic=uic, tp_tranches=tranches, reference_qty=reference_qty, stop_price=stop_price
            )
        )


class TestLiveExitsFlagOff(_JournalCase):
    def test_flag_off_never_builds_or_calls_the_engine(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({uic: 16.5})
        )
        with mock.patch.dict(os.environ, {_ALLOW_ORDERS_ENV: "1"}, clear=False):
            os.environ.pop(_LIVE_EXITS_ENV, None)
            with mock.patch.object(cl, "run_live_exits") as spy:
                report = cl.TickReport()
                cl._run_live_exits_pass(deps, [], report)
                spy.assert_not_called()
                self.assertEqual(report.exits_placed, 0)
        # No broker mutation at all: position + resting stop untouched.
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 100.0)
        sl = next(o for o in broker.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl.amount, 100.0)
        self.assertEqual(alerts, [])


class TestLiveExitsOrdersDisabledGate(_JournalCase):
    def test_flag_on_but_orders_disabled_is_a_clean_no_op(self) -> None:
        # The engine's full-close branch cancels the standalone SL (ungated)
        # immediately before a gated market-sell -- with ALLOW_ORDERS off the
        # whole pass must no-op rather than risk a naked cancel-then-raise.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 1.0),), reference_qty=100.0, stop_price=13.0
        )
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({uic: 16.5})
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "0"}):
            with mock.patch.object(cl, "run_live_exits") as spy:
                cl._run_live_exits_pass(deps, [], cl.TickReport())
                spy.assert_not_called()
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 100.0)


class TestLiveExitsFlagOnFires(_JournalCase):
    def test_a_touched_tranche_fires_and_shrinks_the_sl(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({uic: 16.5})
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, [], report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 50.0)
        sl = next(o for o in broker.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl.amount, 50.0)
        self.assertEqual(report.exits_placed, 1)
        fired = [
            line
            for line in cl._iter_standalone_stop_journal()
            if line.get("kind") == "tranche_fired"
        ]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["uic"], uic)
        self.assertEqual(fired[0]["tag"], "tp1")

    def test_stale_feed_vetoes_all_fires(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({uic: None})
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, [], report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 100.0)
        self.assertEqual(report.exits_placed, 0)

    def test_an_already_fired_tranche_is_not_refired(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 50, avg_price=15.0)
        broker.add_resting_sell("KO", 50, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        cl._append_standalone_stop_journal({"kind": "tranche_fired", "uic": uic, "tag": "tp1"})
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({uic: 16.5})
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, [], report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 50.0)
        self.assertEqual(report.exits_placed, 0)

    def test_no_managed_positions_is_a_quiet_no_op(self) -> None:
        broker = FakeBroker()
        alerts: list[str] = []
        deps = _deps(broker, alerts=alerts, live_exits_feed_factory=lambda m: _FakeFeed({}))
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            with mock.patch.object(cl, "run_live_exits") as spy:
                cl._run_live_exits_pass(deps, [], cl.TickReport())
                spy.assert_not_called()


class TestLiveExitsRunsBeforeProtection(unittest.TestCase):
    def test_live_exits_pass_runs_before_protection_pass(self) -> None:
        order: list[str] = []
        broker = FakeBroker()
        alerts: list[str] = []
        deps = _deps(broker, alerts=alerts)
        with (
            mock.patch.object(
                cl, "_run_live_exits_pass", side_effect=lambda *a, **k: order.append("live_exits")
            ),
            mock.patch.object(
                cl, "_run_protection_pass", side_effect=lambda *a, **k: order.append("protection")
            ),
        ):
            cl.run_once(deps)
        self.assertEqual(order, ["live_exits", "protection"])


if __name__ == "__main__":
    unittest.main()
