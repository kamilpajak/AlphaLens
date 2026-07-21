"""Hermetic tests for the pluggable fill-source (polling implementation).

PollingFillSource diffs successive reconcile snapshots and emits one Transition
per entry the FIRST time its status enters a reportable state (FILLED /
PARTIALLY_FILLED / a terminal cancel/reject/expire). A plain WORKING entry —
including WORKING(PAST-TTL!) — emits NOTHING: TTL divergence is an alert the
reconcile-bridge/position-manager owns, never a fill signal.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.fill_source import (
    FillSource,
    PollingFillSource,
)
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus

_TS = "2026-07-06T18:00:00+00:00"


def _order_state(order_id: str, status: OrderStatus, *, filled: float = 0.0) -> OrderState:
    return OrderState(
        order_id=order_id, status=status, instrument=None, filled_quantity=filled, raw_status=""
    )


class _StubBroker:
    name = "stub"

    def __init__(self) -> None:
        self.open_orders: list[OrderState] = []
        self.outcomes: dict[str, OrderState] = {}
        self.open_refs: list[str] = []
        self.closed_rows: list[dict[str, Any]] = []

    def list_open_orders(self) -> list[OrderState]:
        return list(self.open_orders)

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return self.outcomes.get(order_id, _order_state(order_id, OrderStatus.UNKNOWN))

    def get_open_position_references(self) -> list[str]:
        return list(self.open_refs)

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return list(self.closed_rows)


def _record(**overrides: Any) -> dict[str, Any]:
    bracket: dict[str, Any] = {
        "client_request_id": "rid-1",
        "entry_order_id": "E-1",
        "exit_order_ids": ["T-1", "S-1"],
        "qty": 2,
        "entry": 82.0,
        "stop": 78.0,
        "tp": 90.0,
        "ttl": 5,
    }
    record: dict[str, Any] = {
        "execution_config_version": "execution-v1-test",
        "ts": _TS,
        "brief_date": "2026-07-06",
        "ticker": "KO",
        "mic": "XNYS",
        "uic": "307",
        "brackets": [bracket],
        "precheck": [],
    }
    record.update(overrides)
    return record


class PollingFillSourceProtocolTests(unittest.TestCase):
    def test_is_a_fill_source(self) -> None:
        self.assertIsInstance(PollingFillSource(_StubBroker(), list), FillSource)


class PollingFillSourceTransitionTests(unittest.TestCase):
    def test_working_then_filled_emits_one_filled_transition(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        self.assertEqual(source.poll_tick(), [])
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.FILLED, filled=2.0)}
        broker.open_refs = ["rid-1"]
        transitions = source.poll_tick()
        self.assertEqual(len(transitions), 1)
        t = transitions[0]
        self.assertEqual(t.order_id, "E-1")
        self.assertEqual(t.kind, "FILLED")
        self.assertEqual(t.ticker, "KO")
        self.assertEqual(t.filled_quantity, 2.0)
        self.assertIsNotNone(t.verdict)

    def test_filled_state_does_not_re_emit_on_the_next_tick(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.FILLED, filled=2.0)}
        broker.open_refs = ["rid-1"]
        self.assertEqual(len(source.poll_tick()), 1)
        self.assertEqual(source.poll_tick(), [])

    def test_working_then_expired_emits_terminal(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        source.poll_tick()
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.EXPIRED)}
        transitions = source.poll_tick()
        self.assertEqual([t.kind for t in transitions], ["TERMINAL"])
        self.assertEqual(transitions[0].order_id, "E-1")

    def test_past_ttl_working_is_not_a_transition(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()], today=dt.date(2026, 7, 17))
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        self.assertEqual(source.poll_tick(), [])


if __name__ == "__main__":
    unittest.main()
