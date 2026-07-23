"""Hermetic tests for control_loop.run_once / run_daemon.

Every Task 1-10 dependency is injected as a stub (build_default_deps is covered
by the SIM probe). Under test: kill-gate placement, always reconcile, the
verdict-level advance Action, the broker-state-truth protection pass (single
snapshot -> reconcile_protection -> ordered cancel/place executor), the alert
throttle, and re-derive-on-restart.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.picks import Pick
from alphalens_pipeline.brokers.automanager.position_manager import (
    BrokerView,
    CancelSellLegs,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    UpgradeToOco,
    _exit_oco_ref,
    _exit_stop_ref,
    _exit_tp_ref,
)
from alphalens_pipeline.brokers.contract import (
    BrokerError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

_RID = "rid-KO"
_UIC = 43070


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
    return BrokerView(working_children={_RID: ("T-1",)})


def _empty_pview() -> ProtectionView:
    return ProtectionView(
        long_positions={},
        all_positions={},
        sell_legs_by_uic={},
        planned_by_uic={},
        oco_unsupported=frozenset(),
    )


def _deps(
    broker: Any,
    *,
    kill_file: Path,
    verdicts: list[ReconcileVerdict],
    place_calls: list,
    alerts: list,
    picks: list | None = None,
    chain_alive: bool = True,
    build_protection_view: Any = None,
    execute_protection: Any = None,
    alert_throttled: Any = None,
) -> cl.LoopDeps:
    # Default: un-throttled passthrough (records the alert, always "sent") so
    # existing tests keep asserting on `alerts`. The throttle test injects a real
    # _AlertThrottle to exercise the per-reason dedup.
    def _default_throttled(message: str, reason: str) -> bool:
        alerts.append(message)
        return True

    return cl.LoopDeps(
        broker=broker,
        kill_file=kill_file,
        ensure_alive=lambda: type("C", (), {"alive": chain_alive, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter(picks or []),
        place_pick=lambda pick: place_calls.append(pick) or True,
        read_records=lambda: [{"brackets": [{"client_request_id": _RID}]}],
        verdicts_fn=lambda records, broker: list(verdicts),
        build_position_view=lambda broker, records: _view(),
        build_protection_view=build_protection_view or (lambda broker, records: _empty_pview()),
        execute_protection=execute_protection or (lambda action, kill, report: None),
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: alerts.append(msg),  # noqa: PLW0108
        alert_throttled=alert_throttled or _default_throttled,
    )


# --------------------------------------------------------------------------
# Fixtures for the broker-state protection pass (positions + SELL legs).
# --------------------------------------------------------------------------


def _instrument(uic: int = _UIC) -> InstrumentRef:
    return InstrumentRef(
        ticker="BIO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol="BIO:xnys",
    )


def _pos(qty: float, uic: int = _UIC) -> Position:
    return Position(
        instrument=_instrument(uic),
        quantity=qty,
        avg_price=296.0,
        market_value=None,
        unrealized_pnl=None,
        position_id="pos-1",
    )


def _leg(order_id: str, order_type: str, amount: float, *, uic: int = _UIC) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=uic,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=order_id,
    )


class _ProtBroker:
    """A fake broker exposing the broker-state protection reads + place/cancel.

    ``place_error`` (an exception, or a list of per-call outcomes) drives the
    ``place_standalone_stop`` failure paths; ``cancel_errors`` maps an order_id
    to an exception ``cancel_order`` raises."""

    name = "prot"

    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        sells: list[OrderState] | None = None,
        by_uic: dict[int, Position] | None = None,
        place_error: Any = None,
        cancel_errors: dict[str, BrokerError] | None = None,
    ) -> None:
        self._positions = positions or []
        self._sells = sells or []
        self._by_uic = by_uic or {}
        self._place_error = place_error
        self._place_calls = 0
        self._cancel_errors = cancel_errors or {}
        self.placed: list[tuple[int, str, float, float, str | None]] = []
        self.cancelled: list[str] = []

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def get_long_positions(self) -> list[Position]:
        return [p for p in self._positions if p.quantity > 0.5]

    def list_working_sell_orders(self) -> list[OrderState]:
        return list(self._sells)

    def get_positions_by_uic(self, uic: int) -> Position:
        return self._by_uic.get(uic, _pos(0.0, uic))

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        self._place_calls += 1
        err = self._place_error
        if isinstance(err, list):
            err = err[self._place_calls - 1] if self._place_calls - 1 < len(err) else None
        if err is not None:
            raise err
        self.placed.append((uic, side, qty, stop_price, request_id))
        return PlacedOrder(entry_order_id="S-1", exit_order_ids=())

    def cancel_order(self, order_id: str) -> None:
        err = self._cancel_errors.get(order_id)
        if err is not None:
            raise err
        self.cancelled.append(order_id)


def _seed_planned(journal: Path, uic: int = _UIC, crid: str = "crid-0") -> None:
    with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid=crid,
                uic=uic,
                side="SELL",
                stop_price=216.48,
                take_profit=306.72,
                tier_index=0,
            )
        )


def _throttle_to(alerts: list[str]) -> cl._AlertThrottle:
    return cl._AlertThrottle(alerts.append)


class TestRunOncePlacement(unittest.TestCase):
    def test_drains_armed_pick_when_chain_alive_and_no_kill(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                alerts=[],
                picks=[pick],
            )
            cl.run_once(deps)
            self.assertEqual(place_calls, [pick])


class TestChainDeadHaltsPlacementAndAlerts(unittest.TestCase):
    """Safety + never-silent: when the session-keeper reports the auth chain dead,
    run_once alerts ("chain dead — <reason>; placement halted") AND suppresses the
    placement drain, while reconcile/protection still run. Closes the coverage gap
    where the test harness always defaulted chain_alive=True, so the loop-level
    behaviour (halt + alert) was never exercised — only safety.py's pure predicate."""

    def _dead_chain_deps(self, d: str, place_calls: list, alerts: list, pick: Any) -> cl.LoopDeps:
        deps = _deps(
            _StubBroker(),
            kill_file=Path(d) / "KILL",
            verdicts=[],
            place_calls=place_calls,
            alerts=alerts,
            picks=[pick],
        )
        dead = type("C", (), {"alive": False, "reason": "session token expired"})()
        return cl.LoopDeps(**{**deps.__dict__, "ensure_alive": lambda: dead})

    def test_chain_dead_alerts_and_halts_placement(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            alerts: list = []
            pick = _pick("KO", "2026-07-20")
            deps = self._dead_chain_deps(d, place_calls, alerts, pick)
            report = cl.run_once(deps)
            # placement halted — the armed pick is NOT sent to the broker.
            self.assertEqual(place_calls, [])
            self.assertEqual(report.picks_placed, 0)
            # never-silent — the dead chain surfaces with its reason AND the halt.
            self.assertTrue(
                any(
                    "chain dead — session token expired" in a and "placement halted" in a
                    for a in alerts
                ),
                f"expected a chain-dead placement-halted alert, got {alerts}",
            )

    def test_chain_alive_places_pick_and_emits_no_chain_dead_alert(self) -> None:
        # Positive control: with the chain ALIVE the SAME pick IS placed and no
        # chain-dead alert fires — proving the halt + alert above are gated on the
        # dead chain, not vacuously always-true.
        with TemporaryDirectory() as d:
            place_calls: list = []
            alerts: list = []
            pick = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                alerts=alerts,
                picks=[pick],
            )  # chain_alive defaults True
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [pick])
            self.assertEqual(report.picks_placed, 1)
            self.assertFalse(
                any("chain dead" in a for a in alerts),
                f"no chain-dead alert expected when the chain is alive, got {alerts}",
            )


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
                alerts=[],
                picks=[_pick("KO", "2026-07-20")],
            )
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
        with TemporaryDirectory() as d:
            place_calls: list = []
            p1 = _pick("KO", "2026-07-20")
            p2 = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                alerts=[],
                picks=[p1, p2],
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "read_records": list})
            report = cl.run_once(deps)
            self.assertEqual(place_calls, [p1], "the duplicate armed line must be skipped")
            self.assertEqual(report.picks_placed, 1)


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
                    type("T", (), {"bracket": _bracket("rid-1"), "tier_index": 0, "tp": 12.0})(),
                    type("T", (), {"bracket": _bracket("rid-2"), "tier_index": 1, "tp": 12.0})(),
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


