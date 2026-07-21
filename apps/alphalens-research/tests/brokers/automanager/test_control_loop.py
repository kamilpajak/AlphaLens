"""Hermetic tests for control_loop.run_once / run_daemon.

Every Task 1-10 dependency is injected as a stub (build_default_deps is covered
by the SIM probe). Under test: kill-gate placement, always reconcile, execute
the position-manager Action, re-derive identical classification on restart.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.picks import Pick
from alphalens_pipeline.brokers.automanager.position_manager import BrokerView, DisasterStop
from alphalens_pipeline.brokers.contract import BrokerError
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

_RID = "rid-KO"


def _pick(ticker: str = "KO", date: str = "2026-07-20") -> Pick:
    return Pick(
        ticker=ticker,
        date=dt.date.fromisoformat(date),
        armed_ts="2026-07-20T14:00:00+00:00",
        status="armed",
    )


class _StubBroker:
    name = "stub"

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def _verdict(**over: Any) -> ReconcileVerdict:
    base: dict[str, Any] = {
        "brief_date": "2026-07-20",
        "ticker": "KO",
        "qty": 3,
        "entry_order_id": "E-1",
        "status": "WORKING",
        "verdict": "WORKING",
        "details": {"client_request_id": _RID},
    }
    base.update(over)
    return ReconcileVerdict(**base)


def _view() -> BrokerView:
    return BrokerView(
        protected_request_ids=frozenset(),
        disaster_stops={_RID: DisasterStop(uic=307, side="SELL", stop_price=79.0)},
        working_children={_RID: ("T-1",)},
    )


def _deps(
    broker: _StubBroker,
    *,
    kill_file: Path,
    verdicts: list[ReconcileVerdict],
    place_calls: list,
    stop_calls: list,
    alerts: list,
    picks: list | None = None,
    chain_alive: bool = True,
) -> cl.LoopDeps:
    return cl.LoopDeps(
        broker=broker,
        kill_file=kill_file,
        ensure_alive=lambda: type("C", (), {"alive": chain_alive, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter(picks or []),
        place_pick=lambda pick: place_calls.append(pick) or True,
        read_records=lambda: [{"brackets": [{"client_request_id": _RID}]}],
        verdicts_fn=lambda records, broker: list(verdicts),
        build_position_view=lambda broker, records: _view(),
        place_standalone_stop=lambda uic, side, qty, price, request_id: stop_calls.append(
            (uic, side, qty, price)
        ),
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: alerts.append(msg),  # noqa: PLW0108
    )


class TestRunOncePlacement(unittest.TestCase):
    def test_filled_open_places_standalone_stop_at_realized_qty(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            stop_calls: list = []
            v = _verdict(
                status="FILLED",
                verdict="FILLED",
                note="position open, exit orders working",
                details={"client_request_id": _RID, "filled_quantity": 2.0},
            )
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[v],
                place_calls=[],
                stop_calls=stop_calls,
                alerts=[],
            )
            report = cl.run_once(deps)
            self.assertEqual(stop_calls, [(307, "SELL", 2.0, 79.0)])
            self.assertEqual(report.stops_placed, 1)

    def test_standalone_stop_placer_receives_entry_request_id(self) -> None:
        # I2: the placer is handed the entry's client_request_id so the "placed"
        # journal line can correlate protection by request_id, not Uic.
        with TemporaryDirectory() as d:
            calls: list = []
            v = _verdict(
                status="FILLED",
                verdict="FILLED",
                note="position open, exit orders working",
                details={"client_request_id": _RID, "filled_quantity": 2.0},
            )
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[v],
                place_calls=[],
                stop_calls=[],
                alerts=[],
            )
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "place_standalone_stop": lambda uic, side, qty, price, request_id: calls.append(
                        (uic, side, qty, price, request_id)
                    ),
                }
            )
            cl.run_once(deps)
            self.assertEqual(calls, [(307, "SELL", 2.0, 79.0, _RID)])

    def test_drains_armed_pick_when_chain_alive_and_no_kill(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                stop_calls=[],
                alerts=[],
                picks=[pick],
            )
            cl.run_once(deps)
            self.assertEqual(place_calls, [pick])


class TestPickSubmissionJoin(unittest.TestCase):
    """C1: drain only picks NOT yet joined to submissions.jsonl (design §Data-flow
    step 4). Without the join the daemon re-places every armed pick every tick."""

    def test_pick_already_in_submissions_is_not_re_placed(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                stop_calls=[],
                alerts=[],
                picks=[_pick("KO", "2026-07-20")],
            )
            # A submissions record already carries this (ticker, brief_date).
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "read_records": lambda: [{"ticker": "KO", "brief_date": "2026-07-20"}],
                }
            )
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [])
            self.assertEqual(report.picks_placed, 0)

    def test_genuinely_new_pick_is_placed_once(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("MSFT", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                stop_calls=[],
                alerts=[],
                picks=[pick],
            )
            # Journal holds a DIFFERENT (ticker, brief_date) — no join match.
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "read_records": lambda: [{"ticker": "KO", "brief_date": "2026-07-20"}],
                }
            )
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [pick])
            self.assertEqual(report.picks_placed, 1)

    def test_same_ticker_different_brief_date_is_placed(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("KO", "2026-07-21")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                stop_calls=[],
                alerts=[],
                picks=[pick],
            )
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "read_records": lambda: [{"ticker": "KO", "brief_date": "2026-07-20"}],
                }
            )
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [pick])
            self.assertEqual(report.picks_placed, 1)

    def test_duplicate_armed_pick_in_one_tick_is_placed_once(self) -> None:
        # MEDIUM: already_submitted starts empty (no prior journal record) but the
        # first placement of a (ticker, brief_date) must suppress a second identical
        # armed line later in the SAME tick.
        with TemporaryDirectory() as d:
            place_calls: list = []
            p1 = _pick("KO", "2026-07-20")
            p2 = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                stop_calls=[],
                alerts=[],
                picks=[p1, p2],
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "read_records": list})
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [p1], "the duplicate armed line must be skipped")
            self.assertEqual(report.picks_placed, 1)


class TestStandaloneStopJournalFold(unittest.TestCase):
    """I2: standalone-stop protection is correlated by the entry's
    client_request_id, NOT its Uic. Two entries sharing one Uic each need their
    OWN placed stop — a Uic-keyed correlation would mark both protected the
    moment the first one's stop posts, leaving the second silently unprotected."""

    def test_two_entries_one_uic_only_the_placed_one_is_protected(self) -> None:
        lines = [
            {
                "kind": "planned",
                "client_request_id": "rid-A",
                "uic": 307,
                "side": "SELL",
                "stop_price": 79.0,
            },
            {
                "kind": "planned",
                "client_request_id": "rid-B",
                "uic": 307,
                "side": "SELL",
                "stop_price": 78.0,
            },
            {
                "kind": "placed",
                "client_request_id": "rid-A",
                "uic": 307,
                "side": "SELL",
                "qty": 2.0,
                "stop_price": 79.0,
                "order_id": "S-1",
            },
        ]
        disaster_stops, protected = cl._fold_standalone_stop_journal(lines)
        self.assertEqual(set(disaster_stops), {"rid-A", "rid-B"})
        self.assertEqual(protected, frozenset({"rid-A"}))

    def test_placed_line_without_request_id_protects_nothing(self) -> None:
        # A legacy Uic-only placed line must no longer confer protection.
        lines = [
            {
                "kind": "planned",
                "client_request_id": "rid-A",
                "uic": 307,
                "side": "SELL",
                "stop_price": 79.0,
            },
            {"kind": "placed", "uic": 307, "side": "SELL", "qty": 2.0, "stop_price": 79.0},
        ]
        disaster_stops, protected = cl._fold_standalone_stop_journal(lines)
        self.assertEqual(set(disaster_stops), {"rid-A"})
        self.assertEqual(protected, frozenset())

    def test_intent_line_marks_position_protected_in_flight(self) -> None:
        # HIGH-1: a crash between the standalone-stop POST and its "placed" journal
        # write leaves ONLY the "intent" line. It must confer protection so the
        # fold sees the position as protected/in-flight and advance does NOT
        # re-issue a second PlaceStandaloneStop.
        lines = [
            {
                "kind": "planned",
                "client_request_id": "rid-A",
                "uic": 307,
                "side": "SELL",
                "stop_price": 79.0,
            },
            {
                "kind": "intent",
                "client_request_id": "rid-A",
                "stop_request_id": "rid-A-stop",
                "uic": 307,
                "side": "SELL",
                "qty": 2.0,
                "stop_price": 79.0,
            },
        ]
        disaster_stops, protected = cl._fold_standalone_stop_journal(lines)
        self.assertEqual(set(disaster_stops), {"rid-A"})
        self.assertEqual(protected, frozenset({"rid-A"}))

    def test_advance_returns_noop_when_only_intent_line_present(self) -> None:
        # End-to-end of the crash window: with only planned + intent lines, a FILLED
        # verdict for that entry must yield NoOp, never a duplicate PlaceStandaloneStop.
        from alphalens_pipeline.brokers.automanager.position_manager import (
            BrokerView as PmBrokerView,
        )
        from alphalens_pipeline.brokers.automanager.position_manager import (
            NoOp,
            advance,
        )

        lines = [
            {
                "kind": "planned",
                "client_request_id": _RID,
                "uic": 307,
                "side": "SELL",
                "stop_price": 79.0,
            },
            {
                "kind": "intent",
                "client_request_id": _RID,
                "stop_request_id": f"{_RID}-stop",
                "uic": 307,
                "side": "SELL",
                "qty": 2.0,
                "stop_price": 79.0,
            },
        ]
        disaster_stops, protected = cl._fold_standalone_stop_journal(lines)
        view = PmBrokerView(
            protected_request_ids=protected,
            disaster_stops=disaster_stops,
            working_children={},
        )
        v = _verdict(
            status="FILLED",
            verdict="FILLED",
            note="position open, exit orders working",
            details={"client_request_id": _RID, "filled_quantity": 2.0},
        )
        self.assertIsInstance(advance(v, view), NoOp)


