"""Unit tests for ManagerService / InProcessManagerService (PR-8).

Wires a real InProcessManagerService against the acceptance suite's
FakeBroker + a real LoopDeps assembly — mirroring ``acceptance/world.py``'s
deps-building pattern, but standalone (no dependency on ``ManagerWorld``
itself, so this file stays a plain unit test of the service transport, not a
second acceptance harness).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.service import (
    AlertEvent,
    InProcessManagerService,
    LivenessEvent,
    TickReportEvent,
)
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    InstrumentHint,
    IntentMeta,
    TradeIntent,
    TradeSpec,
)

from .acceptance.fake_broker import FakeBroker

_DEFAULT_STOP = 44.0
_DEFAULT_TP = 59.0
_DEFAULT_FILL = 50.0


class _Chain:
    """A tiny session-chain status object (what ensure_alive returns)."""

    def __init__(self, *, alive: bool = True, reason: str = "") -> None:
        self.alive = alive
        self.reason = reason


def _pick(ticker: str) -> TradeIntent:
    """A minimal, structurally valid armed TradeIntent (mirrors
    acceptance/world.py's ``_pick`` helper)."""
    return TradeIntent(
        intent_id=f"{ticker.upper()}:2026-07-31",
        instrument=InstrumentHint(ticker=ticker.upper(), mic="XNYS"),
        spec=TradeSpec(
            entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=100.0),),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=3.0,
        ),
        meta=IntentMeta(armed_ts="2026-07-31T00:00:00+00:00", brief_date="2026-07-31"),
    )


class _ServiceHarness:
    """Builds a real InProcessManagerService wired to a FakeBroker.

    Same shape as ``acceptance.world.ManagerWorld._deps`` (real
    ``build_protection_view`` + real protection executor against the fake
    broker, no journal/env I/O leaking between tests), rebuilt here so this
    unit-test file does not depend on the acceptance ``ManagerWorld`` class
    itself.
    """

    def __init__(self, test: unittest.TestCase, *, equity: float = 1_000_000.0) -> None:
        self.broker = FakeBroker(equity=equity)
        self.underlying_alerts: list[str] = []
        self.placed: list[TradeIntent] = []
        # The tee lives at the shared _AlertThrottle's BASE sink, not on the
        # deps.alert_throttled FIELD: the protection executor
        # (_make_protection_executor) calls throttle.emit(...) directly on
        # this SAME instance for PlaceStop/UpgradeToOco/AmendStop/AlertOnly,
        # bypassing deps.alert_throttled entirely — teeing the base sink is
        # the only seam that catches every throttled send uniformly. The
        # throttle itself is built ONCE (persists dedup/escalation state
        # across ticks, mirroring acceptance/world.py); the CURRENT
        # event-capturing closure is rebound on every _deps_factory call
        # (InProcessManagerService hands a fresh one each cycle).
        self._current_event_alert_throttled = lambda message, reason: None
        self._throttle = cl._AlertThrottle(self._throttle_base_sink)

        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.journal = root / "standalone_stops.jsonl"
        self.kill_file = root / "KILL"

        self._env = mock.patch.dict(os.environ, {"ALPHALENS_BROKER_ALLOW_ORDERS": "1"}, clear=False)
        self._env.start()
        self._journal_patch = mock.patch.object(
            cl, "_standalone_stop_journal_path", lambda: self.journal
        )
        self._journal_patch.start()
        test.addCleanup(self._close)

        self.service = InProcessManagerService(self._deps_factory)

    def _close(self) -> None:
        self._journal_patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _throttle_base_sink(self, message: str) -> None:
        self.underlying_alerts.append(message)
        self._current_event_alert_throttled(message, "")

    # ==== deps_factory (the one method InProcessManagerService calls) ==========

    def _deps_factory(
        self,
        event_alert,
        event_alert_throttled,
        picks,
    ) -> cl.LoopDeps:
        self._current_event_alert_throttled = event_alert_throttled

        def alert(message: str) -> None:
            self.underlying_alerts.append(message)
            event_alert(message)

        def alert_throttled(message: str, reason: str) -> bool:
            return self._throttle.emit(message, reason=reason)

        executor = cl._make_protection_executor(
            self.broker, self._throttle, place_oco_exit=None, amend_stop=None
        )
        return cl.LoopDeps(
            broker=self.broker,
            kill_file=self.kill_file,
            ensure_alive=lambda: _Chain(alive=True),
            iter_picks=lambda: iter(picks),
            place_pick=self._place_pick,
            read_records=list,
            verdicts_fn=lambda _records, _broker: [],
            build_position_view=lambda _broker, _records: cl.BrokerView(working_children={}),
            build_protection_view=cl.build_protection_view,
            execute_protection=executor,
            sweep_orphans_fn=lambda _broker: [],
            alert=alert,
            alert_throttled=alert_throttled,
        )

    def _place_pick(self, pick) -> bool:
        self.placed.append(pick)
        return True

    # ==== scenario helpers =======================================================

    def entry_fills(
        self,
        ticker: str,
        *,
        shares: float,
        price: float = _DEFAULT_FILL,
        stop: float = _DEFAULT_STOP,
        take_profit: float | None = _DEFAULT_TP,
    ) -> None:
        self.broker.set_position(ticker, shares, avg_price=price)
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid=f"crid-{ticker.upper()}",
                uic=self.broker.uic_of(ticker),
                side="SELL",
                stop_price=stop,
                take_profit=take_profit,
                tier_index=0,
            )
        )