# --- place-pick failure + edge branches (the SIM-only placer must never raise) --


def _acct(currency: str = "USD") -> Any:
    return type("A", (), {"total_value": 100000.0, "currency": currency})()


def _instr(currency: str = "USD") -> Any:
    return type(
        "I", (), {"currency": currency, "broker_instrument_id": 307, "exchange_mic": "XNYS"}
    )()


def _placement(n_tiers: int = 1) -> Any:
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

    tiers = [
        type("T", (), {"bracket": _bracket(f"rid-{i}"), "tier_index": i, "tp": 12.0})()
        for i in range(n_tiers)
    ]
    return type("P", (), {"tiers": tiers, "disaster_stop_price": 9.0})()


class _PlaceBroker:
    """Stub broker for _make_place_pick: happy account/place unless overridden."""

    def __init__(self, *, on_account: Any = None, on_place: Any = None, get_fx_rate: Any = None):
        self._on_account = on_account
        self._on_place = on_place
        if get_fx_rate is not None:
            self.get_fx_rate = get_fx_rate  # optional capability probed via getattr

    def get_account(self) -> Any:
        return self._on_account() if self._on_account is not None else _acct()

    def get_positions(self) -> list:
        return []

    def place_bracket_order(self, bracket: Any) -> Any:
        if self._on_place is not None:
            return self._on_place(bracket)
        return type("Placed", (), {"entry_order_id": "E-1", "exit_order_ids": ()})()