class TestStandaloneStopPlacerRecovery(unittest.TestCase):
    """HIGH-1: the placer journals an 'intent' line (with a DETERMINISTIC stop
    request_id derived from the entry request_id) BEFORE the POST, so a crash in
    the place-then-journal window is recoverable and cannot double-place."""

    def test_intent_journaled_before_post_with_deterministic_stop_request_id(self) -> None:
        import json

        class _StubStopBroker:
            def __init__(self) -> None:
                self.calls: list = []

            def place_standalone_stop(self, uic, side, qty, stop_price, request_id=None):
                self.calls.append((uic, side, qty, stop_price, request_id))
                return type("P", (), {"entry_order_id": "S-9", "exit_order_ids": ()})()

        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _StubStopBroker()
            placer = cl._make_standalone_stop_placer(broker)  # type: ignore[arg-type]
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                placer(307, "SELL", 2.0, 79.0, _RID)
            written = [
                json.loads(line) for line in journal.read_text().splitlines() if line.strip()
            ]
            kinds = [rec["kind"] for rec in written]
            self.assertEqual(kinds, ["intent", "placed"], "intent must be journaled BEFORE placed")
            stop_rid = f"{_RID}-stop"
            self.assertEqual(written[0]["client_request_id"], _RID)
            self.assertEqual(written[0]["stop_request_id"], stop_rid)
            # the deterministic stop request_id is passed into the broker (dedup)
            self.assertEqual(broker.calls, [(307, "SELL", 2.0, 79.0, stop_rid)])


