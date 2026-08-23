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
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails, state_paths
from alphalens_pipeline.brokers.automanager.live_rails import (
    MAX_FEE_BPS_ENV,
    SIZING_EQUITY_ENV,
    SIZING_EQUITY_MODE_ENV,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    AmendStop,
    BrokerView,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    ReanchorFacts,
    UpgradeToOco,
    _exit_amend_ref,
    _exit_oco_ref,
    _exit_stop_ref,
    _exit_tp_ref,
    _reconcile_long,
)
from alphalens_pipeline.brokers.automanager.safety import PORTFOLIO_GROSS_FRAC_ENV
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict
from broker_contract.contract import (
    BrokerCapabilityError,
    BrokerError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)
from broker_contract.exit_geometry import (
    SetupStaticPolicy,
    resolve_exit_policy,
)
from broker_contract.sizing import SetupPlan, TierPlan, TpTranchePlan
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    InstrumentHint,
    IntentMeta,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
    TrailingStop,
)

_RID = "rid-KO"
_UIC = 43070


def _pick(ticker: str = "KO", date: str = "2026-07-20") -> TradeIntent:
    """A minimal, structurally valid armed TradeIntent (PR-7: the daemon drains
    TradeIntent, never a bare (ticker, date) Pick)."""
    spec = TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=2.0, tag="TP1"),),
        suggested_size_pct=2.0,
    )
    return TradeIntent(
        intent_id=f"{ticker}:{date}",
        instrument=InstrumentHint(ticker=ticker.upper(), mic="XNYS"),
        spec=spec,
        meta=IntentMeta(armed_ts="2026-07-20T14:00:00+00:00", brief_date=date),
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
    global_kill_file: Path | None = None,
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
        global_kill_file=global_kill_file,
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


@contextlib.contextmanager
def _isolated_home():
    """Patch ``Path.home()`` to a fresh, empty temp directory for the
    duration of the block.

    ``build_default_deps`` now resolves its state paths (kill_file,
    global_kill_file, the D4 legacy-layout guard) through the state_paths
    seam at call time (ADR 0016 D2-D4) — every test that calls it must be
    isolated from the REAL ``~/.alphalens/broker_orders/`` tree, which on a
    developer machine running the live SIM daemon genuinely holds journal
    files (a pre-ADR-0016 flat layout) that would otherwise make these
    hermetic tests fail non-deterministically depending on host state."""
    with (
        TemporaryDirectory() as home_dir,
        mock.patch("pathlib.Path.home", return_value=Path(home_dir)),
    ):
        yield Path(home_dir)


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


def _leg(
    order_id: str, order_type: str, amount: float, *, uic: int = _UIC, filled: float = 0.0
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
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
        amend_error: Any = None,
    ) -> None:
        self._positions = positions or []
        self._sells = sells or []
        self._by_uic = by_uic or {}
        self._place_error = place_error
        self._place_calls = 0
        self._cancel_errors = cancel_errors or {}
        self._amend_error = amend_error
        self.placed: list[tuple[int, str, float, float, str | None]] = []
        self.cancelled: list[str] = []
        # (uic, order_id, side, order_type, new_qty, stop_price, request_id)
        self.amended: list[tuple[int, str, str, str, float, float, str]] = []

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

    def amend_stop_amount(
        self,
        uic: int,
        order_id: str,
        side: str,
        order_type: str,
        new_qty: float,
        stop_price: float,
        request_id: str,
    ) -> PlacedOrder:
        if self._amend_error is not None:
            raise self._amend_error
        self.amended.append((uic, order_id, side, order_type, new_qty, stop_price, request_id))
        return PlacedOrder(entry_order_id="", exit_order_ids=(order_id,))

    def cancel_order(self, order_id: str) -> None:
        err = self._cancel_errors.get(order_id)
        if err is not None:
            raise err
        self.cancelled.append(order_id)


def _seed_planned(journal: Path, uic: int = _UIC, crid: str = "crid-0") -> None:
    with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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


class TestStandaloneStopJournalDurability(unittest.TestCase):
    """The out-of-band standalone-stop journal is the source of truth for plan
    prices + capability markers; a buffered write lost to a crash silently drops
    a disaster-stop plan. Each append is flushed + fsync'd for crash-durability."""

    def test_append_flushes_and_fsyncs(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                with mock.patch("os.fsync") as fsync:
                    cl._append_standalone_stop_journal({"kind": "gen", "uic": 1, "gen": 0})
                fsync.assert_called_once()
            # The record is durably persisted (survives read-back).
            lines = journal.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('"uic": 1', lines[0])


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

    def test_duplicate_armed_pick_in_one_tick_attempted_once_even_when_place_fails(self) -> None:
        # A within-tick duplicate must be skipped even when the FIRST place returns
        # False (refused / zero-sized / partial-then-failed). The placed_this_tick
        # set records the ATTEMPT, so a same-key line later in the same tick can
        # never re-drive placement (guards the never-double-commit invariant).
        with TemporaryDirectory() as d:
            attempts: list = []
            p1 = _pick("KO", "2026-07-20")
            p2 = _pick("KO", "2026-07-20")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
                picks=[p1, p2],
            )
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "read_records": list,
                    "place_pick": lambda pick: bool(
                        attempts.append(pick)
                    ),  # append -> None -> False
                }
            )
            report = cl.run_once(deps)
            self.assertEqual(
                attempts, [p1], "the duplicate must be skipped even when the first place fails"
            )
            self.assertEqual(report.picks_placed, 0)


class TestRefusedPickNotRetriedAcrossTicks(unittest.TestCase):
    """End-to-end queue semantics over a REAL picks.jsonl: once the placer
    journals a terminal refusal, the NEXT tick's drain never calls the placer
    for that pick again (kills the live 2026-07-30 every-45s retry that would
    self-place a stale week-old brief signal once capacity freed)."""

    def test_refused_pick_is_drained_once_then_never_again(self) -> None:
        from alphalens_pipeline.brokers.automanager import picks as picks_mod

        with TemporaryDirectory() as d:
            picks_path = Path(d) / "picks.jsonl"
            picks_mod.arm_pick(_pick("KO", "2026-07-29"), path=picks_path)
            attempts: list = []

            def _refusing_place(pick: Any) -> bool:
                # Models _place_pick's safety-refusal branch: journal the
                # terminal refusal, do not place.
                attempts.append(pick)
                picks_mod.mark_refused(
                    pick.instrument.ticker,
                    dt.date.fromisoformat(pick.meta.brief_date),
                    "portfolio cap exceeded",
                    path=picks_path,
                )
                return False

            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
            )
            deps = cl.LoopDeps(
                **{
                    **deps.__dict__,
                    "iter_picks": lambda: picks_mod.iter_picks(path=picks_path),
                    "place_pick": _refusing_place,
                    "read_records": list,
                }
            )
            cl.run_once(deps)
            cl.run_once(deps)
            self.assertEqual(len(attempts), 1, "a refused pick must never retry on later ticks")


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
                    "broker_contract.sizing.compute_setup_plan",
                    # A REAL (inert, well-under-cap) plan: the post-sizing gross
                    # cap reads plan.entry_tiers, so a bare object() would crash.
                    lambda _spec, **_k: _fee_plan(10_000.0),
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

    def __init__(
        self,
        *,
        on_account: Any = None,
        on_place: Any = None,
        get_fx_rate: Any = None,
        on_positions: Any = None,
    ):
        self._on_account = on_account
        self._on_place = on_place
        self._on_positions = on_positions
        if get_fx_rate is not None:
            self.get_fx_rate = get_fx_rate  # optional capability probed via getattr

    def get_account(self) -> Any:
        return self._on_account() if self._on_account is not None else _acct()

    def get_positions(self) -> list:
        return list(self._on_positions) if self._on_positions is not None else []

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
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": lambda _b, _t: _instr(),
            "classify": lambda *_a, **_k: _placement(),
            # A REAL (inert, well-under-cap) plan: the post-sizing gross cap
            # reads plan.entry_tiers, so a bare object() would crash.
            "compute_plan": lambda _spec, **_k: _fee_plan(10_000.0),
            "iter_records": lambda _p: [],
            "append": lambda _r: None,
            "build_record": lambda **kw: dict(kw),
            # Default the terminal-refusal writer to a no-op (hermetic — the
            # real writer appends to ~/.alphalens picks.jsonl); tests override
            # with a capture to pin the refused-line append.
            "mark_refused": lambda *_a, **_k: None,
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", m["mark_refused"]))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        return cl._make_place_pick(broker)

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

    def test_terminal_safety_refuse_appends_terminal_refused_line(self) -> None:
        # Queue-semantics fix (2026-07-30): a capacity/cap refusal retires the
        # pick via a terminal refused line — otherwise it retries every ~45s
        # tick and self-places a stale brief signal days later when capacity
        # frees. Re-arming via `alphalens broker arm` is the human path back.
        from alphalens_pipeline.brokers.automanager.safety import Refuse

        refusals: list[tuple] = []
        placer = self._placer(
            _PlaceBroker(),
            safety_check=lambda *_a, **_k: Refuse(reason="portfolio cap exceeded", terminal=True),
            mark_refused=lambda *a: refusals.append(a),
        )
        self.assertFalse(placer(_pick()))
        self.assertEqual(refusals, [("KO", dt.date(2026, 7, 20), "portfolio cap exceeded")])

    def test_non_terminal_safety_refuse_does_not_append_refused_line(self) -> None:
        # Only the CAPACITY rails (MAX_OPEN / portfolio gross) are terminal.
        # The KILL-file, master-arm (ALLOW_ORDERS) and daily-loss rails also
        # return Refuse but are transient by design — an inert/paused daemon
        # must NEVER retire the armed queue; the pick stays armed and places
        # once the rail clears.
        from alphalens_pipeline.brokers.automanager.safety import ALLOW_ORDERS_ENV, Refuse

        for reason in (
            "KILL file present — emergency stop, placement halted",
            f"{ALLOW_ORDERS_ENV} != '1' — master arm not set, placement inert",
            "daily realized r -3.50 <= -3.00 daily-loss limit — the day is closed to new picks",
        ):
            with self.subTest(reason=reason):
                refusals: list[tuple] = []
                placer = self._placer(
                    _PlaceBroker(),
                    safety_check=lambda *_a, _r=reason, **_k: Refuse(reason=_r, terminal=False),
                    mark_refused=lambda *a, _acc=refusals: _acc.append(a),
                )
                self.assertFalse(placer(_pick()))
                self.assertEqual(refusals, [])

    def test_transient_place_error_does_not_append_refused_line(self) -> None:
        # A broker failure during actual placement is transient (429/5xx/reject)
        # — the pick stays armed and retries; no terminal refused line.
        refusals: list[tuple] = []

        def _boom(_bracket: Any) -> Any:
            raise BrokerError("exchange rejected")

        placer = self._placer(
            _PlaceBroker(on_place=_boom), mark_refused=lambda *a: refusals.append(a)
        )
        self.assertFalse(placer(_pick()))
        self.assertEqual(refusals, [])

    def test_broker_read_error_does_not_append_refused_line(self) -> None:
        refusals: list[tuple] = []

        def _boom() -> Any:
            raise BrokerError("account read down")

        placer = self._placer(
            _PlaceBroker(on_account=_boom), mark_refused=lambda *a: refusals.append(a)
        )
        self.assertFalse(placer(_pick()))
        self.assertEqual(refusals, [])

    def test_refused_line_append_oserror_never_crashes_the_drain(self) -> None:
        # The refused-line append is fallible I/O inside the drain: an OSError
        # must be contained (log + return False). The pick then stays armed —
        # the refusal re-fires next tick and re-attempts the append (acceptable
        # degradation, never a crash).
        from alphalens_pipeline.brokers.automanager.safety import Refuse

        def _disk_full(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        placer = self._placer(
            _PlaceBroker(),
            safety_check=lambda *_a, **_k: Refuse(reason="portfolio cap exceeded", terminal=True),
            mark_refused=_disk_full,
        )
        self.assertFalse(placer(_pick()))  # must not raise

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


# --- Sizing cap + fee floor (design memo §4) ---------------------------------


class TestResolveSizingEquity(unittest.TestCase):
    """``_resolve_sizing_equity`` — ``min(pinned, snapshot)`` when
    ``ALPHALENS_BROKER_SIZING_EQUITY`` is explicitly set, else the raw
    account snapshot unchanged (SIM never sets the pin -> byte-identical)."""

    def test_unset_returns_snapshot_unchanged(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(100_000.0), 100_000.0)

    def test_blank_pin_treated_as_unset(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "   "}, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(100_000.0), 100_000.0)

    def test_pinned_below_snapshot_wins(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "10000"}, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(100_000.0), 10_000.0)

    def test_snapshot_below_pinned_wins(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "10000"}, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(5_000.0), 5_000.0)

    def test_malformed_pin_fails_closed_to_zero_equity(self) -> None:
        # A typo'd pin must NEVER crash the tick (drain resilience) and must
        # NEVER silently fall back to the raw snapshot (that would size off
        # the FULL real balance — the exact thing the pin exists to prevent).
        # Fail-closed: effective equity 0.0 -> nothing sizes -> the existing
        # unplannable/zero-tiers refusal path handles the pick.
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "10OOO"}, clear=True):
            with self.assertLogs(cl.logger, level="WARNING") as logs:
                self.assertEqual(cl._resolve_sizing_equity(100_000.0), 0.0)
        self.assertTrue(any(SIZING_EQUITY_ENV in line for line in logs.output))

    # --- declared sizing mode (memo §4.1 broker_sizing_declared_frame) ------

    def test_declared_mode_returns_pin_above_snapshot(self) -> None:
        env = {SIZING_EQUITY_ENV: "16000", SIZING_EQUITY_MODE_ENV: "declared"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(1_984.0), 16_000.0)

    def test_declared_mode_returns_pin_below_snapshot(self) -> None:
        # Declared means the pin IS the frame — not max(pin, snapshot).
        env = {SIZING_EQUITY_ENV: "1000", SIZING_EQUITY_MODE_ENV: "declared"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(5_000.0), 1_000.0)

    def test_mode_env_unset_keeps_min_clamp(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "10000"}, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(5_000.0), 5_000.0)
            self.assertEqual(cl._resolve_sizing_equity(100_000.0), 10_000.0)

    def test_clamped_mode_keeps_min_clamp(self) -> None:
        env = {SIZING_EQUITY_ENV: "10000", SIZING_EQUITY_MODE_ENV: "clamped"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(5_000.0), 5_000.0)
            self.assertEqual(cl._resolve_sizing_equity(100_000.0), 10_000.0)

    def test_unknown_mode_fails_closed_to_zero_equity(self) -> None:
        env = {SIZING_EQUITY_ENV: "10000", SIZING_EQUITY_MODE_ENV: "snapshot"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertLogs(cl.logger, level="WARNING") as logs:
                self.assertEqual(cl._resolve_sizing_equity(100_000.0), 0.0)
        self.assertTrue(any(SIZING_EQUITY_MODE_ENV in line for line in logs.output))

    def test_declared_mode_with_blank_pin_fails_closed_to_zero(self) -> None:
        # Critic B8: declared mode with no pin must NEVER fall back to the
        # raw snapshot — that is exactly the raw-balance sizing the declared
        # frame exists to prevent.
        env = {SIZING_EQUITY_ENV: "   ", SIZING_EQUITY_MODE_ENV: "declared"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertLogs(cl.logger, level="WARNING") as logs:
                self.assertEqual(cl._resolve_sizing_equity(100_000.0), 0.0)
        self.assertTrue(any(SIZING_EQUITY_ENV in line for line in logs.output))
        self.assertTrue(any(SIZING_EQUITY_MODE_ENV in line for line in logs.output))

    def test_declared_mode_with_malformed_pin_still_fails_closed(self) -> None:
        env = {SIZING_EQUITY_ENV: "16OOO", SIZING_EQUITY_MODE_ENV: "declared"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertLogs(cl.logger, level="WARNING") as logs:
                self.assertEqual(cl._resolve_sizing_equity(100_000.0), 0.0)
        self.assertTrue(any(SIZING_EQUITY_ENV in line for line in logs.output))

    def test_mode_value_is_case_and_whitespace_insensitive(self) -> None:
        env = {SIZING_EQUITY_ENV: "16000", SIZING_EQUITY_MODE_ENV: " Declared "}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cl._resolve_sizing_equity(1_984.0), 16_000.0)


class TestResolveAndSizeUsesEffectiveSizingEquity(unittest.TestCase):
    """``_resolve_and_size`` feeds ``compute_setup_plan(paper_equity=...)``
    the EFFECTIVE (min-pinned-snapshot) equity, not the raw account
    snapshot."""

    def _paper_equity(self, *, account_equity: float, env: dict[str, str]) -> Any:
        pkg = "alphalens_pipeline.brokers"
        captured: dict[str, Any] = {}

        def _capture_plan(_spec: Any, **kw: Any) -> Any:
            captured.update(kw)
            return object()

        account = _acct()
        account.total_value = account_equity  # type: ignore[attr-defined]
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.routing.resolve_us_instrument", lambda _b, _t: _instr()))
            p(mock.patch("broker_contract.sizing.compute_setup_plan", _capture_plan))
            p(mock.patch.dict("os.environ", env, clear=True))
            cl._resolve_and_size(_PlaceBroker(), "KO", account, object())
        return captured["paper_equity"]

    def test_pinned_below_snapshot_sizes_off_the_pin(self) -> None:
        equity = self._paper_equity(account_equity=100_000.0, env={SIZING_EQUITY_ENV: "10000"})
        self.assertEqual(equity, 10_000.0)

    def test_pinned_above_snapshot_sizes_off_the_snapshot(self) -> None:
        equity = self._paper_equity(account_equity=5_000.0, env={SIZING_EQUITY_ENV: "10000"})
        self.assertEqual(equity, 5_000.0)

    def test_env_unset_sizes_off_the_raw_snapshot(self) -> None:
        equity = self._paper_equity(account_equity=100_000.0, env={})
        self.assertEqual(equity, 100_000.0)

    def test_declared_mode_sizes_off_the_pin_above_snapshot(self) -> None:
        equity = self._paper_equity(
            account_equity=1_984.0,
            env={SIZING_EQUITY_ENV: "16000", SIZING_EQUITY_MODE_ENV: "declared"},
        )
        self.assertEqual(equity, 16_000.0)


def _fee_plan(notional: float) -> SetupPlan:
    """A minimal ``SetupPlan`` whose ``setup_plan_gross_notional`` is exactly
    ``notional`` (a single fully-filled tier at a $10 limit)."""
    qty = int(notional // 10.0)
    return SetupPlan(
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        total_notional=notional,
        paper_equity=100_000.0,
        disaster_stop=90.0,
        order_ttl_days=1,
        entry_tiers=(TierPlan(tier_index=0, limit_price=10.0, qty=qty, alloc_pct=100.0, tag="T1"),),
        tp_tranches=(),
    )


class TestRoundTripFeeBps(unittest.TestCase):
    """``_round_trip_fee_bps`` — design memo §4:
    ``fee_rt(N) = 2 x max($1, 0.08% x N) + (0.50% x N if FX applies else 0)``,
    reported as bps of ``N``."""

    def test_ad_valorem_commission_dominates_above_the_min_fee_crossover(self) -> None:
        # 0.0008 x 10_000 = $8 > $1 floor -> commission = 2 x $8 = $16 -> 16 bps.
        self.assertAlmostEqual(cl._round_trip_fee_bps(10_000.0, fx_applies=False), 16.0)

    def test_min_fee_floor_dominates_below_the_crossover(self) -> None:
        # 0.0008 x 100 = $0.08 < $1 floor -> commission = 2 x $1 = $2 -> 200 bps.
        self.assertAlmostEqual(cl._round_trip_fee_bps(100.0, fx_applies=False), 200.0)

    def test_fx_round_trip_adds_fifty_bps(self) -> None:
        # N=1000: commission = 2 x max(1, 0.8) = $2 -> 20 bps; + 0.005*1000 = $5 -> +50 bps.
        no_fx = cl._round_trip_fee_bps(1000.0, fx_applies=False)
        with_fx = cl._round_trip_fee_bps(1000.0, fx_applies=True)
        self.assertAlmostEqual(no_fx, 20.0)
        self.assertAlmostEqual(with_fx, 70.0)

    def test_zero_notional_returns_zero_never_divides(self) -> None:
        self.assertEqual(cl._round_trip_fee_bps(0.0, fx_applies=False), 0.0)


class TestCheckFeeFloor(unittest.TestCase):
    """``_check_fee_floor`` — the env gate + refusal-message assembly around
    ``_round_trip_fee_bps``."""

    def test_env_unset_returns_none_even_at_tiny_notional(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(cl._check_fee_floor(_fee_plan(50.0), None, ticker="KO"))

    def test_under_cap_returns_none(self) -> None:
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            self.assertIsNone(cl._check_fee_floor(_fee_plan(10_000.0), None, ticker="KO"))

    def test_over_cap_names_ticker_and_fee_in_the_message(self) -> None:
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            message = cl._check_fee_floor(_fee_plan(50.0), None, ticker="KO")
        self.assertIsNotNone(message)
        assert message is not None  # narrows for the type checker
        self.assertIn("KO", message)
        self.assertIn("fee", message.lower())

    def test_fx_conversion_can_flip_a_pass_into_a_refusal(self) -> None:
        # Same notional, same cap: the FX leg alone tips a pass into a refusal
        # (pins the fx-applies branch distinctly from the same-currency path).
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "50"}, clear=True):
            self.assertIsNone(cl._check_fee_floor(_fee_plan(1000.0), None, ticker="KO"))
            self.assertIsNotNone(cl._check_fee_floor(_fee_plan(1000.0), object(), ticker="KO"))

    def test_malformed_cap_fails_closed_to_a_refusal(self) -> None:
        # A typo'd fee cap must NEVER crash the tick and must NEVER silently
        # disable the floor (fail-open) — the pick is refused with a message
        # naming the env var so the operator fixes the unit, not the pick.
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "1O0"}, clear=True):
            message = cl._check_fee_floor(_fee_plan(10_000.0), None, ticker="KO")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn(MAX_FEE_BPS_ENV, message)

    def test_non_usd_instrument_skips_the_min_commission_clamp(self) -> None:
        # The $1-min commission is a USD-venue figure; comparing it against a
        # notional denominated in another currency mixes units (a 50-PLN
        # notional is NOT $50). Non-USD instruments get the ad-valorem rate
        # only — memo §4's clamp is calibrated for the first-cohort US venue.
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            self.assertIsNotNone(
                cl._check_fee_floor(_fee_plan(50.0), None, ticker="KO", instrument_currency="USD")
            )
            self.assertIsNone(
                cl._check_fee_floor(_fee_plan(50.0), None, ticker="CDR", instrument_currency="PLN")
            )