class TestPlacePickBranches(unittest.TestCase):
    """The SIM-only placer's failure + edge paths: each returns False (or journals
    a note) rather than raising, so one bad pick never crashes a tick."""

    def _placer(self, broker: Any, **over: Any) -> Any:
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        m: dict[str, Any] = {
            "load_brief": lambda *_a, **_k: [
                type("C", (), {"ticker": "KO", "trade_setup": object()})()
            ],
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": lambda _b, _t: _instr(),
            "classify": lambda *_a, **_k: _placement(),
            "compute_plan": lambda **_k: object(),
            "iter_records": lambda _p: [],
            "append": lambda _r: None,
            "build_record": lambda **kw: dict(kw),
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("alphalens_pipeline.paper.brief_loader.load_brief", m["load_brief"]))
        p(mock.patch("alphalens_pipeline.paper.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        return cl._make_place_pick(broker)

    def test_brief_unavailable_returns_false(self) -> None:
        def _raise(*_a: Any, **_k: Any) -> Any:
            raise FileNotFoundError("no brief")

        self.assertFalse(self._placer(_PlaceBroker(), load_brief=_raise)(_pick()))

    def test_no_plannable_trade_setup_returns_false(self) -> None:
        no_setup = [type("C", (), {"ticker": "KO", "trade_setup": None})()]
        self.assertFalse(
            self._placer(_PlaceBroker(), load_brief=lambda *_a, **_k: no_setup)(_pick())
        )

    def test_broker_read_error_returns_false(self) -> None:
        def _boom() -> Any:
            raise BrokerError("account read down")

        self.assertFalse(self._placer(_PlaceBroker(on_account=_boom))(_pick()))

    def test_safety_refuse_returns_false(self) -> None:
        from alphalens_pipeline.brokers.automanager.safety import Refuse

        placer = self._placer(
            _PlaceBroker(), safety_check=lambda *_a, **_k: Refuse(reason="cap hit")
        )
        self.assertFalse(placer(_pick()))

    def test_no_instrument_currency_returns_false(self) -> None:
        placer = self._placer(_PlaceBroker(), resolve=lambda _b, _t: _instr(currency=""))
        self.assertFalse(placer(_pick()))

    def test_fx_needed_but_broker_cannot_convert_returns_false(self) -> None:
        # instrument EUR vs account USD, broker without get_fx_rate -> cannot size.
        placer = self._placer(_PlaceBroker(), resolve=lambda _b, _t: _instr(currency="EUR"))
        self.assertFalse(placer(_pick()))

    def test_fx_conversion_built_when_broker_supports_it(self) -> None:
        broker = _PlaceBroker(get_fx_rate=lambda _base, _quote: 1.1)
        fx_obj = type("FX", (), {"rate": 1.1})()
        with mock.patch(
            "alphalens_pipeline.brokers.execution.build_fx_conversion", lambda _r: fx_obj
        ):
            placer = self._placer(broker, resolve=lambda _b, _t: _instr(currency="EUR"))
            self.assertTrue(placer(_pick()))

    def test_resolve_or_size_error_returns_false(self) -> None:
        def _boom(_b: Any, _t: Any) -> Any:
            raise BrokerError("instrument lookup down")

        self.assertFalse(self._placer(_PlaceBroker(), resolve=_boom)(_pick()))

    def test_zero_sized_tiers_returns_false(self) -> None:
        placer = self._placer(_PlaceBroker(), classify=lambda *_a, **_k: _placement(n_tiers=0))
        self.assertFalse(placer(_pick()))

    def test_place_bracket_error_journals_note_and_returns_false(self) -> None:
        notes: list[Any] = []

        def _boom(_bracket: Any) -> Any:
            raise BrokerError("exchange rejected")

        placer = self._placer(_PlaceBroker(on_place=_boom), append=notes.append)
        self.assertFalse(placer(_pick()))
        self.assertTrue(notes, "a note-only failure record must be journaled")

    def test_summarize_counts_working_verdict_committed_capital(self) -> None:
        today = dt.date.today().isoformat()
        working = _verdict(
            status="WORKING",
            activity_time=f"{today}T00:00:00",
            details={"client_request_id": "rid-x", "realized_r": 1.5},
        )
        captured: dict[str, Any] = {}

        def _capture(_pick_arg: Any, journal_view: Any, _bview: Any, _session: Any) -> Any:
            captured["jv"] = journal_view
            return object()

        placer = self._placer(
            _PlaceBroker(),
            verdicts=lambda _r, _b: [working],
            iter_records=lambda _p: [
                {"brackets": [{"client_request_id": "rid-x", "entry": 10.0, "qty": 5}]}
            ],
            safety_check=_capture,
        )
        self.assertTrue(placer(_pick()))
        self.assertEqual(captured["jv"].open_bracket_count, 1)
        self.assertEqual(captured["jv"].gross_committed, 50.0)
        self.assertEqual(captured["jv"].realized_r_today, 1.5)


class TestRunOnceAlertsEachOrphan(unittest.TestCase):
    def test_each_swept_orphan_is_alerted(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list[str] = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(
                **{**deps.__dict__, "sweep_orphans_fn": lambda _b: ["orphan-A", "orphan-B"]}
            )
            report = cl.run_once(deps, sweep_orphans=True)
        self.assertEqual(report.orphans, 2)
        self.assertTrue(any("orphan-A" in a for a in alerts))


class TestLatestPlannedSkipsMalformedLines(unittest.TestCase):
    def test_missing_keys_or_unparsable_price_are_skipped(self) -> None:
        lines = [
            {"kind": "planned", "uic": 7},  # missing client_request_id
            {"kind": "planned", "client_request_id": "c1"},  # missing uic
            {
                "kind": "planned",
                "client_request_id": "c2",
                "uic": 7,
                "stop_price": "abc",
            },  # bad float
        ]
        self.assertEqual(cl._fold_planned_exits(lines), {})


class TestProtectionExecutorUpgradeToOcoNoop(unittest.TestCase):
    def test_upgrade_to_oco_is_noop_while_oco_disabled(self) -> None:
        # Stage 1 keeps _oco_enabled() False, so an UpgradeToOco action is a pure
        # no-op (the rung-2 arm stays dark) — no place, no cancel, no alert.
        from alphalens_pipeline.brokers.automanager.position_manager import UpgradeToOco

        broker = _StubBroker()
        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append)
        execute = cl._make_protection_executor(broker, throttle)  # type: ignore[arg-type]
        report = cl.TickReport()
        action = UpgradeToOco(
            uic=_UIC,
            side="SELL",
            qty=5.0,
            stop_price=9.0,
            tp_price=12.0,
            entry_crid="c0",
            gen=0,
            supersede_ids=(),
        )
        execute(action, False, report)
        self.assertEqual((report.exits_placed, report.cancels, report.alerts), (0, 0, 0))
        self.assertEqual(broker.cancelled, [])
        self.assertEqual(alerts, [])

    def test_noop_is_silent_and_alertonly_alerts(self) -> None:
        from alphalens_pipeline.brokers.automanager.position_manager import AlertOnly, NoOp

        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append)
        execute = cl._make_protection_executor(_StubBroker(), throttle)  # type: ignore[arg-type]
        report = cl.TickReport()
        execute(NoOp(), False, report)  # NoOp branch: no side effect
        execute(AlertOnly("naked uic 7 — no protective stop"), False, report)  # AlertOnly branch
        self.assertEqual(report.alerts, 1)
        self.assertIn("naked uic 7 — no protective stop", alerts)


class TestDivergenceAlertThrottled(unittest.TestCase):
    """A stuck FILLED-but-unmatched reconcile divergence must page ONCE per re-alert
    interval, not every tick (overnight-spam incident 2026-07-23). The verdict-level
    AlertOnly now routes through the daemon-lifetime throttle, keyed per crid."""

    _DIVERGENCE_REASON = (
        "audit log says FILLED but no open position or closed pair matched "
        "client_request_id 'rid-KO'"
    )

    def _run_advance(self, deps: cl.LoopDeps, verdict: ReconcileVerdict) -> None:
        cl._advance_and_execute(deps, verdict, _view(), cl.TickReport())

    def test_same_divergence_alerts_once_then_re_alerts_after_interval(self) -> None:
        from alphalens_pipeline.brokers.automanager.position_manager import AlertOnly, advance

        alerts: list[str] = []
        clock = {"t": 1000.0}
        throttle = cl._AlertThrottle(alerts.append, clock=lambda: clock["t"], interval_s=1800.0)
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=alerts,
            alert_throttled=lambda m, r: throttle.emit(m, reason=r),
        )
        verdict = _verdict(divergence=True, reason=self._DIVERGENCE_REASON)
        self.assertIsInstance(advance(verdict), AlertOnly, "a divergence verdict -> AlertOnly")

        for _ in range(5):  # five consecutive ticks, same stuck crid
            self._run_advance(deps, verdict)
        self.assertEqual(len(alerts), 1, "a stuck divergence pages ONCE within the interval")

        clock["t"] += 1801.0  # the re-alert interval elapses
        self._run_advance(deps, verdict)
        self.assertEqual(len(alerts), 2, "it re-alerts once per interval, not every tick")

    def test_distinct_crids_are_independent_alerts(self) -> None:
        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append, clock=lambda: 1000.0, interval_s=1800.0)
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=alerts,
            alert_throttled=lambda m, r: throttle.emit(m, reason=r),
        )
        v_a = _verdict(divergence=True, reason="a", details={"client_request_id": "rid-A"})
        v_b = _verdict(divergence=True, reason="b", details={"client_request_id": "rid-B"})
        self._run_advance(deps, v_a)
        self._run_advance(deps, v_b)  # different crid -> distinct throttle key
        self.assertEqual(len(alerts), 2, "distinct client_request_ids alert independently")