class TestSubmitIntent(unittest.TestCase):
    def test_submit_intent_returns_an_armed_ack_matching_the_intent_id(self) -> None:
        harness = _ServiceHarness(self)
        intent = _pick("KO")

        ack = harness.service.submit_intent(intent)

        self.assertEqual(ack.status, "armed")
        self.assertEqual(ack.intent_id, intent.intent_id)
        self.assertIsNone(ack.reason)


class TestRunCycle(unittest.TestCase):
    def test_run_cycle_drives_run_once_and_places_a_submitted_pick(self) -> None:
        harness = _ServiceHarness(self)
        harness.service.submit_intent(_pick("KO"))

        report = harness.service.run_cycle()

        self.assertEqual(report.picks_placed, 1)
        self.assertEqual(len(harness.placed), 1)
        self.assertEqual(harness.placed[0].instrument.ticker, "KO")


class TestStreamEvents(unittest.TestCase):
    def test_quiet_healthy_cycle_yields_no_alert_events(self) -> None:
        harness = _ServiceHarness(self)

        harness.service.run_cycle()
        events = list(harness.service.stream_events())

        alert_events = [e for e in events if isinstance(e, AlertEvent)]
        self.assertEqual(alert_events, [])
        self.assertTrue(any(isinstance(e, TickReportEvent) for e in events))
        self.assertTrue(any(isinstance(e, LivenessEvent) for e in events))

    def test_a_degrade_yields_an_alert_event(self) -> None:
        harness = _ServiceHarness(self)
        harness.entry_fills("KO", shares=100)
        harness.broker.failing_uics.add(harness.broker.uic_of("KO"))

        harness.service.run_cycle()
        events = list(harness.service.stream_events())

        alert_events = [e for e in events if isinstance(e, AlertEvent)]
        self.assertTrue(alert_events, "expected at least one alert event on a broker-write failure")
        self.assertTrue(any("failed" in e.message.lower() for e in alert_events))

    def test_stream_events_drains_fifo_and_clears(self) -> None:
        harness = _ServiceHarness(self)
        harness.entry_fills("KO", shares=100)
        harness.broker.failing_uics.add(harness.broker.uic_of("KO"))
        harness.service.run_cycle()

        first_drain = list(harness.service.stream_events())
        second_drain = list(harness.service.stream_events())

        self.assertTrue(first_drain, "expected the first drain to carry the tick's events")
        self.assertEqual(second_drain, [])


class TestQueryState(unittest.TestCase):
    def test_query_state_projects_a_covered_long_position(self) -> None:
        harness = _ServiceHarness(self)
        harness.entry_fills("KO", shares=100, stop=44.0, take_profit=59.0)
        harness.broker.add_resting_sell("KO", 100, 44.0, order_type="StopIfTraded", relation="Oco")
        harness.broker.add_resting_sell("KO", 100, 59.0, order_type="Limit", relation="Oco")

        states = harness.service.query_state()

        ko_states = [s for s in states if s.symbol == "KO"]
        self.assertEqual(len(ko_states), 1)
        state = ko_states[0]
        self.assertEqual(state.owned_qty, 100)
        self.assertEqual(state.covered_qty, 100)
        self.assertEqual(state.stop_price, 44.0)
        self.assertEqual(state.tp_price, 59.0)
        self.assertFalse(state.terminal)

    def test_query_state_projects_a_naked_orphan_long(self) -> None:
        harness = _ServiceHarness(self)
        harness.broker.set_position("KO", 100, avg_price=50.0)  # no plan, no resting stop

        states = harness.service.query_state()

        ko_states = [s for s in states if s.symbol == "KO"]
        self.assertEqual(len(ko_states), 1)
        state = ko_states[0]
        self.assertEqual(state.owned_qty, 100)
        self.assertEqual(state.covered_qty, 0)
        self.assertIsNone(state.stop_price)
        self.assertIsNone(state.tp_price)

    def test_query_state_is_a_pure_read(self) -> None:
        harness = _ServiceHarness(self)
        harness.entry_fills("KO", shares=100)

        before = len(harness.broker.list_open_orders())
        harness.service.query_state()
        after = len(harness.broker.list_open_orders())

        self.assertEqual(before, after)
        self.assertEqual(harness.placed, [])


if __name__ == "__main__":
    unittest.main()
