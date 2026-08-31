"""Stop-fill reconcile pass (#1219): a standalone stop that FILLED at the broker
must journal ONE terminal ``stop_filled`` line and send ONE operator alert.

Mirrors the entry-side reconcile suite (``test_entry_watch_reconcile.py``) but —
unlike that twin — also asserts on the ALERT side: the silent-exit gap this pass
closes was precisely a missing notification, so every case pins both the journal
and the alert list.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import alphalens_pipeline.brokers.automanager.control_loop as cl
from broker_contract.contract import OrderState, OrderStatus

_UIC = 5555
_STOP_ID = "S-7727"
_REF = "GME-2026-08-27-entry-t0-stop-1"


def _stop_journal(test: unittest.TestCase) -> Path:
    tmp = TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "standalone_stops.jsonl"
    patcher = mock.patch.object(cl, "_standalone_stop_journal_path", lambda: path)
    patcher.start()
    test.addCleanup(patcher.stop)
    return path


def _seed(path: Path, *lines: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _stop_placed(
    *, uic: int = _UIC, order_id: str | None = _STOP_ID, ref: str | None = _REF, ts: float = 100.0
) -> dict[str, Any]:
    line: dict[str, Any] = {"kind": "stop_placed", "uic": uic, "qty": 16.0, "ts": ts}
    if order_id is not None:
        line["order_id"] = order_id
    if ref is not None:
        line["ref"] = ref
    return line


def _order(order_id: str, status: OrderStatus, *, filled: float = 0.0) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled,
        raw_status=status.value,
    )


class _StopBroker:
    """get_order answers WORKING while the id is in ``working``, UNKNOWN once it
    is not (the Saxo shape: dropped from the open-orders view); resolve returns
    the seeded terminal outcome and records each audit read."""

    def __init__(self, *, working: tuple[str, ...] = (), outcome: OrderState | None = None) -> None:
        self._working = set(working)
        self._outcome = outcome
        self.resolve_calls: list[str] = []

    def get_order(self, order_id: str) -> OrderState:
        if order_id in self._working:
            return _order(order_id, OrderStatus.WORKING)
        return _order(order_id, OrderStatus.UNKNOWN)

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        self.resolve_calls.append(order_id)
        assert self._outcome is not None, "resolve called without a seeded outcome"
        return self._outcome


def _deps(broker: Any, alerts: list[str], **over: Any) -> cl.LoopDeps:
    def _throttled(message: str, reason: str) -> bool:
        alerts.append(message)
        return True

    return cl.LoopDeps(
        broker=broker,
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter([]),
        place_pick=lambda pick: True,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: None,
        build_protection_view=lambda broker, records: None,
        execute_protection=lambda action, kill, report: None,
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: alerts.append(msg),  # noqa: PLW0108
        alert_throttled=_throttled,
        **over,
    )


def _run(deps: cl.LoopDeps) -> cl.TickReport:
    report = cl.TickReport()
    cl._run_stop_fill_reconcile_pass(deps, report)
    return report


class TestStopFillReconcile(unittest.TestCase):
    def test_filled_stop_journals_terminal_and_alerts_once(self) -> None:
        path = _stop_journal(self)
        _seed(path, _stop_placed())
        broker = _StopBroker(
            outcome=OrderState(
                order_id=_STOP_ID,
                status=OrderStatus.FILLED,
                instrument=None,
                filled_quantity=16.0,
                raw_status="Filled",
                avg_fill_price=18.51,
            )
        )
        alerts: list[str] = []
        report = _run(_deps(broker, alerts))

        filled = [ln for ln in _lines(path) if ln["kind"] == "stop_filled"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["uic"], _UIC)
        self.assertEqual(filled[0]["order_id"], _STOP_ID)
        self.assertEqual(filled[0]["qty"], 16.0)
        self.assertEqual(filled[0]["avg_price"], 18.51)
        self.assertIn("ts", filled[0])

        self.assertEqual(len(alerts), 1)
        self.assertIn("GME", alerts[0])
        self.assertIn(_STOP_ID, alerts[0])
        self.assertIn("16", alerts[0])
        self.assertIn("18.51", alerts[0])
        self.assertEqual(report.alerts, 1)

    def test_working_stop_is_left_alone(self) -> None:
        path = _stop_journal(self)
        _seed(path, _stop_placed())
        broker = _StopBroker(working=(_STOP_ID,))
        alerts: list[str] = []
        _run(_deps(broker, alerts))

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == "stop_filled"], [])
        self.assertEqual(alerts, [])
        self.assertEqual(broker.resolve_calls, [])

    def test_gone_unfilled_stop_stays_silent(self) -> None:
        # Rotation-race safety: a cancelled/expired stop (daily order rotation,
        # OCO upgrade supersede) must never fabricate a fill alarm.
        path = _stop_journal(self)
        _seed(path, _stop_placed())
        broker = _StopBroker(outcome=_order(_STOP_ID, OrderStatus.CANCELLED))
        alerts: list[str] = []
        _run(_deps(broker, alerts))

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == "stop_filled"], [])
        self.assertEqual(alerts, [])
        self.assertEqual(broker.resolve_calls, [_STOP_ID])

    def test_already_journaled_fill_is_idempotent_across_restart(self) -> None:
        path = _stop_journal(self)
        _seed(
            path,
            _stop_placed(),
            {"kind": "stop_filled", "uic": _UIC, "order_id": _STOP_ID, "qty": 16.0, "ts": 200.0},
        )
        broker = _StopBroker()  # a resolve call would assert (no outcome seeded)
        alerts: list[str] = []
        _run(_deps(broker, alerts))

        self.assertEqual(len([ln for ln in _lines(path) if ln["kind"] == "stop_filled"]), 1)
        self.assertEqual(alerts, [])
        self.assertEqual(broker.resolve_calls, [])

    def test_budget_exhausted_defers_to_next_tick(self) -> None:
        path = _stop_journal(self)
        _seed(path, _stop_placed())
        broker = _StopBroker(outcome=_order(_STOP_ID, OrderStatus.FILLED, filled=16.0))

        class _NoBudget:
            deferred = 0

            def try_acquire(self) -> bool:
                return False

            def note_deferred(self) -> None:
                self.deferred += 1

        alerts: list[str] = []
        _run(_deps(broker, alerts, audit_budget=_NoBudget()))

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == "stop_filled"], [])
        self.assertEqual(alerts, [])
        self.assertEqual(broker.resolve_calls, [])

    def test_pre_1219_record_without_order_id_is_skipped(self) -> None:
        path = _stop_journal(self)
        _seed(path, _stop_placed(order_id=None, ref=None))
        broker = _StopBroker()
        alerts: list[str] = []
        _run(_deps(broker, alerts))  # must not crash, resolve, or alert

        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == "stop_filled"], [])
        self.assertEqual(alerts, [])
        self.assertEqual(broker.resolve_calls, [])

    def test_non_resolving_broker_is_a_noop(self) -> None:
        path = _stop_journal(self)
        _seed(path, _stop_placed())

        class _Plain:
            pass

        alerts: list[str] = []
        _run(_deps(_Plain(), alerts))
        self.assertEqual([ln for ln in _lines(path) if ln["kind"] == "stop_filled"], [])
        self.assertEqual(alerts, [])


class TestFoldStandingStopIds(unittest.TestCase):
    def test_latest_stop_placed_wins_per_uic(self) -> None:
        fold = cl._fold_standing_stop_ids(
            [
                _stop_placed(order_id="S-old", ts=100.0),
                _stop_placed(order_id="S-new", ts=200.0),
            ]
        )
        self.assertEqual(fold[_UIC].order_id, "S-new")
        self.assertEqual(fold[_UIC].ref, _REF)

    def test_filled_order_id_is_excluded(self) -> None:
        fold = cl._fold_standing_stop_ids(
            [
                _stop_placed(),
                {"kind": "stop_filled", "uic": _UIC, "order_id": _STOP_ID, "ts": 200.0},
            ]
        )
        self.assertEqual(fold, {})

    def test_id_less_records_yield_no_candidate(self) -> None:
        fold = cl._fold_standing_stop_ids([_stop_placed(order_id=None)])
        self.assertEqual(fold, {})


class TestCompactorKeepsStopFilled(unittest.TestCase):
    def test_stop_filled_survives_compaction_and_fold_is_identical(self) -> None:
        lines = [
            _stop_placed(),
            {"kind": "stop_filled", "uic": _UIC, "order_id": _STOP_ID, "qty": 16.0, "ts": 200.0},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(lines)
        self.assertIn("stop_filled", {ln.get("kind") for ln in compacted})
        self.assertEqual(cl._fold_standing_stop_ids(lines), cl._fold_standing_stop_ids(compacted))


if __name__ == "__main__":
    unittest.main()