def _oco_placer(calls: list, *, error: Exception | None = None):
    """A fake ``place_oco_exit`` recording each call; optionally raising ``error``."""

    def _place(
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        take_profit: float,
        request_id: str,
        position_id: str | None = None,
    ) -> PlacedOrder:
        calls.append((uic, side, qty, stop_price, take_profit, request_id, position_id))
        if error is not None:
            raise error
        return PlacedOrder(entry_order_id="", exit_order_ids=("stop-id", "tp-id"))

    return _place


_OCO_ON = {"ALPHALENS_BROKER_OCO_ENABLED": "1"}


class TestUpgradeOcoSuccessSupersedesRung1(unittest.TestCase):
    """A successful OCO place covers the downside, so the rung-1 stop is cancelled
    ONLY AFTER the place confirms — never a naked window (saxo-oco memo §6)."""

    def test_success_places_then_cancels_rung1(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), place_oco_exit=_oco_placer(calls)
            )
            action = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            executor(action, False, report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][:6], (_UIC, "SELL", 46.0, 216.48, 306.72, _exit_oco_ref("crid-0", 0))
        )
        self.assertEqual(broker.cancelled, ["rung1-stop"], "rung-1 cancelled AFTER the OCO place")
        self.assertEqual(report.exits_placed, 1)


class TestUpgradeOcoFailKeepsRung1AndMarksUnsupported(unittest.TestCase):
    """A structural OCO reject (SellOrdersAlreadyExist) is caught: the rung-1 stop
    stays LIVE (never naked), the uic is persisted oco_unsupported, and a rebuilt
    view on the next tick degrades arm-C to NoOp — no re-attempt churn."""

    def test_fail_marks_unsupported_no_reattempt_rung1_kept(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            rung1 = _leg("rung1-stop", "StopIfTraded", 46.0)
            broker = _ProtBroker(positions=[_pos(46.0)], sells=[rung1], by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(
                calls,
                error=OrderRejectedError(
                    "blocked", error_code="SellOrdersAlreadyExistForOwnedContracts"
                ),
            )
            throttle = _throttle_to(alerts)
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(
                    broker, throttle, place_oco_exit=placer
                ),
            )
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)  # planned line carries take_profit=306.72
                r1 = cl.run_once(deps)
                r2 = cl.run_once(deps)
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertEqual(len(calls), 1, "OCO attempted once, never re-attempted after the mark")
        self.assertEqual(broker.cancelled, [], "rung-1 stop kept LIVE on the degrade path")
        self.assertEqual((r1.exits_placed, r2.exits_placed), (0, 0))
        self.assertIn(_UIC, folded, "the failed uic is persisted oco_unsupported")
        self.assertTrue(any("oco" in a.lower() for a in alerts), "degrade alert fires")


class TestUpgradeOcoBroadCatch(unittest.TestCase):
    """ANY BrokerError (a plain 202 / rate-limit / reject) degrades to stop-only:
    caught, rung-1 kept, uic marked unsupported — never a naked window."""

    def test_plain_broker_error_caught_rung1_kept(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(calls, error=BrokerError("202 TradeNotCompleted"))
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            action = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                executor(action, False, report)  # must NOT raise
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(broker.cancelled, [], "rung-1 kept — never a naked window on the degrade")
        self.assertEqual(report.exits_placed, 0)
        self.assertIn(_UIC, folded)
        self.assertTrue(any("oco" in a.lower() for a in alerts))


class TestKillBlocksUpgrade(unittest.TestCase):
    """Under KILL the OCO upgrade is skipped (no new order churn, no rung-1
    cancel), while the exposure-reducing protective stop still places."""

    def test_kill_skips_oco_but_stop_still_places(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), place_oco_exit=_oco_placer(calls)
            )
            upgrade = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            executor(upgrade, True, report)  # kill = True -> upgrade skipped
            self.assertEqual(calls, [], "no OCO place under KILL")
            self.assertEqual(broker.cancelled, [], "rung-1 not cancelled under KILL")
            # The protective stop only REDUCES exposure -> still allowed under KILL.
            stop = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
            executor(stop, True, report)
            self.assertEqual(len(broker.placed), 1, "protective stop still places under KILL")


class TestUpgradeOcoDarkByDefault(unittest.TestCase):
    """Ship dark: with the env flag unset, an UpgradeToOco action is a pure no-op
    even when a placer IS wired (the single ``_oco_enabled()`` env gate governs)."""

    def test_flag_unset_no_place_even_with_placer(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_OCO_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), place_oco_exit=_oco_placer(calls)
            )
            action = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            executor(action, False, report)
        self.assertEqual(calls, [], "no OCO place while dark")
        self.assertEqual(broker.cancelled, [])
        self.assertEqual(report.exits_placed, 0)