class _CrashError(Exception):
    """A hard, non-BrokerError crash (models a process death / uncaught bug)."""


class TestPlacePickPerTierJournaling(unittest.TestCase):
    """HIGH-2: each tier's submission record is journaled IMMEDIATELY after its
    place_bracket_order, not batched after the whole loop. A crash mid-loop then
    leaves the pick already joined to submissions.jsonl (at most a partial
    ladder), so the pick-drain does NOT re-place the full set on restart."""

    def _run(self) -> Any:
        import contextlib

        submitted: list[dict[str, Any]] = []

        class _Placed:
            def __init__(self, oid: str) -> None:
                self.entry_order_id = oid
                self.exit_order_ids: tuple[str, ...] = ()

        def _bracket(rid: str) -> Any:
            return type(
                "B",
                (),
                {
                    "client_request_id": rid,
                    "quantity": 1,
                    "entry_limit": 10.0,
                    "stop_loss": 9.0,
                    "take_profit": 12.0,
                    "entry_ttl_days": 1,
                },
            )()

        placement = type(
            "P",
            (),
            {
                "tiers": [
                    type("T", (), {"bracket": _bracket("rid-1")})(),
                    type("T", (), {"bracket": _bracket("rid-2")})(),
                ],
                "disaster_stop_price": 9.0,
            },
        )()
        account = type("A", (), {"total_value": 100000.0, "currency": "USD"})()
        instrument = type(
            "I", (), {"currency": "USD", "broker_instrument_id": 307, "exchange_mic": "XNYS"}
        )()
        candidate = type("C", (), {"ticker": "KO", "trade_setup": object()})()

        class _Broker:
            def __init__(self) -> None:
                self.calls = 0
                self.journal_at_second_tier: list[dict[str, Any]] | None = None

            def get_account(self) -> Any:
                return account

            def get_positions(self) -> list:
                return []

            def place_bracket_order(self, _bracket: Any) -> Any:
                self.calls += 1
                if self.calls == 1:
                    return _Placed("E-1")
                # tier 2: capture the journal state as it stood the instant BEFORE
                # this tier is attempted, then die hard (uncaught).
                self.journal_at_second_tier = list(submitted)
                raise _CrashError("process dies mid-ladder")

        broker = _Broker()
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", submitted.append))
            p(mock.patch(f"{pkg}.submission_log.iter_submission_records", lambda _p: []))
            p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b: []))
            p(mock.patch(f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()))
            p(mock.patch(f"{pkg}.routing.resolve_us_instrument", lambda _b, _t: instrument))
            p(
                mock.patch(
                    f"{pkg}.automanager.placement_planner.classify", lambda *_a, **_k: placement
                )
            )
            p(
                mock.patch(
                    "alphalens_pipeline.paper.brief_loader.load_brief",
                    lambda *_a, **_k: [candidate],
                )
            )
            p(
                mock.patch(
                    "alphalens_pipeline.paper.sizing.compute_setup_plan", lambda **_k: object()
                )
            )
            p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
            placer = cl._make_place_pick(broker)  # type: ignore[arg-type]
            with self.assertRaises(_CrashError):
                placer(_pick("KO", "2026-07-20"))
        return broker

    def test_first_tier_journaled_before_second_tier_attempted(self) -> None:
        broker = self._run()
        snapshot = broker.journal_at_second_tier
        self.assertTrue(snapshot, "tier 1 must be journaled BEFORE the second tier is attempted")
        keys = cl._submitted_pick_keys(snapshot or [])
        self.assertIn(
            ("KO", "2026-07-20"),
            keys,
            "the pick-drain join must see the pick as submitted after tier 1",
        )