class _RecordingBroker(_PlaceBroker):
    """``_PlaceBroker`` that records every ``place_bracket_order`` call, so a
    fee-floor test can assert the broker was NEVER reached."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.placed: list[Any] = []

    def place_bracket_order(self, bracket: Any) -> Any:
        self.placed.append(bracket)
        return super().place_bracket_order(bracket)


class TestPlacePickFeeFloorIntegration(unittest.TestCase):
    """The fee floor gate inside ``_place_pick`` (design memo §4): computed
    AFTER the setup plan + fx are known, BEFORE any bracket construction or
    placement. Mirrors the existing terminal safety-refusal flow (mark_refused
    + throttled alert), never a bare skip."""

    def _placer(
        self, broker: Any, *, notional: float, resolve: Any = None, **over: Any
    ) -> tuple[Any, list[tuple[str, str]], list[tuple[Any, ...]]]:
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        alerts: list[tuple[str, str]] = []
        refusals: list[tuple[Any, ...]] = []

        def _alert_throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        m: dict[str, Any] = {
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": resolve if resolve is not None else (lambda _b, _t: _instr()),
            "classify": lambda *_a, **_k: _placement(),
            "compute_plan": lambda _spec, **_k: _fee_plan(notional),
            "iter_records": lambda _p: [],
            "append": lambda _r: None,
            "build_record": lambda **kw: dict(kw),
            "mark_refused": lambda *a: refusals.append(a),
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", m["mark_refused"]))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        placer = cl._make_place_pick(broker, alert_throttled=_alert_throttled)
        return placer, alerts, refusals

    def test_journal_records_the_effective_sizing_equity_not_the_snapshot(self) -> None:
        # Audit fidelity (T8): the plan is sized off min(pin, snapshot); the
        # submission record must carry that SAME figure — a record stamped
        # with the raw balance would misdescribe every quantity in it.
        appended: list[dict] = []
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {SIZING_EQUITY_ENV: "10000"}, clear=True):
            placer, _alerts, _refusals = self._placer(
                broker, notional=10_000.0, append=appended.append
            )
            self.assertTrue(placer(_pick()))
        # Two records per placement since the write-ahead dedup line (memo
        # §4.4 B2): the note-only "placement attempt" + the real bracket
        # record — BOTH must carry the effective equity.
        self.assertEqual(len(appended), 2)
        for record in appended:
            self.assertEqual(
                record["sizing_equity"],
                10_000.0,
                "journal must record the effective (pinned) equity, not total_value=100000",
            )

    def test_small_notional_over_cap_is_refused_terminal_never_placed(self) -> None:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            placer, alerts, refusals = self._placer(broker, notional=50.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [], "the fee floor must refuse BEFORE any bracket places")
        self.assertEqual(len(refusals), 1)
        ticker, brief_date, reason = refusals[0]
        self.assertEqual(ticker, "KO")
        self.assertEqual(brief_date, dt.date(2026, 7, 20))
        self.assertIn("fee", reason.lower())
        self.assertEqual(len(alerts), 1)
        message, reason_key = alerts[0]
        self.assertIn("fee", message.lower())
        self.assertIn("KO", reason_key)

    def test_large_notional_under_cap_places(self) -> None:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            placer, alerts, refusals = self._placer(broker, notional=10_000.0)
            self.assertTrue(placer(_pick()))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_env_unset_skips_fee_check_even_at_tiny_notional(self) -> None:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {}, clear=True):
            placer, alerts, refusals = self._placer(broker, notional=1.0)
            self.assertTrue(placer(_pick()))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_alert_throttled_none_is_tolerated(self) -> None:
        # Every pre-existing call site / test builds the placer without an
        # alert sink at all — the fee floor must still refuse + journal.
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        refusals: list[tuple[Any, ...]] = []
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", lambda *a: refusals.append(a)))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", lambda _r: None))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", lambda _p: []))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b: []))
        p(mock.patch(f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", lambda _b, _t: _instr()))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", lambda _s, **_k: _fee_plan(50.0)))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "100"}, clear=True):
            placer = cl._make_place_pick(_PlaceBroker())
            self.assertFalse(placer(_pick()))  # must not raise despite no alert sink
        self.assertEqual(len(refusals), 1)

    def test_fx_conversion_increases_the_fee_and_can_flip_a_refusal(self) -> None:
        # EUR instrument vs USD account -> fx builds; the FX round-trip leg
        # alone can push a pick that would otherwise pass into a refusal.
        fx_obj = type("FX", (), {"rate": 1.1})()
        broker = _RecordingBroker(get_fx_rate=lambda _base, _quote: 1.1)
        with (
            mock.patch(
                "alphalens_pipeline.brokers.execution.build_fx_conversion", lambda _r: fx_obj
            ),
            mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "50"}, clear=True),
        ):
            placer, _alerts, refusals = self._placer(
                broker, notional=1000.0, resolve=lambda _b, _t: _instr(currency="EUR")
            )
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)


# --- Post-sizing portfolio gross cap (broker sizing memo §3) -----------------


def _fx(rate: float, instrument_currency: str = "") -> Any:
    """A minimal FxConversion double — ``rate`` is instrument-ccy per 1
    account-ccy (broker_contract.fx.FxConversion direction). An empty
    ``instrument_currency`` (the default) leaves the currency guard inert,
    mirroring doubles that predate the guard."""
    return type("FX", (), {"rate": rate, "instrument_currency": instrument_currency})()


def _position(market_value: float | None, ticker: str = "NVAX", currency: str = "USD") -> Position:
    """A broker Position whose mark-to-market is exactly ``market_value``
    (INSTRUMENT currency; ``None`` = broker quote unavailable, SIM NoAccess)."""
    return Position(
        instrument=InstrumentRef(
            ticker=ticker,
            exchange_mic="XNAS",
            asset_type="Stock",
            broker_instrument_id="6820",
            broker_symbol=f"{ticker}:xnas",
            currency=currency,
        ),
        quantity=100.0,
        avg_price=10.0,
        market_value=market_value,
        unrealized_pnl=None,
        position_id="P-1",
    )


class TestCheckGrossCap(unittest.TestCase):
    """``_check_gross_cap`` — the POST-sizing, candidate-inclusive,
    ACCOUNT-currency gross check (sibling of ``_check_fee_floor``):

        committed_working_acct + candidate_gross_acct + filled_positions_acct
            <= GROSS_FRAC x account.total_value

    Repairs the three pre-sizing ``safety.check`` gross-rail defects: currency
    mismatch (journal gross is instrument-ccy), candidate exclusion (first
    pick of any size always passed), and filled-position blindness."""

    def setUp(self) -> None:
        # Isolate the entry-trails seam: without this the class would
        # implicitly depend on the developer's real
        # ~/.alphalens/broker_orders/<env>/entry_trails.jsonl being absent —
        # green today (nothing writes it yet), a latent cross-test flake once
        # PR-T1 starts journaling on a machine that also runs tests.
        _entry_trail_journal(self, None)

    def _check(
        self,
        *,
        notional: float = 10_000.0,
        fx: Any = None,
        verdicts: Any = (),
        records: Any = (),
        positions: Any = (),
    ) -> str | None:
        return cl._check_gross_cap(
            _fee_plan(notional),
            fx,
            account=_acct(),
            open_verdicts=list(verdicts),
            records=list(records),
            positions=list(positions),
            ticker="KO",
        )

    def test_candidate_alone_under_limit_passes(self) -> None:
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.2"}, clear=True):
            self.assertIsNone(self._check(notional=10_000.0))

    def test_env_unset_uses_the_default_gross_frac(self) -> None:
        # safety.DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0 -> limit = total_value.
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(self._check(notional=10_000.0))

    def test_candidate_tips_over_limit_names_all_components(self) -> None:
        # The pre-sizing safety.check runs BEFORE sizing, so the candidate
        # itself never counted — the first pick of any size always passed.
        # Here the candidate ALONE must tip the total over the limit, and the
        # message must name the total, all three components, the limit,
        # GROSS_FRAC and total_value.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            message = self._check(notional=10_000.0)
        self.assertIsNotNone(message)
        assert message is not None  # narrows for the type checker
        self.assertIn("KO", message)
        self.assertIn("10,000.00", message)  # total (== candidate here)
        self.assertIn("5,000.00", message)  # limit
        self.assertIn("0.05", message)  # GROSS_FRAC
        self.assertIn("100,000.00", message)  # account total_value
        self.assertIn("0.00", message)  # the empty working/filled components

    def test_candidate_fx_notional_divides_into_account_currency(self) -> None:
        # fx.rate is instrument-ccy per 1 account-ccy: 10_000 USD at rate 0.25
        # is 40_000 in account currency (division — a multiplication bug would
        # yield 2_500 and pass). Same-currency (fx=None) folds raw and passes.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.3"}, clear=True):
            self.assertIsNotNone(self._check(notional=10_000.0, fx=_fx(0.25)))
            self.assertIsNone(self._check(notional=10_000.0, fx=None))

    def test_committed_working_converts_via_the_records_own_journaled_rate(self) -> None:
        # Journaled entry x qty is INSTRUMENT currency; each record folds
        # through its OWN journaled fx_rate (acct->instr Mid): 1_600 USD at
        # rate 0.4 is 4_000 in account currency. Unconverted (the pre-sizing
        # defect) it would be 1_600 and the total would pass the 4_000 limit.
        working = _verdict(status="WORKING", details={"client_request_id": "rid-a"})
        records = [
            {
                "brackets": [{"client_request_id": "rid-a", "entry": 10.0, "qty": 160}],
                "fx_rate": 0.4,
            }
        ]
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.04"}, clear=True):
            message = self._check(notional=100.0, verdicts=[working], records=records)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("4,000.00", message)

    def test_committed_record_without_fx_rate_folds_raw(self) -> None:
        # Schema-1 / same-currency records carry fx_rate null — fold as-is.
        working = _verdict(status="WORKING", details={"client_request_id": "rid-a"})
        records = [{"brackets": [{"client_request_id": "rid-a", "entry": 10.0, "qty": 95}]}]
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.01"}, clear=True):
            message = self._check(notional=100.0, verdicts=[working], records=records)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("950.00", message)

    def test_exactly_at_the_limit_passes(self) -> None:
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.01"}, clear=True):
            self.assertIsNone(self._check(notional=1_000.0))

    def test_non_working_verdicts_do_not_count_toward_committed(self) -> None:
        # A FILLED verdict's exposure enters via broker positions, never via
        # the journal (that would double-count once the position exists).
        filled = _verdict(status="FILLED", details={"client_request_id": "rid-a"})
        records = [{"brackets": [{"client_request_id": "rid-a", "entry": 10.0, "qty": 1000}]}]
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            self.assertIsNone(self._check(notional=100.0, verdicts=[filled], records=records))

    def test_working_verdict_without_a_joinable_bracket_fails_closed(self) -> None:
        # A working verdict we cannot join to a journaled entry bracket is
        # real broker exposure the cap cannot value — refusing beats silently
        # under-counting on a money rail (zen pre-merge finding; contrast the
        # pre-sizing _summarize_open_verdicts, which tolerates the same skew
        # because its rail is only the cheap early exit).
        orphan = _verdict(status="WORKING", details={"client_request_id": "rid-unknown"})
        no_entry = _verdict(status="WORKING", details={"client_request_id": "rid-b"})
        records = [{"brackets": [{"client_request_id": "rid-b", "entry": None, "qty": 5}]}]
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            violation = self._check(notional=100.0, verdicts=[orphan, no_entry], records=records)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("2 working order(s)", violation)
        self.assertIn("failing closed", violation)

    def test_duplicate_request_ids_across_records_fold_the_last_journaled_bracket(self) -> None:
        # The join index is a dict comprehension — a client_request_id
        # journaled twice (e.g. a re-journaled record) resolves to the LAST
        # record's bracket + fx_rate. Pin that last-wins semantics so a future
        # refactor to first-wins is a deliberate choice, not an accident.
        verdict = _verdict(status="WORKING", details={"client_request_id": "rid-dup"})
        records = [
            {"brackets": [{"client_request_id": "rid-dup", "entry": 10.0, "qty": 100}]},
            {
                "fx_rate": 0.5,
                "brackets": [{"client_request_id": "rid-dup", "entry": 8.0, "qty": 100}],
            },
        ]
        # Last record wins: 8.0 x 100 / 0.5 = 1_600 acct-ccy committed; with
        # the candidate 100 the total 1_700 exceeds the 1_000 limit. First-
        # wins (10.0 x 100 raw = 1_000 committed + 100) would ALSO refuse, so
        # pin via the message's committed component instead of the verdict.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.01"}, clear=True):
            violation = self._check(notional=100.0, verdicts=[verdict], records=records)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("working 1,600.00", violation)

    def test_position_in_a_foreign_currency_fails_closed(self) -> None:
        # The single candidate fx rate can only convert positions trading in
        # the SAME instrument currency — a stamped mismatching currency must
        # refuse, never mis-value through a foreign rate (zen pre-merge
        # finding).
        eur_position = _position(1_000.0, ticker="SAP", currency="EUR")
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            violation = self._check(
                notional=100.0,
                fx=_fx(0.25, instrument_currency="USD"),
                positions=[eur_position],
            )
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("SAP", violation)
        self.assertIn("EUR", violation)
        self.assertIn("failing closed", violation)

    def test_position_with_unstamped_currency_is_tolerated(self) -> None:
        # InstrumentRef.currency is "" on best-effort reverse-lookup position
        # rows (contract docstring) — absent is not wrong; the guard fires
        # only on a STAMPED mismatch.
        unstamped = _position(1_000.0, currency="")
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            self.assertIsNone(
                self._check(
                    notional=100.0,
                    fx=_fx(1.0, instrument_currency="USD"),
                    positions=[unstamped],
                )
            )

    def test_filled_positions_convert_via_the_candidates_fx(self) -> None:
        # market_value is INSTRUMENT currency: 7_000 USD at rate 0.25 is
        # 28_000 in account currency (+ candidate 4_000 = 32_000 > 30_000).
        # Same-currency (fx=None): 7_000 + 1_000 = 8_000 passes.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.3"}, clear=True):
            message = self._check(notional=1_000.0, fx=_fx(0.25), positions=[_position(7_000.0)])
            self.assertIsNotNone(message)
            assert message is not None
            self.assertIn("28,000.00", message)
            self.assertIsNone(self._check(notional=1_000.0, positions=[_position(7_000.0)]))

    def test_position_with_no_mark_fails_closed(self) -> None:
        # A None mark (SIM NoAccess) cannot be valued conservatively HIGH
        # without a price — refuse rather than silently skip the position,
        # even when the limit is nowhere near.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            message = self._check(notional=100.0, positions=[_position(None)])
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("NVAX", message)
        self.assertIn("KO", message)

    def test_malformed_gross_frac_env_falls_back_to_default(self) -> None:
        # Mirrors safety._float_env: a typo'd fraction falls back to the
        # DEFAULT (1.0) — exactly how safety.check reads the same env var, so
        # the pre- and post-sizing rails never disagree on the limit.
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.O"}, clear=True):
            self.assertIsNone(self._check(notional=10_000.0))


class TestPlacePickGrossCapIntegration(unittest.TestCase):
    """The gross cap gate inside ``_place_pick``: computed AFTER the fee floor
    (same post-sizing inputs), BEFORE any bracket construction or placement.
    Violation mirrors the fee floor's terminal refusal flow verbatim
    (mark_refused + throttled alert, NO submission record)."""

    def setUp(self) -> None:
        # See TestCheckGrossCap.setUp — isolate the entry-trails seam.
        _entry_trail_journal(self, None)

    def _placer(
        self, broker: Any, *, notional: float, **over: Any
    ) -> tuple[Any, list[tuple[str, str]], list[tuple[Any, ...]], list[Any]]:
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        alerts: list[tuple[str, str]] = []
        refusals: list[tuple[Any, ...]] = []
        appended: list[Any] = []

        def _alert_throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        m: dict[str, Any] = {
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": lambda _b, _t: _instr(),
            "classify": lambda *_a, **_k: _placement(),
            "compute_plan": lambda _spec, **_k: _fee_plan(notional),
            "iter_records": lambda _p: [],
            "append": appended.append,
            "build_record": lambda **kw: dict(kw),
            "mark_refused": lambda *a: refusals.append(a),
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", m["mark_refused"]))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        placer = cl._make_place_pick(broker, alert_throttled=_alert_throttled)
        return placer, alerts, refusals, appended

    def test_over_cap_refused_terminal_never_placed_no_submission_record(self) -> None:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            placer, alerts, refusals, appended = self._placer(broker, notional=10_000.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [], "the gross cap must refuse BEFORE any bracket places")
        self.assertEqual(appended, [], "a refused pick must never journal a submission record")
        self.assertEqual(len(refusals), 1)
        ticker, brief_date, reason = refusals[0]
        self.assertEqual(ticker, "KO")
        self.assertEqual(brief_date, dt.date(2026, 7, 20))
        self.assertIn("gross", reason.lower())
        self.assertEqual(len(alerts), 1)
        message, reason_key = alerts[0]
        self.assertIn("gross", message.lower())
        self.assertEqual(reason_key, "gross-cap:KO")

    def test_under_cap_places(self) -> None:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.5"}, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=10_000.0)
            self.assertTrue(placer(_pick()))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_committed_working_exposure_counts_toward_the_cap(self) -> None:
        # The wiring half of the committed-working component: the SAME
        # verdicts + records _place_pick already fetched for safety.check
        # feed the post-sizing check (45_000 working + 10_000 candidate
        # > 0.5 x 100_000).
        broker = _RecordingBroker()
        working = _verdict(status="WORKING", details={"client_request_id": "rid-x"})
        records = [{"brackets": [{"client_request_id": "rid-x", "entry": 10.0, "qty": 4500}]}]
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.5"}, clear=True):
            placer, _alerts, refusals, _appended = self._placer(
                broker,
                notional=10_000.0,
                verdicts=lambda _r, _b: [working],
                iter_records=lambda _p: records,
            )
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)

    def test_position_mark_missing_fails_closed_never_placed(self) -> None:
        broker = _RecordingBroker(on_positions=[_position(None)])
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=100.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][1], "gross-cap:KO")

    def test_refused_line_append_oserror_never_crashes_the_drain(self) -> None:
        # _refuse_pick_terminal contains the fallible mark_refused I/O: on
        # OSError the drain must survive (return False), the pick stays armed
        # and the refusal re-fires next tick.
        def _disk_full(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            placer, _alerts, _refusals, _appended = self._placer(
                broker, notional=10_000.0, mark_refused=_disk_full
            )
            self.assertFalse(placer(_pick()))  # must not raise
        self.assertEqual(broker.placed, [])

    def test_fee_floor_violation_wins_when_both_gates_trip(self) -> None:
        # Sibling ordering: the fee floor runs first, so a pick violating both
        # gates is refused with the FEE message/key (one page, not two).
        broker = _RecordingBroker()
        env = {MAX_FEE_BPS_ENV: "100", PORTFOLIO_GROSS_FRAC_ENV: "0.0001"}
        with mock.patch.dict("os.environ", env, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=50.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(len(refusals), 1)
        self.assertIn("fee", refusals[0][2].lower())
        self.assertEqual(alerts[0][1], "fee-floor:KO")


# --- Cash floor (broker sizing declared-frame memo §4.2) ---------------------


def _cash_acct(margin_available: Any, currency: str = "USD") -> Any:
    """An account double carrying ``margin_available`` (the cash floor's
    ``available`` input — P1 probe: a resting Saxo buy reserves NOTHING in
    ANY balance field, so the floor folds its own reservation ledger)."""
    return type(
        "A",
        (),
        {"total_value": 100_000.0, "currency": currency, "margin_available": margin_available},
    )()


_DECLARED_ENV = {SIZING_EQUITY_ENV: "16000", SIZING_EQUITY_MODE_ENV: "declared"}


class TestCheckCashFloor(unittest.TestCase):
    """``_check_cash_floor`` — sibling of ``_check_fee_floor``, active ONLY in
    declared sizing mode (memo §4.2):

        candidate_buffered + reserved_resting > margin_available -> refuse

    where ``candidate_buffered`` is the whole-ladder gross in ACCOUNT currency
    x (1 + 4% buffer) and ``reserved_resting`` is the committed-working entry
    gross folded from the journal (P1: the broker reserves nothing for a
    resting buy, so the floor must carry its own reservation ledger)."""

    def _check(
        self,
        *,
        notional: float = 10_000.0,
        fx: Any = None,
        verdicts: Any = (),
        records: Any = (),
        margin_available: Any = 50_000.0,
    ) -> str | None:
        return cl._check_cash_floor(
            _fee_plan(notional),
            fx,
            account=_cash_acct(margin_available),
            open_verdicts=list(verdicts),
            records=list(records),
            ticker="KO",
        )

    def test_clamped_or_unset_mode_returns_none_even_when_broke(self) -> None:
        # Byte-identical inertness outside declared mode: SIM (unset) and an
        # explicit clamped unit never consult the floor, however broke.
        for env in ({}, {SIZING_EQUITY_ENV: "10000", SIZING_EQUITY_MODE_ENV: "clamped"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env, clear=True):
                self.assertIsNone(self._check(notional=10_000.0, margin_available=1.0))

    def test_declared_and_sufficient_returns_none(self) -> None:
        # 10_000 x 1.04 = 10_400 <= 20_000.
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNone(self._check(notional=10_000.0, margin_available=20_000.0))

    def test_declared_candidate_alone_over_names_amounts_and_currency(self) -> None:
        # 10_000 x 1.04 = 10_400 > 5_000 — the message must name the buffered
        # candidate, the (empty) resting reservation, the available figure and
        # the account currency, so the operator can size the deposit.
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            message = self._check(notional=10_000.0, margin_available=5_000.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("KO", message)
        self.assertIn("10,400.00", message)  # candidate_buffered
        self.assertIn("0.00", message)  # reserved_resting (empty journal)
        self.assertIn("5,000.00", message)  # available
        self.assertIn("USD", message)  # account currency

    def test_reserved_resting_tips_it_over_the_p1_regression(self) -> None:
        # THE P1 regression: the candidate alone fits (10_400 <= 12_000) but a
        # resting journaled buy (10.0 x 200 = 2_000) the broker does NOT
        # reserve for pushes the total to 12_400 > 12_000. Without the folded
        # reservation ledger this pick would double-spend the same cash.
        working = _verdict(status="WORKING", details={"client_request_id": "rid-a"})
        records = [{"brackets": [{"client_request_id": "rid-a", "entry": 10.0, "qty": 200}]}]
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNone(self._check(notional=10_000.0, margin_available=12_000.0))
            message = self._check(
                notional=10_000.0,
                margin_available=12_000.0,
                verdicts=[working],
                records=records,
            )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("2,000.00", message)  # reserved_resting

    def test_margin_available_none_fails_closed(self) -> None:
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            message = self._check(notional=10_000.0, margin_available=None)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("KO", message)
        self.assertIn("margin_available", message)

    def test_fx_notional_divides_into_account_currency(self) -> None:
        # fx.rate is instrument-ccy per 1 account-ccy: 10_000 USD at rate 0.25
        # is 40_000 acct-ccy, buffered 41_600 > 41_000 (a multiplication bug
        # would yield 2_600 and pass). Same-currency folds raw and passes.
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            message = self._check(notional=10_000.0, fx=_fx(0.25), margin_available=41_000.0)
            self.assertIsNotNone(message)
            assert message is not None
            self.assertIn("41,600.00", message)
            self.assertIsNone(self._check(notional=10_000.0, fx=None, margin_available=41_000.0))

    def test_buffer_tips_a_borderline_candidate(self) -> None:
        # Unbuffered 10_000 fits 10_200; the 4% funding buffer (10_400) must
        # tip it — entry commissions + one-way FX markup + drift over GTD-7d
        # + T+2 are real cash the entry fill will need.
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNotNone(self._check(notional=10_000.0, margin_available=10_200.0))

    def test_zero_gross_returns_none_before_the_margin_read(self) -> None:
        # An unplannable/zero-tier pick funds nothing — inert even when the
        # margin field is unusable (the zero-tiers refusal downstream owns it).
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNone(self._check(notional=0.0, margin_available=None))


class TestPlacePickCashFloorIntegration(unittest.TestCase):
    """The cash floor gate inside ``_place_pick``: AFTER the gross cap (same
    post-sizing inputs), BEFORE classify. Violation mirrors the fee-floor /
    gross-cap terminal refusal flow verbatim (mark_refused + throttled alert,
    NO submission record)."""

    def setUp(self) -> None:
        # See TestCheckGrossCap.setUp — isolate the entry-trails seam.
        _entry_trail_journal(self, None)

    def _placer(
        self, broker: Any, *, notional: float, **over: Any
    ) -> tuple[Any, list[tuple[str, str]], list[tuple[Any, ...]], list[Any]]:
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        alerts: list[tuple[str, str]] = []
        refusals: list[tuple[Any, ...]] = []
        appended: list[Any] = []

        def _alert_throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        m: dict[str, Any] = {
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": lambda _b, _t: _instr(),
            "classify": lambda *_a, **_k: _placement(),
            "compute_plan": lambda _spec, **_k: _fee_plan(notional),
            "iter_records": lambda _p: [],
            "append": appended.append,
            "build_record": lambda **kw: dict(kw),
            "mark_refused": lambda *a: refusals.append(a),
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", m["mark_refused"]))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        placer = cl._make_place_pick(broker, alert_throttled=_alert_throttled)
        return placer, alerts, refusals, appended

    def test_declared_refusal_terminal_alerted_nothing_placed_no_record(self) -> None:
        broker = _RecordingBroker(on_account=lambda: _cash_acct(5_000.0))
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            placer, alerts, refusals, appended = self._placer(broker, notional=10_000.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(broker.placed, [], "the cash floor must refuse BEFORE any bracket places")
        self.assertEqual(appended, [], "a refused pick must never journal a submission record")
        self.assertEqual(len(refusals), 1)
        ticker, brief_date, reason = refusals[0]
        self.assertEqual(ticker, "KO")
        self.assertEqual(brief_date, dt.date(2026, 7, 20))
        self.assertIn("cash", reason.lower())
        self.assertEqual(len(alerts), 1)
        message, reason_key = alerts[0]
        self.assertIn("cash", message.lower())
        self.assertEqual(reason_key, "cash-floor:KO")

    def test_clamped_mode_places_as_today(self) -> None:
        # Same broke account, mode clamped: the floor never consults the
        # balance — byte-identical to pre-cash-floor behavior.
        broker = _RecordingBroker(on_account=lambda: _cash_acct(5_000.0))
        env = {SIZING_EQUITY_ENV: "10000", SIZING_EQUITY_MODE_ENV: "clamped"}
        with mock.patch.dict("os.environ", env, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=10_000.0)
            self.assertTrue(placer(_pick()))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_declared_and_funded_places(self) -> None:
        broker = _RecordingBroker(on_account=lambda: _cash_acct(50_000.0))
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=10_000.0)
            self.assertTrue(placer(_pick()))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_gross_cap_fires_before_cash_floor_when_both_trip(self) -> None:
        # Sibling ordering: fee floor -> gross cap -> cash floor. A pick
        # violating both later gates is refused with the GROSS message/key
        # (one page, not two).
        broker = _RecordingBroker(on_account=lambda: _cash_acct(5_000.0))
        env = {**_DECLARED_ENV, PORTFOLIO_GROSS_FRAC_ENV: "0.0001"}
        with mock.patch.dict("os.environ", env, clear=True):
            placer, alerts, refusals, _appended = self._placer(broker, notional=10_000.0)
            self.assertFalse(placer(_pick()))
        self.assertEqual(len(refusals), 1)
        self.assertIn("gross", refusals[0][2].lower())
        self.assertEqual(alerts[0][1], "gross-cap:KO")


def _entry_trail_journal(test: unittest.TestCase, lines: list[str] | None) -> None:
    """Point the entry-trails journal seam at a temp file holding ``lines``
    (``None`` = no journal at all) for the duration of the test."""
    d = TemporaryDirectory()
    test.addCleanup(d.cleanup)
    journal = Path(d.name) / "entry_trails.jsonl"
    if lines is not None:
        journal.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    patcher = mock.patch.object(entry_trails, "_entry_trail_journal_path", lambda: journal)
    patcher.start()
    test.addCleanup(patcher.stop)


def _watch_open_line(crid: str = "crid-w0", *, limit: float = 10.0, qty: float = 450.0) -> str:
    import json

    return json.dumps(
        {
            "kind": "watch_open",
            "crid": crid,
            "limit": limit,
            "qty": qty,
            "d_bps": 50,
            "window_end": "2026-08-21",
            "fx_rate": None,
        },
        sort_keys=True,
    )


class TestCheckGrossCapWatchingReservation(unittest.TestCase):
    """The G5 watching-reservation term inside ``_check_gross_cap``
    (entry-trailing PR-T0): watching tiers have NO broker order, so the cap
    folds their limit-valued reservation from ``entry_trails.jsonl``. With no
    journal the verdicts AND the message text are byte-identical to the
    pre-trailing gate (inertness proof)."""

    def _check(self, *, notional: float = 10_000.0) -> str | None:
        return cl._check_gross_cap(
            _fee_plan(notional),
            None,
            account=_acct(),
            open_verdicts=[],
            records=[],
            positions=[],
            ticker="KO",
        )

    def test_no_journal_accept_and_refusal_message_are_byte_identical(self) -> None:
        _entry_trail_journal(self, None)
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.2"}, clear=True):
            self.assertIsNone(self._check(notional=10_000.0))
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            message = self._check(notional=10_000.0)
        self.assertEqual(
            message,
            "gross cap: KO total gross 10,000.00 USD (working 0.00 + candidate 10,000.00 "
            "+ filled 0.00) exceeds limit 5,000.00 (0.05 x total_value 100,000.00) "
            "— pick refused",
            "with no entry-trails journal the refusal text must not change",
        )

    def test_watching_reservation_tips_the_pick_over_and_is_named(self) -> None:
        # Candidate 1_000 alone fits the 5_000 limit; a non-terminal watching
        # tier reserving 10.0 x 450 = 4_500 pushes the total to 5_500.
        _entry_trail_journal(self, [_watch_open_line(limit=10.0, qty=450.0)])
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            message = self._check(notional=1_000.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("watching 4,500.00", message)
        self.assertIn("5,500.00", message)

    def test_terminal_tier_releases_its_reservation(self) -> None:
        import json

        _entry_trail_journal(
            self,
            [
                _watch_open_line(limit=10.0, qty=450.0),
                json.dumps({"kind": "expired", "crid": "crid-w0"}),
            ],
        )
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}, clear=True):
            self.assertIsNone(self._check(notional=1_000.0))

    def test_malformed_entry_trail_record_fails_closed(self) -> None:
        # Exactly like the unjoined-working-orders path: a record the fold
        # cannot value may be a reservation the cap cannot see — refuse.
        _entry_trail_journal(self, ["{not json"])
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            message = self._check(notional=100.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("KO", message)
        self.assertIn("1 entry-trail", message)
        self.assertIn("failing closed", message)

    def test_unvaluable_watch_open_fails_closed(self) -> None:
        import json

        _entry_trail_journal(
            self,
            [json.dumps({"kind": "watch_open", "crid": "crid-w0", "limit": None, "qty": 5})],
        )
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            message = self._check(notional=100.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("failing closed", message)


class TestCheckCashFloorWatchingReservation(unittest.TestCase):
    """The same G5 watching term inside ``_check_cash_floor`` (declared mode):
    the watching reservation joins the resting reservation in the funding
    check. With no journal the arithmetic and message are unchanged."""

    def _check(self, *, notional: float = 10_000.0, margin_available: Any = 12_000.0) -> str | None:
        return cl._check_cash_floor(
            _fee_plan(notional),
            None,
            account=_cash_acct(margin_available),
            open_verdicts=[],
            records=[],
            ticker="KO",
        )

    def test_no_journal_verdict_and_message_are_byte_identical(self) -> None:
        _entry_trail_journal(self, None)
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNone(self._check(notional=10_000.0, margin_available=12_000.0))
            message = self._check(notional=10_000.0, margin_available=5_000.0)
        self.assertEqual(
            message,
            "cash floor: KO needs 10,400.00 USD (incl. 4% buffer) + 0.00 already "
            "reserved by resting entries, but only 5,000.00 USD is available — "
            "deposit and re-arm",
            "with no entry-trails journal the refusal text must not change",
        )

    def test_watching_reservation_tips_it_over(self) -> None:
        # Candidate buffered 10_400 fits 12_000; a watching tier reserving
        # 10.0 x 200 = 2_000 pushes the reserved total to 12_400.
        _entry_trail_journal(self, [_watch_open_line(limit=10.0, qty=200.0)])
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            message = self._check(notional=10_000.0, margin_available=12_000.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("2,000.00", message)

    def test_terminal_tier_does_not_reserve(self) -> None:
        import json

        _entry_trail_journal(
            self,
            [
                _watch_open_line(limit=10.0, qty=200.0),
                json.dumps({"kind": "cancelled", "crid": "crid-w0"}),
            ],
        )
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            self.assertIsNone(self._check(notional=10_000.0, margin_available=12_000.0))

    def test_unvaluable_watching_record_fails_closed(self) -> None:
        # Independent of the _place_pick gate ordering (the gross cap fails
        # closed first in production): a direct/future caller must never
        # silently under-reserve on an unvaluable watching record.
        import json

        _entry_trail_journal(
            self,
            [json.dumps({"kind": "watch_open", "crid": "crid-w0", "limit": None, "qty": 5})],
        )
        with mock.patch.dict("os.environ", _DECLARED_ENV, clear=True):
            message = self._check(notional=100.0, margin_available=1_000_000.0)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("failing closed", message)

    def test_clamped_mode_stays_inert_even_with_a_watching_tier(self) -> None:
        # The cash floor is inert outside declared mode (memo G5) — the
        # watching term must not change that; the GROSS cap carries it there.
        _entry_trail_journal(self, [_watch_open_line(limit=10.0, qty=200.0)])
        env = {SIZING_EQUITY_ENV: "10000", SIZING_EQUITY_MODE_ENV: "clamped"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsNone(self._check(notional=10_000.0, margin_available=1.0))


class TestBuildDefaultDepsBootCompactsJournals(unittest.TestCase):
    """Startup maintenance: build_default_deps compacts BOTH append-only
    journals (standalone stops #895, entry trails PR-T0) before the tick loop
    — at boot, so no concurrent tick can race a rewrite against an append."""

    def test_clean_sim_boot_compacts_both_journals(self) -> None:
        standalone = mock.Mock()
        trails = mock.Mock()
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.object(cl, "_compact_standalone_stop_journal", standalone),
            mock.patch.object(entry_trails, "compact_entry_trail_journal", trails),
        ):
            cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)
        standalone.assert_called_once_with()
        trails.assert_called_once_with()


def _exit_spec(*, stop: float, tp: float, atr: float, ceiling: float | None = None) -> Any:
    return ExitGeometrySpec(
        initial_levels=InitialLevels(stop=stop, tp=tp),
        reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=atr, ceiling_price=ceiling),),
    )


class TestPlaceTiersExitGeometryOverride(unittest.TestCase):
    """``_place_tiers`` journals the geometry SHADOW STAMP unconditionally
    (memo §4.3 — the dark shadow measures anchor divergence before any flip), but
    only OVERRIDES the journaled stop/TP prices when the CACHED ``exit_policy``
    reports ``applies_geometry`` AND a buildable ``exit_spec`` exists. The inert
    ``SetupStaticPolicy`` (``applies_geometry=False``) path must stay
    BYTE-IDENTICAL to pre-PR-6a. The gate reads the resolved-once policy object,
    NOT the ``ALPHALENS_BROKER_EXIT_POLICY`` env var (Task 4 — name→registry
    refactor of WHICH policy decides placement geometry)."""

    def _run(
        self, *, exit_spec: Any, trade_setup: Any = None, exit_policy: Any = None
    ) -> tuple[int, list[dict[str, Any]]]:
        journaled: list[dict[str, Any]] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", lambda _r: None))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", journaled.append))
            count = cl._place_tiers(
                _PlaceBroker(),
                _pick("KO", "2026-07-20"),
                "KO",
                _instr(),
                _acct(),
                None,
                _placement(),
                trade_setup,
                exit_spec,
                exit_policy=exit_policy if exit_policy is not None else SetupStaticPolicy(),
            )
        return count, journaled

    def test_setup_static_policy_planned_line_byte_identical_to_pre_pr6a(self) -> None:
        spec = _exit_spec(stop=8.5, tp=13.0, atr=1.0)
        count, journaled = self._run(exit_spec=spec, exit_policy=SetupStaticPolicy())
        self.assertEqual(count, 1)
        line = journaled[0]
        self.assertAlmostEqual(line["stop_price"], 9.0)  # placement.disaster_stop_price, unchanged
        self.assertAlmostEqual(line["take_profit"], 12.0)  # tier.tp, unchanged

    def test_setup_static_policy_still_journals_the_geometry_shadow_stamp(self) -> None:
        spec = _exit_spec(stop=8.5, tp=13.0, atr=1.0, ceiling=20.0)
        _count, journaled = self._run(exit_spec=spec, exit_policy=SetupStaticPolicy())
        stamp = journaled[0]["geometry"]
        self.assertEqual(stamp["policy_name"], "atr_bracket_1p5")
        self.assertEqual(stamp["policy_version"], 1)
        self.assertAlmostEqual(stamp["geometry_stop"], 8.5)
        self.assertAlmostEqual(stamp["geometry_tp"], 13.0)
        self.assertAlmostEqual(stamp["k_atr"], 1.5)  # PR-6b: reanchor.k_atr (see _exit_spec)
        self.assertAlmostEqual(stamp["atr"], 1.0)
        self.assertAlmostEqual(stamp["ceiling_price"], 20.0)
        self.assertFalse(stamp["applied"])

    def test_geometry_policy_overrides_the_journaled_prices(self) -> None:
        spec = _exit_spec(stop=8.5, tp=13.0, atr=1.0)
        _count, journaled = self._run(
            exit_spec=spec, exit_policy=resolve_exit_policy("atr_bracket_1p5")
        )
        line = journaled[0]
        self.assertAlmostEqual(line["stop_price"], 8.5)
        self.assertAlmostEqual(line["take_profit"], 13.0)
        self.assertTrue(line["geometry"]["applied"])

    def test_exit_spec_none_never_overrides_even_when_policy_applies_geometry(self) -> None:
        _count, journaled = self._run(
            exit_spec=None, exit_policy=resolve_exit_policy("atr_bracket_1p5")
        )
        line = journaled[0]
        self.assertAlmostEqual(line["stop_price"], 9.0)
        self.assertAlmostEqual(line["take_profit"], 12.0)
        self.assertNotIn("geometry", line)

    def test_exit_spec_none_never_crashes_placement(self) -> None:
        count, _journaled = self._run(exit_spec=None)
        self.assertEqual(count, 1)

    def test_empty_reaction_plan_stamps_without_crashing(self) -> None:
        # PR-7 opened a decode boundary (iter_picks -> codec): the schema permits
        # a non-None exit with an EMPTY reaction_plan (reserved kind="levels", or a
        # future policy-only client). Pre-PR-7 exit was always built in-process with
        # a 1-element reaction_plan, so reaction_plan[0] was safe; a decoded
        # empty-plan intent must stamp the geometry LEVELS and leave the reanchor
        # facts None, never IndexError-crash the unattended drain.
        spec = ExitGeometrySpec(initial_levels=InitialLevels(stop=8.5, tp=13.0))
        count, journaled = self._run(exit_spec=spec)
        self.assertEqual(count, 1)
        stamp = journaled[0]["geometry"]
        self.assertAlmostEqual(stamp["geometry_stop"], 8.5)
        self.assertAlmostEqual(stamp["geometry_tp"], 13.0)
        self.assertIsNone(stamp["k_atr"])
        self.assertIsNone(stamp["atr"])
        self.assertIsNone(stamp["ceiling_price"])

    def test_non_reanchor_primitive_first_stamps_none_reanchor_facts(self) -> None:
        # The "reaction_plan[0] is always ReanchorOnFill" assumption also breaks at
        # the decode boundary when a non-reanchor primitive (TrailingStop / ModelPush)
        # sits first. The stamp's k_atr/atr/ceiling are ReanchorOnFill-specific, so
        # they must resolve by TYPE (not position) and stay None when absent —
        # blind attribute access on a TrailingStop would AttributeError-crash.
        spec = ExitGeometrySpec(
            initial_levels=InitialLevels(stop=8.5, tp=13.0),
            reaction_plan=(TrailingStop(arm_trigger_r=1.0, trail_frac=0.6),),
        )
        count, journaled = self._run(exit_spec=spec)
        self.assertEqual(count, 1)
        stamp = journaled[0]["geometry"]
        self.assertAlmostEqual(stamp["geometry_stop"], 8.5)
        self.assertIsNone(stamp["k_atr"])


class TestPlaceTiersJournalsTranchePlan(unittest.TestCase):
    """``_place_tiers`` (INC-5 Task 1) journals ONE ``tranche_plan`` line per uic
    when a sized ``SetupPlan`` with a non-empty ``tp_tranches`` is passed —
    ADDITIVE to (never replacing) the existing per-tier ``planned`` journaling."""

    def _run(
        self, *, plan: Any, exit_spec: Any = None, exit_policy: Any = None
    ) -> list[dict[str, Any]]:
        journaled: list[dict[str, Any]] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", lambda _r: None))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", journaled.append))
            cl._place_tiers(
                _PlaceBroker(),
                _pick("KO", "2026-07-20"),
                "KO",
                _instr(),
                _acct(),
                None,
                _placement(),
                None,
                exit_spec,
                exit_policy=exit_policy if exit_policy is not None else SetupStaticPolicy(),
                plan=plan,
            )
        return journaled

    def _plan(self, *, tp_tranches: tuple[TpTranchePlan, ...] = ()) -> SetupPlan:
        return SetupPlan(
            suggested_size_pct=2.0,
            scale_factor=1.0,
            final_size_pct=2.0,
            total_notional=1000.0,
            paper_equity=100000.0,
            disaster_stop=9.0,
            order_ttl_days=5,
            entry_tiers=(
                TierPlan(tier_index=0, limit_price=10.0, qty=60, alloc_pct=60.0, tag="T1"),
                TierPlan(tier_index=1, limit_price=9.5, qty=40, alloc_pct=40.0, tag="T2"),
            ),
            tp_tranches=tp_tranches,
        )

    def test_journals_one_tranche_plan_line_with_the_full_ladder(self) -> None:
        tranches = (
            TpTranchePlan(
                tranche_index=0, target_price=11.0, tranche_pct=0.5, r_multiple=1.0, tag="tp1"
            ),
            TpTranchePlan(
                tranche_index=1, target_price=12.0, tranche_pct=0.5, r_multiple=2.0, tag="tp2"
            ),
        )
        journaled = self._run(plan=self._plan(tp_tranches=tranches))
        tranche_plan_lines = [line for line in journaled if line["kind"] == "tranche_plan"]
        self.assertEqual(len(tranche_plan_lines), 1)
        line = tranche_plan_lines[0]
        self.assertEqual(line["uic"], _instr().broker_instrument_id)
        self.assertEqual(line["reference_qty"], 100.0)  # 60 + 40 entry-tier qty
        self.assertAlmostEqual(line["stop_price"], 9.0)  # placement.disaster_stop_price
        self.assertEqual(len(line["tp_tranches"]), 2)
        # The existing per-tier `planned` journaling is UNCHANGED alongside it.
        self.assertEqual(len([line for line in journaled if line["kind"] == "planned"]), 1)

    def test_no_plan_journals_nothing_extra(self) -> None:
        journaled = self._run(plan=None)
        self.assertEqual([line for line in journaled if line["kind"] == "tranche_plan"], [])

    def test_bracket_path_line_carries_no_pick_key(self) -> None:
        # Byte-identity pin (2026-08-19 adjudication finding 4): only the
        # entry-trail watch routing stamps pick_key; the bracket wrapper's
        # journaled line is unchanged, so every bracket plan line keeps
        # today's always-reset fold semantics.
        tranches = (
            TpTranchePlan(
                tranche_index=0, target_price=11.0, tranche_pct=1.0, r_multiple=1.0, tag="tp1"
            ),
        )
        journaled = self._run(plan=self._plan(tp_tranches=tranches))
        line = next(line for line in journaled if line["kind"] == "tranche_plan")
        self.assertNotIn("pick_key", line)

    def test_empty_tp_tranches_journals_nothing_extra(self) -> None:
        journaled = self._run(plan=self._plan(tp_tranches=()))
        self.assertEqual([line for line in journaled if line["kind"] == "tranche_plan"], [])

    def test_geometry_policy_journals_one_tranche_plan_from_exit_spec_tp(self) -> None:
        # INC-5 production bug: under the geometry policy (atr_bracket_1p5) the
        # brief's static plan.tp_tranches is EMPTY -- the TP lives in exit_spec
        # instead -- so real picks never got a tranche_plan line and the
        # live-exit engine skipped every real position. The geometry TP must
        # journal as a single 100% tranche, sourced from exit_spec, not from
        # the (empty) static plan.tp_tranches.
        spec = _exit_spec(stop=8.5, tp=13.0, atr=1.0)
        journaled = self._run(
            plan=self._plan(tp_tranches=()),
            exit_spec=spec,
            exit_policy=resolve_exit_policy("atr_bracket_1p5"),
        )
        tranche_plan_lines = [line for line in journaled if line["kind"] == "tranche_plan"]
        self.assertEqual(len(tranche_plan_lines), 1)
        line = tranche_plan_lines[0]
        self.assertEqual(line["uic"], _instr().broker_instrument_id)
        self.assertEqual(line["reference_qty"], 100.0)  # 60 + 40 entry-tier qty
        self.assertAlmostEqual(line["stop_price"], 8.5)  # exit_spec.initial_levels.stop
        self.assertEqual(len(line["tp_tranches"]), 1)
        self.assertAlmostEqual(line["tp_tranches"][0]["target_price"], 13.0)
        self.assertAlmostEqual(line["tp_tranches"][0]["tranche_pct"], 1.0)

    def test_a_non_finite_geometry_level_journals_no_tranche_plan_line(self) -> None:
        # _build_tranche_plan_line writes both levels verbatim (float(...)), so a
        # NaN/zero level from a future geometry policy must skip the line rather
        # than poison the journal the live-exit engine folds.
        for stop, tp in ((float("nan"), 13.0), (8.5, float("nan")), (0.0, 13.0), (8.5, 0.0)):
            with self.subTest(stop=stop, tp=tp):
                journaled = self._run(
                    plan=self._plan(tp_tranches=()),
                    exit_spec=_exit_spec(stop=stop, tp=tp, atr=1.0),
                    exit_policy=resolve_exit_policy("atr_bracket_1p5"),
                )
                self.assertEqual([ln for ln in journaled if ln["kind"] == "tranche_plan"], [])

    def test_a_non_finite_geometry_level_is_logged(self) -> None:
        # The skip is silent otherwise: the live-exit engine then finds no ladder
        # for that uic and the position sits stop-only, indistinguishable in the
        # journal from a pre-INC-5 pick. Name both levels so the anomaly is
        # diagnosable from journalctl alone.
        with self.assertLogs(cl.logger, level="WARNING") as caught:
            self._run(
                plan=self._plan(tp_tranches=()),
                exit_spec=_exit_spec(stop=float("nan"), tp=13.0, atr=1.0),
                exit_policy=resolve_exit_policy("atr_bracket_1p5"),
            )
        logged = "\n".join(caught.output)
        self.assertIn("tranche_plan", logged)
        self.assertIn(str(_instr().broker_instrument_id), logged)

    def test_a_non_finite_geometry_level_does_not_fall_back_to_the_static_ladder(self) -> None:
        # Under the geometry policy the static plan.tp_tranches is NOT the active
        # ladder, so an unusable geometry level must journal nothing -- silently
        # falling back would arm the engine on a ladder the policy never placed.
        tranches = (
            TpTranchePlan(
                tranche_index=0, target_price=11.0, tranche_pct=1.0, r_multiple=1.0, tag="tp1"
            ),
        )
        journaled = self._run(
            plan=self._plan(tp_tranches=tranches),
            exit_spec=_exit_spec(stop=float("nan"), tp=13.0, atr=1.0),
            exit_policy=resolve_exit_policy("atr_bracket_1p5"),
        )
        self.assertEqual([ln for ln in journaled if ln["kind"] == "tranche_plan"], [])


class _LadderBroker:
    """A broker double for the ``_place_tiers`` ladder paths: places tiers
    E-1, E-2, ... until ``fail_at`` (1-based), where it raises ``error``;
    records cancels, with optional per-order cancel failures."""

    name = "ladder"

    def __init__(
        self,
        *,
        fail_at: int | None = None,
        error: BrokerError | None = None,
        cancel_errors: dict[str, BrokerError] | None = None,
    ) -> None:
        self.calls = 0
        self.cancelled: list[str] = []
        self._fail_at = fail_at
        self._error = error
        self._cancel_errors = cancel_errors or {}

    def place_bracket_order(self, _bracket: Any) -> Any:
        self.calls += 1
        if self._fail_at is not None and self.calls == self._fail_at:
            raise self._error or BrokerError("boom")
        return type("Placed", (), {"entry_order_id": f"E-{self.calls}", "exit_order_ids": ()})()

    def cancel_order(self, order_id: str) -> None:
        err = self._cancel_errors.get(order_id)
        if err is not None:
            raise err
        self.cancelled.append(order_id)


def _tiered_plan(
    *,
    tiers: tuple[TierPlan, ...],
    tp_tranches: tuple[TpTranchePlan, ...] = (),
) -> SetupPlan:
    """A SetupPlan with explicit tiers/tranches for the honest fee estimate."""
    return SetupPlan(
        suggested_size_pct=1.0,
        scale_factor=1.0,
        final_size_pct=1.0,
        total_notional=sum(t.qty * t.limit_price for t in tiers),
        paper_equity=100_000.0,
        disaster_stop=9.0,
        order_ttl_days=1,
        entry_tiers=tiers,
        tp_tranches=tp_tranches,
    )


class TestEstimateRoundTripFeeBps(unittest.TestCase):
    """``_estimate_round_trip_fee_bps`` — the HONEST per-tier round-trip
    estimate (memo §4.5): each entry tier pays ``max($1, 0.08% x qty x
    limit)``; the exit side sums the same shape over the TP tranches (qtys
    derived as ``tranche_pct/100 x total entry qty``) or mirrors the entry
    fees when no tranches are derivable; + 0.50% FX round trip when fx
    applies. The fee FLOOR check is UNCHANGED — this only journals."""

    def test_single_tier_mirror_exit_matches_the_aggregate_model(self) -> None:
        # 1 tier, qty 1000 @ $10: entry max(1, 8) = 8; no tranches -> exit
        # mirrors 8; no fx. (8 + 8) / 10_000 x 10^4 = 16 bps.
        estimate = cl._estimate_round_trip_fee_bps(_fee_plan(10_000.0), None)
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate, 16.0)

    def test_per_tier_min_commission_tranches_and_fx_hand_computed(self) -> None:
        # 2 tiers of $1000 each pay the $1 MINIMUM twice (the aggregate model
        # would charge max(1, 0.0008 x 2000) = $1.60 ONCE — the leak §4.5
        # closes). Exit: total qty 150, two 50% tranches of 75 sh @ 12/14 ->
        # $1 min each. FX: 0.005 x 2000 = $10.
        # (2 + 2 + 10) / 2000 x 10^4 = 70 bps.
        plan = _tiered_plan(
            tiers=(
                TierPlan(tier_index=0, limit_price=10.0, qty=100, alloc_pct=50.0, tag="T1"),
                TierPlan(tier_index=1, limit_price=20.0, qty=50, alloc_pct=50.0, tag="T2"),
            ),
            tp_tranches=(
                TpTranchePlan(
                    tranche_index=0, target_price=12.0, tranche_pct=50.0, r_multiple=1.0, tag="tp1"
                ),
                TpTranchePlan(
                    tranche_index=1, target_price=14.0, tranche_pct=50.0, r_multiple=2.0, tag="tp2"
                ),
            ),
        )
        estimate = cl._estimate_round_trip_fee_bps(plan, _fx(1.0))
        assert estimate is not None
        self.assertAlmostEqual(estimate, 70.0)

    def test_non_usd_instrument_skips_the_min_commission_clamp(self) -> None:
        # The $1 clamp is a USD figure (mirrors _round_trip_fee_bps): a
        # 1000-unit non-USD notional pays ad-valorem only.
        plan = _tiered_plan(
            tiers=(TierPlan(tier_index=0, limit_price=10.0, qty=100, alloc_pct=100.0, tag="T1"),),
        )
        usd = cl._estimate_round_trip_fee_bps(plan, None, instrument_currency="USD")
        pln = cl._estimate_round_trip_fee_bps(plan, None, instrument_currency="PLN")
        assert usd is not None and pln is not None
        self.assertAlmostEqual(usd, 20.0)  # 2 x $1 min / 1000 x 10^4
        self.assertAlmostEqual(pln, 16.0)  # 2 x 0.8 ad-valorem / 1000 x 10^4

    def test_zero_qty_tiers_pay_no_commission(self) -> None:
        # A zero-qty tier is never POSTed (_ZERO_QTY_TIER_POLICY = skip-log),
        # so it must not add a phantom $1 minimum.
        plan = _tiered_plan(
            tiers=(
                TierPlan(tier_index=0, limit_price=10.0, qty=1000, alloc_pct=50.0, tag="T1"),
                TierPlan(tier_index=1, limit_price=9.0, qty=0, alloc_pct=50.0, tag="T2"),
            ),
        )
        estimate = cl._estimate_round_trip_fee_bps(plan, None)
        assert estimate is not None
        self.assertAlmostEqual(estimate, 16.0)

    def test_no_plan_or_zero_gross_returns_none(self) -> None:
        self.assertIsNone(cl._estimate_round_trip_fee_bps(None, None))
        self.assertIsNone(cl._estimate_round_trip_fee_bps(object(), None))  # bare stub, no tiers
        self.assertIsNone(cl._estimate_round_trip_fee_bps(_fee_plan(0.0), None))


class TestPlaceTiersFeeEstimateStamp(unittest.TestCase):
    """Memo §4.5 (operator decision §7.3): the honest per-tier round-trip
    estimate is stamped on EVERY record ``_place_tiers`` journals — the
    calibration series for path B's 150 bps target — while the fee FLOOR
    check itself is unchanged (MAX_FEE_BPS stays the degenerate-class
    backstop)."""

    def _run(self, *, plan: Any, fx: Any = None) -> list[dict[str, Any]]:
        appended: list[dict[str, Any]] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", appended.append))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
            cl._place_tiers(
                _LadderBroker(),
                _pick("KO", "2026-07-20"),
                "KO",
                _instr(),
                _acct(),
                fx,
                _placement(n_tiers=2),
                plan=plan,
            )
        return appended

    def test_estimate_stamped_on_every_record_and_matches_hand_computed(self) -> None:
        appended = self._run(plan=_fee_plan(10_000.0))
        self.assertEqual(len(appended), 3)  # write-ahead + 2 tier records
        for record in appended:
            self.assertAlmostEqual(record["est_round_trip_fee_bps"], 16.0)

    def test_no_plan_stamps_a_real_null(self) -> None:
        appended = self._run(plan=None)
        self.assertTrue(appended)
        for record in appended:
            self.assertIsNone(record["est_round_trip_fee_bps"])


def _failure_notes(appended: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The FAILURE note records journaled by ``_place_tiers`` — excludes the
    write-ahead "placement attempt" dedup line (memo §4.4 B2), which also
    carries a note."""
    return [r for r in appended if r.get("note") and r["note"] != "placement attempt"]


class TestPlaceTiersInsufficientFundsRollback(unittest.TestCase):
    """Memo §4.4 B1: a mid-ladder insufficient-funds reject (classified on the
    STRUCTURED Saxo error code, never the message string) must CANCEL this
    pick's just-placed unfilled entry tiers before journaling the note record
    — converting the dangerous partial ladder into the promised nothing. Any
    OTHER BrokerError keeps today's behavior (tiers left resting)."""

    def _run(self, broker: _LadderBroker, *, n_tiers: int = 3) -> tuple[int, list[Any]]:
        appended: list[Any] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", appended.append))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
            count = cl._place_tiers(
                broker,
                _pick("KO", "2026-07-20"),
                "KO",
                _instr(),
                _acct(),
                None,
                _placement(n_tiers=n_tiers),
            )
        return count, appended

    def test_insufficient_funds_mid_ladder_cancels_earlier_tiers_and_journals_note(self) -> None:
        broker = _LadderBroker(
            fail_at=2, error=OrderRejectedError("no cash", error_code="InsufficentCash")
        )
        with self.assertLogs(cl.logger, level="WARNING") as caught:
            count, appended = self._run(broker)
        self.assertEqual(count, 1)
        self.assertEqual(broker.cancelled, ["E-1"], "the placed tier-1 entry must be cancelled")
        notes = _failure_notes(appended)
        self.assertEqual(len(notes), 1, "the failure note record must still be journaled")
        self.assertIn("insufficient funds", notes[0]["note"])
        self.assertIn("cancelled 1/1", notes[0]["note"])
        self.assertTrue(any("insufficient funds" in line for line in caught.output))

    def test_non_funds_broker_error_leaves_placed_tiers_resting(self) -> None:
        # Today's behavior for every other BrokerError — including an
        # unstructured error_code=None rejection (no message parsing): tiers
        # stay resting, only the note record retires the pick.
        for error in (
            BrokerError("exchange rejected"),
            OrderRejectedError("rejected", error_code="TooFarFromMarket"),
            OrderRejectedError("insufficient cash in message only"),
        ):
            with self.subTest(error=error):
                broker = _LadderBroker(fail_at=2, error=error)
                count, appended = self._run(broker)
                self.assertEqual(count, 1)
                self.assertEqual(broker.cancelled, [], "non-funds errors must NOT roll back")
                self.assertTrue(_failure_notes(appended))

    def test_one_cancel_failure_does_not_abort_the_remaining_cancels(self) -> None:
        broker = _LadderBroker(
            fail_at=3,
            error=OrderRejectedError("no cash", error_code="InsufficentCash"),
            cancel_errors={"E-1": BrokerError("cancel rejected")},
        )
        count, appended = self._run(broker)
        self.assertEqual(count, 2)
        self.assertEqual(broker.cancelled, ["E-2"], "the E-1 failure must not stop E-2's cancel")
        self.assertIn("cancelled 1/2", _failure_notes(appended)[0]["note"])

    def test_insufficient_funds_on_the_first_tier_cancels_nothing(self) -> None:
        broker = _LadderBroker(
            fail_at=1, error=OrderRejectedError("no cash", error_code="InsufficentCash")
        )
        count, appended = self._run(broker)
        self.assertEqual(count, 0)
        self.assertEqual(broker.cancelled, [])
        self.assertTrue(_failure_notes(appended))


class TestPlaceTiersWriteAheadDedup(unittest.TestCase):
    """Memo §4.4 B2: ``_place_tiers`` appends a note-only record (brackets=[],
    note="placement attempt") BEFORE the first broker POST, so a crash between
    the POST and the per-tier journal append can no longer re-place the whole
    (frame-sized, no longer balance-bounded) ladder on restart —
    ``_submitted_pick_keys`` already treats note-only records as submitted."""

    def _run_crashing(self) -> list[dict[str, Any]]:
        class _CrashBroker:
            def place_bracket_order(self, _bracket: Any) -> Any:
                raise _CrashError("process dies between POST and journal")

        appended: list[dict[str, Any]] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", appended.append))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
            with self.assertRaises(_CrashError):
                cl._place_tiers(
                    _CrashBroker(),
                    _pick("KO", "2026-07-20"),
                    "KO",
                    _instr(),
                    _acct(),
                    None,
                    _placement(),
                )
        return appended

    def test_crash_between_post_and_journal_still_retires_the_pick_key(self) -> None:
        appended = self._run_crashing()
        self.assertTrue(appended, "the write-ahead record must land BEFORE the first POST")
        self.assertIn(
            ("KO", "2026-07-20"),
            cl._submitted_pick_keys(appended),
            "the restart drain must see the pick as submitted and never re-place it",
        )

    def test_write_ahead_record_is_note_only(self) -> None:
        record = self._run_crashing()[0]
        self.assertEqual(record["brackets"], [])
        self.assertEqual(record["note"], "placement attempt")

    def test_successful_placement_keeps_the_real_bracket_record_too(self) -> None:
        appended: list[dict[str, Any]] = []
        pkg = "alphalens_pipeline.brokers"
        with contextlib.ExitStack() as stack:
            p = stack.enter_context
            p(mock.patch(f"{pkg}.submission_log.append_submission_record", appended.append))
            p(mock.patch(f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)))
            p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
            count = cl._place_tiers(
                _LadderBroker(),
                _pick("KO", "2026-07-20"),
                "KO",
                _instr(),
                _acct(),
                None,
                _placement(),
            )
        self.assertEqual(count, 1)
        # Write-ahead first, then the post-placement record with the REAL
        # brackets (the existing append stays — it carries the order ids).
        self.assertEqual(len(appended), 2)
        self.assertEqual(appended[0]["brackets"], [])
        self.assertEqual(len(appended[1]["brackets"]), 1)
        self.assertEqual(appended[1]["brackets"][0]["entry_order_id"], "E-1")

    def test_note_only_record_folds_zero_in_summarize_open_verdicts(self) -> None:
        # The extra record must be INERT everywhere brackets are folded: no
        # brackets -> zero committed gross, zero open brackets.
        from alphalens_pipeline.brokers.submission_log import build_submission_record

        note_record = build_submission_record(
            brief_date="2026-07-20",
            ticker="KO",
            mic="XNYS",
            uic="307",
            brackets=[],
            note="placement attempt",
        )
        summary = cl._summarize_open_verdicts([], [note_record], "2026-07-20")
        self.assertEqual(summary, (0, 0.0, 0.0))
        total, unjoined = cl._committed_working_gross_acct([], [note_record])
        self.assertEqual((total, unjoined), (0.0, 0))