class TestUpgradeOcoFlatUicSkips(unittest.TestCase):
    """Execute-time owned re-check: the snapshot showed owned=46 but the position
    is flat now -> the OCO is skipped and the rung-1 stop is left untouched."""

    def test_flat_at_execute_skips_oco(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(0.0)})  # flat now
            calls: list = []
            alerts: list[str] = []
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=_oco_placer(calls)
            )
            action = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            executor(action, False, report)
        self.assertEqual(calls, [], "no OCO placed on a flat uic")
        self.assertEqual(broker.cancelled, [], "rung-1 stop untouched")
        self.assertEqual(report.exits_placed, 0)


class TestUpgradeOcoMissingCapabilityStaysStopOnly(unittest.TestCase):
    """Flag on but the wired broker has no OCO capability (placer is None): the
    upgrade must not raise (AttributeError would escape the per-action boundary) —
    it stays rung-1 stop-only, rung-1 untouched."""

    def test_no_placer_no_crash_rung1_kept(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            executor = cl._make_protection_executor(broker, _throttle_to([]))  # no placer
            action = UpgradeToOco(
                uic=_UIC,
                side="SELL",
                qty=46.0,
                stop_price=216.48,
                tp_price=306.72,
                entry_crid="crid-0",
                gen=0,
                supersede_ids=("rung1-stop",),
            )
            report = cl.TickReport()
            executor(action, False, report)  # must NOT raise
        self.assertEqual(broker.cancelled, [], "rung-1 kept when OCO capability is absent")
        self.assertEqual(report.exits_placed, 0)


def _raise_broker_error(*_a: Any, **_k: Any) -> Any:
    raise BrokerError("boom")


class TestBrokerErrorBoundary(unittest.TestCase):
    """CRITICAL: a persistent BrokerError outside entry-placement must never
    crash the tick. One bad read/action is alerted and skipped so the daemon
    keeps reconciling and protecting every OTHER position."""

    def test_verdicts_fn_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
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
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "build_position_view": _raise_broker_error})
            report = cl.run_once(deps)
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts)

    def test_build_protection_view_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
                build_protection_view=_raise_broker_error,
            )
            report = cl.run_once(deps)  # must NOT propagate
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts, "protection-view build failure must alert")

    def test_protection_runs_even_when_verdicts_fail(self) -> None:
        # Reconcile (verdicts) failing must NOT starve the safety-critical
        # protection pass — a naked long is still protected this tick.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
            throttle = _throttle_to(alerts)
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(broker, throttle),
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "verdicts_fn": _raise_broker_error})
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)
                report = cl.run_once(deps)  # must NOT propagate
            self.assertEqual(len(broker.placed), 1, "protection runs despite the reconcile failure")
            self.assertEqual(report.exits_placed, 1)
            self.assertTrue(any("reconcile failed" in a for a in alerts))

    def test_advance_action_broker_error_does_not_crash_tick(self) -> None:
        # A CANCELLED verdict -> CancelRemaining; the cancel of leftover exits
        # raises. The tick must survive (per-action boundary) and alert.
        with TemporaryDirectory() as d:
            alerts: list = []

            class _CancelRaises(_StubBroker):
                def cancel_order(self, order_id: str) -> None:
                    raise BrokerError("locked pre-execution")

            deps = _deps(
                _CancelRaises(),
                kill_file=Path(d) / "KILL",
                verdicts=[_verdict(status="CANCELLED", verdict="CANCELLED")],
                place_calls=[],
                alerts=alerts,
            )
            report = cl.run_once(deps)  # must NOT propagate
            self.assertIsInstance(report, cl.TickReport)
            self.assertTrue(alerts, "the failed cancel must alert")

    def test_orphan_sweep_broker_error_does_not_crash_tick(self) -> None:
        with TemporaryDirectory() as d:
            alerts: list = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
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
            alerts: list = []
            terminal = _verdict(status="CANCELLED", verdict="CANCELLED")
            deps = _deps(
                broker,
                kill_file=kill,
                verdicts=[terminal],
                place_calls=place_calls,
                alerts=alerts,
                picks=["pick-KO"],
            )
            cl.run_once(deps)
            self.assertEqual(place_calls, [], "entry placement is suppressed under KILL")
            # Cancels still run under KILL (cleanup is always safe); a protective
            # stop would also be allowed (it only reduces exposure), but this
            # tick's empty protection view yields none.
            self.assertEqual(broker.cancelled, ["T-1"])


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


# --------------------------------------------------------------------------
# Broker-state-truth protection pass (Task 6): build_protection_view +
# _make_protection_executor wired through run_once.
# --------------------------------------------------------------------------


class TestFailedPostLeavesNoProtectionAndRetries(unittest.TestCase):
    """Bug A end-to-end: a failed stop POST records NO protection (protection is
    broker-state truth, not a journal line), the tick survives, and the NEXT tick
    re-derives the same deficit and re-issues the place."""

    def test_failed_place_tick1_then_retry_places_tick2(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            # Tick 1 place raises BrokerError; tick 2 succeeds.
            broker = _ProtBroker(
                positions=[_pos(46.0)],
                sells=[],
                by_uic={_UIC: _pos(46.0)},
                place_error=[BrokerError("network blip"), None],
            )
            throttle = _throttle_to(alerts)
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],  # no journal verdict — the loop iterates POSITIONS
                place_calls=[],
                alerts=alerts,
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(broker, throttle),
            )
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)
                r1 = cl.run_once(deps)
                self.assertEqual(broker.placed, [], "tick 1 POST failed — nothing placed")
                self.assertEqual(r1.exits_placed, 0)
                r2 = cl.run_once(deps)
            self.assertEqual(
                len(broker.placed), 1, "tick 2 must re-issue the place (no journal lie)"
            )
            self.assertEqual(broker.placed[0][:4], (_UIC, "SELL", 46.0, 216.48))
            self.assertEqual(r2.exits_placed, 1)