def _raise_broker_error(*_a: Any, **_k: Any) -> Any:
    raise BrokerError("boom")


class TestBrokerErrorBoundary(unittest.TestCase):
    """CRITICAL: a persistent BrokerError outside entry-placement must never
    crash the tick. One bad position/action is alerted and skipped so the daemon
    keeps reconciling and protecting every OTHER position."""

    def test_verdicts_fn_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                stop_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "verdicts_fn": _raise_broker_error})
            report = cl.run_once(deps)  # must NOT propagate
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts, "reconcile failure must alert")

    def test_build_position_view_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[_verdict(status="CANCELLED", verdict="CANCELLED")],
                place_calls=[],
                stop_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "build_position_view": _raise_broker_error})
            report = cl.run_once(deps)
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts)

    def test_one_action_broker_error_still_processes_other_verdicts(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            alerts: list = []
            # verdict A: FILLED -> PlaceStandaloneStop, whose placer raises.
            # verdict B: CANCELLED -> CancelRemaining, must still cancel T-1.
            a = _verdict(
                status="FILLED",
                verdict="FILLED",
                note="position open, exit orders working",
                details={"client_request_id": _RID, "filled_quantity": 2.0},
            )
            b = _verdict(status="CANCELLED", verdict="CANCELLED")
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[a, b],
                place_calls=[],
                stop_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "place_standalone_stop": _raise_broker_error})
            report = cl.run_once(deps)  # must NOT propagate
            self.assertEqual(broker.cancelled, ["T-1"], "the other verdict is still processed")
            self.assertTrue(alerts, "the failed action must alert")
            self.assertEqual(report.stops_placed, 0)

    def test_orphan_sweep_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                stop_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "sweep_orphans_fn": _raise_broker_error})
            report = cl.run_once(deps, sweep_orphans=True)
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts)