class TestBuildPlannedLineGeometryStamp(unittest.TestCase):
    """Direct unit coverage of ``_build_planned_line``'s ``geometry_stamp`` param
    (PR-6a) -- the namespacing + byte-identical-when-omitted contract."""

    def _line(self, **over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "entry_crid": "crid-0",
            "uic": _UIC,
            "side": "SELL",
            "stop_price": 216.48,
            "take_profit": 306.72,
            "tier_index": 0,
        }
        base.update(over)
        return cl._build_planned_line(**base)

    def test_omitted_by_default_no_geometry_key(self) -> None:
        line = self._line()
        self.assertNotIn("geometry", line)

    def test_none_geometry_stamp_omits_the_key(self) -> None:
        line = self._line(geometry_stamp=None)
        self.assertNotIn("geometry", line)

    def test_present_geometry_stamp_is_namespaced_under_geometry_key(self) -> None:
        stamp = {"policy_name": "atr_bracket_1p5", "applied": False}
        line = self._line(geometry_stamp=stamp)
        self.assertEqual(line["geometry"], stamp)
        # Never overwrites/collides with the fold-read fields.
        self.assertAlmostEqual(line["stop_price"], 216.48)
        self.assertAlmostEqual(line["take_profit"], 306.72)


class TestFoldPlannedExitsIgnoresGeometryStamp(unittest.TestCase):
    """Regression guard (PR-6a): the additive ``"geometry"`` shadow stamp is
    telemetry-only and must never change ``_fold_planned_exits``'s output --
    a future refactor that starts reading it by mistake would silently change
    live protection derivation."""

    def test_fold_output_identical_with_and_without_geometry_stamp(self) -> None:
        plain = cl._build_planned_line(
            entry_crid="crid-0",
            uic=_UIC,
            side="SELL",
            stop_price=216.48,
            take_profit=306.72,
            tier_index=0,
        )
        stamped = cl._build_planned_line(
            entry_crid="crid-0",
            uic=_UIC,
            side="SELL",
            stop_price=216.48,
            take_profit=306.72,
            tier_index=0,
            geometry_stamp={
                "policy_name": "atr_bracket_1p5",
                "policy_version": 1,
                "planned_blend": 100.0,
                "geometry_stop": 97.0,
                "geometry_tp": 103.0,
                "atr": 2.0,
                "ceiling_price": None,
                "applied": True,
            },
        )
        without_stamp = cl._fold_planned_exits([plain])
        with_stamp = cl._fold_planned_exits([stamped])
        self.assertEqual(
            {u: (p.stop_price, p.tp_price, p.entry_crid) for u, p in without_stamp.items()},
            {u: (p.stop_price, p.tp_price, p.entry_crid) for u, p in with_stamp.items()},
        )


class TestFoldPlannedExitsBuildsReanchorFacts(unittest.TestCase):
    """PR-6b: ``_fold_planned_exits`` folds the governing line's geometry stamp
    (when it carries ``k_atr`` + ``atr``) into ``PlannedExit.reanchor``. Every
    pre-PR-6a journal line (no ``"geometry"`` key at all) still folds to
    ``reanchor=None`` — BYTE-IDENTICAL to before this PR for that history."""

    def test_geometry_blob_with_k_atr_and_atr_builds_reanchor_facts(self) -> None:
        stamped = cl._build_planned_line(
            entry_crid="crid-0",
            uic=_UIC,
            side="SELL",
            stop_price=216.48,
            take_profit=306.72,
            tier_index=0,
            geometry_stamp={
                "policy_name": "atr_bracket_1p5",
                "policy_version": 1,
                "planned_blend": 100.0,
                "geometry_stop": 94.0,
                "geometry_tp": 112.0,
                "k_atr": 1.5,
                "atr": 4.0,
                "ceiling_price": None,
                "applied": False,
            },
        )
        planned = cl._fold_planned_exits([stamped])[_UIC]
        self.assertEqual(planned.reanchor, ReanchorFacts(k_atr=1.5, atr=4.0))

    def test_geometry_absent_folds_reanchor_none_byte_identical(self) -> None:
        plain = cl._build_planned_line(
            entry_crid="crid-0",
            uic=_UIC,
            side="SELL",
            stop_price=216.48,
            take_profit=306.72,
            tier_index=0,
        )
        planned = cl._fold_planned_exits([plain])[_UIC]
        self.assertIsNone(planned.reanchor)

    def test_geometry_missing_k_atr_folds_reanchor_none(self) -> None:
        # A stamp journaled before PR-6b added "k_atr" to _geometry_shadow_stamp: the
        # key is absent -> the fold must never KeyError, just fold to None.
        stamped = cl._build_planned_line(
            entry_crid="crid-0",
            uic=_UIC,
            side="SELL",
            stop_price=216.48,
            take_profit=306.72,
            tier_index=0,
            geometry_stamp={
                "policy_name": "atr_bracket_1p5",
                "policy_version": 1,
                "planned_blend": 100.0,
                "geometry_stop": 94.0,
                "geometry_tp": 112.0,
                "atr": 4.0,
                "ceiling_price": None,
                "applied": False,
            },
        )
        planned = cl._fold_planned_exits([stamped])[_UIC]
        self.assertIsNone(planned.reanchor)


class TestRunOnceAlertsEachOrphan(unittest.TestCase):
    def test_each_swept_orphan_is_alerted(self) -> None:
        from alphalens_pipeline.brokers.automanager.orphan_sweeper import Orphan

        orphans = [
            Orphan(order_id="", external_reference="OLN-2026-08-18-entry-t0-fire", kind="position"),
            Orphan(order_id="99", external_reference="", kind="order"),
        ]
        with TemporaryDirectory() as d:
            alerts: list[str] = []
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=alerts,
            )
            deps = cl.LoopDeps(**{**deps.__dict__, "sweep_orphans_fn": lambda _b: orphans})
            report = cl.run_once(deps, sweep_orphans=True)
        self.assertEqual(report.orphans, 2)
        # The human line renders the E{n} label + kind; the raw machine ref never
        # appears in the operator alert (it rides a debug logger instead).
        self.assertTrue(any("OLN E1 (fire)" in a and "[position]" in a for a in alerts))
        self.assertFalse(any("OLN-2026-08-18-entry-t0-fire" in a for a in alerts))
        # An order orphan with no ref falls back to the order id.
        self.assertTrue(any("99" in a and "[order]" in a for a in alerts))


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


def _b0_action(**over: Any) -> UpgradeToOco:
    """A B0 OCO-direct-on-fill action (supersede_ids ALWAYS empty in Stage 3)."""
    base: dict[str, Any] = {
        "uic": _UIC,
        "side": "SELL",
        "qty": 46.0,
        "stop_price": 216.48,
        "tp_price": 306.72,
        "entry_crid": "crid-0",
        "gen": 0,
        "supersede_ids": (),
    }
    base.update(over)
    return UpgradeToOco(**base)


class TestExecuteB0Success(unittest.TestCase):
    """B0 OCO-direct-on-fill success (saxo Stage-3 memo): a truly naked fresh fill
    reaches a resting OCO pair. On a confirmed 2xx the executor counts the exit AND
    journals an ``oco_placed`` marker (so the next tick's B0 is suppressed while
    list-orders lags), with NO fallback stop placed."""

    def test_execute_b0_success_places_oco_and_journals_oco_placed(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), place_oco_exit=_oco_placer(calls)
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "oco_placed"
                ]
        self.assertEqual(len(calls), 1, "the OCO pair was placed once")
        self.assertEqual(
            calls[0][:6], (_UIC, "SELL", 46.0, 216.48, 306.72, _exit_oco_ref("crid-0", 0))
        )
        self.assertEqual(broker.placed, [], "no fallback standalone stop on a successful OCO")
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual([m.get("uic") for m in markers], [_UIC], "oco_placed marker journaled")