class TestLoopIteratesPositionsNotVerdicts(unittest.TestCase):
    """C-S5: a position on the broker with owned>0 and NO journal verdict is still
    protected — the protection pass iterates live positions, not verdicts."""

    def test_position_without_verdict_is_protected(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
            throttle = _throttle_to(alerts)
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(broker, throttle),
            )
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)
                report = cl.run_once(deps)
            self.assertEqual(len(broker.placed), 1)
            self.assertEqual(report.exits_placed, 1)


class TestExecuteTimeRecheckSkipsFlatUic(unittest.TestCase):
    """B-F3/A-S4: the snapshot showed owned=46 but the position is flat at execute
    time -> the place is skipped, no stop planted (it could later fire into a short)."""

    def test_flat_at_execute_skips_place(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(0.0)})  # flat now
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
        report = cl.TickReport()
        executor(action, False, report)
        self.assertEqual(broker.placed, [], "no stop planted on a flat uic")
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(any("gone" in a for a in alerts))

    def test_shrunk_position_clips_qty_never_oversells(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(20.0)})  # only 20 left
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
        report = cl.TickReport()
        executor(action, False, report)
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(broker.placed[0][2], 20.0, "qty clipped to live owned")


class TestKillAllowsProtectiveStop(unittest.TestCase):
    """B-S1: a protective stop only REDUCES exposure, so it is allowed under KILL."""

    def test_place_stop_executes_under_kill(self) -> None:
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
        executor = cl._make_protection_executor(broker, _throttle_to([]))
        action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
        report = cl.TickReport()
        executor(action, True, report)  # kill = True
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(report.exits_placed, 1)


class TestSellOrdersAlreadyExistDefersNotCrashes(unittest.TestCase):
    """A SellOrdersAlreadyExist rejection defers to next tick — alert + return,
    never a crash, nothing recorded as protected."""

    def test_sell_exist_defers(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(
            by_uic={_UIC: _pos(46.0)},
            place_error=OrderRejectedError(
                "blocked", error_code="SellOrdersAlreadyExistForOwnedContracts"
            ),
        )
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
        report = cl.TickReport()
        executor(action, False, report)  # must NOT raise
        self.assertEqual(broker.placed, [])
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(any("deferred" in a for a in alerts))

    def test_cancel_conflicting_tp_cancelled_before_place(self) -> None:
        # Bug B: a lone TP holds the conflicting sell commitment; the executor
        # cancels it BEFORE placing the stop.
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
        executor = cl._make_protection_executor(broker, _throttle_to([]))
        action = PlaceStop(
            _UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0), cancel_conflicting=("tp-1",)
        )
        report = cl.TickReport()
        executor(action, False, report)
        self.assertEqual(broker.cancelled, ["tp-1"], "the lone TP is cancelled BEFORE the place")
        self.assertEqual(len(broker.placed), 1)

    def test_supersede_ids_cancelled_after_place(self) -> None:
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
        executor = cl._make_protection_executor(broker, _throttle_to([]))
        action = PlaceStop(
            _UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 1), supersede_ids=("old-stop",)
        )
        report = cl.TickReport()
        executor(action, False, report)
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(broker.cancelled, ["old-stop"], "old stop cancelled AFTER the place")

    def test_supersede_not_cancelled_when_place_fails(self) -> None:
        # A failed place must leave the OLD stop live (no naked window).
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)}, place_error=BrokerError("rejected"))
        executor = cl._make_protection_executor(broker, _throttle_to([]))
        action = PlaceStop(
            _UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 1), supersede_ids=("old-stop",)
        )
        report = cl.TickReport()
        executor(action, False, report)
        self.assertEqual(broker.placed, [])
        self.assertEqual(broker.cancelled, [], "old stop NOT cancelled when the new place fails")


class TestIdempotentCancelNoThrash(unittest.TestCase):
    """A-S5: cancelling an already-gone order is a success, not a raise."""

    def test_already_gone_is_success(self) -> None:
        broker = _ProtBroker(cancel_errors={"gone": BrokerError("cancel HTTP 404: not found")})
        cl._idempotent_cancel(broker, "gone")  # must NOT raise

    def test_real_error_propagates(self) -> None:
        broker = _ProtBroker(cancel_errors={"locked": BrokerError("locked pre-execution")})
        with self.assertRaises(BrokerError):
            cl._idempotent_cancel(broker, "locked")

    def test_cancel_sell_legs_swallows_gone_sibling(self) -> None:
        broker = _ProtBroker(cancel_errors={"gone": BrokerError("OrderNotFound")})
        executor = cl._make_protection_executor(broker, _throttle_to([]))
        action = CancelSellLegs(_UIC, ("live-1", "gone"), reason="orphan sweep")
        report = cl.TickReport()
        executor(action, False, report)  # must NOT raise
        self.assertEqual(broker.cancelled, ["live-1"])
        self.assertEqual(report.cancels, 2)