class TestKillFileGate(unittest.TestCase):
    def test_kill_present_suppresses_placement_but_still_cancels(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"
            kill.write_text("halt")
            broker = _StubBroker()
            place_calls: list = []
            stop_calls: list = []
            alerts: list = []
            terminal = _verdict(status="CANCELLED", verdict="CANCELLED")
            filled = _verdict(
                status="FILLED",
                verdict="FILLED",
                note="position open, exit orders working",
                details={"client_request_id": _RID, "filled_quantity": 2.0},
            )
            deps = _deps(
                broker,
                kill_file=kill,
                verdicts=[terminal, filled],
                place_calls=place_calls,
                stop_calls=stop_calls,
                alerts=alerts,
                picks=["pick-KO"],
            )
            cl.run_once(deps)
            self.assertEqual(place_calls, [])
            self.assertEqual(stop_calls, [])
            self.assertEqual(broker.cancelled, ["T-1"])
            self.assertTrue(any("KILL" in a for a in alerts))


class TestCrashRecovery(unittest.TestCase):
    def test_restart_re_derives_identical_classification(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            v = _verdict(status="CANCELLED", verdict="CANCELLED")
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[v],
                place_calls=[],
                stop_calls=[],
                alerts=[],
            )
            r1 = cl.run_once(deps)
            r2 = cl.run_once(deps)
            self.assertEqual(r1.actions, r2.actions)
            self.assertEqual(r1.verdict_count, r2.verdict_count)


class TestRunDaemonOnce(unittest.TestCase):
    def test_once_runs_single_tick_sweeps_orphans_and_never_sleeps(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            sweeps: list = []
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                stop_calls=[],
                alerts=[],
            )
            deps = cl.LoopDeps(
                **{**deps.__dict__, "sweep_orphans_fn": lambda b: sweeps.append(1) or []}
            )
            slept: list = []
            beats: list = []
            cl.run_daemon(
                deps,
                once=True,
                poll_seconds=45,
                sleep_fn=lambda s: slept.append(s),  # noqa: PLW0108
                heartbeat_fn=lambda: beats.append(1),
            )
            self.assertEqual(len(sweeps), 1)
            self.assertEqual(slept, [])
            self.assertEqual(len(beats), 1)


class TestManageCommandRegistered(unittest.TestCase):
    def test_broker_app_has_manage_command(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        names = {c.name for c in broker_app.registered_commands}
        self.assertIn("manage", names)


class TestHeartbeatEmitter(unittest.TestCase):
    def test_default_emit_heartbeat_writes_gauge_to_textfile_dir(self) -> None:
        import os
        from tempfile import TemporaryDirectory

        from alphalens_pipeline.brokers.automanager import control_loop as cl

        with TemporaryDirectory() as d:
            old = os.environ.get("ALPHALENS_TEXTFILE_DIR")
            os.environ["ALPHALENS_TEXTFILE_DIR"] = d
            try:
                cl._default_emit_heartbeat()
            finally:
                if old is None:
                    os.environ.pop("ALPHALENS_TEXTFILE_DIR", None)
                else:
                    os.environ["ALPHALENS_TEXTFILE_DIR"] = old
            written = Path(d) / "alphalens_domain_broker-manager.prom"
            self.assertTrue(written.is_file())
            body = written.read_text()
            self.assertIn("alphalens_broker_manager_last_tick_timestamp_seconds", body)
            self.assertIn('job="broker-manager"', body)

    def test_run_daemon_uses_default_heartbeat_signature(self) -> None:
        import inspect

        from alphalens_pipeline.brokers.automanager import control_loop as cl

        sig = inspect.signature(cl.run_daemon)
        self.assertIs(sig.parameters["heartbeat_fn"].default, cl._default_emit_heartbeat)


if __name__ == "__main__":
    unittest.main()