class TestRung1RefuseViaLoopStaysStopOnly(unittest.TestCase):
    """Stage 3 rung-1 REFUSE end-to-end (saxo Stage-3 memo): a resting rung-1
    standalone stop with OCO enabled is NEVER upgraded through the loop — the pure
    reconciler returns NoOp, no OCO is attempted, the rung-1 stop stays LIVE, and
    the uic is NOT degraded to oco_unsupported. OCO is reached only via B0 on a
    fresh naked fill; the stop-only residue drains purely by turnover."""

    def test_resting_rung1_stop_not_upgraded_no_oco_no_degrade(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            rung1 = _leg("rung1-stop", "StopIfTraded", 46.0)
            broker = _ProtBroker(positions=[_pos(46.0)], sells=[rung1], by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(calls)
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)  # planned line carries take_profit=306.72
                r1 = cl.run_once(deps)
                r2 = cl.run_once(deps)
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertEqual(len(calls), 0, "rung-1 REFUSE: OCO never attempted from a resting stop")
        self.assertEqual(broker.cancelled, [], "rung-1 stop kept LIVE (never touched)")
        self.assertEqual((r1.exits_placed, r2.exits_placed), (0, 0))
        self.assertNotIn(_UIC, folded, "no degrade: a refused rung-1 is not marked oco_unsupported")


class TestExecuteB0FailureTaxonomy(unittest.TestCase):
    """B0's three-way failure taxonomy (saxo Stage-3 memo, mitigation H1/A2/H4).

    An AMBIGUOUS write (a non-``OrderRejectedError`` BrokerError — 5xx / network /
    rate-limit) MAY have landed: NO inline fallback (would double-commit), NO
    ``oco_placed`` marker (next tick reconciles against live state), NO degrade —
    only a CRITICAL alert. A CLEAN structural reject is provably NOT landed: cover
    the naked fill NOW with a plain standalone stop AND mark the uic
    ``oco_unsupported``. A benign ``SellOrdersAlreadyExist`` means an OCO already
    rests from a prior tick's landed write: NO fallback, NO degrade, just defer."""

    def test_execute_b0_ambiguous_write_no_fallback_no_marker_alerts_critical(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(calls, error=BrokerError("500 network blip after send"))
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)  # must NOT raise
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "oco_placed"
                ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(broker.placed, [], "NO inline fallback — the OCO may have landed")
        self.assertEqual(broker.cancelled, [])
        self.assertEqual(report.exits_placed, 0)
        self.assertNotIn(_UIC, folded, "ambiguous never degrades to oco_unsupported")
        self.assertEqual(markers, [], "no oco_placed marker on an ambiguous write")
        self.assertTrue(
            any("CRITICAL" in a for a in alerts), f"expected a CRITICAL alert, got {alerts}"
        )

    def test_execute_b0_clean_reject_places_fallback_and_marks_oco_unsupported(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            # A clean structural reject (NOT SellOrdersAlreadyExist) — provably not landed.
            placer = _oco_placer(
                calls, error=OrderRejectedError("bad OCO", error_code="OrderRelationInvalid")
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)  # must NOT raise
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            len(broker.placed), 1, "the naked fill is covered by a plain standalone stop"
        )
        self.assertEqual(broker.placed[0][:4], (_UIC, "SELL", 46.0, 216.48))
        self.assertEqual(report.exits_placed, 1)
        self.assertIn(_UIC, folded, "a clean structural reject degrades the uic to oco_unsupported")

    def test_execute_b0_sell_orders_already_exist_benign(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(
                calls,
                error=OrderRejectedError(
                    "already", error_code="SellOrdersAlreadyExistForOwnedContracts"
                ),
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)  # must NOT raise
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            broker.placed, [], "an OCO already rests — NO fallback (would double-commit)"
        )
        self.assertNotIn(_UIC, folded, "benign fill-race never degrades to oco_unsupported")
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(alerts, "the benign already-rests case is surfaced as a deferring alert")

    def test_execute_b0_capability_error_provably_unsent_no_fallback_no_degrade(self) -> None:
        # A BrokerCapabilityError (ALLOW_ORDERS off / no placement capability) is a
        # BrokerError subclass but is PROVABLY UNSENT — it must NOT read as an
        # ambiguous write (no CRITICAL; a fallback stop is equally gated so it would
        # fail too) NOR as a clean structural reject (no oco_unsupported degrade — a
        # transient env gate is not an instrument incapability).
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(calls, error=BrokerCapabilityError("order placement disabled"))
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)  # must NOT raise
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "oco_placed"
                ]
        self.assertEqual(
            broker.placed, [], "no fallback — placement is globally gated (would fail too)"
        )
        self.assertNotIn(_UIC, folded, "an env gate never degrades the uic to oco_unsupported")
        self.assertEqual(markers, [], "no oco_placed marker on a provably-unsent write")
        self.assertEqual(report.exits_placed, 0)
        self.assertFalse(
            any("CRITICAL" in a for a in alerts),
            f"a provably-unsent capability error is NOT a CRITICAL ambiguous write: {alerts}",
        )
        self.assertTrue(alerts, "the orders-disabled state is surfaced (throttled)")


class TestExecuteB0TooFarFromMarketTransient(unittest.TestCase):
    """A ``TooFarFromMarket`` OCO reject is PRICE-dependent and transient (VRNS
    incident 2026-07-29: one volatile open must not permanently degrade the uic
    to stop-only). The executor journals a timestamped ``oco_too_far`` TTL marker
    instead of the permanent ``oco_unsupported`` flag; the CURRENT fill is still
    covered by the fallback stop (never-naked first, unchanged)."""

    def test_too_far_reject_journals_ttl_marker_not_permanent(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            placer = _oco_placer(
                calls, error=OrderRejectedError("too far", error_code="TooFarFromMarket")
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=placer
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, report)  # must NOT raise
                lines = list(cl._iter_standalone_stop_journal())
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            len(broker.placed), 1, "the naked fill is covered by a plain standalone stop"
        )
        self.assertEqual(broker.placed[0][:4], (_UIC, "SELL", 46.0, 216.48))
        self.assertEqual(report.exits_placed, 1)
        self.assertNotIn(
            _UIC,
            cl._fold_oco_unsupported(lines),
            "a transient TooFarFromMarket must NOT write the permanent marker",
        )
        markers = [line for line in lines if line.get("kind") == "oco_too_far"]
        self.assertEqual(
            [m.get("uic") for m in markers], [_UIC], "a timestamped oco_too_far marker journaled"
        )
        self.assertIn("ts", markers[0], "the marker is timestamped so the TTL fold can expire it")

    def test_structural_reject_still_marks_permanent_no_ttl_marker(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, _OCO_ON):
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            placer = _oco_placer(
                [], error=OrderRejectedError("bad OCO", error_code="OrderRelationInvalid")
            )
            executor = cl._make_protection_executor(broker, _throttle_to([]), place_oco_exit=placer)
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_b0_action(), False, cl.TickReport())
                lines = list(cl._iter_standalone_stop_journal())
        self.assertIn(_UIC, cl._fold_oco_unsupported(lines), "structural reject stays permanent")
        self.assertEqual(
            [line for line in lines if line.get("kind") == "oco_too_far"],
            [],
            "no TTL marker on a structural reject",
        )


class TestBuildProtectionViewOcoTooFarTtl(unittest.TestCase):
    """The view folds unexpired ``oco_too_far`` markers into the EXISTING
    ``ProtectionView.oco_unsupported`` set (union with the permanent markers), so
    all downstream B0 logic is untouched and the uic re-qualifies for OCO on
    fresh fills once the TTL expires. Permanent markers never expire."""

    def _broker(self) -> _ProtBroker:
        return _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})

    def test_fresh_too_far_marker_degrades_transiently(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_oco_too_far(_UIC, clock=lambda: 1000.0 - 30.0)  # 30s ago
                view = cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
        self.assertIn(_UIC, view.oco_unsupported, "an unexpired oco_too_far degrades the uic")

    def test_expired_too_far_marker_re_enables_oco(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_oco_too_far(_UIC, clock=lambda: 1000.0 - cl._OCO_TOO_FAR_TTL_S - 1.0)
                view = cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
        self.assertNotIn(
            _UIC, view.oco_unsupported, "an expired oco_too_far re-enables OCO for fresh fills"
        )

    def test_permanent_marker_never_expires(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._mark_oco_unsupported(_UIC)
                # A clock arbitrarily far in the future — the permanent marker
                # (no ts) is TTL-immune; clearing it is a manual operator action.
                view = cl.build_protection_view(
                    self._broker(), [], clock=lambda: 10.0 * cl._OCO_TOO_FAR_TTL_S
                )
        self.assertIn(_UIC, view.oco_unsupported, "a permanent oco_unsupported stays forever")


class TestExecuteB0UnderKill(unittest.TestCase):
    """Under KILL a B0 naked fill still needs covering — no OCO churn (a new OCO is
    order churn, not exposure reduction), but a plain standalone stop IS placed (it
    only reduces exposure). The fill is never left naked under KILL."""

    def test_execute_b0_under_kill_places_plain_stop_no_oco(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            calls: list = []
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), place_oco_exit=_oco_placer(calls)
            )
            report = cl.TickReport()
            executor(_b0_action(), True, report)  # kill = True
        self.assertEqual(calls, [], "no OCO place under KILL")
        self.assertEqual(
            len(broker.placed), 1, "the naked fill is covered by a plain stop under KILL"
        )
        self.assertEqual(broker.placed[0][:4], (_UIC, "SELL", 46.0, 216.48))
        self.assertEqual(report.exits_placed, 1)


class TestExecuteB0FlatUicSkips(unittest.TestCase):
    """Execute-time owned re-check: the snapshot showed owned=46 but the position
    is flat now -> the OCO is skipped (never oversell / plant on a flat uic), no
    fallback stop, a flat-skip alert."""

    def test_flat_at_execute_skips_oco(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(0.0)})  # flat now
            calls: list = []
            alerts: list[str] = []
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), place_oco_exit=_oco_placer(calls)
            )
            report = cl.TickReport()
            executor(_b0_action(), False, report)
        self.assertEqual(calls, [], "no OCO placed on a flat uic")
        self.assertEqual(broker.placed, [], "no fallback stop on a flat uic")
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(any("gone" in a for a in alerts))


class TestExecuteB0NoCapability(unittest.TestCase):
    """Flag on but the wired broker has no OCO capability (placer is None): B0 must
    not raise (an AttributeError would escape the per-action boundary) — it covers
    the naked fill with a plain standalone stop instead."""

    def test_execute_b0_no_capability_places_plain_stop(self) -> None:
        with mock.patch.dict(os.environ, _OCO_ON):
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            executor = cl._make_protection_executor(broker, _throttle_to([]))  # no placer
            report = cl.TickReport()
            executor(_b0_action(), False, report)  # must NOT raise
        self.assertEqual(len(broker.placed), 1, "the naked fill is covered by a plain stop")
        self.assertEqual(broker.placed[0][:4], (_UIC, "SELL", 46.0, 216.48))
        self.assertEqual(report.exits_placed, 1)


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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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


class TestGlobalKillFileGate(unittest.TestCase):
    """D3 (ADR 0016): the GLOBAL kill (deps.global_kill_file) gates placement
    IN ADDITION to the per-instance kill_file — defense in depth. Same
    suppress-but-still-cancel semantics as the instance KILL
    (TestKillFileGate); the instance-only case (global_kill_file left at its
    None default) is already covered there and elsewhere in this module."""

    def test_global_kill_alone_suppresses_placement_but_still_cancels(self) -> None:
        with TemporaryDirectory() as d:
            instance_kill = Path(d) / "KILL"  # absent -> instance rail clear
            global_kill = Path(d) / "GLOBAL_KILL"
            global_kill.write_text("halt everything")
            broker = _StubBroker()
            place_calls: list = []
            alerts: list = []
            terminal = _verdict(status="CANCELLED", verdict="CANCELLED")
            deps = _deps(
                broker,
                kill_file=instance_kill,
                verdicts=[terminal],
                place_calls=place_calls,
                alerts=alerts,
                picks=["pick-KO"],
                global_kill_file=global_kill,
            )
            cl.run_once(deps)
            self.assertEqual(
                place_calls, [], "GLOBAL KILL alone suppresses placement, even absent instance KILL"
            )
            self.assertEqual(broker.cancelled, ["T-1"])

    def test_neither_kill_present_allows_placement(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("KO")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                alerts=[],
                picks=[pick],
                global_kill_file=Path(d) / "GLOBAL_KILL",
            )
            cl.run_once(deps)
            self.assertEqual(place_calls, [pick], "neither KILL present -> placement proceeds")

    def test_global_kill_file_none_never_touches_the_filesystem(self) -> None:
        """The default LoopDeps.global_kill_file=None must never resolve a
        real path — this is what keeps every pre-ADR-0016 caller of _deps/
        LoopDeps (the vast majority of this module) behaviorally unchanged."""
        with TemporaryDirectory() as d:
            place_calls: list = []
            pick = _pick("KO")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=place_calls,
                alerts=[],
                picks=[pick],
            )
            self.assertIsNone(deps.global_kill_file)
            cl.run_once(deps)
            self.assertEqual(place_calls, [pick])


class TestKillActiveObservability(unittest.TestCase):
    """The kill-active OBSERVABILITY sites (the heartbeat gauge's ``kill``
    argument in ``run_daemon``, and ``InProcessManagerService``'s
    ``LivenessEvent.kill_active`` in service.py) must report the SAME
    kill-active verdict as the placement-gating computation in ``run_once``
    (D3, ADR 0016): per-instance KILL OR the GLOBAL KILL. A GLOBAL-only KILL
    (the documented emergency-stop muscle-memory command) must not go
    invisible to Prometheus just because the per-instance kill_file is
    absent."""

    def test_kill_active_helper_true_on_global_only(self) -> None:
        with TemporaryDirectory() as d:
            global_kill = Path(d) / "GLOBAL_KILL"
            global_kill.write_text("halt everything")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",  # absent -> instance rail clear
                verdicts=[],
                place_calls=[],
                alerts=[],
                global_kill_file=global_kill,
            )
            self.assertTrue(cl._kill_active(deps))

    def test_kill_active_helper_false_when_neither_present(self) -> None:
        with TemporaryDirectory() as d:
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
                global_kill_file=Path(d) / "GLOBAL_KILL",
            )
            self.assertFalse(cl._kill_active(deps))

    def test_run_daemon_heartbeat_reports_global_only_kill(self) -> None:
        """Regression: pre-fix, run_daemon's heartbeat_fn read only
        deps.kill_file.exists(), so a GLOBAL-only KILL never lit the
        Prometheus KILL_ACTIVE gauge."""
        with TemporaryDirectory() as d:
            global_kill = Path(d) / "GLOBAL_KILL"
            global_kill.write_text("halt everything")
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",  # absent -> instance rail clear
                verdicts=[],
                place_calls=[],
                alerts=[],
                global_kill_file=global_kill,
            )
            beats: list[bool] = []
            cl.run_daemon(
                deps,
                once=True,
                poll_seconds=45,
                sleep_fn=lambda s: None,
                heartbeat_fn=beats.append,
            )
            self.assertEqual(beats, [True], "GLOBAL-only KILL must still light the heartbeat gauge")


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
                heartbeat_fn=lambda _kill: beats.append(1),
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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


class TestEntryTrailNeverNaked(unittest.TestCase):
    """PR-T2b never-naked: the planned disaster-SL line the entry-trail executor
    writes at FIRE-ARM is the SAME shape the normal path writes, so when the
    resting native trailing order FILLS into a Position the UNCHANGED protection
    pass (build_protection_view + reconcile_protection) places the covering SELL
    disaster stop — zero new protection code."""

    def test_fill_of_an_armed_trail_yields_a_disaster_stop(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                # 1) fire-arm writes the planned disaster-SL line (never-naked).
                cl._journal_entry_planned_disaster(
                    {"disaster_stop": 216.48, "tier_index": 0},
                    _UIC,
                    "KO-2026-07-20-entry-t0-fire",
                )
                # 2) the trail fills -> a naked long Position at the uic, no sell leg.
                broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
                # 3) the UNCHANGED protection pass derives the covering stop.
                view = cl.build_protection_view(broker, [])
                actions = cl.reconcile_protection(view)
        places = [a for a in actions if isinstance(a, PlaceStop)]
        self.assertEqual(len(places), 1, "the filled trail is covered by a disaster stop")
        self.assertEqual(places[0].uic, _UIC)
        self.assertEqual(places[0].side, "SELL")
        self.assertEqual(places[0].stop_price, 216.48, "the brief disaster floor from fire-arm")


class TestExecutePlaceStopJournalsStopPlaced(unittest.TestCase):
    """A successful standalone-stop placement journals a timestamped ``stop_placed``
    outcome record (observability-only: fill-to-protection latency for the non-OCO
    path). The qty journaled is the qty ACTUALLY placed (post execute-time clamp),
    and NO record is written on any rejection / error / flat-skip path."""

    def _stop_placed_lines(self) -> list[dict[str, Any]]:
        return [
            line for line in cl._iter_standalone_stop_journal() if line.get("kind") == "stop_placed"
        ]

    def test_journal_stop_placed_record_shape_with_injected_clock(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._journal_stop_placed(_UIC, 46.0, clock=lambda: 1234.5)
                lines = self._stop_placed_lines()
        self.assertEqual(lines, [{"kind": "stop_placed", "uic": _UIC, "qty": 46.0, "ts": 1234.5}])

    def test_success_appends_stop_placed_with_ts_and_qty(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
            executor = cl._make_protection_executor(broker, _throttle_to([]))
            action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(action, False, report)
                lines = self._stop_placed_lines()
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["uic"], _UIC)
        self.assertEqual(lines[0]["qty"], 46.0)
        self.assertIsInstance(lines[0]["ts"], float)

    def test_clamped_qty_is_journaled_when_position_shrank(self) -> None:
        # The live re-check clips 46 -> 20; the journal must carry the 20 actually
        # placed, never the stale action.qty.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(by_uic={_UIC: _pos(20.0)})
            executor = cl._make_protection_executor(broker, _throttle_to([]))
            action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(action, False, report)
                lines = self._stop_placed_lines()
        self.assertEqual(broker.placed[0][2], 20.0)
        self.assertEqual([line["qty"] for line in lines], [20.0])

    def test_no_stop_placed_on_failure_paths(self) -> None:
        failure_brokers = {
            "flat-skip": _ProtBroker(by_uic={_UIC: _pos(0.0)}),
            "defer-sell-exist": _ProtBroker(
                by_uic={_UIC: _pos(46.0)},
                place_error=OrderRejectedError(
                    "blocked", error_code="SellOrdersAlreadyExistForOwnedContracts"
                ),
            ),
            "clean-reject": _ProtBroker(
                by_uic={_UIC: _pos(46.0)}, place_error=OrderRejectedError("rejected")
            ),
            "broker-error": _ProtBroker(by_uic={_UIC: _pos(46.0)}, place_error=BrokerError("boom")),
        }
        for label, broker in failure_brokers.items():
            with self.subTest(label), TemporaryDirectory() as d:
                journal = Path(d) / "standalone_stops.jsonl"
                executor = cl._make_protection_executor(broker, _throttle_to([]))
                action = PlaceStop(_UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 0))
                report = cl.TickReport()
                with mock.patch.object(
                    cl, "_standalone_stop_journal_path", lambda journal=journal: journal
                ):
                    executor(action, False, report)
                    lines = self._stop_placed_lines()
                self.assertEqual(report.exits_placed, 0)
                self.assertEqual(lines, [], f"no stop_placed on the {label} path")


class TestOutcomeJournalIoFailureNeverBlocksProtection(unittest.TestCase):
    """The ``stop_placed`` / ``amend_ok`` outcome records are observability-only,
    so a fallible journal append (OSError: disk full, ENOSPC on fsync, permission)
    must NEVER change protection behavior: the supersede cancels of the OLD stop
    still run (never two live sell stops on the same shares), no exception escapes
    the executor (an OSError is not a BrokerError, so it would blow through the
    per-action boundary and kill the tick), and the failure surfaces as a
    throttled alert only."""

    def test_supersede_cancels_survive_stop_placed_journal_oserror(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = PlaceStop(
            _UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 1), supersede_ids=("old-stop",)
        )
        report = cl.TickReport()
        with mock.patch.object(
            cl,
            "_append_standalone_stop_journal",
            side_effect=OSError("No space left on device"),
        ):
            executor(action, False, report)  # must NOT raise
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(
            broker.cancelled,
            ["old-stop"],
            "superseded stop still cancelled — never two live sell stops on the same shares",
        )
        self.assertEqual(report.exits_placed, 1)
        self.assertTrue(
            any("journal" in a for a in alerts),
            f"journal write failure surfaced as a throttled alert: {alerts}",
        )

    def test_non_oserror_from_journal_append_does_not_escape_executor(self) -> None:
        """The containment intent is 'the journal can NEVER abort protection', not
        'disk errors cannot'. A non-OSError bug in the append (e.g. a future
        non-JSON-serializable field -> TypeError, or a RuntimeError) must be
        contained the same way — it is not a BrokerError either, so unhandled it
        would abort the remaining protection actions of the tick."""
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(46.0)})
        executor = cl._make_protection_executor(broker, _throttle_to(alerts))
        action = PlaceStop(
            _UIC, "SELL", 46.0, 216.48, _exit_stop_ref("crid-0", 1), supersede_ids=("old-stop",)
        )
        report = cl.TickReport()
        with mock.patch.object(
            cl,
            "_append_standalone_stop_journal",
            side_effect=RuntimeError("journal append bug"),
        ):
            executor(action, False, report)  # must NOT raise
        self.assertEqual(broker.cancelled, ["old-stop"], "supersede cancel still ran")
        self.assertEqual(report.exits_placed, 1)
        self.assertTrue(
            any("journal" in a for a in alerts),
            f"journal write failure surfaced as a throttled alert: {alerts}",
        )

    def test_amend_ok_journal_oserror_does_not_escape_executor(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(4.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)])
        executor = cl._make_protection_executor(
            broker, _throttle_to(alerts), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        with mock.patch.object(
            cl,
            "_append_standalone_stop_journal",
            side_effect=OSError("Permission denied"),
        ):
            executor(_amend_action(), False, report)  # must NOT raise
        self.assertEqual(len(broker.amended), 1, "the amend itself succeeded")
        self.assertEqual(report.exits_placed, 1)
        self.assertTrue(
            any("journal" in a for a in alerts),
            f"journal write failure surfaced as a throttled alert: {alerts}",
        )


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


class TestAlertSinkJournalsToLogger(unittest.TestCase):
    """VRNS incident 2026-07-29: alerts went to Telegram ONLY, so journalctl greps
    came back empty mid-incident. Every alert the sink actually emits must ALSO be
    logger.warning'd (journald) BEFORE the Telegram send — at the sink seam, not
    the ~30 call sites."""

    def test_journaled_alert_logs_message_before_telegram_send(self) -> None:
        sent: list[str] = []
        sink = cl._journaled_alert(sent.append)
        with self.assertLogs(cl.logger, level="WARNING") as captured:
            sink("OCO rejected uic 123: stop deferred")
        self.assertEqual(sent, ["OCO rejected uic 123: stop deferred"])
        self.assertTrue(
            any("OCO rejected uic 123: stop deferred" in line for line in captured.output),
            captured.output,
        )

    def test_journaled_alert_logs_even_when_telegram_send_raises(self) -> None:
        def _exploding_send(message: str) -> None:
            raise RuntimeError("telegram down")

        sink = cl._journaled_alert(_exploding_send)
        with self.assertLogs(cl.logger, level="WARNING") as captured:
            with contextlib.suppress(RuntimeError):
                sink("stop deferred — sell-commit not yet released")
        self.assertTrue(
            any("sell-commit not yet released" in line for line in captured.output),
            captured.output,
        )

    def test_throttle_suppressed_repeat_does_not_log(self) -> None:
        sent: list[str] = []
        throttle = cl._AlertThrottle(
            cl._journaled_alert(sent.append), clock=lambda: 0.0, interval_s=1800.0
        )
        with self.assertLogs(cl.logger, level="WARNING"):
            self.assertTrue(throttle.emit("naked", uic=1, reason="deficit"))
        with self.assertNoLogs(cl.logger, level="WARNING"):
            self.assertFalse(throttle.emit("naked", uic=1, reason="deficit"))
        self.assertEqual(sent, ["naked"])


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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._mark_oco_unsupported(_UIC)
                folded = cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
        self.assertIn(_UIC, folded)

    def test_build_protection_view_populates_oco_unsupported_from_journal(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
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
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                next_gen = cl._make_next_gen(43070)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(30.0), 1)
                self.assertEqual(next_gen(30.0), 1)
                self.assertEqual(next_gen(45.0), 2)

    def test_float_tolerance_no_gen_flicker(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                next_gen = cl._make_next_gen(43070)
                self.assertEqual(next_gen(46.0), 0)
                self.assertEqual(next_gen(45.9999999), 0)

    def test_gen_persists_append_only_across_fresh_callables(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._make_next_gen(43070)(46.0)
                cl._make_next_gen(43070)(30.0)
                self.assertEqual(cl._make_next_gen(43070)(30.0), 1)
                self.assertEqual(cl._make_next_gen(43070)(20.0), 2)
                self.assertEqual(cl._make_next_gen(99999)(10.0), 0)


_AMEND_ON = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}


def _amend_action(**over: Any) -> AmendStop:
    base: dict[str, Any] = {
        "uic": _UIC,
        "side": "SELL",
        "order_id": "stop-1",
        "order_type": "StopIfTraded",
        "target_qty": 4.0,
        "stop_price": 216.48,
        "request_id": _exit_amend_ref("crid-0", 0),
        "reason": "grow — PATCH amend stop up in place",
    }
    base.update(over)
    return AmendStop(**base)


class TestExecuteAmendStop(unittest.TestCase):
    """The Stage-3 AmendStop executor (saxo Stage-3 memo). Absolute-target: it
    re-reads LIVE owned at execute time and amends to it in BOTH directions (a
    position that grew or shrank since the snapshot is covered up to live owned,
    never stranded naked, never oversold). NO cancel; ALLOWED under KILL (an
    in-place resize only reduces exposure or enlarges cover). On ANY amend failure
    it journals ``amend_failed`` (TTL fold -> ``amend_recently_failed`` skips amend
    next tick, falling to the proven B1 additive / place-first) AND escalates via
    ``record_place_failure`` — no permanent capability latch."""

    def test_execute_amend_targets_live_owned_when_grew(self) -> None:
        # grew to 6 since the snapshot (4); the resting stop is present + unfilled.
        broker = _ProtBroker(by_uic={_UIC: _pos(6.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)])
        executor = cl._make_protection_executor(
            broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        executor(_amend_action(target_qty=4.0), False, report)
        self.assertEqual(len(broker.amended), 1)
        self.assertEqual(broker.amended[0][4], 6.0, "amend to LIVE owned (grew), never the stale 4")
        self.assertEqual(report.exits_placed, 1)

    def test_execute_amend_targets_live_owned_when_shrank(self) -> None:
        # shrank to 4 since the snapshot (7); the resting stop is present + unfilled.
        broker = _ProtBroker(by_uic={_UIC: _pos(4.0)}, sells=[_leg("stop-1", "StopIfTraded", 7.0)])
        executor = cl._make_protection_executor(
            broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        executor(_amend_action(target_qty=7.0), False, report)
        self.assertEqual(len(broker.amended), 1)
        self.assertEqual(
            broker.amended[0][4], 4.0, "amend to LIVE owned (shrank), never oversell 7"
        )

    def test_execute_amend_flat_skip_when_live_below_eps(self) -> None:
        alerts: list[str] = []
        broker = _ProtBroker(by_uic={_UIC: _pos(0.2)})  # effectively flat
        executor = cl._make_protection_executor(
            broker, _throttle_to(alerts), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        executor(_amend_action(target_qty=4.0), False, report)
        self.assertEqual(broker.amended, [], "no amend on a flat uic")
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(any("gone" in a or "skip" in a for a in alerts))

    def test_execute_amend_capability_error_no_journal_no_escalation(self) -> None:
        # A BrokerCapabilityError (orders disabled) is PROVABLY UNSENT, not an amend
        # rejection: it must NOT journal amend_failed (which would needlessly skip
        # amend next tick) nor escalate via record_place_failure — just a throttled
        # alert; the env gate self-clears and amend retries next tick.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            sent: list[str] = []
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],
                amend_error=BrokerCapabilityError("order placement disabled"),
            )
            throttle = cl._AlertThrottle(sent.append, clock=lambda: 0.0)
            executor = cl._make_protection_executor(
                broker, throttle, amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)  # must NOT raise
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual(
            markers, [], "a provably-unsent capability error never journals amend_failed"
        )
        self.assertEqual(broker.amended, [], "nothing was amended")
        self.assertEqual(report.exits_placed, 0)
        self.assertTrue(sent, "the orders-disabled state is surfaced (throttled)")
        self.assertFalse(
            any("amend failed" in a for a in sent),
            f"a provably-unsent error does not escalate as a place-failure: {sent}",
        )

    def test_execute_amend_reject_journals_amend_failed_and_records_failure(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            sent: list[str] = []
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],
                amend_error=OrderRejectedError("terminal order", error_code="OrderNotWorking"),
            )
            throttle = cl._AlertThrottle(sent.append, clock=lambda: 0.0)
            executor = cl._make_protection_executor(
                broker, throttle, amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)  # must NOT raise
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual([m.get("uic") for m in markers], [_UIC], "amend_failed marker journaled")
        self.assertEqual(report.exits_placed, 0)
        # record_place_failure emitted the routine place-failure alert (below threshold).
        self.assertTrue(sent, "the amend failure escalates via record_place_failure")
        # No permanent latch: the uic is NOT marked oco_unsupported by an amend failure.
        with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
            self.assertNotIn(
                _UIC, cl._fold_oco_unsupported(list(cl._iter_standalone_stop_journal()))
            )

    def test_execute_amend_allowed_under_kill(self) -> None:
        broker = _ProtBroker(by_uic={_UIC: _pos(4.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)])
        executor = cl._make_protection_executor(
            broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        executor(_amend_action(), True, report)  # kill = True
        self.assertEqual(
            len(broker.amended), 1, "an in-place protective resize is allowed under KILL"
        )
        self.assertEqual(report.exits_placed, 1)

    def test_execute_amend_no_capability_is_noop(self) -> None:
        # A broker without SupportsAmendStop leaves amend_stop=None; the executor
        # must NOT crash (AttributeError escapes the per-action boundary) — it is a
        # pure no-op (the pure arm never emits AmendStop without the capability).
        broker = _ProtBroker(by_uic={_UIC: _pos(4.0)})
        executor = cl._make_protection_executor(broker, _throttle_to([]))  # amend_stop=None
        report = cl.TickReport()
        executor(_amend_action(), False, report)  # must NOT raise
        self.assertEqual(broker.amended, [])
        self.assertEqual(report.exits_placed, 0)

    def test_execute_amend_bails_when_resting_order_partially_filled(self) -> None:
        # Q10 mid-fill TOCTOU: the SPECIFIC resting stop being amended partially
        # filled between the decision snapshot and the PATCH. Saxo's partial-fill
        # amend semantics are unproven -> do NOT amend; journal amend_failed + a
        # throttled alert so the next tick falls to the proven B1 additive primitive.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0, filled=2.0)],  # 2 of 4 already filled
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual(broker.amended, [], "no amend on a partially-filled resting stop (Q10)")
        self.assertEqual(report.exits_placed, 0)
        self.assertEqual(
            [m.get("uic") for m in markers], [_UIC], "amend_failed journaled -> B1 next tick"
        )
        self.assertTrue(any("skip" in a.lower() for a in alerts), alerts)

    def test_execute_amend_bails_when_resting_order_gone(self) -> None:
        # The resting stop vanished (gone/filled) between snapshot and execute — it
        # is absent from list_working_sell_orders. Same bail: no amend, journal
        # amend_failed, alert; the residual is covered next tick (never naked).
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            alerts: list[str] = []
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("other-stop", "StopIfTraded", 4.0)],  # NOT the amended order_id
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to(alerts), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual(broker.amended, [], "no amend on a vanished resting stop (Q10)")
        self.assertEqual(report.exits_placed, 0)
        self.assertEqual([m.get("uic") for m in markers], [_UIC], "amend_failed journaled")
        self.assertTrue(any("skip" in a.lower() for a in alerts), alerts)

    def test_execute_amend_proceeds_when_resting_order_fully_unfilled(self) -> None:
        # The resting stop is present and untouched (filled_quantity == 0) -> the
        # amend proceeds unchanged (re-read owned + clamp + PATCH), no amend_failed.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],  # present, unfilled
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(target_qty=4.0), False, report)
                markers = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual(len(broker.amended), 1, "unfilled resting stop -> amend proceeds")
        self.assertEqual(broker.amended[0][4], 4.0)
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(markers, [], "no amend_failed journaled on a clean amend")


class TestExecuteAmendStopJournalsAmendOk(unittest.TestCase):
    """A successful AmendStop PATCH journals a timestamped ``amend_ok`` outcome
    record carrying the qty actually amended to (the live-clamped target), so
    fill-to-protection latency is measurable on the amend path too. Failure paths
    keep their existing ``amend_failed`` / no-journal behavior — never ``amend_ok``."""

    def _amend_ok_lines(self) -> list[dict[str, Any]]:
        return [
            line for line in cl._iter_standalone_stop_journal() if line.get("kind") == "amend_ok"
        ]

    def test_journal_amend_ok_record_shape_with_injected_clock(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._journal_amend_ok(_UIC, 6.0, clock=lambda: 1234.5)
                lines = self._amend_ok_lines()
        self.assertEqual(lines, [{"kind": "amend_ok", "uic": _UIC, "qty": 6.0, "ts": 1234.5}])

    def test_amend_success_appends_amend_ok_with_live_clamped_qty(self) -> None:
        # grew to 6 since the snapshot (target_qty 4): the amend targets LIVE owned
        # and the journal carries that actually-amended 6, never the stale 4.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(6.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)]
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(target_qty=4.0), False, report)
                lines = self._amend_ok_lines()
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["uic"], _UIC)
        self.assertEqual(lines[0]["qty"], 6.0)
        self.assertIsInstance(lines[0]["ts"], float)

    def test_no_amend_ok_on_capability_error(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],
                amend_error=BrokerCapabilityError("order placement disabled"),
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)
                lines = self._amend_ok_lines()
        self.assertEqual(lines, [], "no amend_ok on a provably-unsent capability error")
        self.assertEqual(report.exits_placed, 0)

    def test_no_amend_ok_on_broker_error_and_amend_failed_kept(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],
                amend_error=OrderRejectedError("terminal order", error_code="OrderNotWorking"),
            )
            throttle = cl._AlertThrottle([].append, clock=lambda: 0.0)
            executor = cl._make_protection_executor(
                broker, throttle, amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)
                ok_lines = self._amend_ok_lines()
                failed_lines = [
                    line
                    for line in cl._iter_standalone_stop_journal()
                    if line.get("kind") == "amend_failed"
                ]
        self.assertEqual(ok_lines, [], "no amend_ok on a rejected amend")
        self.assertEqual(
            [m.get("uic") for m in failed_lines], [_UIC], "amend_failed keeps its behavior"
        )
        self.assertEqual(report.exits_placed, 0)