class _AttemptRecordingBroker(_ProtBroker):
    """Records EVERY cancel attempt (even ones that raise) so a test can assert
    the CancelSellLegs loop does not abort after the first failure."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempted: list[str] = []

    def cancel_order(self, order_id: str) -> None:
        self.attempted.append(order_id)
        super().cancel_order(order_id)


class TestCancelSellLegsResilientToPerLegFailure(unittest.TestCase):
    """A genuine transient BrokerError on ONE leg must not strand the remaining
    legs uncancelled — each cancel is isolated, the tick does not raise, and the
    failure is throttle-alerted."""

    def test_first_leg_failure_does_not_abort_remaining_cancels(self) -> None:
        # "locked" is NOT an already-gone token -> _idempotent_cancel re-raises
        # a real BrokerError; the executor must catch it per-leg and continue.
        broker = _AttemptRecordingBroker(
            cancel_errors={"leg-1": BrokerError("locked pre-execution")}
        )
        alerts: list[str] = []
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = CancelSellLegs(_UIC, ("leg-1", "leg-2", "leg-3"), reason="orphan sweep")
        report = cl.TickReport()

        executor(action, False, report)  # must NOT raise

        self.assertEqual(
            broker.attempted,
            ["leg-1", "leg-2", "leg-3"],
            "all legs attempted despite the first raising",
        )
        self.assertEqual(broker.cancelled, ["leg-2", "leg-3"], "the two good legs cancelled")
        self.assertEqual(report.cancels, 2, "only the successful cancels are counted")
        self.assertTrue(
            any("leg-1" in a for a in alerts),
            "the per-leg cancel failure is surfaced as an alert",
        )


class TestAlertThrottleByUicReason(unittest.TestCase):
    """A-S2/B-S3/C-S10: the same (uic, reason) within the interval alerts once; N
    consecutive place failures escalate once then back off."""

    def test_same_uic_reason_alerts_once_within_interval(self) -> None:
        sent: list[str] = []
        clock = {"t": 0.0}
        throttle = cl._AlertThrottle(sent.append, clock=lambda: clock["t"], interval_s=1800.0)
        self.assertTrue(throttle.emit("naked", uic=1, reason="deficit"))
        self.assertFalse(throttle.emit("naked", uic=1, reason="deficit"))
        self.assertEqual(len(sent), 1)
        # A different reason on the same uic is a distinct alert.
        self.assertTrue(throttle.emit("other", uic=1, reason="orphan"))
        self.assertEqual(len(sent), 2)
        # After the interval elapses, the first key alerts again.
        clock["t"] = 1801.0
        self.assertTrue(throttle.emit("naked", uic=1, reason="deficit"))
        self.assertEqual(len(sent), 3)

    def test_consecutive_failures_escalate_once_then_backoff(self) -> None:
        sent: list[str] = []
        throttle = cl._AlertThrottle(sent.append, clock=lambda: 0.0)
        throttle.record_place_failure(7, "fail-1")
        throttle.record_place_failure(7, "fail-2")
        before = len(sent)
        throttle.record_place_failure(7, "fail-3")  # threshold -> CRITICAL once
        self.assertEqual(len(sent), before + 1)
        self.assertTrue(any("CRITICAL" in s and "NAKED" in s for s in sent))
        after_escalation = len(sent)
        throttle.record_place_failure(7, "fail-4")  # backoff -> silent
        throttle.record_place_failure(7, "fail-5")
        self.assertEqual(len(sent), after_escalation, "escalated uic backs off silently")

    def test_place_success_resets_failure_counter(self) -> None:
        sent: list[str] = []
        throttle = cl._AlertThrottle(sent.append, clock=lambda: 0.0)
        throttle.record_place_failure(7, "fail")
        throttle.record_place_success(7)
        # A fresh streak starts from zero (no escalation on the very next failure).
        throttle.record_place_failure(7, "fail-again")
        self.assertFalse(any("CRITICAL" in s for s in sent))


class TestPerCallBrokerErrorBoundary(unittest.TestCase):
    """One uic's broker error inside the protection pass does not prevent other
    uics being processed (per-action boundary in run_once)."""

    def test_one_uic_cancel_error_still_sweeps_the_other(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            uic_a, uic_b = 111, 222
            # Two flat uics, each with an orphan SELL leg -> two CancelSellLegs.
            broker = _ProtBroker(
                positions=[],  # both flat -> orphan sweep for both
                sells=[
                    _leg("A-1", "StopIfTraded", 5.0, uic=uic_a),
                    _leg("B-1", "StopIfTraded", 5.0, uic=uic_b),
                ],
                cancel_errors={"A-1": BrokerError("locked pre-execution")},
            )
            throttle = _throttle_to(alerts)
            deps = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(broker, throttle),
            )
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                report = cl.run_once(deps)
            self.assertIn("B-1", broker.cancelled, "the second orphan uic is still swept")
            self.assertTrue(alerts, "the failed cancel alerts")
            self.assertIsInstance(report, cl.TickReport)


# --------------------------------------------------------------------------
# Journal fold (Task 4) — planned-exit prices only, keyed by uic.
# --------------------------------------------------------------------------


class TestFoldPlannedExitsPricesOnly(unittest.TestCase):
    """Task 4 (memo §7): the planned-exits fold keys by UIC and returns PLAN
    PRICES only. It NEVER returns a ``frozenset`` protected set — protection is
    derived from live broker state (Tasks 5/6), never from a journal line. An
    ``intent`` / ``placed`` line contributes nothing to a protection decision."""

    def test_two_tiers_one_uic_fold_to_one_planned_exit(self) -> None:
        lines = [
            {
                "kind": "planned",
                "client_request_id": "crid-0",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 216.48,
                "take_profit": 306.72,
                "tier_index": 0,
                "gen": 0,
            },
            {
                "kind": "planned",
                "client_request_id": "crid-1",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 216.48,
                "take_profit": 297.5,
                "tier_index": 1,
                "gen": 0,
            },
        ]
        result = cl._fold_planned_exits(lines)
        self.assertEqual(set(result), {43070})
        planned = result[43070]
        self.assertIsInstance(planned, PlannedExit)
        self.assertEqual(planned.uic, 43070)
        self.assertEqual(planned.side, "SELL")
        self.assertAlmostEqual(planned.stop_price, 216.48)
        self.assertIsNotNone(planned.tp_price)
        self.assertAlmostEqual(planned.tp_price or 0.0, 306.72)
        self.assertEqual(planned.entry_crid, "crid-0")
        self.assertFalse(planned.conflicting)
        self.assertEqual(planned.n_plans, 1)

    def test_fold_returns_a_plain_dict_no_protected_frozenset(self) -> None:
        lines = [
            {
                "kind": "planned",
                "client_request_id": "crid-0",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 216.48,
                "take_profit": 306.72,
                "tier_index": 0,
                "gen": 0,
            }
        ]
        result = cl._fold_planned_exits(lines)
        self.assertIsInstance(result, dict)
        self.assertNotIsInstance(result, tuple)

    def test_intent_and_placed_lines_contribute_nothing(self) -> None:
        lines = [
            {
                "kind": "intent",
                "client_request_id": "crid-0",
                "uic": 43070,
                "side": "SELL",
                "qty": 46.0,
                "stop_price": 216.48,
            },
            {
                "kind": "placed",
                "client_request_id": "crid-0",
                "uic": 43070,
                "side": "SELL",
                "qty": 46.0,
                "stop_price": 216.48,
                "order_id": "S-1",
            },
        ]
        self.assertEqual(cl._fold_planned_exits(lines), {})

    def test_grows_conflicting_when_two_plans_hit_one_uic(self) -> None:
        lines = [
            {
                "kind": "planned",
                "client_request_id": "crid-A0",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 216.48,
                "take_profit": 306.72,
                "tier_index": 0,
                "gen": 0,
            },
            {
                "kind": "planned",
                "client_request_id": "crid-B0",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 210.00,
                "take_profit": 300.00,
                "tier_index": 0,
                "gen": 0,
            },
        ]
        planned = cl._fold_planned_exits(lines)[43070]
        self.assertTrue(planned.conflicting)
        self.assertEqual(planned.n_plans, 2)

    def test_tiers_disagree_takes_max_stop_for_a_long(self) -> None:
        lines = [
            {
                "kind": "planned",
                "client_request_id": "crid-0",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 216.48,
                "take_profit": 306.72,
                "tier_index": 0,
                "gen": 0,
            },
            {
                "kind": "planned",
                "client_request_id": "crid-1",
                "uic": 43070,
                "side": "SELL",
                "stop_price": 220.00,
                "take_profit": 297.5,
                "tier_index": 1,
                "gen": 0,
            },
        ]
        planned = cl._fold_planned_exits(lines)[43070]
        self.assertAlmostEqual(planned.stop_price, 220.00)

    def test_planned_line_round_trips_tp_price_through_journal(self) -> None:
        # The Stage-2 upgrade needs a TP price to place; the planned line carries
        # it (memo §7) and the fold reads it back into PlannedExit.tp_price.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                cl._append_standalone_stop_journal(
                    cl._build_planned_line(
                        entry_crid="crid-0",
                        uic=_UIC,
                        side="SELL",
                        stop_price=216.48,
                        take_profit=306.72,
                        tier_index=0,
                    )
                )
                folded = cl._fold_planned_exits(list(cl._iter_standalone_stop_journal()))
        self.assertIn(_UIC, folded)
        self.assertAlmostEqual(folded[_UIC].tp_price or 0.0, 306.72)


class TestFoldOcoUnsupported(unittest.TestCase):
    """Stage 2 (memo §7): the persisted per-instrument OCO-unsupported capability
    flag folds by uic into a ``frozenset[int]``. A uic marked once stays marked
    (append-only, survives a systemd restart) so the rung-2 upgrade is never
    re-attempted on a structurally OCO-incapable instrument."""

    def test_fold_reads_marked_uics_and_skips_other_kinds(self) -> None:
        lines = [
            {"kind": "oco_unsupported", "uic": 43070},
            {"kind": "oco_unsupported", "uic": 111},
            {"kind": "planned", "uic": 999, "client_request_id": "c1", "stop_price": 1.0},
            {"kind": "gen", "uic": 888, "gen": 2, "qty": 5.0},
            {"kind": "oco_unsupported"},  # missing uic — skipped
            {"kind": "oco_unsupported", "uic": "abc"},  # unparsable uic — skipped
        ]
        self.assertEqual(cl._fold_oco_unsupported(lines), frozenset({43070, 111}))

    def test_fold_empty_when_no_lines(self) -> None:
        self.assertEqual(cl._fold_oco_unsupported([]), frozenset())

    def test_mark_round_trips_and_survives_a_fresh_fold(self) -> None:
        # mark -> a FRESH read of the journal (a restart) still carries the flag.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                cl._mark_oco_unsupported(_UIC)
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertIn(_UIC, folded)

    def test_build_protection_view_populates_oco_unsupported_from_journal(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)
                cl._mark_oco_unsupported(_UIC)
                view = cl.build_protection_view(broker, [])  # type: ignore[arg-type]
        self.assertIn(_UIC, view.oco_unsupported)
        # The planned prices still fold alongside the capability flag (one journal read).
        self.assertIn(_UIC, view.planned_by_uic)

    def test_build_protection_view_oco_unsupported_empty_without_mark(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                _seed_planned(journal)
                view = cl.build_protection_view(broker, [])  # type: ignore[arg-type]
        self.assertEqual(view.oco_unsupported, frozenset())


class TestGenStampedRefChangesOnResize(unittest.TestCase):
    """Task 4 (memo §4.5): deterministic gen-stamped request-ids — stable for a
    same-size crash-retry (Saxo dedup catches it), distinct on a resize (never
    falsely deduped to the stale, smaller order). ``gen`` is persisted append-only
    per uic so it survives a daemon restart."""

    def test_ref_helpers_are_gen_stamped(self) -> None:
        self.assertEqual(_exit_stop_ref("crid-0", 0), "crid-0-stop-0")
        self.assertEqual(_exit_tp_ref("crid-0", 0), "crid-0-tp-0")
        self.assertEqual(_exit_stop_ref("crid-0", 2), "crid-0-stop-2")
        self.assertEqual(_exit_tp_ref("crid-0", 3), "crid-0-tp-3")

    def test_resize_increments_gen_same_size_retry_keeps_it(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                next_gen = cl._make_next_gen(43070)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(30.0), 1)
                self.assertEqual(next_gen(30.0), 1)
                self.assertEqual(next_gen(45.0), 2)

    def test_float_tolerance_no_gen_flicker(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                next_gen = cl._make_next_gen(43070)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(45.9999999), 0)

    def test_gen_persists_append_only_across_fresh_callables(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "STANDALONE_STOP_JOURNAL_PATH", journal):
                cl._make_next_gen(43070)(46.0)
                cl._make_next_gen(43070)(30.0)
                self.assertEqual(cl._make_next_gen(43070)(30.0), 1)
                self.assertEqual(cl._make_next_gen(43070)(20.0), 2)
                self.assertEqual(cl._make_next_gen(99999)(10.0), 0)


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