class TestExecuteAmendStopJournalsReanchored(unittest.TestCase):
    """PR-6b: a CONFIRMED AmendStop success journals a ``reanchored`` marker
    ONLY when ``action.reanchor_avg_price`` is set (a plain grow/downsize amend
    carries ``None`` and never journals it). A failed amend never latches —
    it journals ``amend_failed`` like any other amend and retries."""

    def _reanchored_lines(self) -> list[dict[str, Any]]:
        return [
            line for line in cl._iter_standalone_stop_journal() if line.get("kind") == "reanchored"
        ]

    def test_journal_reanchored_record_shape_with_injected_clock(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._journal_reanchored(_UIC, 95.0, clock=lambda: 1234.5)
                lines = self._reanchored_lines()
        self.assertEqual(
            lines, [{"kind": "reanchored", "uic": _UIC, "avg_price": 95.0, "ts": 1234.5}]
        )

    def test_confirmed_success_with_reanchor_avg_price_journals_reanchored(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)]
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(
                    _amend_action(reason="reanchor-on-fill", reanchor_avg_price=95.0),
                    False,
                    report,
                )
                lines = self._reanchored_lines()
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["uic"], _UIC)
        self.assertEqual(lines[0]["avg_price"], 95.0)
        self.assertIsInstance(lines[0]["ts"], float)

    def test_confirmed_success_without_reanchor_avg_price_never_journals(self) -> None:
        # A plain grow/downsize AmendStop (reanchor_avg_price=None, the default)
        # must NEVER journal a reanchored marker on success.
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)}, sells=[_leg("stop-1", "StopIfTraded", 4.0)]
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(_amend_action(), False, report)  # reanchor_avg_price defaults None
                lines = self._reanchored_lines()
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(lines, [], "a non-reanchor amend never journals reanchored")

    def test_failed_amend_never_journals_reanchored(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(4.0)},
                sells=[_leg("stop-1", "StopIfTraded", 4.0)],
                amend_error=OrderRejectedError("terminal order", error_code="OrderNotWorking"),
            )
            throttle = cl._AlertThrottle([].append, clock=lambda: 0.0)
            executor = cl._make_protection_executor(
                broker, throttle, amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(
                    _amend_action(reason="reanchor-on-fill", reanchor_avg_price=95.0),
                    False,
                    report,
                )
                lines = self._reanchored_lines()
        self.assertEqual(lines, [], "a failed reanchor amend never latches")
        self.assertEqual(report.exits_placed, 0)


def _oco_leg(
    order_id: str, order_type: str, amount: float, *, base: str = "crid-oco-0"
) -> OrderState:
    """A resting OCO exit leg (``OrderRelation='Oco'`` + shared base ref), what
    ``_build_oco_exit_body`` stamps and ``_to_order_state`` maps back."""
    suffix = "-stop" if order_type in ("Stop", "StopIfTraded", "StopLimit") else "-tp"
    return OrderState(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=None,
        filled_quantity=0.0,
        raw_status="Working",
        uic=_UIC,
        side="SELL",
        order_type=order_type,
        amount=amount,
        external_reference=f"{base}{suffix}",
        order_relation="Oco",
    )


class TestOcoAmendExecutorReuse(unittest.TestCase):
    """Stage-3.5 REUSES the Stage-3 AmendStop executor + dispatch BYTE-FOR-BYTE for
    an OCO-leg amend. An OCO-leg ``AmendStop`` is the SAME dataclass — only its
    ``order_id`` points at a resting OCO child stop and its ``reason`` carries the
    OCO telemetry string. These pins prove the executor is leg-shape-agnostic: the
    dispatch routes it, the executor re-reads + clamps to live owned, and an OCO-leg
    amend failure journals ``amend_failed`` so the NEXT tick skips the OCO amend and
    falls to the proven B1 additive fallback (never a naked window)."""

    def test_amend_stop_dispatch_routes_oco_leg_amend(self) -> None:
        # An OCO-leg AmendStop (order_id = OCO child stop, reason 'grow-after-OCO')
        # routes through the UNCHANGED isinstance(AmendStop) dispatch into
        # _execute_amend_stop — the same executor as a standalone amend.
        broker = _ProtBroker(
            by_uic={_UIC: _pos(7.0)}, sells=[_oco_leg("oco-stop-1", "StopIfTraded", 5.0)]
        )
        executor = cl._make_protection_executor(
            broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        action = _amend_action(
            order_id="oco-stop-1",
            target_qty=7.0,
            reason="grow-after-OCO — PATCH OCO stop leg up in place",
        )
        executor(action, False, report)
        self.assertEqual(len(broker.amended), 1, "OCO-leg AmendStop routes through the dispatch")
        self.assertEqual(broker.amended[0][1], "oco-stop-1", "PATCH targets the OCO child stop id")
        self.assertEqual(report.exits_placed, 1)

    def test_executor_rereads_and_clamps_oco_amend_target(self) -> None:
        # owned shrank to 5 between the decision (stale target 9) and execute; the
        # executor re-reads LIVE owned via get_positions_by_uic and clamps the PATCH
        # target to it, never the stale 9 — identical to the standalone path.
        broker = _ProtBroker(
            by_uic={_UIC: _pos(5.0)}, sells=[_oco_leg("oco-stop-1", "StopIfTraded", 9.0)]
        )
        executor = cl._make_protection_executor(
            broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
        )
        report = cl.TickReport()
        executor(
            _amend_action(
                order_id="oco-stop-1",
                target_qty=9.0,
                reason="OCO downsize — PATCH OCO stop leg down in place",
            ),
            False,
            report,
        )
        self.assertEqual(len(broker.amended), 1)
        self.assertEqual(
            broker.amended[0][4], 5.0, "OCO amend clamps to re-read live owned, never the stale 9"
        )

    def test_oco_amend_failure_journals_amend_failed(self) -> None:
        # An OCO-leg amend that rejects journals ``amend_failed`` for the uic (same
        # executor path). The NEXT tick folds it into ``amend_recently_failed`` and
        # the pure OCO-grow arm SKIPS the amend, falling to the B1 additive delta
        # (a PlaceStop with NO pre-cancel of the OCO pair — never naked).
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(
                by_uic={_UIC: _pos(7.0)},
                sells=[_oco_leg("oco-stop-1", "StopIfTraded", 5.0)],
                amend_error=OrderRejectedError("stale order", error_code="OrderNotWorking"),
            )
            executor = cl._make_protection_executor(
                broker, _throttle_to([]), amend_stop=broker.amend_stop_amount
            )
            report = cl.TickReport()
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                executor(
                    _amend_action(
                        order_id="oco-stop-1",
                        reason="grow-after-OCO — PATCH OCO stop leg up in place",
                    ),
                    False,
                    report,
                )
                lines = list(cl._iter_standalone_stop_journal())
                markers = [line for line in lines if line.get("kind") == "amend_failed"]
                folded = cl._fold_ttl_markers(
                    lines, "amend_failed", now=0.0, ttl_s=cl._AMEND_FAILED_TTL_S
                )
        self.assertEqual([m.get("uic") for m in markers], [_UIC], "amend_failed journaled for uic")
        self.assertEqual(report.exits_placed, 0)
        self.assertIn(_UIC, folded, "the fold marks the uic amend_recently_failed next tick")

        # Next tick: the resting OCO pair (grew to owned=7) with the uic in
        # amend_recently_failed must SKIP the OCO-grow amend and fall to B1 additive.
        pos = _pos(7.0)
        legs = (
            _oco_leg("oco-stop-1", "StopIfTraded", 5.0),
            _oco_leg("oco-tp-1", "Limit", 5.0),
        )
        view = ProtectionView(
            long_positions={_UIC: pos},
            all_positions={_UIC: pos},
            sell_legs_by_uic={_UIC: legs},
            planned_by_uic={
                _UIC: PlannedExit(
                    uic=_UIC,
                    entry_crid="crid-0",
                    side="SELL",
                    stop_price=216.48,
                    tp_price=306.72,
                    conflicting=False,
                    n_plans=1,
                )
            },
            oco_unsupported=frozenset(),
            amend_recently_failed=frozenset({_UIC}),
        )
        with mock.patch.dict(os.environ, {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}):
            actions = _reconcile_long(_UIC, pos, view)
        self.assertFalse(
            any(isinstance(a, AmendStop) for a in actions),
            "amend_recently_failed skips the OCO amend on the next tick",
        )
        places = [a for a in actions if isinstance(a, PlaceStop)]
        self.assertEqual(len(places), 1, "the delta falls to a B1 additive PlaceStop")
        self.assertEqual(
            set(places[0].cancel_conflicting) & {"oco-stop-1", "oco-tp-1"},
            set(),
            "the B1 additive fallback never pre-cancels an OCO leg (never naked)",
        )


class TestBuildProtectionViewTtlFolds(unittest.TestCase):
    """build_protection_view folds the append-only TTL markers against the injected
    clock (saxo Stage-3 memo): only markers newer than the TTL count. A stale marker
    expires so B0 re-fires / amend retries after the window."""

    def _broker(self) -> _ProtBroker:
        return _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})

    def test_build_protection_view_folds_oco_recently_placed_within_ttl_and_expires_after(
        self,
    ) -> None:
        fresh_uic, stale_uic = _UIC, 99999
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_oco_placed(fresh_uic, clock=lambda: 1000.0 - 30.0)  # 30s ago
                cl._journal_oco_placed(stale_uic, clock=lambda: 1000.0 - 300.0)  # 300s ago
                view = cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
        self.assertIn(fresh_uic, view.oco_recently_placed, "the 30s-old marker is fresh (TTL 120s)")
        self.assertNotIn(stale_uic, view.oco_recently_placed, "the 300s-old marker expired")

    def test_build_protection_view_folds_amend_recently_failed_within_ttl(self) -> None:
        fresh_uic, stale_uic = _UIC, 99999
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_amend_failed(fresh_uic, clock=lambda: 1000.0 - 30.0)
                cl._journal_amend_failed(stale_uic, clock=lambda: 1000.0 - 300.0)
                view = cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
        self.assertIn(fresh_uic, view.amend_recently_failed)
        self.assertNotIn(stale_uic, view.amend_recently_failed)

    def test_ttl_folds_default_empty_without_markers(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                view = cl.build_protection_view(self._broker(), [])
        self.assertEqual(view.oco_recently_placed, frozenset())
        self.assertEqual(view.amend_recently_failed, frozenset())


class TestFoldReanchoredMarkers(unittest.TestCase):
    """PR-6b: ``_fold_reanchored_markers`` folds the LATEST (by ts) avg_price per
    uic — a plain dict, PERMANENT (no TTL), unlike ``_fold_ttl_markers``."""

    def test_latest_avg_price_wins_per_uic(self) -> None:
        lines = [
            {"kind": "reanchored", "uic": _UIC, "avg_price": 95.0, "ts": 100.0},
            {"kind": "reanchored", "uic": _UIC, "avg_price": 97.5, "ts": 200.0},  # newer wins
            {"kind": "reanchored", "uic": 99999, "avg_price": 10.0, "ts": 50.0},
        ]
        result = cl._fold_reanchored_markers(lines)
        self.assertEqual(result, {_UIC: 97.5, 99999: 10.0})

    def test_malformed_lines_skipped(self) -> None:
        lines = [
            {"kind": "reanchored", "uic": _UIC},  # missing avg_price/ts
            {"kind": "reanchored", "avg_price": 95.0, "ts": 100.0},  # missing uic
            {"kind": "reanchored", "uic": "bad", "avg_price": 95.0, "ts": 100.0},
        ]
        self.assertEqual(cl._fold_reanchored_markers(lines), {})

    def test_no_markers_folds_empty_dict(self) -> None:
        self.assertEqual(cl._fold_reanchored_markers([]), {})


class TestBuildProtectionViewWiresReanchoredByUic(unittest.TestCase):
    """PR-6b: build_protection_view wires ``reanchored_by_uic`` from the
    append-only ``reanchored`` journal markers into ``ProtectionView``."""

    def _broker(self) -> _ProtBroker:
        return _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})

    def test_wires_the_latched_avg_price(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_reanchored(_UIC, 95.0, clock=lambda: 1000.0)
                view = cl.build_protection_view(self._broker(), [], clock=lambda: 2000.0)
        self.assertEqual(view.reanchored_by_uic, {_UIC: 95.0})

    def test_empty_without_markers(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                view = cl.build_protection_view(self._broker(), [])
        self.assertEqual(view.reanchored_by_uic, {})


class TestProtectionViewIgnoresOutcomeRecords(unittest.TestCase):
    """``stop_placed`` / ``amend_ok`` are observability-only: build_protection_view
    and every fold must produce EXACTLY the same view with or without them — zero
    behavioral change to protection logic."""

    def _broker(self) -> _ProtBroker:
        return _ProtBroker(positions=[_pos(46.0)], by_uic={_UIC: _pos(46.0)})

    def _view_fields(self, view: ProtectionView) -> tuple[Any, ...]:
        return (
            _planned_fold_data(view.planned_by_uic),
            view.oco_unsupported,
            view.oco_recently_placed,
            view.amend_recently_failed,
        )

    def test_view_identical_with_and_without_outcome_records(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl._journal_oco_placed(_UIC, clock=lambda: 1000.0 - 30.0)
                cl._journal_amend_failed(_UIC, clock=lambda: 1000.0 - 30.0)
                before = self._view_fields(
                    cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
                )
                cl._journal_stop_placed(_UIC, 46.0, clock=lambda: 1000.0 - 5.0)
                cl._journal_amend_ok(_UIC, 46.0, clock=lambda: 1000.0 - 5.0)
                after = self._view_fields(
                    cl.build_protection_view(self._broker(), [], clock=lambda: 1000.0)
                )
        self.assertEqual(before, after, "the outcome records change nothing in the view")


class TestAmendSeqMonotonicJournalBacked(unittest.TestCase):
    """The journal-backed amend sequence is ALWAYS max+1 (never qty-keyed), so a
    re-resize to a previously-seen target qty gets a fresh ref and is never
    dedup-swallowed (mitigation A3). It persists append-only across restarts."""

    def test_amend_seq_is_monotonic_and_persists(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                self.assertEqual(cl._make_next_amend_seq(_UIC)(), 0)
                self.assertEqual(cl._make_next_amend_seq(_UIC)(), 1)
                self.assertEqual(cl._make_next_amend_seq(_UIC)(), 2)
                self.assertEqual(cl._make_next_amend_seq(88888)(), 0, "seq is per-uic")

    def test_fold_planned_exits_wires_journal_backed_amend_seq(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                planned = cl._fold_planned_exits(list(cl._iter_standalone_stop_journal()))[_UIC]
                s0 = planned.next_amend_seq()
                s1 = planned.next_amend_seq()
        self.assertEqual((s0, s1), (0, 1), "the folded PlannedExit carries the monotonic seq")


class _StopOnlyBroker:
    """SupportsStandaloneStop but NOT SupportsAmendStop (no amend_stop_amount)."""

    name = "stoponly"

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ) -> PlacedOrder:
        return PlacedOrder(entry_order_id="S-1", exit_order_ids=())


class TestBuildDefaultDepsAmendFailFast(unittest.TestCase):
    """build_default_deps FAIL-FASTS when the amend flag is on but the wired broker
    has no SupportsAmendStop capability — so the pure layer may emit AmendStop
    freely, knowing a capable broker is guaranteed at runtime (saxo Stage-3 memo)."""

    def test_fail_fast_when_amend_enabled_but_no_capability(self) -> None:
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.dict(os.environ, _AMEND_ON),
        ):
            with self.assertRaises(BrokerCapabilityError):
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)


class TestBuildDefaultDepsExitPolicyCapabilityGate(unittest.TestCase):
    """PR-6b: the PR-6a BLANKET fail-fast on ALPHALENS_BROKER_EXIT_POLICY !=
    "setup_static" is replaced by a CAPABILITY gate. Geometry-live now ships with
    the fill-complete avg_price reanchor (position_manager._maybe_reanchor), which
    rides the AmendStop rail — so it is allowed only when the wired broker can amend
    (SupportsAmendStop); a stop-only broker cannot run the reanchor and would leave a
    wrong-distance stop, so it still fail-fasts. The default flag never gates."""

    def test_does_not_raise_when_flag_flipped_and_broker_can_amend(self) -> None:
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_ProtBroker(),  # SupportsStandaloneStop + SupportsAmendStop
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, {"ALPHALENS_BROKER_EXIT_POLICY": "atr_bracket_1p5"}),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        self.assertIsNotNone(deps)
        # The policy is resolved ONCE at startup and cached on the deps so the hot
        # protection path never re-resolves the env string (adversarial-review P0).
        self.assertEqual(deps.exit_policy.name, "atr_bracket_1p5")
        self.assertTrue(deps.exit_policy.requires_amend_stop)

    def test_fail_fasts_when_flag_flipped_but_broker_cannot_amend(self) -> None:
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),  # no SupportsAmendStop -> no reanchor rail
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, {"ALPHALENS_BROKER_EXIT_POLICY": "atr_bracket_1p5"}),
        ):
            with self.assertRaises(BrokerCapabilityError):
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)

    def test_does_not_raise_when_exit_policy_left_at_default(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_EXIT_POLICY"}
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        self.assertIsNotNone(deps)
        # Default env resolves to the inert setup_static policy, cached on the deps.
        self.assertEqual(deps.exit_policy.name, "setup_static")
        self.assertFalse(deps.exit_policy.requires_amend_stop)

    def test_raises_value_error_on_unknown_exit_policy_name(self) -> None:
        # An unknown env name FAILS FAST at startup (build_default_deps), never
        # deferred into a tick where a ValueError would starve the protection pass.
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_ProtBroker(),  # capable broker: the name, not capability, fails
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
            mock.patch.dict(os.environ, {"ALPHALENS_BROKER_EXIT_POLICY": "bogus"}),
        ):
            with self.assertRaises(ValueError):
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)


class TestBuildDefaultDepsWiresNotificationPorts(unittest.TestCase):
    """PR-4 (NotificationPort): build_default_deps takes the concrete alert
    sinks as REQUIRED kwargs from its caller (the CLI composition root) —
    control_loop.py itself never imports telegram. ``notify`` becomes the
    daemon's journaled base alert; ``chain_loss_notify`` is threaded into the
    OAuth provider for the refresh-chain-lost alert."""

    def test_notify_is_wrapped_in_journaled_alert(self) -> None:
        sent: list[str] = []
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
        ):
            deps = cl.build_default_deps(notify=sent.append, chain_loss_notify=lambda _msg: None)
        with self.assertLogs(cl.logger, level="WARNING") as captured:
            deps.alert("naked position uic 999")
        self.assertEqual(sent, ["naked position uic 999"])
        self.assertTrue(
            any("naked position uic 999" in line for line in captured.output), captured.output
        )

    def test_chain_loss_notify_is_threaded_into_the_oauth_provider(self) -> None:
        chain_loss_sink = mock.Mock()
        with (
            _isolated_home(),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.object(
                cl, "_default_oauth_provider", return_value=mock.Mock()
            ) as oauth_factory,
        ):
            cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=chain_loss_sink)
        oauth_factory.assert_called_once_with(alert=chain_loss_sink)


class TestBuildDefaultDepsStateGuards(unittest.TestCase):
    """D4 (legacy-layout guard, ADR 0016) + the ``env == live`` branch, ADR
    0017. D4 still runs FIRST, before any broker/journal I/O — a legacy-layout
    mistake must never reach a partially-wired daemon (fail-loud, not
    fail-empty). The old D7 hard-raise (ADR 0016, "LIVE cannot boot yet") is
    GONE: env=live now routes into the LIVE factory
    (``create_saxo_broker_live_from_env``), which itself refuses to construct
    anything until ``assert_live_rails`` passes — so a rails-unset LIVE boot
    fails via THAT message, and the SIM registry path
    (``get_default_broker``) is never reached. Composition-root-specific
    coverage (patched-factory happy path, SessionKeeper identity, streaming
    skip) lives in ``test_live_composition.py``."""

    def test_refuses_to_boot_a_live_instance_with_rails_unset(self) -> None:
        with (
            _isolated_home(),
            mock.patch.dict(os.environ, {"ALPHALENS_BROKER_ENVIRONMENT": "live"}, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker"
            ) as mock_get_default_broker,
        ):
            with self.assertRaises(BrokerCapabilityError) as ctx:
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)
        message = str(ctx.exception)
        self.assertIn("ADR 0017", message)
        self.assertIn("ALPHALENS_BROKER_MAX_OPEN", message, "the missing rail must be named")
        mock_get_default_broker.assert_not_called()

    def test_refuses_a_pre_migration_flat_layout(self) -> None:
        with TemporaryDirectory() as d:
            home = Path(d)
            root = home / ".alphalens" / "broker_orders"
            root.mkdir(parents=True)
            (root / "submissions.jsonl").write_text("{}\n")
            with mock.patch("pathlib.Path.home", return_value=home):
                with self.assertRaises(state_paths.BrokerStateLayoutError) as ctx:
                    cl.build_default_deps(
                        notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
                    )
        self.assertIn("submissions.jsonl", str(ctx.exception))

    def test_clean_sim_layout_boots_and_wires_both_kill_paths_via_the_seam(self) -> None:
        with (
            _isolated_home() as home,
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ),
            mock.patch.object(cl, "_default_oauth_provider", return_value=mock.Mock()),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        self.assertEqual(deps.kill_file, home / ".alphalens" / "broker_orders" / "sim" / "KILL")
        self.assertEqual(deps.global_kill_file, home / ".alphalens" / "broker_orders" / "KILL")


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
            # Default $ALPHALENS_BROKER_ENVIRONMENT is "sim" (ADR 0016 D1/D5) -
            # the domain file and the {job=...} label both carry the "-sim" suffix.
            written = Path(d) / "alphalens_domain_broker-manager-sim.prom"
            self.assertTrue(written.is_file())
            body = written.read_text()
            self.assertIn("alphalens_broker_manager_last_tick_timestamp_seconds", body)
            self.assertIn('job="broker-manager-sim"', body)

    def test_run_daemon_uses_default_heartbeat_signature(self) -> None:
        import inspect

        from alphalens_pipeline.brokers.automanager import control_loop as cl

        sig = inspect.signature(cl.run_daemon)
        self.assertIs(sig.parameters["heartbeat_fn"].default, cl._default_emit_heartbeat)

    def test_live_instance_emits_under_its_own_job_and_domain(self) -> None:
        """ADR 0016 D5: the job label + textfile domain track the resolved
        instance, not a fixed constant — a SIM and a LIVE daemon on the same
        host must never write to the same file."""
        import os
        from tempfile import TemporaryDirectory

        from alphalens_pipeline.brokers.automanager import control_loop as cl

        with (
            TemporaryDirectory() as d,
            mock.patch.dict(
                os.environ, {"ALPHALENS_TEXTFILE_DIR": d, "ALPHALENS_BROKER_ENVIRONMENT": "live"}
            ),
        ):
            cl._default_emit_heartbeat()
            written = Path(d) / "alphalens_domain_broker-manager-live.prom"
            self.assertTrue(written.is_file())
            body = written.read_text()
            self.assertIn("alphalens_broker_manager_last_tick_timestamp_seconds", body)
            self.assertIn('job="broker-manager-live"', body)


def _rich_standalone_stop_journal() -> list[dict[str, Any]]:
    """A synthetic journal exercising every compactable line kind, with a
    redundant older entry per key that compaction must fold away."""
    uic_a, uic_b = 111, 222
    return [
        # planned — crid-A0 appears twice; the higher-gen resize wins.
        {
            "kind": "planned",
            "client_request_id": "crid-A0",
            "uic": uic_a,
            "side": "SELL",
            "stop_price": 10.0,
            "take_profit": 20.0,
            "tier_index": 0,
            "gen": 0,
        },
        {
            "kind": "planned",
            "client_request_id": "crid-A0",
            "uic": uic_a,
            "side": "SELL",
            "stop_price": 11.0,
            "take_profit": 21.0,
            "tier_index": 0,
            "gen": 1,
        },
        {
            "kind": "planned",
            "client_request_id": "crid-A1",
            "uic": uic_a,
            "side": "SELL",
            "stop_price": 10.0,
            "take_profit": 19.0,
            "tier_index": 1,
            "gen": 0,
        },
        {
            "kind": "planned",
            "client_request_id": "crid-B0",
            "uic": uic_b,
            "side": "SELL",
            "stop_price": 5.0,
            "take_profit": 8.0,
            "tier_index": 0,
            "gen": 0,
        },
        # gen kind — never read by the four verified folds; dropped by compaction.
        {"kind": "gen", "uic": uic_a, "gen": 1, "qty": 7.0},
        # oco_unsupported — duplicated on one uic; folds to a single set member.
        {"kind": "oco_unsupported", "uic": uic_a},
        {"kind": "oco_unsupported", "uic": uic_a},
        # oco_placed — the newer ts is the one that governs the TTL fold.
        {"kind": "oco_placed", "uic": uic_a, "ts": 100.0},
        {"kind": "oco_placed", "uic": uic_a, "ts": 250.0},
        # amend_failed — newer ts governs.
        {"kind": "amend_failed", "uic": uic_b, "ts": 100.0},
        {"kind": "amend_failed", "uic": uic_b, "ts": 300.0},
        # oco_too_far — transient TooFarFromMarket TTL marker; newer ts governs.
        {"kind": "oco_too_far", "uic": uic_b, "ts": 150.0},
        {"kind": "oco_too_far", "uic": uic_b, "ts": 280.0},
        # amend_seq — the max per uic is what _read_persisted_amend_seq returns.
        {"kind": "amend_seq", "uic": uic_a, "seq": 0},
        {"kind": "amend_seq", "uic": uic_a, "seq": 1},
        {"kind": "amend_seq", "uic": uic_a, "seq": 2},
        {"kind": "amend_seq", "uic": uic_b, "seq": 0},
        # malformed — a planned line with no client_request_id; every fold skips it.
        {"kind": "planned", "uic": uic_a, "stop_price": 1.0},
    ]


def _tranche_plan_line(
    uic: int, *, pick_key: str | None = None, target_price: float = 20.0
) -> dict[str, Any]:
    """A well-formed ``tranche_plan`` journal line carrying a one-tranche ladder."""
    return cl._build_tranche_plan_line(
        uic=uic,
        tp_tranches=(
            TpTranchePlan(
                tranche_index=0,
                target_price=target_price,
                tranche_pct=0.5,
                r_multiple=1.0,
                tag="tp1",
            ),
        ),
        reference_qty=10.0,
        stop_price=9.0,
        pick_key=pick_key,
    )


def _tranche_journal() -> list[dict[str, Any]]:
    """Tranche-ladder lines across three uics: a live ladder with a fired tranche
    (333), a re-picked ladder whose old fired tag must stay reset (444), and a
    fully retracted ladder (555)."""
    return [
        _tranche_plan_line(333, pick_key="OLN:2026-08-14"),
        {"kind": "tranche_fired", "uic": 333, "tag": "tp1"},
        _tranche_plan_line(444, pick_key="ABC:2026-08-10", target_price=15.0),
        {"kind": "tranche_fired", "uic": 444, "tag": "tp1"},
        _tranche_plan_line(444, pick_key="ABC:2026-08-17", target_price=18.0),
        {"kind": "tranche_fired", "uic": 444, "tag": "tp2"},
        _tranche_plan_line(555, pick_key="XYZ:2026-08-12"),
        {"kind": "tranche_plan_retracted", "uic": 555, "pick_key": "XYZ:2026-08-12"},
    ]


def _planned_fold_data(
    fold: dict[int, PlannedExit],
) -> dict[int, tuple[Any, ...]]:
    """PlannedExit fields excluding the ``next_gen`` / ``next_amend_seq`` closures
    (which compare by identity, so a fresh fold is never ``==`` a prior one)."""
    return {
        uic: (
            planned.uic,
            planned.entry_crid,
            planned.side,
            planned.stop_price,
            planned.tp_price,
            planned.conflicting,
            planned.n_plans,
        )
        for uic, planned in fold.items()
    }


def _position_row(uic: int | None, qty: float) -> Any:
    """A duck-typed broker position ledger row; ``uic=None`` builds a row whose
    ``broker_instrument_id`` cannot be parsed back to a uic."""
    instr = type(
        "I", (), {"broker_instrument_id": None if uic is None else str(uic), "currency": "USD"}
    )()
    return type("Pos", (), {"instrument": instr, "quantity": qty})()


class TestNetOpenPositionUics(unittest.TestCase):
    """EOD-netting risk-unit counting: a LIVE Saxo intraday round-trip is two
    ledger rows (+q / -q) netting to zero until the nightly netting — MAX_OPEN
    must count distinct net-nonzero uics, never raw rows."""

    def test_round_trip_rows_net_to_no_open_uic(self) -> None:
        uics, unresolvable = cl._net_open_position_uics(
            [_position_row(42, 8.0), _position_row(42, -8.0)]
        )
        self.assertEqual(uics, frozenset())
        self.assertEqual(unresolvable, 0)

    def test_partially_closed_long_is_one_open_uic(self) -> None:
        uics, unresolvable = cl._net_open_position_uics(
            [_position_row(42, 8.0), _position_row(42, -3.0)]
        )
        self.assertEqual(uics, frozenset({42}))
        self.assertEqual(unresolvable, 0)

    def test_two_net_nonzero_uics_are_two_open_uics(self) -> None:
        # A net short is a risk unit too — only net-ZERO drops out.
        uics, unresolvable = cl._net_open_position_uics(
            [_position_row(42, 8.0), _position_row(43, -5.0)]
        )
        self.assertEqual(uics, frozenset({42, 43}))
        self.assertEqual(unresolvable, 0)

    def test_unresolvable_uic_rows_count_one_each(self) -> None:
        # Fail-conservative: a row we cannot attribute to a uic still occupies
        # a slot — never undercount risk units.
        uics, unresolvable = cl._net_open_position_uics(
            [_position_row(None, 8.0), _position_row(None, 3.0)]
        )
        self.assertEqual(uics, frozenset())
        self.assertEqual(unresolvable, 2)

    def test_empty_book_is_zero(self) -> None:
        self.assertEqual(cl._net_open_position_uics([]), (frozenset(), 0))


class TestCompactStandaloneStopJournalLines(unittest.TestCase):
    """Issue #895: the pure compaction returns the minimal fold-equivalent set."""

    def test_folds_are_identical_on_original_vs_compacted(self) -> None:
        original = _rich_standalone_stop_journal()
        compacted = cl._compact_standalone_stop_journal_lines(original)

        self.assertEqual(
            _planned_fold_data(cl._fold_planned_exits(original)),
            _planned_fold_data(cl._fold_planned_exits(compacted)),
        )
        self.assertEqual(
            cl._fold_oco_unsupported(original),
            cl._fold_oco_unsupported(compacted),
        )
        for kind in ("oco_placed", "amend_failed", "oco_too_far"):
            for now in (300.0, 1000.0):
                self.assertEqual(
                    cl._fold_ttl_markers(original, kind, now, 120.0),
                    cl._fold_ttl_markers(compacted, kind, now, 120.0),
                    msg=f"{kind} @ now={now}",
                )

    def test_compacted_set_is_minimal_one_line_per_key(self) -> None:
        compacted = cl._compact_standalone_stop_journal_lines(_rich_standalone_stop_journal())
        kinds = [line["kind"] for line in compacted]
        # 3 planned (crid-A0 newest, crid-A1, crid-B0), 1 oco_unsupported,
        # 1 oco_placed, 1 amend_failed, 1 oco_too_far, 2 amend_seq (one per uic).
        # No gen/malformed.
        self.assertEqual(kinds.count("planned"), 3)
        self.assertEqual(kinds.count("oco_unsupported"), 1)
        self.assertEqual(kinds.count("oco_placed"), 1)
        self.assertEqual(kinds.count("amend_failed"), 1)
        self.assertEqual(kinds.count("oco_too_far"), 1)
        self.assertEqual(kinds.count("amend_seq"), 2)
        self.assertNotIn("gen", kinds)
        self.assertEqual(len(compacted), 9)

    def test_newest_planned_per_crid_survives(self) -> None:
        compacted = cl._compact_standalone_stop_journal_lines(_rich_standalone_stop_journal())
        a0 = [
            line
            for line in compacted
            if line.get("kind") == "planned" and line.get("client_request_id") == "crid-A0"
        ]
        self.assertEqual(len(a0), 1)
        self.assertEqual(a0[0]["gen"], 1)
        self.assertAlmostEqual(a0[0]["stop_price"], 11.0)

    def test_empty_input_is_empty_output(self) -> None:
        self.assertEqual(cl._compact_standalone_stop_journal_lines([]), [])

    def test_keeps_newest_stop_placed_and_amend_ok_per_uic(self) -> None:
        # The compactor drops unknown kinds, so the outcome records MUST be listed
        # in its newest-per-uic retention or a startup compaction silently loses
        # the latency observability the records exist for.
        lines = [
            {"kind": "stop_placed", "uic": 111, "qty": 46.0, "ts": 100.0},
            {"kind": "stop_placed", "uic": 111, "qty": 20.0, "ts": 250.0},
            {"kind": "stop_placed", "uic": 222, "qty": 7.0, "ts": 50.0},
            {"kind": "amend_ok", "uic": 111, "qty": 6.0, "ts": 90.0},
            {"kind": "amend_ok", "uic": 111, "qty": 8.0, "ts": 260.0},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(lines)
        stop_placed = [line for line in compacted if line["kind"] == "stop_placed"]
        amend_ok = [line for line in compacted if line["kind"] == "amend_ok"]
        self.assertEqual(
            sorted((line["uic"], line["ts"], line["qty"]) for line in stop_placed),
            [(111, 250.0, 20.0), (222, 50.0, 7.0)],
            "the newest stop_placed per uic survives; older ones are dropped",
        )
        self.assertEqual(
            [(line["uic"], line["ts"], line["qty"]) for line in amend_ok],
            [(111, 260.0, 8.0)],
            "the newest amend_ok per uic survives; the older one is dropped",
        )

    def test_tranche_plan_and_fired_survive_compaction(self) -> None:
        # Pre-fix the compactor dropped tranche_plan / tranche_fired entirely, so
        # every daemon restart erased the live-exit TP ladders and re-armed
        # already-fired tranches for all open positions.
        original = [
            _tranche_plan_line(333, pick_key="OLN:2026-08-14"),
            {"kind": "tranche_fired", "uic": 333, "tag": "tp1"},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(original)
        self.assertIn(333, cl.fold_tranche_plans(original))  # guard: non-vacuous
        self.assertEqual(cl.fold_tranche_plans(original), cl.fold_tranche_plans(compacted))
        self.assertEqual({333: frozenset({"tp1"})}, cl._fold_fired_since_latest_plan(compacted))

    def test_retracted_ladder_compacts_to_no_tranche_lines(self) -> None:
        original = [
            _tranche_plan_line(555, pick_key="XYZ:2026-08-12"),
            {"kind": "tranche_plan_retracted", "uic": 555, "pick_key": "XYZ:2026-08-12"},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(original)
        self.assertEqual(cl.fold_tranche_plans(compacted), {})
        self.assertEqual(cl._fold_fired_since_latest_plan(compacted), {})
        self.assertEqual(
            [line for line in compacted if str(line.get("kind", "")).startswith("tranche")],
            [],
            "a fully retracted uic keeps no tranche lines",
        )

    def test_repick_preserves_fired_reset_semantics(self) -> None:
        original = [
            _tranche_plan_line(444, pick_key="ABC:2026-08-10", target_price=15.0),
            {"kind": "tranche_fired", "uic": 444, "tag": "tp1"},
            _tranche_plan_line(444, pick_key="ABC:2026-08-17", target_price=18.0),
            {"kind": "tranche_fired", "uic": 444, "tag": "tp2"},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(original)
        self.assertEqual(cl.fold_tranche_plans(original), cl.fold_tranche_plans(compacted))
        self.assertEqual({444: frozenset({"tp2"})}, cl._fold_fired_since_latest_plan(original))
        self.assertEqual(
            cl._fold_fired_since_latest_plan(original),
            cl._fold_fired_since_latest_plan(compacted),
        )

    def test_idempotent_reappend_keeps_prior_fired_tags(self) -> None:
        # The crash-recovery re-drive re-appends the SAME pick's plan every tick;
        # a fired tag recorded BEFORE such a re-append must survive compaction.
        # The kept governing plan precedes the kept fired lines in the compacted
        # output, so the fold's same-identity no-reset path is preserved.
        original = [
            _tranche_plan_line(333, pick_key="OLN:2026-08-14"),
            {"kind": "tranche_fired", "uic": 333, "tag": "tp1"},
            _tranche_plan_line(333, pick_key="OLN:2026-08-14"),
        ]
        compacted = cl._compact_standalone_stop_journal_lines(original)
        self.assertEqual({333: frozenset({"tp1"})}, cl._fold_fired_since_latest_plan(original))
        self.assertEqual(
            cl._fold_fired_since_latest_plan(original),
            cl._fold_fired_since_latest_plan(compacted),
        )
        self.assertEqual(cl.fold_tranche_plans(original), cl.fold_tranche_plans(compacted))

    def test_compaction_is_idempotent_on_mixed_journal(self) -> None:
        mixed = _rich_standalone_stop_journal() + _tranche_journal()
        once = cl._compact_standalone_stop_journal_lines(mixed)
        self.assertEqual(once, cl._compact_standalone_stop_journal_lines(once))

    def test_mixed_journal_folds_identical_and_existing_kinds_unchanged(self) -> None:
        mixed = _rich_standalone_stop_journal() + _tranche_journal()
        compacted = cl._compact_standalone_stop_journal_lines(mixed)
        self.assertEqual(cl.fold_tranche_plans(mixed), cl.fold_tranche_plans(compacted))
        self.assertEqual(
            cl._fold_fired_since_latest_plan(mixed),
            cl._fold_fired_since_latest_plan(compacted),
        )
        non_tranche = [
            line for line in compacted if not str(line.get("kind", "")).startswith("tranche")
        ]
        self.assertEqual(
            non_tranche,
            cl._compact_standalone_stop_journal_lines(_rich_standalone_stop_journal()),
            "tranche retention must not disturb the existing kept-kinds election",
        )


class TestCompactStandaloneStopJournalFile(unittest.TestCase):
    """Issue #895: the startup rewrite is atomic, a no-op on absent/empty files,
    and preserves the newest-per-key semantics the folds and amend-seq reader see."""

    def test_absent_file_is_noop(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._compact_standalone_stop_journal()
            self.assertFalse(journal.exists())

    def test_empty_file_is_noop(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            journal.write_text("", encoding="utf-8")
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                cl._compact_standalone_stop_journal()
            self.assertTrue(journal.exists())
            self.assertEqual(journal.read_text(encoding="utf-8"), "")

    def test_rewrite_shrinks_file_and_preserves_folds_and_amend_seq(self) -> None:
        import json

        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            journal.write_text(
                "".join(
                    json.dumps(line, sort_keys=True) + "\n"
                    for line in _rich_standalone_stop_journal()
                ),
                encoding="utf-8",
            )
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                before_lines = list(cl._iter_standalone_stop_journal())
                planned_before = _planned_fold_data(cl._fold_planned_exits(before_lines))
                oco_before = cl._fold_oco_unsupported(before_lines)
                seq_a_before = cl._read_persisted_amend_seq(111)
                seq_b_before = cl._read_persisted_amend_seq(222)

                cl._compact_standalone_stop_journal()

                after_lines = list(cl._iter_standalone_stop_journal())
                self.assertLess(len(after_lines), len(before_lines))
                self.assertEqual(
                    planned_before,
                    _planned_fold_data(cl._fold_planned_exits(after_lines)),
                )
                self.assertEqual(oco_before, cl._fold_oco_unsupported(after_lines))
                self.assertEqual(seq_a_before, cl._read_persisted_amend_seq(111))
                self.assertEqual(seq_b_before, cl._read_persisted_amend_seq(222))
                for kind in ("oco_placed", "amend_failed", "oco_too_far"):
                    self.assertEqual(
                        cl._fold_ttl_markers(before_lines, kind, 300.0, 120.0),
                        cl._fold_ttl_markers(after_lines, kind, 300.0, 120.0),
                    )
            # No temp artifacts left behind in the journal dir.
            leftovers = [p.name for p in Path(d).iterdir() if p.name != journal.name]
            self.assertEqual(leftovers, [])


_AMEND_ON = {"ALPHALENS_BROKER_AMEND_ENABLED": "1"}


def _m1_lag_pview(*, owned: float = 3.0, stop_amount: float = 3.0, tp_amount: float = 5.0):
    """A CLEAN unfilled resting OCO pair whose stop is already <= owned but whose TP
    read still lags Q9's symmetric propagation (tp_amount > owned). With amend on the
    OCO-downsize arm is skipped (stop already <= owned) and _reconcile_long emits the
    M1 ``oco-lag-hold`` NoOp — the exact persistently-stuck-lag shape (issue #5)."""
    pos = _pos(owned)
    legs = (
        _oco_leg("oco-stop-1", "StopIfTraded", stop_amount),
        _oco_leg("oco-tp-1", "Limit", tp_amount),
    )
    return ProtectionView(
        long_positions={_UIC: pos},
        all_positions={_UIC: pos},
        sell_legs_by_uic={_UIC: legs},
        planned_by_uic={
            _UIC: PlannedExit(
                uic=_UIC,
                entry_crid="crid-0",
                side="SELL",
                stop_price=216.48,
                tp_price=306.72,
                conflicting=False,
                n_plans=1,
            )
        },
        oco_unsupported=frozenset(),
        amend_recently_failed=frozenset(),
    )


class TestPersistentOcoLagMonitor(unittest.TestCase):
    """Issue #5: the M1 guard NoOp'ing a clean over-covered OCO pair is SAFE for a
    tick or two, but a genuinely-stalled Q9 propagation must not stay invisible. The
    protection driver counts a uic's consecutive ``oco-lag-hold`` holds on a daemon-
    lifetime dict and pages ONCE (throttled) at ``_OCO_LAG_ALERT_TICKS``; any other
    action on that uic resets the count. Dark when amend is off (the M1 guard is
    ``_amend_enabled()``-gated, so it never emits and the counter never advances)."""

    def _lag_action(self) -> list:
        return [NoOp(uic=_UIC, reason=cl._OCO_LAG_HOLD_REASON)]

    def test_persistent_lag_pages_once_at_threshold(self) -> None:
        # N == _OCO_LAG_ALERT_TICKS consecutive holds -> exactly ONE throttled alert.
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=[],
        )
        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append)
        deps = cl.LoopDeps(
            **{**deps.__dict__, "alert_throttled": lambda m, r: throttle.emit(m, reason=r)}
        )
        report = cl.TickReport()
        for _ in range(cl._OCO_LAG_ALERT_TICKS + 3):  # keep holding past the threshold
            cl._track_oco_lag(deps, self._lag_action(), report)
        self.assertEqual(deps.oco_lag_counts[_UIC], cl._OCO_LAG_ALERT_TICKS + 3)
        persistent = [a for a in alerts if "propagation lag held" in a]
        self.assertEqual(
            len(persistent), 1, f"expected exactly one persistent-lag page, got {alerts}"
        )
        self.assertIn(str(_UIC), persistent[0])

    def test_no_alert_below_threshold(self) -> None:
        # _OCO_LAG_ALERT_TICKS - 1 holds stay silent (safe transient lag).
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=[],
        )
        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append)
        deps = cl.LoopDeps(
            **{**deps.__dict__, "alert_throttled": lambda m, r: throttle.emit(m, reason=r)}
        )
        report = cl.TickReport()
        for _ in range(cl._OCO_LAG_ALERT_TICKS - 1):
            cl._track_oco_lag(deps, self._lag_action(), report)
        self.assertEqual(deps.oco_lag_counts[_UIC], cl._OCO_LAG_ALERT_TICKS - 1)
        self.assertEqual([a for a in alerts if "propagation lag held" in a], [])

    def test_non_lag_action_resets_the_counter(self) -> None:
        # A real action for the uic (a PlaceStop) means the lag cleared -> reset.
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=[],
        )
        alerts: list[str] = []
        throttle = cl._AlertThrottle(alerts.append)
        deps = cl.LoopDeps(
            **{**deps.__dict__, "alert_throttled": lambda m, r: throttle.emit(m, reason=r)}
        )
        report = cl.TickReport()
        for _ in range(cl._OCO_LAG_ALERT_TICKS - 1):  # climb to just below the threshold
            cl._track_oco_lag(deps, self._lag_action(), report)
        # A non-lag action for the uic resets it (drops the key).
        place = PlaceStop(_UIC, "SELL", 3.0, 216.48, "crid-0-stop-0")
        cl._track_oco_lag(deps, [place], report)
        self.assertNotIn(_UIC, deps.oco_lag_counts)
        # The next hold restarts the climb from 1 -> still below threshold, silent.
        cl._track_oco_lag(deps, self._lag_action(), report)
        self.assertEqual(deps.oco_lag_counts[_UIC], 1)
        self.assertEqual([a for a in alerts if "propagation lag held" in a], [])

    def test_bare_noop_does_not_advance_counter(self) -> None:
        # A healthy-covered bare NoOp (reason "") is NOT a lag hold -> no counting.
        deps = _deps(
            _StubBroker(),
            kill_file=Path("/nonexistent/KILL"),
            verdicts=[],
            place_calls=[],
            alerts=[],
        )
        report = cl.TickReport()
        for _ in range(cl._OCO_LAG_ALERT_TICKS + 2):
            cl._track_oco_lag(deps, [NoOp()], report)
        self.assertEqual(deps.oco_lag_counts, {})

    def test_run_once_end_to_end_pages_once_on_persistent_lag(self) -> None:
        # Full path: run_once -> _run_protection_pass -> reconcile_protection emits
        # the M1 NoOp -> _track_oco_lag counts + pages once through the real throttle.
        with TemporaryDirectory() as d:
            alerts: list[str] = []
            throttle = cl._AlertThrottle(alerts.append)
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
                build_protection_view=lambda broker, records: _m1_lag_pview(),
                alert_throttled=lambda m, r: throttle.emit(m, reason=r),
            )
            with mock.patch.dict(os.environ, _AMEND_ON):
                for _ in range(cl._OCO_LAG_ALERT_TICKS + 2):
                    cl.run_once(deps)
        self.assertEqual(deps.oco_lag_counts[_UIC], cl._OCO_LAG_ALERT_TICKS + 2)
        persistent = [a for a in alerts if "propagation lag held" in a]
        self.assertEqual(len(persistent), 1, f"expected one persistent-lag page, got {alerts}")

    def test_dark_when_amend_off_no_hold_no_counter(self) -> None:
        # Amend OFF: the M1 guard never fires (place-residual-first arm runs instead),
        # so run_once never emits an oco-lag-hold and the counter stays empty.
        with TemporaryDirectory() as d:
            deps = _deps(
                _StubBroker(),
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
                build_protection_view=lambda broker, records: _m1_lag_pview(),
            )
            env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_AMEND_ENABLED"}
            with mock.patch.dict(os.environ, env, clear=True):
                for _ in range(cl._OCO_LAG_ALERT_TICKS + 2):
                    cl.run_once(deps)
        self.assertEqual(deps.oco_lag_counts, {})


# --------------------------------------------------------------------------
# Task 4 — streaming daemon wiring (the never-worse-than-poll core).
# run_daemon's absolute-deadline interruptible wait + the per-tick stream
# hook. The single guarantee: a total streaming failure degrades to EXACTLY
# today's poll-only cadence, never worse.
# --------------------------------------------------------------------------


class _Clock:
    """A mutable monotonic clock the scripted fake Event advances."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _ScriptedWait:
    """A fake ``threading.Event`` whose ``.wait(timeout)`` consumes one scripted
    step, advancing a shared clock, and returns whether it was an EARLY wake.

    Steps are ``("timeout",)`` (advance the clock by the full ``timeout`` and
    return False, i.e. the backstop fired) or ``("wake", dt)`` (advance the
    clock by ``dt`` < timeout and return True, i.e. the stream woke us early).
    Records every ``timeout`` passed to ``.wait`` and every ``set()`` call."""

    def __init__(self, clock: _Clock, steps: list[tuple[Any, ...]]) -> None:
        self._clock = clock
        self._steps = list(steps)
        self.timeouts: list[float] = []
        self.set_calls = 0

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.timeouts.append(timeout)
        kind, *rest = self._steps.pop(0)
        if kind == "timeout":
            self._clock.now += timeout
            return False
        self._clock.now += rest[0]
        return True

    def set(self) -> None:
        self.set_calls += 1

    def clear(self) -> None:
        pass


def _stop_after(n: int) -> Callable[[], bool]:
    """is_running that returns True for the first ``n`` checks, then False."""
    state = {"i": 0}

    def _running() -> bool:
        state["i"] += 1
        return state["i"] <= n

    return _running


def _daemon_deps(run_once_at: list[float], clock: _Clock) -> cl.LoopDeps:
    """LoopDeps whose run_once (via build_protection_view) records the clock
    reading at each tick — the observable proxy for 'a protection pass ran'."""

    def _record_view(broker: Any, records: Any) -> ProtectionView:
        run_once_at.append(clock.now)
        return _empty_pview()

    with TemporaryDirectory() as d:
        base = _deps(
            _StubBroker(),
            kill_file=Path(d) / "KILL",
            verdicts=[],
            place_calls=[],
            alerts=[],
            build_protection_view=_record_view,
        )
    return base


class TestRunDaemonNeverNaked(unittest.TestCase):
    """The never-naked-under-streaming-failure property: whatever the stream
    does (nothing / crashes / storms), the protection pass runs at least every
    poll_seconds of wall clock — exactly today's poll-only floor."""

    def test_wake_event_never_set_runs_exactly_one_run_once_per_poll_seconds(self) -> None:
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        event = _ScriptedWait(clock, [("timeout",), ("timeout",), ("timeout",)])
        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(3),
            heartbeat_fn=lambda _kill: None,
            wake_event=event,  # type: ignore[arg-type]
            monotonic=clock,
        )
        # One pass per poll_seconds — the backstop timeout is always the full 45s.
        self.assertEqual(len(run_at), 3)
        self.assertEqual(event.timeouts, [45.0, 45.0, 45.0])
        self.assertEqual(run_at, [1000.0, 1045.0, 1090.0])

    def test_streaming_thread_crash_does_not_stall_loop(self) -> None:
        # A raising/hung stub stream thread never fires the Event -> identical to
        # the never-set case: the backstop cadence is unchanged.
        crashed = {"raised": False}

        def _crash() -> None:
            crashed["raised"] = True
            raise RuntimeError("stream reader died")

        thread = __import__("threading").Thread(target=_crash, daemon=True)
        thread.start()
        thread.join()
        self.assertTrue(crashed["raised"])

        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        event = _ScriptedWait(clock, [("timeout",), ("timeout",)])
        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(2),
            heartbeat_fn=lambda _kill: None,
            wake_event=event,  # type: ignore[arg-type]
            monotonic=clock,
        )
        self.assertEqual(run_at, [1000.0, 1045.0])

    def test_wake_event_none_path_byte_identical_to_sleep_fn(self) -> None:
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        slept: list[float] = []

        # monotonic must NOT be consulted on the disabled path (byte-identical to
        # today's blocking sleep_fn); a sentinel that raises proves it.
        def _forbidden_monotonic() -> float:
            raise AssertionError("monotonic must not be read on the wake_event=None path")

        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(2),
            heartbeat_fn=lambda _kill: None,
            sleep_fn=slept.append,
            wake_event=None,
            monotonic=_forbidden_monotonic,
        )
        self.assertEqual(slept, [45.0, 45.0])
        self.assertEqual(len(run_at), 2)


class TestRunDaemonAbsoluteDeadline(unittest.TestCase):
    """Absolute-deadline scheduling: early wakes give EXTRA passes but never push
    the guaranteed backstop past the fixed wall-clock grid (adversary-2 fix)."""

    def test_stale_early_wakes_do_not_push_backstop_past_grid(self) -> None:
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        # Two stale early wakes (advance 10s, 5s) then a timeout: the timeout pass
        # must land at exactly start+45, NOT start+45+10+5.
        event = _ScriptedWait(
            clock, [("wake", 10.0), ("wake", 5.0), ("timeout",), ("timeout",), ("timeout",)]
        )
        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(5),
            heartbeat_fn=lambda _kill: None,
            wake_event=event,  # type: ignore[arg-type]
            monotonic=clock,
        )
        # run_once at t=0 (1000), 10, 15 (early wakes), then the backstop timeout
        # pass at exactly t=45 (1045) — the grid was NOT pushed out.
        self.assertEqual(run_at[:4], [1000.0, 1010.0, 1015.0, 1045.0])
        # The waits shrank to the remaining time to the absolute deadline.
        self.assertEqual(event.timeouts[:3], [45.0, 35.0, 30.0])
        # After the timeout pass, the next grid point is start+90.
        self.assertEqual(run_at[4], 1090.0)

    def test_early_wake_runs_extra_run_once_then_backstop_still_fires(self) -> None:
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        event = _ScriptedWait(clock, [("wake", 3.0), ("timeout",)])
        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(2),
            heartbeat_fn=lambda _kill: None,
            wake_event=event,  # type: ignore[arg-type]
            monotonic=clock,
        )
        # The early wake produced an EXTRA pass at t=3, then the backstop pass
        # still fired within poll_seconds of the last full-cadence pass (t=45).
        self.assertEqual(run_at, [1000.0, 1003.0])
        self.assertEqual(event.timeouts, [45.0, 42.0])


class TestRunDaemonGuards(unittest.TestCase):
    def test_wake_event_with_nonfinite_or_zero_poll_seconds_raises(self) -> None:
        clock = _Clock()
        deps = _daemon_deps([], clock)
        event = _ScriptedWait(clock, [])
        for bad in (0.0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                cl.run_daemon(
                    deps,
                    once=False,
                    poll_seconds=bad,
                    is_running=_stop_after(1),
                    heartbeat_fn=lambda _kill: None,
                    wake_event=event,  # type: ignore[arg-type]
                    monotonic=clock,
                )

    def test_nonfinite_poll_seconds_allowed_when_streaming_off(self) -> None:
        # The disabled path is unchanged: no interruptible wait, no guard — a
        # finite poll_seconds is the operator's responsibility as it is today.
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)
        cl.run_daemon(
            deps,
            once=True,
            poll_seconds=45.0,
            is_running=_stop_after(1),
            heartbeat_fn=lambda _kill: None,
            sleep_fn=lambda s: None,
            wake_event=None,
        )
        self.assertEqual(len(run_at), 1)


class TestWokenPassRecomputesState(unittest.TestCase):
    def test_woken_tick_rereads_kill_and_records_and_fresh_report(self) -> None:
        # A woken pass calls the SAME run_once, which re-reads kill + records and
        # builds a fresh view — no partial path. Prove it by toggling the KILL
        # file between the leading wake and the backstop pass and asserting the
        # protection executor sees the flipped kill flag on the later pass.
        clock = _Clock()
        with TemporaryDirectory() as d:
            kill_file = Path(d) / "KILL"
            reads: list[bool] = []
            kills_seen: list[bool] = []

            def _read_records() -> list[dict[str, Any]]:
                reads.append(True)
                # Create the KILL file after the first read so the SECOND tick
                # sees it (proves records + kill are recomputed per pass).
                if len(reads) == 1:
                    kill_file.touch()
                return []

            def _exec(action: Any, kill: bool, report: Any) -> None:
                kills_seen.append(kill)

            broker = _ProtBroker(positions=[_pos(3.0)], sells=[], by_uic={_UIC: _pos(3.0)})
            base = _deps(
                broker,
                kill_file=kill_file,
                verdicts=[],
                place_calls=[],
                alerts=[],
                build_protection_view=lambda b, r: ProtectionView(
                    long_positions={_UIC: _pos(3.0)},
                    all_positions={_UIC: _pos(3.0)},
                    sell_legs_by_uic={},
                    planned_by_uic={
                        _UIC: PlannedExit(
                            uic=_UIC,
                            entry_crid="crid-0",
                            side="SELL",
                            stop_price=216.48,
                            tp_price=None,
                            conflicting=False,
                            n_plans=1,
                            next_gen=lambda qty: 0,
                            next_amend_seq=lambda: 0,
                        )
                    },
                    oco_unsupported=frozenset(),
                ),
                execute_protection=_exec,
            )
            deps = cl.LoopDeps(**{**base.__dict__, "read_records": _read_records})
            event = _ScriptedWait(clock, [("wake", 1.0), ("timeout",)])
            cl.run_daemon(
                deps,
                once=False,
                poll_seconds=45.0,
                is_running=_stop_after(2),
                heartbeat_fn=lambda _kill: None,
                wake_event=event,  # type: ignore[arg-type]
                monotonic=clock,
            )
        # The protection executor saw kill=False on the first pass and kill=True
        # on the woken/backstop pass — kill IS recomputed per run_once, not cached
        # from the first tick. (read_records is called once per protection pass
        # plus once more by the tick-1 placement drain, so >= 2 reads occurred.)
        self.assertGreaterEqual(len(reads), 2)
        self.assertEqual(kills_seen, [False, True])


class TestStreamRestBudget(unittest.TestCase):
    def test_reconnect_storm_does_not_starve_protective_place(self) -> None:
        # A reconnect/reset storm = the stream thread spamming spurious early
        # wakes. The protective place_standalone_stop still executes on every
        # pass and the backstop still fires within poll_seconds — never starved.
        clock = _Clock()
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            broker = _ProtBroker(positions=[_pos(3.0)], sells=[], by_uic={_UIC: _pos(3.0)})
            throttle = _throttle_to([])
            base = _deps(
                broker,
                kill_file=Path(d) / "KILL",
                verdicts=[],
                place_calls=[],
                alerts=[],
                build_protection_view=cl.build_protection_view,
                execute_protection=cl._make_protection_executor(broker, throttle),
            )
            # Storm: many tiny early wakes, then one backstop timeout.
            event = _ScriptedWait(
                clock,
                [("wake", 0.01)] * 5 + [("timeout",)],
            )
            with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
                _seed_planned(journal)
                cl.run_daemon(
                    base,
                    once=False,
                    poll_seconds=45.0,
                    is_running=_stop_after(6),
                    heartbeat_fn=lambda _kill: None,
                    wake_event=event,  # type: ignore[arg-type]
                    monotonic=clock,
                )
        # The protective stop was placed (deficit -> place_standalone_stop ran);
        # the storm never starved it, and the backstop timeout still fired.
        self.assertGreaterEqual(len(broker.placed), 1)
        self.assertEqual(broker.placed[0][:2], (_UIC, "SELL"))
        self.assertIn(45.0, event.timeouts)


class _FakeStreamTrigger:
    """The ONE shared stub of the full ``StreamTrigger`` surface the rewritten
    tick consumes (rearm design memo §6 INC-3 / §7.11): read-only liveness
    (``is_running`` / ``is_streaming`` / ``frames_delivered`` / ``trips_total``
    / ``consecutive_failures`` / ``seconds_since_last_message``) plus the
    main-thread acts (``push_token`` / ``reset_liveness`` / ``rearm``). One
    class instead of five ad-hoc ``_Trig`` stubs, so the next surface addition
    breaks one class, not five."""

    def __init__(
        self,
        *,
        running: bool = True,
        streaming: bool = True,
        frames: int = 0,
        trips: int = 0,
        failures: int = 0,
        silence: float | None = None,
        rearm_result: bool = True,
    ) -> None:
        self.running = running
        self.is_streaming = streaming
        self.frames_delivered = frames
        self.trips_total = trips
        self.consecutive_failures = failures
        self.silence = silence
        self.pushed: list[str] = []
        self.rearm_calls = 0
        self.reset_liveness_calls = 0
        self.rearm_result = rearm_result
        # Optional scripted rearm behaviour (memo §7.12: the stub must FOLLOW a
        # scripted state transition driven by re-arm calls, never a bare False).
        self.on_rearm: Callable[[_FakeStreamTrigger], bool] | None = None

    def is_running(self) -> bool:
        return self.running

    def push_token(self, tok: str) -> None:
        self.pushed.append(tok)

    def seconds_since_last_message(self) -> float | None:
        return self.silence

    def reset_liveness(self) -> None:
        self.reset_liveness_calls += 1
        self.silence = None

    def rearm(self) -> bool:
        self.rearm_calls += 1
        if self.on_rearm is not None:
            return self.on_rearm(self)
        return self.rearm_result

    # ----- scenario helpers (what the real client does on these transitions) --

    def go_dark_with_trip(self) -> None:
        """Breaker trips: reader exits, streaming shuts, trips_total bumps."""
        self.is_streaming = False
        self.running = False
        self.trips_total += 1

    def come_up(self, *, silence: float = 5.0) -> None:
        """A trial connects AND delivers a frame (the delivery proof)."""
        self.running = True
        self.is_streaming = True
        self.frames_delivered += 1
        self.silence = silence


def _stream_tick_harness(
    trig: _FakeStreamTrigger,
    clock: _Clock,
    *,
    stale_s: float = 45.0,
    bearer: Any = "B",
    gauges: list[dict[str, float]] | None = None,
    in_session: Callable[[], bool] | None = None,
) -> tuple[Callable[[], None], list[str], list[tuple[str, str]]]:
    """Build the rewritten tick with recording sinks: ``pages`` records the
    guaranteed-send edge sink, ``throttled`` the interval-throttled one."""
    pages: list[str] = []
    throttled: list[tuple[str, str]] = []
    get_bearer = bearer if callable(bearer) else (lambda: bearer)
    tick = cl._make_stream_tick(
        trig,
        get_bearer=get_bearer,
        alert=pages.append,
        alert_throttled=lambda m, r: throttled.append((m, r)) or True,
        stale_s=stale_s,
        emit_gauge=(gauges.append if gauges is not None else (lambda values: None)),
        monotonic=clock,
        in_session=in_session if in_session is not None else (lambda: True),
    )
    return tick, pages, throttled


# Every gauge base name the tick must land in ONE atomic domain emit (rearm
# design memo §4.6) — an omitted key deletes its Prometheus series.
_ALL_STREAM_GAUGE_NAMES = (
    "alphalens_broker_manager_stream_reader_up",
    "alphalens_broker_manager_stream_breaker_open",
    "alphalens_broker_manager_stream_last_message_age_seconds",
    "alphalens_broker_manager_stream_consecutive_failures",
    "alphalens_broker_manager_stream_trips_total",
    "alphalens_broker_manager_stream_in_session",
)


class TestStreamStaleAlert(unittest.TestCase):
    def test_stream_silence_beyond_stale_s_raises_throttled_alert_on_main_thread(self) -> None:
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []
        # Silence beyond stale_s on a HEALTHY reader (dark-but-connected) ->
        # throttled 'stream-dead' alert fires; the episode machine stays closed.
        trig = _FakeStreamTrigger(frames=1, silence=90.0)
        tick, pages, throttled = _stream_tick_harness(trig, clock, bearer="BEARER-1", gauges=gauges)
        tick()
        self.assertEqual(trig.pushed, ["BEARER-1"])
        self.assertEqual(len(throttled), 1)
        self.assertEqual(throttled[0][1], "stream-dead")
        self.assertEqual(pages, [])
        self.assertEqual(gauges[-1][cl._STREAM_LAST_MESSAGE_METRIC_NAME], 90.0)

        # Fresh stream (silence <= stale_s) -> no alert, still pushes + gauges.
        gauges.clear()
        trig2 = _FakeStreamTrigger(frames=1, silence=5.0)
        tick2, pages2, throttled2 = _stream_tick_harness(
            trig2, clock, bearer="BEARER-2", gauges=gauges
        )
        tick2()
        self.assertEqual(trig2.pushed, ["BEARER-2"])
        self.assertEqual(throttled2, [])
        self.assertEqual(pages2, [])
        self.assertEqual(gauges[-1][cl._STREAM_LAST_MESSAGE_METRIC_NAME], 5.0)

    def test_no_message_yet_does_not_alert_but_still_gauges(self) -> None:
        # Rearm design memo §4.6: the gauges are written on EVERY tick and the
        # age key is never omitted — before any message it reports seconds since
        # the tick closure was built (an omitted key deletes its series).
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []
        trig = _FakeStreamTrigger(silence=None)
        tick, pages, throttled = _stream_tick_harness(trig, clock, gauges=gauges)
        clock.now += 30.0
        tick()
        self.assertEqual(pages, [])
        self.assertEqual(throttled, [])
        self.assertEqual(len(gauges), 1)
        self.assertEqual(gauges[0][cl._STREAM_LAST_MESSAGE_METRIC_NAME], 30.0)

    def test_bearer_read_failure_never_crashes_the_tick(self) -> None:
        # A token flock / chain-loss error while reading the bearer must degrade
        # to poll-only silently (never-worse-than-poll), never crash the loop.
        clock = _Clock(start=0.0)

        def _raise_bearer() -> str:
            raise RuntimeError("chain lost")

        trig = _FakeStreamTrigger(silence=None)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock, bearer=_raise_bearer)
        tick()  # must not raise
        self.assertEqual(trig.pushed, [])

    def test_default_stale_threshold_le_poll_seconds(self) -> None:
        # The stale alert must not lag a full poll cycle behind protection.
        self.assertLessEqual(cl._DEFAULT_STREAM_STALE_S, 45.0)

    def test_stream_stale_s_reads_env_with_finite_positive_guard(self) -> None:
        with mock.patch.dict(os.environ, {"ALPHALENS_BROKER_STREAM_STALE_S": "30"}, clear=False):
            self.assertEqual(cl._stream_stale_s(), 30.0)
        for bad in ("0", "-5", "nan", "inf", "notafloat", ""):
            with mock.patch.dict(os.environ, {"ALPHALENS_BROKER_STREAM_STALE_S": bad}, clear=False):
                self.assertEqual(cl._stream_stale_s(), cl._DEFAULT_STREAM_STALE_S)
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_STREAM_STALE_S"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cl._stream_stale_s(), cl._DEFAULT_STREAM_STALE_S)


class TestBlockingOnTickDoesNotExtendGap(unittest.TestCase):
    """PR #900 adversarial-review MEDIUM: a slow on_tick (a hung Telegram POST in
    the stale/breaker alert) must be ABSORBED into the poll interval, never added
    on top of a fresh poll. The absolute deadline is anchored to the moment the
    protection pass completed (before on_tick), so on_tick's blocking duration only
    shrinks the remaining wait — it can never push the backstop past poll_seconds
    (which would be worse than poll-only)."""

    def test_blocking_on_tick_absorbed_into_poll_not_added_on_top(self) -> None:
        clock = _Clock()
        run_at: list[float] = []
        deps = _daemon_deps(run_at, clock)

        def _blocking_tick() -> None:
            clock.now += 30.0  # a ~30s synchronous alert POST on the main thread

        event = _ScriptedWait(clock, [("timeout",), ("timeout",), ("timeout",)])
        cl.run_daemon(
            deps,
            once=False,
            poll_seconds=45.0,
            is_running=_stop_after(3),
            heartbeat_fn=lambda _kill: None,
            on_tick=_blocking_tick,
            wake_event=event,  # type: ignore[arg-type]
            monotonic=clock,
        )
        # Passes stay on the 45s grid; the 30s block shrinks each wait to 15s
        # instead of scheduling a fresh 45s AFTER the block (which would put the
        # 3rd pass at 1120, a 75s gap).
        # Exact 45s grid (each gap 45s) — NOT [1000, 1045, 1120] (a 75s 3rd gap).
        self.assertEqual(run_at, [1000.0, 1045.0, 1090.0])
        self.assertEqual(event.timeouts, [15.0, 15.0, 15.0])


class TestStreamBreakerAlert(unittest.TestCase):
    """REWRITTEN for the breaker re-arm design (memo §6 INC-3). The old
    ``test_breaker_tripped_pages_even_with_no_message`` pinned the THROTTLED
    repeating 'stream-breaker' page — the mechanism behind the 2026-08-22
    metronome (29 identical Telegram messages over 14 h). A trip now opens an
    EPISODE: exactly one guaranteed-send OPEN page (even when the stream never
    delivered a message and ``seconds_since_last_message`` stays None), silence
    while open, one delivery-confirmed CLOSE page."""

    def test_breaker_trip_with_no_message_pages_once_via_guaranteed_sink(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, silence=None)
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        tick()
        self.assertEqual(len(pages), 1)  # OPEN — paged even with no message ever
        for _ in range(10):
            clock.now += 45.0
            tick()
        self.assertEqual(len(pages), 1)  # a LEVEL never pages on an interval
        self.assertEqual(throttled, [])  # the 'stream-breaker' metronome is gone

    def test_live_stream_does_not_page_breaker(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(frames=1, silence=5.0)
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        tick()
        self.assertEqual(pages, [])
        self.assertEqual(throttled, [])


class TestStreamEpisodeLatch(unittest.TestCase):
    """Rearm design memo §4.5: Telegram gets EDGES, once per EPISODE — one OPEN
    page on the down edge, zero while open, one delivery-confirmed CLOSE page.
    Prometheus owns every level."""

    def test_sustained_breaker_open_pages_once_across_sixty_ticks(self) -> None:
        # The 2026-08-22 metronome regression, red first: 60 ticks x 45s (~45
        # min) of an unbroken dark stream used to page every ~30.5 min.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, rearm_result=False)
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        for _ in range(60):
            tick()
            clock.now += 45.0
        self.assertEqual(len(pages), 1)
        self.assertEqual(throttled, [])

    def test_recovery_pages_only_after_a_delivered_frame_and_the_dwell(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)
        trig.on_rearm = lambda t: (t.come_up(), True)[1]
        tick, pages, throttled = _stream_tick_harness(trig, clock)

        tick()  # t=0: episode OPEN
        self.assertEqual(len(pages), 1)
        clock.now = 60.0
        tick()  # first trial: rearm + deliver
        self.assertEqual(trig.rearm_calls, 1)
        # up is sampled BEFORE the rearm, so the delivery registers next tick.
        clock.now = 105.0
        tick()  # up_since = 105
        # No CLOSE page until the 300s dwell has been held CONTINUOUSLY.
        for t in (150.0, 195.0, 240.0, 285.0, 330.0, 375.0, 404.9):
            clock.now = t
            tick()
        self.assertEqual(len(pages), 1, pages)
        clock.now = 405.0  # 105 + _STREAM_HEALTHY_DWELL_S — dwell boundary
        tick()
        self.assertEqual(len(pages), 2)
        self.assertIn("RECOVERED", pages[-1])
        self.assertEqual(throttled, [])

    def test_incident_replay_two_messages_instead_of_twenty_nine(self) -> None:
        # The REAL 2026-08-22 shape, replayed per memo §4.7: 6 consecutive
        # failures trip the breaker at 08:46:21 (t=0 here), the vendor recovers
        # ~08:48 — trial 1 at +60s still hits the outage and re-trips (no page),
        # trial 2 at +180s delivers, the 300s dwell holds, CLOSE follows. The
        # journal's 31-page metronome becomes exactly TWO Telegram lines and
        # ~3 min dark instead of 14 hours.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, failures=6, trips=0)
        outage_over = False

        def _saxo(t: _FakeStreamTrigger) -> bool:
            if outage_over:
                t.come_up()  # a heartbeat lands on the re-armed context
            else:
                t.go_dark_with_trip()  # still 504/500 — the single trial re-trips
            return True

        trig.on_rearm = _saxo
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        trig.go_dark_with_trip()  # 08:46:21 — the breaker trips

        tick()  # t=0: OPEN — the ONE down-edge page
        self.assertEqual(len(pages), 1)
        self.assertIn("DOWN", pages[0])
        rearm_times: list[float] = []
        for t in (45.0, 60.0, 105.0, 150.0, 180.0):
            clock.now = t
            before = trig.rearm_calls
            tick()
            if trig.rearm_calls > before:
                rearm_times.append(t)
            if t == 60.0:
                outage_over = True  # Saxo recovered ~08:48 — after trial 1, before trial 2
        self.assertEqual(rearm_times, [60.0, 180.0])  # the ladder schedule: 60 then +120
        # Delivery registered at trial 2 (t=180); dwell from the next tick.
        clock.now = 225.0
        tick()  # up_since = 225
        for t in (270.0, 315.0, 360.0, 405.0, 450.0, 495.0, 524.9):
            clock.now = t
            tick()
        self.assertEqual(len(pages), 1)  # still just the OPEN page while dwelling
        clock.now = 525.0  # 225 + 300s dwell -> CLOSE (~08:55 wall clock)
        tick()
        self.assertEqual(len(pages), 2)
        self.assertIn("RECOVERED", pages[-1])
        # No interval-throttled traffic at any point: the metronome is gone.
        self.assertEqual(throttled, [])
        # Exactly one failed trial + one delivering trial, on the ladder grid.
        self.assertEqual(trig.rearm_calls, 2)

    def test_a_trial_that_dies_before_delivering_pages_nothing(self) -> None:
        # Memo §7.1: the winning proposal's worst defect — a trial that connects
        # but never delivers must NOT page "re-armed / streaming again".
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)

        def _connect_without_delivery(t: _FakeStreamTrigger) -> bool:
            t.running = True
            t.is_streaming = True  # rearm sets the flag True BEFORE any evidence
            return True

        trig.on_rearm = _connect_without_delivery
        tick, pages, _throttled = _stream_tick_harness(trig, clock)
        tick()  # OPEN
        clock.now = 60.0
        tick()  # trial connects, no frame
        clock.now = 105.0
        tick()  # up stays False (frames == delivered_at_rearm)
        trig.go_dark_with_trip()  # the trial dies
        clock.now = 150.0
        tick()
        self.assertEqual(len(pages), 1)  # only the original OPEN page

    def test_a_reader_thread_that_dies_without_tripping_is_rearmed(self) -> None:
        # Memo §7.6: _thread_main swallows a crash, leaving is_streaming True —
        # the episode must key on reader_dark (which includes not is_running()).
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=True, silence=30.0)
        tick, pages, _throttled = _stream_tick_harness(trig, clock)
        tick()
        self.assertEqual(len(pages), 1)  # crashed reader opens an episode
        clock.now = 60.0
        tick()
        self.assertEqual(trig.rearm_calls, 1)  # and is re-armed at the floor

    def test_edge_pages_use_the_guaranteed_send_sink_not_the_interval_throttle(self) -> None:
        # Memo §4.5: routing a genuine edge through an interval throttle is what
        # produced the metronome — edges go to deps.alert (guaranteed-send).
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)
        trig.on_rearm = lambda t: (t.come_up(), True)[1]
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        for t in (0.0, 60.0, 105.0, 150.0, 405.0, 450.0):
            clock.now = t
            tick()
        self.assertEqual(len(pages), 2)  # OPEN + CLOSE on the guaranteed sink
        self.assertEqual(throttled, [])  # and NOTHING on the throttled sink

    def test_bearer_is_pushed_before_every_branch_including_the_dark_one(self) -> None:
        # Memo §2.2/§4.4 step 1: the old tick returned from the breaker branch
        # BEFORE push_token, freezing the reader's bearer at the trip instant.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock, bearer="FRESH")
        tick()
        self.assertEqual(trig.pushed, ["FRESH"])

    def test_bearer_read_failure_still_never_crashes_the_tick(self) -> None:
        clock = _Clock(start=0.0)

        def _raise_bearer() -> str:
            raise RuntimeError("chain lost")

        trig = _FakeStreamTrigger(running=False, streaming=False)
        tick, pages, _throttled = _stream_tick_harness(trig, clock, bearer=_raise_bearer)
        tick()  # must not raise, and the episode machine still runs
        self.assertEqual(trig.pushed, [])
        self.assertEqual(len(pages), 1)  # the OPEN page still fires

    def test_a_raising_rearm_never_escapes_the_tick(self) -> None:
        # Memo §7.5: run_daemon calls on_tick() bare and the CLI catches only
        # BrokerError — a RuntimeError from Thread.start() must never unwind
        # the protective daemon.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)

        def _boom(_t: _FakeStreamTrigger) -> bool:
            raise RuntimeError("thread exhaustion")

        trig.on_rearm = _boom
        tick, _pages, _throttled = _stream_tick_harness(trig, clock)
        tick()
        clock.now = 60.0
        tick()  # the raising rearm is swallowed
        self.assertEqual(trig.rearm_calls, 1)
        clock.now = 180.0
        tick()  # and the ladder keeps retrying on later ticks
        self.assertEqual(trig.rearm_calls, 2)

    def test_stream_dead_alert_is_silent_while_an_episode_is_open(self) -> None:
        # Memo §7.2: after 14h dark seconds_since_last_message() is ~50400 —
        # without the episode gate the metronome relocates onto 'stream-dead'.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, silence=50400.0)
        trig.on_rearm = lambda t: False  # keep the episode open, liveness reset
        tick, _pages, throttled = _stream_tick_harness(trig, clock)
        for _ in range(60):
            tick()
            clock.now += 45.0
        self.assertEqual([r for _, r in throttled], [])

    def test_rearm_resets_liveness_so_an_hours_old_epoch_never_pages_stream_dead(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, silence=50400.0)
        trig.on_rearm = lambda t: (t.come_up(), True)[1]
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        for t in (0.0, 60.0, 105.0, 405.0, 450.0):
            clock.now = t
            tick()
        self.assertGreaterEqual(trig.reset_liveness_calls, 1)
        self.assertNotIn("stream-dead", [r for _, r in throttled])
        self.assertEqual(len(pages), 2)  # a clean OPEN -> CLOSE episode


def _drive_flap_episodes(
    *,
    kills: int,
    ticks: int = 60,
) -> tuple[list[str], list[tuple[str, str]], _FakeStreamTrigger]:
    """Drive repeated trip -> rearm -> deliver -> dwell -> close episodes: the
    trigger starts healthy, an initial trip opens episode 1, and after each
    delivery-confirmed CLOSE the stream is killed again (up to ``kills`` times).
    45s tick grid, all inside one _STREAM_FLAP_WINDOW_S."""
    clock = _Clock(start=0.0)
    trig = _FakeStreamTrigger(frames=1, silence=5.0)
    trig.on_rearm = lambda t: (t.come_up(), True)[1]
    tick, pages, throttled = _stream_tick_harness(trig, clock)
    trig.go_dark_with_trip()
    kills_done = 0
    for _ in range(ticks):
        before = len(pages)
        tick()
        just_closed = len(pages) > before and "RECOVERED" in pages[-1]
        if just_closed and kills_done < kills:
            trig.go_dark_with_trip()
            kills_done += 1
        clock.now += 45.0
    return pages, throttled, trig


class TestStreamRearmLadder(unittest.TestCase):
    """Rearm design memo §3 Q3 / §4.5 / §5: the cooldown ladder
    60 -> 120 -> 240 -> 480 -> 900 -> 900 s lives in the tick closure, resets to
    the floor ONLY after a delivery-confirmed 300s dwell, and flapping escalates
    ONE CRITICAL after 3 trips inside the rolling hour."""

    def test_first_tick_after_a_trip_arms_the_cooldown_and_does_not_rearm(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, rearm_result=False)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock)
        tick()  # t=0: OPEN, cooldown armed — NO trial on the opening tick
        self.assertEqual(trig.rearm_calls, 0)
        clock.now = 45.0
        tick()  # still inside the 60s floor
        self.assertEqual(trig.rearm_calls, 0)
        clock.now = 59.9
        tick()
        self.assertEqual(trig.rearm_calls, 0)
        clock.now = 60.0  # the floor boundary
        tick()
        self.assertEqual(trig.rearm_calls, 1)

    def test_cooldown_doubles_from_sixty_and_caps_at_nine_hundred(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, rearm_result=False)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock)
        tick()  # t=0: OPEN
        # Trial times on the ladder: gaps 60, 120, 240, 480, 900, 900 (cap).
        expected_trial_times = [60.0, 180.0, 420.0, 900.0, 1800.0, 2700.0, 3600.0]
        for i, trial_at in enumerate(expected_trial_times, start=1):
            clock.now = trial_at - 0.1
            tick()
            self.assertEqual(trig.rearm_calls, i - 1, f"early trial before t={trial_at}")
            clock.now = trial_at
            tick()
            self.assertEqual(trig.rearm_calls, i, f"missing trial at t={trial_at}")

    def test_at_most_one_rearm_attempt_per_tick(self) -> None:
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False, rearm_result=False)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock)
        tick()  # OPEN
        clock.now = 5000.0  # far past SEVERAL ladder rungs in one gap
        tick()
        self.assertEqual(trig.rearm_calls, 1)

    def test_a_flap_inside_the_dwell_climbs_the_ladder_instead_of_resetting_it(self) -> None:
        # Memo §3 Q3 damping layer 2: a deliver-once-then-die flapper must climb
        # 60 -> 120 -> 240, not loop at the floor off its sub-dwell delivery.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(running=False, streaming=False)
        trig.on_rearm = lambda t: (t.come_up(), True)[1]
        tick, pages, _throttled = _stream_tick_harness(trig, clock)
        tick()  # t=0: OPEN
        clock.now = 60.0
        tick()  # trial 1 delivers
        clock.now = 105.0
        tick()  # up, dwell running (105s < 300s held)
        trig.go_dark_with_trip()  # dies INSIDE the dwell
        clock.now = 150.0
        tick()  # dark again; next rung is 180 (climbed), not 150+60
        self.assertEqual(trig.rearm_calls, 1)
        clock.now = 180.0
        tick()  # trial 2 at the SECOND rung
        self.assertEqual(trig.rearm_calls, 2)
        trig.go_dark_with_trip()
        clock.now = 419.9
        tick()
        self.assertEqual(trig.rearm_calls, 2)
        clock.now = 420.0  # third rung: 180 + 240 — the ladder kept climbing
        tick()
        self.assertEqual(trig.rearm_calls, 3)
        # The sub-dwell deliveries never paged a recovery.
        self.assertEqual(len(pages), 1)

    def test_three_trips_in_the_flap_window_escalate_once_then_suppress_open_pages(self) -> None:
        pages, _throttled, trig = _drive_flap_episodes(kills=2)
        criticals = [p for p in pages if "CRITICAL" in p]
        opens = [p for p in pages if "DOWN" in p and "CRITICAL" not in p]
        closes = [p for p in pages if "RECOVERED" in p]
        self.assertEqual(len(criticals), 1)  # escalate ONCE at the 3rd trip
        self.assertEqual(len(opens), 2)  # episode 3's OPEN page is suppressed
        self.assertEqual(len(closes), 3)  # every episode still closes loudly
        self.assertEqual(trig.trips_total, 3)

    def test_the_close_page_is_never_suppressed_by_the_flap_latch(self) -> None:
        # Memo §4.5: an unpaired page is worse than none — the operator must
        # always see an episode end, latch or no latch.
        pages, _throttled, _trig = _drive_flap_episodes(kills=2)
        critical_at = next(i for i, p in enumerate(pages) if "CRITICAL" in p)
        self.assertTrue(
            any("RECOVERED" in p for p in pages[critical_at + 1 :]),
            f"no CLOSE page after the flap CRITICAL: {pages}",
        )

    def test_a_scripted_trip_rearm_fail_cycle_pages_at_most_twice_per_hour(self) -> None:
        # Memo §7.12 replacement: the stub FOLLOWS a scripted trip/rearm/fail
        # cycle across a simulated hour — total messages stay <= 2 (one OPEN +
        # at most one flap CRITICAL), vs the metronome's 2/hour FOREVER plus
        # false recovery pages a flag-keyed edge would add.
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(frames=1, silence=5.0)

        def _trial_connects_without_delivery(t: _FakeStreamTrigger) -> bool:
            t.running = True
            t.is_streaming = True
            return True

        trig.on_rearm = _trial_connects_without_delivery
        tick, pages, throttled = _stream_tick_harness(trig, clock)
        trig.go_dark_with_trip()
        rearms_seen = 0
        trial_live_ticks = 0
        for _ in range(80):  # 80 x 45s = one hour
            tick()
            if trig.rearm_calls > rearms_seen:
                rearms_seen = trig.rearm_calls
                trial_live_ticks = 1
            elif trial_live_ticks:
                trial_live_ticks = 0
                trig.go_dark_with_trip()  # every trial dies one tick later
            clock.now += 45.0
        self.assertLessEqual(len(pages), 2, pages)
        self.assertEqual(throttled, [])


class TestStreamGauges(unittest.TestCase):
    """Rearm design memo §4.6 / §6 INC-4: six gauges, written on EVERY tick
    (including while dark — the pre-rearm tick returned from the breaker branch
    before emitting, freezing the textfile so no age rule could ever fire), in
    ONE atomic domain emit. Prometheus owns every level; Telegram gets edges."""

    def test_gauges_are_written_on_every_tick_including_while_dark(self) -> None:
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []
        trig = _FakeStreamTrigger(
            running=False, streaming=False, trips=1, failures=6, rearm_result=False
        )
        tick, _pages, _throttled = _stream_tick_harness(
            trig, clock, gauges=gauges, in_session=lambda: False
        )
        for _ in range(5):
            tick()
            clock.now += 45.0
        self.assertEqual(len(gauges), 5)
        for emitted in gauges:
            self.assertEqual(sorted(emitted), sorted(_ALL_STREAM_GAUGE_NAMES))
        # The dark reader reads down; the levels carry the streak composition
        # the 2026-08-22 journal could not recover; the session gauge follows
        # the injected predicate.
        last = gauges[-1]
        self.assertEqual(last["alphalens_broker_manager_stream_reader_up"], 0.0)
        self.assertEqual(last["alphalens_broker_manager_stream_breaker_open"], 1.0)
        self.assertEqual(last["alphalens_broker_manager_stream_consecutive_failures"], 6.0)
        self.assertEqual(last["alphalens_broker_manager_stream_trips_total"], 1.0)
        self.assertEqual(last["alphalens_broker_manager_stream_in_session"], 0.0)

    def test_the_age_gauge_key_is_never_omitted_when_no_message_has_arrived(self) -> None:
        # An omitted key deletes its Prometheus series (emit_domain_metrics
        # overwrites the whole domain file) — epoch None must still write the
        # age key, reporting seconds since the closure was built.
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []
        trig = _FakeStreamTrigger(silence=None)
        tick, _pages, _throttled = _stream_tick_harness(trig, clock, gauges=gauges)
        clock.now += 120.0
        tick()
        self.assertIn("alphalens_broker_manager_stream_last_message_age_seconds", gauges[0])
        self.assertEqual(
            gauges[0]["alphalens_broker_manager_stream_last_message_age_seconds"], 120.0
        )

    def test_breaker_open_stays_one_across_a_whole_episode_and_does_not_flicker_per_trial(
        self,
    ) -> None:
        # Memo §7.3: a per-trial gauge resets to 0 on every ladder rung, so no
        # Prometheus `for:` longer than one rung could ever fire. The gauge is
        # EPISODE-scoped: 1 from OPEN until the delivery-confirmed CLOSE.
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []
        trig = _FakeStreamTrigger(running=False, streaming=False)
        trig.on_rearm = lambda t: (t.come_up(), True)[1]
        tick, pages, _throttled = _stream_tick_harness(trig, clock, gauges=gauges)
        tick()  # t=0: OPEN
        clock.now = 60.0
        tick()  # trial connects AND delivers — dwell starts, episode still open
        clock.now = 105.0
        tick()  # up, mid-dwell: the trial must NOT flicker the gauge to 0
        breaker_values = [g["alphalens_broker_manager_stream_breaker_open"] for g in gauges]
        self.assertEqual(breaker_values, [1.0, 1.0, 1.0])
        # The dwell clock starts at the first tick that OBSERVES delivery-backed
        # health (t=105), not at the trial itself.
        clock.now = 105.0 + 300.0
        tick()  # dwell held -> delivery-confirmed CLOSE
        self.assertEqual(gauges[-1]["alphalens_broker_manager_stream_breaker_open"], 0.0)
        self.assertEqual(len(pages), 2)  # one OPEN, one CLOSE

    def test_all_six_stream_gauges_land_in_one_atomic_domain_emit(self) -> None:
        # Drive the REAL default emitter (_emit_stream_gauge) into a temp
        # textfile dir: all six series must land in the ONE stream domain file
        # (a second emit to the domain would clobber the first, and a separate
        # file would prove a second call happened).
        clock = _Clock(start=0.0)
        trig = _FakeStreamTrigger(frames=1, silence=5.0)
        with (
            TemporaryDirectory() as d,
            mock.patch.dict(os.environ, {"ALPHALENS_TEXTFILE_DIR": d}, clear=False),
        ):
            tick = cl._make_stream_tick(
                trig,
                get_bearer=lambda: "B",
                alert=lambda _m: None,
                alert_throttled=lambda _m, _r: True,
                stale_s=45.0,
                monotonic=clock,
                in_session=lambda: True,
            )
            tick()
            proms = sorted(Path(d).glob("*.prom"))
            self.assertEqual(len(proms), 1, [p.name for p in proms])
            self.assertEqual(proms[0].name, "alphalens_domain_broker-manager-sim-stream.prom")
            text = proms[0].read_text()
        for name in _ALL_STREAM_GAUGE_NAMES:
            self.assertIn(f'{name}{{job="broker-manager-sim"}}', text)

    def test_a_raising_session_predicate_reports_in_session_and_keeps_gauges(self) -> None:
        # The calendar predicate fails OPEN by contract (a calendar bug must
        # never silence observability): a raise reports in_session=1 and the
        # other five gauges still land.
        clock = _Clock(start=0.0)
        gauges: list[dict[str, float]] = []

        def _raising_session() -> bool:
            raise RuntimeError("calendar exploded")

        trig = _FakeStreamTrigger(frames=1, silence=5.0)
        tick, _pages, _throttled = _stream_tick_harness(
            trig, clock, gauges=gauges, in_session=_raising_session
        )
        tick()
        self.assertEqual(len(gauges), 1)
        self.assertEqual(sorted(gauges[0]), sorted(_ALL_STREAM_GAUGE_NAMES))
        self.assertEqual(gauges[0]["alphalens_broker_manager_stream_in_session"], 1.0)


class TestStreamingSubscriberIsolation(unittest.TestCase):
    """zen HIGH: the streaming subscriber must NOT share the process-wide
    SaxoClient singleton's requests.Session (requests.Session is not thread-safe;
    concurrent subscription-REST on the stream thread + get_positions on the main
    thread could corrupt the pool and skip a protection pass). It gets its OWN
    session while sharing the thread-safe OAuth provider for token consistency."""

    def _provider(self):
        class _StubProvider:
            def get_access_token(self) -> str:
                return "TOK"

            def invalidate(self) -> None:
                pass

        return _StubProvider()

    def test_subscriber_has_own_session_and_shared_provider(self) -> None:
        from alphalens_pipeline.brokers.saxo.client import SaxoClient

        provider = self._provider()
        sub = cl._build_streaming_subscriber(provider)
        self.assertIsInstance(sub, SaxoClient)
        self.assertIs(sub._token_provider, provider)
        # A fresh session per construction -> never the shared singleton's session.
        other = cl._build_streaming_subscriber(provider)
        self.assertIsNot(sub._session, other._session)


class TestStreamGaugeDoesNotClobberHeartbeat(unittest.TestCase):
    """Live-validation regression (streaming ON): the stream-liveness gauge and the
    per-tick heartbeat gauge must NOT share one textfile. emit_domain_metrics
    atomically OVERWRITES alphalens_domain_<job>.prom with only the metrics it is
    handed, and _emit_stream_gauge runs after heartbeat_fn every tick — so writing
    both under job 'broker-manager' made the stream gauge clobber the heartbeat
    every tick, erasing alphalens_broker_manager_last_tick_timestamp_seconds and
    breaking the AlphalensBrokerManagerHeartbeatStale liveness alert. They now write
    to DISTINCT domain files; node_exporter merges all .prom files in the dir, so
    both series stay scraped (the metric names + {job} labels are unchanged)."""

    def test_both_gauges_survive_in_the_textfile_dir(self) -> None:
        with (
            TemporaryDirectory() as d,
            mock.patch.dict(os.environ, {"ALPHALENS_TEXTFILE_DIR": d}, clear=False),
        ):
            cl._default_emit_heartbeat()  # per-tick heartbeat gauge
            # stream gauges (run AFTER heartbeat every tick; multi-key since INC-3)
            cl._emit_stream_gauge({cl._STREAM_LAST_MESSAGE_METRIC_NAME: 5.0})
            proms = sorted(Path(d).glob("*.prom"))
            blob = "\n".join(p.read_text() for p in proms)
        # Both series present (neither atomic overwrite clobbered the other) ...
        self.assertIn("alphalens_broker_manager_last_tick_timestamp_seconds", blob)
        self.assertIn("alphalens_broker_manager_stream_last_message_age_seconds", blob)
        # ... because they live in two separate domain files.
        self.assertEqual(len(proms), 2, [p.name for p in proms])


class TestStreamingEnabledGate(unittest.TestCase):
    def test_streaming_enabled_reads_env_flag(self) -> None:
        with mock.patch.dict(os.environ, {"ALPHALENS_BROKER_STREAMING_ENABLED": "1"}, clear=False):
            self.assertTrue(cl._streaming_enabled())
        for off in ("0", "", "true", "yes"):
            with mock.patch.dict(
                os.environ, {"ALPHALENS_BROKER_STREAMING_ENABLED": off}, clear=False
            ):
                self.assertFalse(cl._streaming_enabled())
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_BROKER_STREAMING_ENABLED"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(cl._streaming_enabled())


class TestKillActiveMetric(unittest.TestCase):
    """The KILL-active gauge (level 0/1) MUST co-emit with the per-tick heartbeat in
    ONE emit_domain_metrics(job, {...}) call — a second call to the same domain would
    atomically OVERWRITE (clobber) the heartbeat gauge, and vice-versa. Value is 1
    when the KILL file is present, 0 when absent, so Prometheus can alert on an
    active emergency stop (previously journald-only, invisible to monitoring)."""

    def _emitted(self, kill: bool) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _spy(job: str, metrics: Any) -> None:
            captured["job"] = job
            captured["metrics"] = dict(metrics)

        with mock.patch("alphalens_pipeline.observability.textfile.emit_domain_metrics", _spy):
            cl._default_emit_heartbeat(kill)
        return captured

    def test_kill_present_co_emits_kill_active_1_and_heartbeat(self) -> None:
        cap = self._emitted(True)
        # Default $ALPHALENS_BROKER_ENVIRONMENT is "sim" (ADR 0016 D1/D5).
        self.assertEqual(cap["job"], "broker-manager-sim")
        self.assertEqual(cap["metrics"][cl.kill_active_metric(cap["job"])], 1)
        self.assertIn(
            cl.heartbeat_metric(cap["job"]), cap["metrics"], "heartbeat co-emitted, not clobbered"
        )

    def test_kill_absent_co_emits_kill_active_0_and_heartbeat(self) -> None:
        cap = self._emitted(False)
        self.assertEqual(cap["metrics"][cl.kill_active_metric(cap["job"])], 0)
        self.assertIn(
            cl.heartbeat_metric(cap["job"]), cap["metrics"], "heartbeat co-emitted, not clobbered"
        )


class TestKillEdgeAlert(unittest.TestCase):
    """Edge-triggered KILL alert (observability only — placement/protection gating is
    unchanged). deps.alert fires ONCE on each False->True / True->False transition,
    never every tick while KILL is held. An empty state holder treats the missing
    previous value as False, so a startup WITH KILL alerts once and a clean startup is
    silent."""

    def _deps_for(self, kill_file: Path, alerts: list[str]) -> cl.LoopDeps:
        return _deps(
            _StubBroker(),
            kill_file=kill_file,
            verdicts=[],
            place_calls=[],
            alerts=alerts,
        )

    def test_activation_alerts_once_then_silent_while_held(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"
            alerts: list[str] = []
            deps = self._deps_for(kill, alerts)
            cl.run_once(deps)  # no KILL yet -> silent
            self.assertEqual([a for a in alerts if "KILL" in a], [])
            kill.write_text("halt")
            cl.run_once(deps)  # False->True edge -> one alert
            cl.run_once(deps)  # still True -> NO second alert
            active = [a for a in alerts if "KILL active" in a]
            self.assertEqual(len(active), 1, f"activation alerts exactly once, got {alerts}")

    def test_clear_alerts_resumed_once(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"
            kill.write_text("halt")
            alerts: list[str] = []
            deps = self._deps_for(kill, alerts)
            cl.run_once(deps)  # startup WITH KILL -> active alert
            kill.unlink()
            cl.run_once(deps)  # True->False edge -> cleared alert
            cleared = [a for a in alerts if "KILL cleared" in a]
            self.assertEqual(len(cleared), 1, f"clear alerts exactly once, got {alerts}")

    def test_startup_with_kill_present_alerts_once(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"
            kill.write_text("halt")
            alerts: list[str] = []
            deps = self._deps_for(kill, alerts)
            cl.run_once(deps)
            active = [a for a in alerts if "KILL active" in a]
            self.assertEqual(
                len(active), 1, f"a startup with KILL already present alerts once, got {alerts}"
            )

    def test_clean_startup_no_kill_does_not_alert(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"  # never created
            alerts: list[str] = []
            deps = self._deps_for(kill, alerts)
            cl.run_once(deps)
            self.assertEqual(
                [a for a in alerts if "KILL" in a], [], "a clean no-KILL startup is silent"
            )


# --- Day-1 gap gate (execution-quality placement discipline) -----------------


@contextlib.contextmanager
def _frozen_now(fixed: dt.datetime):
    """Freeze ``control_loop.dt.datetime.now()`` to ``fixed`` for the block.

    ``control_loop`` does ``import datetime as dt`` at module scope, so ``dt``
    IS the real stdlib ``datetime`` module — patching its ``datetime`` class
    attribute for the duration of a ``with`` block is the same precedented
    pattern ``_isolated_home`` above uses for ``pathlib.Path.home`` (scoped,
    restored after)."""

    class _Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return fixed if tz is None else fixed.astimezone(tz)

    with mock.patch.object(cl.dt, "datetime", _Frozen):
        yield


def _day1_spec(limit_price: float = 100.0) -> TradeSpec:
    return TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=limit_price, alloc_pct=100.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(),
        suggested_size_pct=2.0,
    )


class _RaisingProbe:
    """A ``day1_gap_price_probe`` stub that raises if ever called — proves the
    gate-off / non-day1-window call sites never reach the probe (a real
    network round-trip in production)."""

    def __call__(self, ticker: str, exchange_mic: str) -> float | None:
        raise AssertionError(f"probe must not be called for {ticker}/{exchange_mic}")


class TestDay1GapGateSessionInfo(unittest.TestCase):
    """``_day1_gap_gate_session_info`` — pure calendar math, no I/O."""

    def test_monday_brief_day1_is_tuesday(self) -> None:
        day1, day1_open = cl._day1_gap_gate_session_info(dt.date(2026, 8, 10), "XNYS")
        self.assertEqual(day1, dt.date(2026, 8, 11))
        self.assertEqual(day1_open, dt.datetime(2026, 8, 11, 13, 30, tzinfo=dt.UTC))

    def test_friday_brief_day1_is_the_following_monday(self) -> None:
        day1, _day1_open = cl._day1_gap_gate_session_info(dt.date(2026, 8, 14), "XNYS")
        self.assertEqual(day1, dt.date(2026, 8, 17))

    def test_unresolvable_exchange_returns_none(self) -> None:
        with self.assertLogs("alphalens_pipeline.brokers.automanager.control_loop", "WARNING"):
            result = cl._day1_gap_gate_session_info(dt.date(2026, 8, 10), "ZZZZ")
        self.assertIsNone(result)


class TestDay1GapGateDecision(unittest.TestCase):
    """``_day1_gap_gate_decision`` — pure, total, no I/O (design memo: N=30/588
    population analysis, day-1 gap-through-E1 fills carry median -1R vs
    +0.21R baseline; later-day gaps are benign)."""

    _BRIEF = dt.date(2026, 8, 10)  # Monday
    _DAY1 = dt.date(2026, 8, 11)  # Tuesday (strictly after the Monday brief)
    _DAY1_OPEN = dt.datetime(2026, 8, 11, 13, 30, tzinfo=dt.UTC)
    _E1 = 100.0

    def test_e1_limit_none_passes_with_warning(self) -> None:
        with self.assertLogs("alphalens_pipeline.brokers.automanager.control_loop", "WARNING"):
            verdict = cl._day1_gap_gate_decision(self._DAY1_OPEN, self._BRIEF, None, 50.0, "XNYS")
        self.assertEqual(verdict, "pass")

    def test_before_day1_defers_preopen(self) -> None:
        now = dt.datetime(2026, 8, 10, 14, 0, tzinfo=dt.UTC)  # brief day itself
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, None, "XNYS")
        self.assertEqual(verdict, "defer_preopen")

    def test_day1_before_open_defers_preopen(self) -> None:
        now = self._DAY1_OPEN - dt.timedelta(minutes=1)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 200.0, "XNYS")
        self.assertEqual(verdict, "defer_preopen")

    def test_day1_within_grace_window_defers_preopen(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(seconds=cl._DAY1_GAP_GATE_OPEN_GRACE_S - 1)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 200.0, "XNYS")
        self.assertEqual(verdict, "defer_preopen")

    def test_day1_at_grace_boundary_with_no_price_defers_no_price(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(seconds=cl._DAY1_GAP_GATE_OPEN_GRACE_S)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, None, "XNYS")
        self.assertEqual(verdict, "defer_no_price")

    def test_day1_after_grace_price_below_e1_defers_below_e1(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(minutes=30)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 99.99, "XNYS")
        self.assertEqual(verdict, "defer_below_e1")

    def test_day1_after_grace_price_at_e1_passes(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(minutes=30)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, self._E1, "XNYS")
        self.assertEqual(verdict, "pass")

    def test_day1_after_grace_price_above_e1_passes(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(minutes=30)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 101.0, "XNYS")
        self.assertEqual(verdict, "pass")

    def test_day_after_day1_passes_regardless_of_price(self) -> None:
        now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)  # Wednesday, day1 + 1
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 1.0, "XNYS")
        self.assertEqual(verdict, "pass")

    def test_unresolvable_exchange_passes(self) -> None:
        now = self._DAY1_OPEN + dt.timedelta(minutes=30)
        verdict = cl._day1_gap_gate_decision(now, self._BRIEF, self._E1, 1.0, "ZZZZ")
        self.assertEqual(verdict, "pass")


class TestEvaluateDay1GapGate(unittest.TestCase):
    """``_evaluate_day1_gap_gate`` — resolves E1, calls the probe ONLY when the
    pick is within its day1 session at/after the open+grace window, then
    delegates to the pure decision helper."""

    _BRIEF = dt.date(2026, 8, 10)  # Monday
    _DAY1_OPEN = dt.datetime(2026, 8, 11, 13, 30, tzinfo=dt.UTC)

    def test_probe_not_called_before_day1(self) -> None:
        with _frozen_now(dt.datetime(2026, 8, 10, 20, 0, tzinfo=dt.UTC)):
            verdict = cl._evaluate_day1_gap_gate(
                "KO", self._BRIEF, _day1_spec(), "XNYS", _RaisingProbe()
            )
        self.assertEqual(verdict, "defer_preopen")

    def test_probe_not_called_within_open_grace(self) -> None:
        with _frozen_now(self._DAY1_OPEN + dt.timedelta(seconds=1)):
            verdict = cl._evaluate_day1_gap_gate(
                "KO", self._BRIEF, _day1_spec(), "XNYS", _RaisingProbe()
            )
        self.assertEqual(verdict, "defer_preopen")

    def test_probe_not_called_on_day_after(self) -> None:
        with _frozen_now(dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)):
            verdict = cl._evaluate_day1_gap_gate(
                "KO", self._BRIEF, _day1_spec(), "XNYS", _RaisingProbe()
            )
        self.assertEqual(verdict, "pass")

    def test_probe_called_exactly_once_within_day1_after_grace(self) -> None:
        calls: list[tuple[str, str]] = []

        def _probe(ticker: str, exchange_mic: str) -> float | None:
            calls.append((ticker, exchange_mic))
            return 99.0

        with _frozen_now(self._DAY1_OPEN + dt.timedelta(minutes=30)):
            verdict = cl._evaluate_day1_gap_gate("KO", self._BRIEF, _day1_spec(), "XNYS", _probe)
        self.assertEqual(calls, [("KO", "XNYS")])
        self.assertEqual(verdict, "defer_below_e1")

    def test_none_probe_within_day1_after_grace_defers_without_crash(self) -> None:
        with _frozen_now(self._DAY1_OPEN + dt.timedelta(minutes=30)):
            verdict = cl._evaluate_day1_gap_gate("KO", self._BRIEF, _day1_spec(), "XNYS", None)
        self.assertEqual(verdict, "defer_no_price")


class TestDay1GapGateEnabledFlag(unittest.TestCase):
    def test_unset_is_disabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(cl._day1_gap_gate_enabled())

    def test_one_is_enabled(self) -> None:
        with mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True):
            self.assertTrue(cl._day1_gap_gate_enabled())

    def test_other_value_is_disabled(self) -> None:
        with mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "true"}, clear=True):
            self.assertFalse(cl._day1_gap_gate_enabled())


class TestDay1SessionOpenExtraction(unittest.TestCase):
    """``_extract_day1_session_open`` — PriceInfoDetails.Open only; any
    missing/malformed/non-positive value is a veto (``None``), never a crash.
    The gate decides on the day-1 OPENING PRINT (stable all session), not the
    instantaneous price — an intraday dip below E1 after a healthy open is
    the normal pullback fill the ladder wants."""

    def test_open_present_wins(self) -> None:
        payload = {"PriceInfoDetails": {"Open": 7.92, "LastTraded": 8.0}}
        self.assertEqual(cl._extract_day1_session_open(payload), 7.92)

    def test_missing_details_returns_none(self) -> None:
        self.assertIsNone(cl._extract_day1_session_open({"Quote": {"Bid": 7.99}}))

    def test_missing_open_returns_none(self) -> None:
        self.assertIsNone(cl._extract_day1_session_open({"PriceInfoDetails": {"LastTraded": 8.0}}))

    def test_none_payload_returns_none(self) -> None:
        self.assertIsNone(cl._extract_day1_session_open(None))  # type: ignore[arg-type]

    def test_malformed_open_returns_none(self) -> None:
        self.assertIsNone(cl._extract_day1_session_open({"PriceInfoDetails": {"Open": "n/a"}}))

    def test_zero_or_nonfinite_open_is_a_veto(self) -> None:
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(open=bad):
                self.assertIsNone(
                    cl._extract_day1_session_open({"PriceInfoDetails": {"Open": bad}})
                )


class TestDay1GapProbeVenueFallback(unittest.TestCase):
    """The probe must PROBE US venues like placement routing does (XNYS then
    XNAS) instead of trusting the advisory ``InstrumentHint.mic``: every
    armed intent carries the hardcoded advisory "XNYS", so a NASDAQ name
    (live-verified 2026-08-11: NVAX resolves only on XNAS, uic 6820) would
    otherwise resolve to None and the gate would silently defer its entire
    day 1 — a false veto."""

    class _FakeClient:
        def __init__(self, *, token_provider: object = None) -> None:
            self.resolved: list[str] = []
            self._session = mock.Mock()

        def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
            self.resolved.append(exchange_mic)
            return 6820 if exchange_mic == "XNAS" else None

        def get_stock_infoprice(self, uic: int, **_kw) -> dict[str, Any]:
            assert uic == 6820
            return {"PriceInfoDetails": {"Open": 7.92}}

    def _probe_with(self, fake_cls: type) -> tuple[Any, Any]:
        holder: dict[str, Any] = {}

        def _ctor(*, token_provider: object = None) -> Any:
            holder["client"] = fake_cls(token_provider=token_provider)
            return holder["client"]

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_client.SaxoMarketDataClient",
                side_effect=_ctor,
            )
        )
        stack.enter_context(
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.LiveAuthConfig.from_env",
                return_value=object(),
            )
        )
        stack.enter_context(
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.LiveTokenProvider",
                return_value=object(),
            )
        )
        return cl._build_day1_gap_price_probe(), holder

    def test_hinted_xnys_falls_back_to_xnas(self) -> None:
        probe, holder = self._probe_with(self._FakeClient)
        self.assertEqual(probe("NVAX", "XNYS"), 7.92)
        self.assertEqual(holder["client"].resolved, ["XNYS", "XNAS"])
        holder["client"]._session.close.assert_called_once()

    def test_unresolvable_on_every_venue_returns_none(self) -> None:
        class _NeverResolves(self._FakeClient):
            def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
                self.resolved.append(exchange_mic)
                return None

        probe, holder = self._probe_with(_NeverResolves)
        self.assertIsNone(probe("ZZZZ", "XNYS"))
        self.assertEqual(holder["client"].resolved, ["XNYS", "XNAS", "XASE"])


class TestDay1GapProbeOrder(unittest.TestCase):
    """The gate's US venue probe order — includes XASE (NYSE American,
    live-verified UUUU:xase / uic 549463, 2026-08-12) and must never diverge
    from placement routing's probe order (the two would otherwise disagree on
    which names are resolvable)."""

    def test_probe_order_is_xnys_xnas_xase(self) -> None:
        self.assertEqual(cl._DAY1_GAP_US_VENUE_PROBE_ORDER, ("XNYS", "XNAS", "XASE"))

    def test_probe_order_matches_placement_routing_order(self) -> None:
        from alphalens_pipeline.brokers import routing

        self.assertEqual(cl._DAY1_GAP_US_VENUE_PROBE_ORDER, routing.US_MIC_PROBE_ORDER)


class TestDay1GapGateDefersObservability(unittest.TestCase):
    """``_day1_gap_gate_defers`` observability: "defer_no_price" is an
    INFRASTRUCTURE failure (the probe could not produce a price at all —
    real incident 2026-08-12: LAC's resolve failure silently deferred its
    whole day 1 at DEBUG), so it must WARN + page (throttled) under its own
    ``day1-gap-noprice:`` key; "defer_preopen" stays DEBUG-quiet (expected,
    high-frequency)."""

    _BRIEF = dt.date(2026, 8, 10)  # Monday
    _WITHIN_DAY1 = dt.datetime(2026, 8, 11, 14, 30, tzinfo=dt.UTC)
    _PREOPEN = dt.datetime(2026, 8, 11, 13, 0, tzinfo=dt.UTC)
    _LOGGER = "alphalens_pipeline.brokers.automanager.control_loop"

    def _defers(self, *, now: dt.datetime, probe: Any) -> tuple[bool, list[tuple[str, str]]]:
        alerts: list[tuple[str, str]] = []

        def _alert(message: str, key: str) -> bool:
            alerts.append((message, key))
            return True

        with (
            _frozen_now(now),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            deferred = cl._day1_gap_gate_defers(
                "KO", self._BRIEF, _day1_spec(), "XNYS", probe, _alert
            )
        return deferred, alerts

    def test_defer_no_price_warns_and_alerts_with_noprice_key(self) -> None:
        with self.assertLogs(self._LOGGER, level="WARNING") as cm:
            deferred, alerts = self._defers(now=self._WITHIN_DAY1, probe=lambda *_a: None)
        self.assertTrue(deferred)
        self.assertTrue(
            any("KO" in line for line in cm.output),
            f"the no-price warning must name the ticker; got {cm.output}",
        )
        self.assertEqual(len(alerts), 1)
        message, key = alerts[0]
        self.assertEqual(key, "day1-gap-noprice:KO")
        self.assertIn("KO", message)
        self.assertIn("price probe", message.lower())

    def test_defer_preopen_stays_quiet(self) -> None:
        with self.assertNoLogs(self._LOGGER, level="WARNING"):
            deferred, alerts = self._defers(now=self._PREOPEN, probe=_RaisingProbe())
        self.assertTrue(deferred)
        self.assertEqual(alerts, [], "a pre-open defer is DEBUG-only, never an alert")


class TestPlacePickDay1GapGateIntegration(unittest.TestCase):
    """The day-1 gap gate wired into ``_place_pick`` (deliverable 1d): the gate
    is evaluated at the TOP, before any broker/safety/sizing I/O, so a
    deferral never journals a refusal (the pick stays armed) and never
    touches the broker."""

    _BRIEF = "2026-08-10"  # Monday
    _DAY1_OPEN = dt.datetime(2026, 8, 11, 13, 30, tzinfo=dt.UTC)
    _WITHIN_DAY1 = _DAY1_OPEN + dt.timedelta(minutes=30)
    _DAY_AFTER = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)

    def _placer(
        self, broker: Any, *, day1_gap_price_probe: Any = None, **over: Any
    ) -> tuple[Any, list[tuple[str, str]], list[tuple[Any, ...]]]:
        pkg = "alphalens_pipeline.brokers"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        alerts: list[tuple[str, str]] = []
        refusals: list[tuple[Any, ...]] = []

        def _alert_throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        m: dict[str, Any] = {
            "verdicts": lambda _r, _b: [],
            "safety_check": lambda *_a, **_k: object(),
            "resolve": lambda _b, _t: _instr(),
            "classify": lambda *_a, **_k: _placement(),
            # A REAL (inert, well-under-cap) plan: the post-sizing gross cap
            # reads plan.entry_tiers, so a bare object() would crash.
            "compute_plan": lambda _spec, **_k: _fee_plan(10_000.0),
            "iter_records": lambda _p: [],
            "append": lambda _r: None,
            "build_record": lambda **kw: dict(kw),
            "mark_refused": lambda *a: refusals.append(a),
            **over,
        }
        p = stack.enter_context
        p(mock.patch(f"{pkg}.automanager.picks.mark_refused", m["mark_refused"]))
        p(mock.patch(f"{pkg}.submission_log.build_submission_record", m["build_record"]))
        p(mock.patch(f"{pkg}.submission_log.append_submission_record", m["append"]))
        p(mock.patch(f"{pkg}.submission_log.iter_submission_records", m["iter_records"]))
        p(mock.patch(f"{pkg}.automanager.reconcile_bridge.verdicts", m["verdicts"]))
        p(mock.patch(f"{pkg}.automanager.safety.check", m["safety_check"]))
        p(mock.patch(f"{pkg}.routing.resolve_us_instrument", m["resolve"]))
        p(mock.patch(f"{pkg}.automanager.placement_planner.classify", m["classify"]))
        p(mock.patch("broker_contract.sizing.compute_setup_plan", m["compute_plan"]))
        p(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _line: None))
        placer = cl._make_place_pick(
            broker, alert_throttled=_alert_throttled, day1_gap_price_probe=day1_gap_price_probe
        )
        return placer, alerts, refusals

    def test_flag_off_places_even_when_probe_would_defer(self) -> None:
        # The gate is completely inert with the flag unset — _place_pick must
        # never even build the calendar/probe path, so a probe stub that
        # raises if called proves it.
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", {}, clear=True):
            placer, alerts, refusals = self._placer(broker, day1_gap_price_probe=_RaisingProbe())
            self.assertTrue(placer(_pick("KO", self._BRIEF)))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_probe_below_e1_defers_never_places_never_marks_refused_alerts_once(self) -> None:
        broker = _RecordingBroker()
        with (
            _frozen_now(self._WITHIN_DAY1),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, alerts, refusals = self._placer(broker, day1_gap_price_probe=lambda *_a: 99.0)
            self.assertFalse(placer(_pick("KO", self._BRIEF)))
        self.assertEqual(broker.placed, [], "the gate must defer BEFORE any bracket places")
        self.assertEqual(refusals, [], "a day1-gap deferral must NEVER journal a refused line")
        self.assertEqual(len(alerts), 1)
        message, reason_key = alerts[0]
        self.assertIn("day1 gap gate", message.lower())
        self.assertIn("KO", message)
        self.assertEqual(reason_key, "day1-gap:KO")

    def test_probe_at_or_above_e1_places(self) -> None:
        broker = _RecordingBroker()
        with (
            _frozen_now(self._WITHIN_DAY1),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, alerts, refusals = self._placer(broker, day1_gap_price_probe=lambda *_a: 100.0)
            self.assertTrue(placer(_pick("KO", self._BRIEF)))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_day_after_places_even_with_probe_below_e1(self) -> None:
        broker = _RecordingBroker()
        with (
            _frozen_now(self._DAY_AFTER),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, alerts, refusals = self._placer(broker, day1_gap_price_probe=_RaisingProbe())
            self.assertTrue(placer(_pick("KO", self._BRIEF)))
        self.assertTrue(broker.placed)
        self.assertEqual(alerts, [])
        self.assertEqual(refusals, [])

    def test_default_none_probe_flag_on_during_day1_defers_without_crash(self) -> None:
        broker = _RecordingBroker()
        with (
            _frozen_now(self._WITHIN_DAY1),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, _alerts, refusals = self._placer(broker)  # day1_gap_price_probe defaults None
            self.assertFalse(placer(_pick("KO", self._BRIEF)))
        self.assertEqual(broker.placed, [])
        self.assertEqual(refusals, [])

    def test_preopen_defers_without_probe_or_placement(self) -> None:
        broker = _RecordingBroker()
        with (
            _frozen_now(self._DAY1_OPEN - dt.timedelta(minutes=1)),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, alerts, refusals = self._placer(broker, day1_gap_price_probe=_RaisingProbe())
            self.assertFalse(placer(_pick("KO", self._BRIEF)))
        self.assertEqual(broker.placed, [])
        self.assertEqual(refusals, [])
        self.assertEqual(alerts, [], "a pre-open defer is DEBUG-only, never an alert")


if __name__ == "__main__":
    unittest.main()
