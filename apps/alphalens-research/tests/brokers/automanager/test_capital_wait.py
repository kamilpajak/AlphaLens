"""A pick that does not fit in free capital waits, inside its TTL window (#1734).

Owner decision 2026-10-05: a pick the gross cap or the cash floor cannot admit
stays armed and is checked again every tick, and is placed as soon as capital
allows. It obeys the SAME validity window as a placed entry: ``order_ttl_days``
sessions counted from ``meta.trade_date``. Waiting never starts a new window,
a pick placed late gets only the remaining window, and a pick still unplaced
when the window ends expires with one journal line and one alert.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import capital_wait, entry_trails, pick_waits, picks
from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import pick_money_gates as pmg
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager.live_rails import SIZING_EQUITY_MODE_ENV
from alphalens_pipeline.brokers.automanager.safety import PORTFOLIO_GROSS_FRAC_ENV, Refuse
from broker_contract.sizing import SetupPlan, TierPlan
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    InstrumentHint,
    IntentMeta,
    PickSize,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
)

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.test_control_loop import (
    _cash_acct,
    _entry_trail_journal,
    _fake_build_record,
    _instr,
    _placement,
    _position,
    _RecordingBroker,
    _verdict,
)

_TRADE_DATE = "2026-09-30"
# trade_date 2026-09-30 + 7 XNYS sessions -> Friday 2026-10-09, close 20:00 UTC.
_WINDOW_END = dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC)
_INSIDE = dt.datetime(2026, 9, 30, 15, 0, tzinfo=dt.UTC)
_GROSS_5000 = {PORTFOLIO_GROSS_FRAC_ENV: "0.05"}  # limit 5_000 of total_value 100_000
# Inside the window, in the minute before 00:00 UTC where a classic placement
# sits out one tick AFTER the money gates have run.
_BEFORE_MIDNIGHT = dt.datetime(2026, 10, 6, 23, 59, 30, tzinfo=dt.UTC)


def _doc(
    ticker: str = "KO",
    *,
    trade_date: str = _TRADE_DATE,
    ttl: int = 0,
    notional: float = 2000.0,
    armed_ts: str = "2026-09-30T13:00:00+00:00",
    immediate: bool = False,
) -> TradeIntent:
    tiers = (
        (
            EntryTierSpec(limit_price=10.0, alloc_pct=50.0, tag="T1", entry_mode="immediate"),
            EntryTierSpec(limit_price=9.0, alloc_pct=50.0, tag="T2"),
        )
        if immediate
        else (EntryTierSpec(limit_price=10.0, alloc_pct=100.0, tag="T1"),)
    )
    return TradeIntent(
        intent_id=f"{ticker}:{trade_date}",
        instrument=InstrumentHint(ticker=ticker, mic="XNYS"),
        spec=TradeSpec(
            entry_tiers=tiers,
            disaster_stop=8.0,
            tp_tranches=(TpTrancheSpec(price=12.0, tranche_pct=100.0, r_multiple=1.0, tag="TP1"),),
            size=PickSize(notional_acct=notional, currency="USD"),
            order_ttl_days=ttl,
        ),
        meta=IntentMeta(armed_ts=armed_ts, trade_date=trade_date, source="manual"),
    )


def _plan_for(spec: Any, **_k: Any) -> SetupPlan:
    """Size the document the way the stub sizer does: $10 a share for its amount."""
    notional = float(spec.size.notional_acct)
    if any(tier.entry_mode == "immediate" for tier in spec.entry_tiers):
        half = int(notional / 2 // 10.0)
        tiers = (
            TierPlan(0, 10.0, half, 50.0, "T1", entry_mode="immediate"),
            TierPlan(1, 9.0, half, 50.0, "T2"),
        )
    else:
        tiers = (TierPlan(0, 10.0, int(notional // 10.0), 100.0, "T1"),)
    return SetupPlan(
        total_notional=notional,
        disaster_stop=8.0,
        order_ttl_days=spec.order_ttl_days,
        entry_tiers=tiers,
        tp_tranches=(),
    )


class _Placer:
    """``_make_place_pick`` with every broker-side seam stubbed, the real money
    gates, the real pick/wait journals under the isolated home, and a clock."""

    def __init__(
        self,
        case: unittest.TestCase,
        broker: Any,
        *,
        records: list[dict[str, Any]] | None = None,
        verdicts: list[Any] | None = None,
        safety: Callable[..., Any] | None = None,
        tick_admissions: Any = None,
    ) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.refusals: list[tuple[Any, ...]] = []
        self.submissions: list[Any] = []
        self.classify_calls: list[dict[str, Any]] = []
        self.now = _INSIDE
        pkg = "alphalens_pipeline.brokers"

        def _classify(*_a: Any, **kw: Any) -> Any:
            self.classify_calls.append(kw)
            return _placement()

        stack = contextlib.ExitStack()
        case.addCleanup(stack.close)
        for target, fn in (
            (
                f"{pkg}.automanager.reconcile_bridge.verdicts",
                lambda _r, _b, **_k: list(verdicts or []),
            ),
            (f"{pkg}.automanager.safety.check", safety or (lambda *_a, **_k: object())),
            (f"{pkg}.routing.resolve_us_instrument", lambda _b, _t, **_kw: _instr()),
            (f"{pkg}.submission_log.iter_submission_records", lambda _p: list(records or [])),
            (f"{pkg}.submission_log.build_submission_record", _fake_build_record),
            (f"{pkg}.submission_log.append_submission_record", self.submissions.append),
            ("broker_contract.sizing.compute_setup_plan", _plan_for),
            (f"{pkg}.automanager.placement_planner.classify", _classify),
            (f"{pkg}.automanager.picks.mark_refused", lambda *a, **_k: self.refusals.append(a)),
        ):
            stack.enter_context(mock.patch(target, fn))
        stack.enter_context(
            mock.patch.object(sj, "_append_standalone_stop_journal", lambda _l: None)
        )
        stack.enter_context(mock.patch.object(cl, "_utc_now", lambda: self.now))

        def _throttled(message: str, key: str) -> bool:
            self.alerts.append((message, key))
            return True

        self.place = cl._make_place_pick(
            broker, alert_throttled=_throttled, tick_admissions=tick_admissions
        )

    def keys(self) -> list[str]:
        return [key for _message, key in self.alerts]


class _CapitalWaitCase(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        capital_wait._reset_for_tests()
        self.addCleanup(capital_wait._reset_for_tests)
        _entry_trail_journal(self, None)


# --------------------------------------------------------------------------
# The gates say WHY they refuse: capital (wait) or state (held)
# --------------------------------------------------------------------------


class GateVerdictKindTest(_CapitalWaitCase):
    def _gross(self, *, notional: float, **kw: Any) -> Any:
        from tests.brokers.automanager.test_control_loop import _acct, _fee_plan

        return pmg._check_gross_cap(
            _fee_plan(notional),
            None,
            account=_acct(),
            open_verdicts=kw.get("verdicts", []),
            records=kw.get("records", []),
            positions=kw.get("positions", []),
            ticker="KO",
        )

    def _cash(self, *, notional: float, margin: Any) -> Any:
        from tests.brokers.automanager.test_control_loop import _fee_plan

        return pmg._check_cash_floor(
            _fee_plan(notional),
            None,
            account=_cash_acct(margin),
            open_verdicts=[],
            records=[],
            ticker="KO",
        )

    def test_a_gross_shortfall_is_a_capital_verdict(self) -> None:
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            verdict = self._gross(notional=10_000.0)
        self.assertEqual((verdict.gate, verdict.kind), ("gross_cap", "capital"))
        self.assertIn("waits for capital", verdict.message)

    def test_an_unjoined_working_order_is_a_state_verdict(self) -> None:
        orphan = _verdict(status="WORKING", details={"client_request_id": "rid-unknown"})
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            verdict = self._gross(notional=100.0, verdicts=[orphan])
        self.assertEqual((verdict.gate, verdict.kind), ("gross_cap", "state"))

    def test_a_position_without_a_mark_is_a_state_verdict(self) -> None:
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            verdict = self._gross(notional=100.0, positions=[_position(None)])
        self.assertEqual(verdict.kind, "state")

    def test_an_unvaluable_watch_is_a_state_verdict_on_both_gates(self) -> None:
        _entry_trail_journal(self, ["{not json"])
        with mock.patch.dict(
            "os.environ",
            {PORTFOLIO_GROSS_FRAC_ENV: "1.0", SIZING_EQUITY_MODE_ENV: "declared"},
            clear=True,
        ):
            self.assertEqual(self._gross(notional=100.0).kind, "state")
            self.assertEqual(self._cash(notional=100.0, margin=1e9).kind, "state")

    def test_a_cash_shortfall_is_a_capital_verdict(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_MODE_ENV: "declared"}, clear=True):
            verdict = self._cash(notional=10_000.0, margin=5_000.0)
        self.assertEqual((verdict.gate, verdict.kind), ("cash_floor", "capital"))

    def test_no_margin_figure_is_a_state_verdict(self) -> None:
        with mock.patch.dict("os.environ", {SIZING_EQUITY_MODE_ENV: "declared"}, clear=True):
            verdict = self._cash(notional=10_000.0, margin=None)
        self.assertEqual((verdict.gate, verdict.kind), ("cash_floor", "state"))


# --------------------------------------------------------------------------
# Waiting: no refusal, one line, one page, placed once capital frees
# --------------------------------------------------------------------------


class WaitForCapitalTest(_CapitalWaitCase):
    def test_a_waiting_pick_is_placed_on_the_tick_capital_frees(self) -> None:
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            self.assertFalse(placer.place(_doc(notional=6_000.0)))
            self.assertEqual(broker.placed, [])
            # Capital frees: the limit rises to 10_000 on the next tick.
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.1"}):
                self.assertTrue(placer.place(_doc(notional=6_000.0)))
        self.assertEqual(placer.refusals, [])
        self.assertEqual(len(broker.placed), 1)

    def test_the_wait_is_journaled_once_and_paged_once_while_it_lasts(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            for minute in range(3):
                placer.now = _INSIDE + dt.timedelta(minutes=minute)
                self.assertFalse(placer.place(_doc(notional=6_000.0)))
        self.assertEqual(placer.keys(), [f"capital-wait:KO:{_TRADE_DATE}"])
        lines = pick_waits.read_waits().latest
        self.assertEqual(list(lines), [f"KO:{_TRADE_DATE}"])
        wait = lines[f"KO:{_TRADE_DATE}"]
        self.assertEqual(wait.window_end, _WINDOW_END.isoformat())
        self.assertEqual(wait.record["date"], _TRADE_DATE)
        self.assertEqual(len(_lines(pick_waits)), 1)

    def test_the_wait_is_logged_at_warning_once_then_at_debug(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        with (
            mock.patch.dict("os.environ", _GROSS_5000, clear=True),
            self.assertLogs(capital_wait.logger, level="DEBUG") as logs,
        ):
            for _tick in range(3):
                placer.place(_doc(notional=6_000.0))
        waits = [r for r in logs.records if "stays armed until" in r.getMessage()]
        self.assertEqual(
            [r.levelno for r in waits], [logging.WARNING, logging.DEBUG, logging.DEBUG]
        )

    def test_a_restart_does_not_page_again_for_a_wait_already_journaled(self) -> None:
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            first = _Placer(self, _RecordingBroker())
            first.place(_doc(notional=6_000.0))
            capital_wait._reset_for_tests()  # the daemon restarts
            second = _Placer(self, _RecordingBroker())
            second.place(_doc(notional=6_000.0))
        self.assertEqual(len(first.alerts), 1)
        self.assertEqual(second.alerts, [])

    def test_a_flip_of_the_binding_gate_is_journaled_but_not_paged(self) -> None:
        # Marks move: the gate holding a pick may change. The decision the
        # operator has to make does not, so one page per pick.
        env = {**_GROSS_5000, SIZING_EQUITY_MODE_ENV: "declared"}
        rich = _RecordingBroker(on_account=lambda: _cash_acct(1e9))
        poor = _RecordingBroker(on_account=lambda: _cash_acct(1_000.0))
        with mock.patch.dict("os.environ", env, clear=True):
            gross = _Placer(self, rich)
            gross.place(_doc(notional=6_000.0))
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}):
                cash = _Placer(self, poor)
                cash.now = _INSIDE + capital_wait.WAIT_LINE_MIN_INTERVAL
                cash.place(_doc(notional=6_000.0))
        self.assertEqual(len(gross.alerts) + len(cash.alerts), 1)
        self.assertEqual(pick_waits.read_waits().latest[f"KO:{_TRADE_DATE}"].gate, "cash_floor")

    def test_a_flip_of_the_binding_gate_inside_the_interval_writes_no_line(self) -> None:
        env = {**_GROSS_5000, SIZING_EQUITY_MODE_ENV: "declared"}
        rich = _RecordingBroker(on_account=lambda: _cash_acct(1e9))
        poor = _RecordingBroker(on_account=lambda: _cash_acct(1_000.0))
        with mock.patch.dict("os.environ", env, clear=True):
            _Placer(self, rich).place(_doc(notional=6_000.0))
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}):
                cash = _Placer(self, poor)
                cash.now = _INSIDE + capital_wait.WAIT_LINE_MIN_INTERVAL - dt.timedelta(seconds=1)
                cash.place(_doc(notional=6_000.0))
        self.assertEqual(len(_lines(pick_waits)), 1)
        self.assertEqual(pick_waits.read_waits().latest[f"KO:{_TRADE_DATE}"].gate, "gross_cap")

    def test_a_wait_is_closed_once_when_the_gates_pass_but_the_pick_stays_unplaced(
        self,
    ) -> None:
        # The gates pass a minute before 00:00 UTC, where a classic placement
        # sits out one tick: the pick is still unplaced, but no longer for lack
        # of capital, so `broker status` must stop saying it waits for capital.
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            placer.place(_doc(notional=6_000.0))
            placer.now = _BEFORE_MIDNIGHT
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.1"}):
                for _tick in range(2):
                    self.assertFalse(placer.place(_doc(notional=6_000.0)))
        self.assertEqual(broker.placed, [])
        self.assertEqual(pick_waits.read_waits().latest, {})
        self.assertEqual(len(_lines(pick_waits)), 2, "one wait line and ONE clearing line")

    def test_a_pick_that_waits_again_after_a_clear_is_not_paged_again(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            placer.place(_doc(notional=6_000.0))
            placer.now = _BEFORE_MIDNIGHT
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.1"}):
                placer.place(_doc(notional=6_000.0))
            placer.now = _BEFORE_MIDNIGHT + capital_wait.WAIT_LINE_MIN_INTERVAL
            placer.place(_doc(notional=6_000.0))
        self.assertIn(f"KO:{_TRADE_DATE}", pick_waits.read_waits().latest)
        self.assertEqual(placer.keys(), [f"capital-wait:KO:{_TRADE_DATE}"])

    def test_a_wait_right_after_a_clear_writes_no_line_inside_the_interval(self) -> None:
        # Fit / no-fit can alternate tick by tick while something else keeps
        # the pick unplaced; one line pair per tick would flood the journal.
        placer = _Placer(self, _RecordingBroker())
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            placer.place(_doc(notional=6_000.0))
            placer.now = _BEFORE_MIDNIGHT
            with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.1"}):
                placer.place(_doc(notional=6_000.0))
            placer.now = _BEFORE_MIDNIGHT + dt.timedelta(seconds=10)
            placer.place(_doc(notional=6_000.0))
        self.assertEqual(len(_lines(pick_waits)), 2)

    def test_a_state_the_gate_cannot_value_holds_without_a_wait_line(self) -> None:
        broker = _RecordingBroker(on_positions=[_position(None)])
        placer = _Placer(self, broker)
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=True):
            self.assertFalse(placer.place(_doc()))
        self.assertEqual((placer.refusals, broker.placed), ([], []))
        self.assertEqual(placer.keys(), ["gross-cap-state:KO"])
        self.assertEqual(pick_waits.read_waits().latest, {})

    def test_the_fee_floor_stays_terminal(self) -> None:
        from alphalens_pipeline.brokers.automanager.live_rails import MAX_FEE_BPS_ENV

        placer = _Placer(self, _RecordingBroker())
        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "1"}, clear=True):
            self.assertFalse(placer.place(_doc(notional=50.0)))
        self.assertEqual(len(placer.refusals), 1)
        self.assertEqual(pick_waits.read_waits().latest, {})


# --------------------------------------------------------------------------
# The window: counted from trade_date, never restarted, ends the wait
# --------------------------------------------------------------------------


class ValidityWindowTest(_CapitalWaitCase):
    def test_at_the_window_end_an_unplaced_pick_expires_without_broker_io(self) -> None:
        class _NoIo(_RecordingBroker):
            def get_account(self) -> Any:
                raise AssertionError("an expired pick must not read the broker")

        placer = _Placer(self, _NoIo())
        placer.now = _WINDOW_END
        self.assertFalse(placer.place(_doc()))
        (record,) = picks.read_pick_fold().records
        self.assertEqual(record.status, picks.STATUS_EXPIRED)
        self.assertEqual(record.record["window_end"], _WINDOW_END.isoformat())
        self.assertEqual(record.record["reason"], capital_wait.UNPLACED_AT_WINDOW_END)
        self.assertEqual(placer.keys(), [f"pick-expired:KO:{_TRADE_DATE}"])

    def test_one_second_before_the_end_it_is_still_placed(self) -> None:
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        placer.now = _WINDOW_END - dt.timedelta(seconds=1)
        self.assertTrue(placer.place(_doc()))
        self.assertEqual(picks.read_pick_fold().records, [])

    def test_the_expiry_names_what_held_the_pick(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            placer.place(_doc(notional=6_000.0))
            placer.now = _WINDOW_END
            placer.place(_doc(notional=6_000.0))
        (record,) = picks.read_pick_fold().records
        self.assertTrue(record.record["reason"].startswith("waiting for capital: gross cap"))

    def test_a_pick_held_by_any_other_rail_expires_too(self) -> None:
        placer = _Placer(self, _RecordingBroker(), safety=lambda *_a, **_k: Refuse(reason="KILL"))
        self.assertFalse(placer.place(_doc()))
        placer.now = _WINDOW_END
        self.assertFalse(placer.place(_doc()))
        (record,) = picks.read_pick_fold().records
        self.assertEqual(record.status, picks.STATUS_EXPIRED)
        self.assertIn("safety rail", record.record["reason"])

    def test_a_pick_held_by_the_day1_gate_expires_naming_that_gate(self) -> None:
        from alphalens_pipeline.brokers.automanager import day1_gap_gate as d1g

        placer = _Placer(self, _RecordingBroker())
        with mock.patch.object(d1g, "_day1_gap_gate_defers", return_value=True):
            self.assertFalse(placer.place(_doc()))
            placer.now = _WINDOW_END
            self.assertFalse(placer.place(_doc()))
        (record,) = picks.read_pick_fold().records
        self.assertEqual(record.status, picks.STATUS_EXPIRED)
        self.assertEqual(record.record["reason"], "deferred by the day-1 gap gate")

    def test_a_stated_ttl_moves_the_end(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        # ttl 3 from 2026-09-30 ends at the close of Monday 2026-10-05.
        placer.now = dt.datetime(2026, 10, 5, 20, 0, tzinfo=dt.UTC)
        self.assertFalse(placer.place(_doc(ttl=3)))
        self.assertEqual(picks.read_pick_fold().records[0].status, picks.STATUS_EXPIRED)

    def test_an_expired_line_that_cannot_be_written_never_crashes_the_drain(self) -> None:
        def _disk_full(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        placer = _Placer(self, _RecordingBroker())
        placer.now = _WINDOW_END
        with mock.patch.object(picks, "mark_expired", _disk_full):
            self.assertFalse(placer.place(_doc()))  # must not raise
        self.assertEqual(placer.keys(), [f"pick-expired:KO:{_TRADE_DATE}"])

    def test_a_document_without_a_window_is_refused_not_raised(self) -> None:
        # A journal line that never passed today's door (order_ttl_days < 0)
        # must not end the tick before the protection pass.
        class _NoIo(_RecordingBroker):
            def get_account(self) -> Any:
                raise AssertionError("no broker read for a document with no window")

        placer = _Placer(self, _NoIo())
        self.assertFalse(placer.place(_doc(ttl=-1)))
        self.assertEqual(len(placer.refusals), 1)
        self.assertIn("validity window", placer.refusals[0][2])
        self.assertEqual(placer.keys(), ["pick-window:KO"])

    def test_the_drain_retires_an_expired_pick_for_good(self) -> None:
        from tests.brokers.automanager.test_control_loop import _deps, _StubBroker

        picks.arm_pick(_doc())
        placer = _Placer(self, _RecordingBroker())
        placer.now = _WINDOW_END
        calls: list[Any] = []

        def _place(pick: Any) -> bool:
            calls.append(pick)
            return placer.place(pick)

        deps = _deps(
            _StubBroker(), kill_file=self.home / "KILL", verdicts=[], place_calls=[], alerts=[]
        )
        deps = cl.LoopDeps(
            **{
                **deps.__dict__,
                "iter_picks": picks.iter_picks,
                "place_pick": _place,
                "read_records": list,
            }
        )
        cl._run_placement_drain(deps, cl.TickReport(), enabled=True)
        cl._run_placement_drain(deps, cl.TickReport(), enabled=True)
        self.assertEqual(len(calls), 1, "an expired pick is never drained again")
        self.assertEqual(len(placer.alerts), 1)


class LatePlacementGetsTheRemainingWindowTest(_CapitalWaitCase):
    def test_a_classic_placement_on_day_five_rests_two_sessions(self) -> None:
        # Placed 2026-10-07 for trade_date 2026-09-30: the window ends 10-09,
        # so the GTD count is 2 — not the 7 a fresh window would give.
        placer = _Placer(self, _RecordingBroker())
        placer.now = dt.datetime(2026, 10, 7, 15, 0, tzinfo=dt.UTC)
        self.assertTrue(placer.place(_doc()))
        self.assertEqual([call["entry_ttl_days"] for call in placer.classify_calls], [2])

    def test_on_the_last_session_the_count_is_zero_not_the_default(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        placer.now = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC)
        self.assertTrue(placer.place(_doc()))
        self.assertEqual([call["entry_ttl_days"] for call in placer.classify_calls], [0])

    def test_on_the_trade_date_the_whole_window_is_given(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        self.assertTrue(placer.place(_doc()))
        self.assertEqual([call["entry_ttl_days"] for call in placer.classify_calls], [7])

    def test_a_window_that_ends_during_placement_places_nothing(self) -> None:
        # Several broker reads separate the check at the top from the POST.
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        ticks = iter([_WINDOW_END - dt.timedelta(seconds=5), _WINDOW_END])
        last = [_WINDOW_END]

        def _clock() -> dt.datetime:
            last[0] = next(ticks, last[0])
            return last[0]

        with mock.patch.object(cl, "_utc_now", _clock):
            self.assertFalse(placer.place(_doc()))
        self.assertEqual(broker.placed, [])

    def test_a_window_that_ends_during_placement_opens_no_watch(self) -> None:
        # The watch route has no GTD of its own to check, so the re-read of the
        # clock after the gates is what keeps a watch from opening past the end.
        from tests.brokers.automanager.test_entry_watch_wiring import _ENV, _journal, _lines
        from tests.brokers.automanager.test_entry_watch_wiring import (
            _RecordingBroker as _TrailingBroker,
        )

        path = _journal(self)
        placer = _Placer(self, _TrailingBroker())
        ticks = iter([_WINDOW_END - dt.timedelta(seconds=5), _WINDOW_END])
        last = [_WINDOW_END]

        def _clock() -> dt.datetime:
            last[0] = next(ticks, last[0])
            return last[0]

        with (
            mock.patch.dict("os.environ", {_ENV: "50"}, clear=True),
            mock.patch.object(cl, "_utc_now", _clock),
        ):
            self.assertFalse(placer.place(_doc()))
        self.assertEqual(_lines(path), [])

    def test_the_minute_before_utc_midnight_defers_a_classic_placement(self) -> None:
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        placer.now = dt.datetime(2026, 10, 6, 23, 59, 30, tzinfo=dt.UTC)
        self.assertFalse(placer.place(_doc()))
        self.assertEqual(broker.placed, [])
        placer.now = dt.datetime(2026, 10, 7, 0, 0, 30, tzinfo=dt.UTC)
        self.assertTrue(placer.place(_doc()))

    def test_a_watch_carries_the_pick_window_not_seven_sessions_from_today(self) -> None:
        from tests.brokers.automanager.test_entry_watch_wiring import _ENV, _journal, _lines
        from tests.brokers.automanager.test_entry_watch_wiring import (
            _RecordingBroker as _TrailingBroker,
        )

        path = _journal(self)
        placer = _Placer(self, _TrailingBroker())
        placer.now = dt.datetime(2026, 10, 1, 15, 0, tzinfo=dt.UTC)
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            self.assertTrue(placer.place(_doc(ttl=3)))
        opens = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual({ln["window_end"] for ln in opens}, {"2026-10-05T20:00:00+00:00"})


# --------------------------------------------------------------------------
# Armed order, first fit, and no double spend inside one tick
# --------------------------------------------------------------------------


class ArmedOrderTest(_CapitalWaitCase):
    def _drain(
        self, picks_in_fold_order: list[Any], place: Callable[[Any], bool], **deps_kw: Any
    ) -> None:
        from tests.brokers.automanager.test_control_loop import _deps, _StubBroker

        deps = _deps(
            _StubBroker(), kill_file=self.home / "KILL", verdicts=[], place_calls=[], alerts=[]
        )
        deps = cl.LoopDeps(
            **{
                **deps.__dict__,
                "iter_picks": lambda: iter(picks_in_fold_order),
                "place_pick": place,
                "read_records": list,
                **deps_kw,
            }
        )
        cl._run_placement_drain(deps, cl.TickReport(), enabled=True)

    def test_picks_are_tried_in_armed_order_not_queue_order(self) -> None:
        late = _doc("MU", armed_ts="2026-09-30T14:00:00+00:00")
        early = _doc("KO", armed_ts="2026-09-30T13:00:00+02:00")  # 11:00 UTC
        unknown = _doc("AA", armed_ts="not a time")
        seen: list[str] = []
        self._drain([unknown, late, early], lambda p: bool(seen.append(p.instrument.ticker)))
        self.assertEqual(seen, ["KO", "MU", "AA"], "parsed, offset-aware; unknown last")

    def test_armed_order_is_by_time_where_the_text_order_disagrees(self) -> None:
        # Text order puts MU (13:00) before KO (14:00); in time KO is 09:00 UTC.
        # In the second pair "Z" sorts after "." as text, so BB's text comes
        # first although AA is half a second earlier.
        mu = _doc("MU", armed_ts="2026-09-30T13:00:00+00:00")
        ko = _doc("KO", armed_ts="2026-09-30T14:00:00+05:00")
        aa = _doc("AA", armed_ts="2026-09-30T15:00:00Z")
        bb = _doc("BB", armed_ts="2026-09-30T15:00:00.500000+00:00")
        self.assertEqual(sorted([aa.meta.armed_ts, bb.meta.armed_ts])[0], bb.meta.armed_ts)
        seen: list[str] = []
        self._drain([bb, aa, mu, ko], lambda p: bool(seen.append(p.instrument.ticker)))
        self.assertEqual(seen, ["KO", "MU", "AA", "BB"])

    def test_first_fit_a_later_small_pick_is_placed_while_an_earlier_big_one_waits(self) -> None:
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        big = _doc("BIG", notional=6_000.0, armed_ts="2026-09-30T12:00:00+00:00")
        small = _doc("SML", notional=2_000.0, armed_ts="2026-09-30T13:00:00+00:00")
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            self._drain([small, big], placer.place)
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(list(pick_waits.read_waits().latest), [f"BIG:{_TRADE_DATE}"])

    def test_the_earlier_pick_gets_freed_capital_first(self) -> None:
        broker = _RecordingBroker()
        placer = _Placer(self, broker)
        first = _doc("FST", notional=3_000.0, armed_ts="2026-09-30T12:00:00+00:00")
        second = _doc("SND", notional=3_000.0, armed_ts="2026-09-30T13:00:00+00:00")
        admissions = capital_wait.TickAdmissions()
        placer = _Placer(self, broker, tick_admissions=admissions)
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            self._drain([second, first], placer.place, tick_admissions=admissions)
        self.assertEqual(list(pick_waits.read_waits().latest), [f"SND:{_TRADE_DATE}"])

    def test_a_pick_admitted_this_tick_counts_even_when_the_broker_does_not_show_it(self) -> None:
        # The broker double never reports the first pick's order (no verdict,
        # no position): only the in-tick ledger keeps the second from spending
        # the same capital.
        broker = _RecordingBroker()
        admissions = capital_wait.TickAdmissions()
        placer = _Placer(self, broker, tick_admissions=admissions)
        a = _doc("AAA", notional=3_000.0, armed_ts="2026-09-30T12:00:00+00:00")
        b = _doc("BBB", notional=3_000.0, armed_ts="2026-09-30T13:00:00+00:00")
        with mock.patch.dict("os.environ", _GROSS_5000, clear=True):
            self._drain([a, b], placer.place, tick_admissions=admissions)
            self.assertEqual(len(broker.placed), 1)
            self.assertIn(
                "admitted this tick 3,000.00",
                pick_waits.read_waits().latest[f"BBB:{_TRADE_DATE}"].message,
            )
            # The next tick starts a fresh ledger.
            self._drain([b], placer.place, tick_admissions=admissions)
        self.assertEqual(len(broker.placed), 2)

    def test_a_watch_opened_earlier_this_tick_is_not_counted_twice(self) -> None:
        # The first pick's watch is in the re-read entry-trail fold the moment
        # it opens, so the gates already value it as "watching". The in-tick
        # ledger must not add it again: 2_000 + 2_000 fits a 5_000 limit,
        # 2_000 x 2 + 2_000 does not (LIVE 2026-10-06: HUT, AUR and FLY waited
        # one tick on SRRK and PGEN counted twice).
        from tests.brokers.automanager.test_entry_watch_wiring import _ENV, _journal
        from tests.brokers.automanager.test_entry_watch_wiring import (
            _RecordingBroker as _TrailingBroker,
        )

        _journal(self)
        admissions = capital_wait.TickAdmissions()
        placer = _Placer(self, _TrailingBroker(), tick_admissions=admissions)
        a = _doc("AAA", notional=2_000.0, armed_ts="2026-09-30T12:00:00+00:00")
        b = _doc("BBB", notional=2_000.0, armed_ts="2026-09-30T13:00:00+00:00")
        env = {_ENV: "50", PORTFOLIO_GROSS_FRAC_ENV: "0.05"}
        with mock.patch.dict("os.environ", env, clear=True):
            self._drain([a, b], placer.place, tick_admissions=admissions)
        self.assertEqual(pick_waits.read_waits().latest, {})
        self.assertEqual(
            [r.get("note") for r in placer.submissions], ["entry-trail watch opened"] * 2
        )

    def test_the_wait_journal_is_read_at_most_once_per_tick(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        docs = [_doc(f"T{n}", notional=6_000.0) for n in range(3)]
        with (
            mock.patch.dict("os.environ", _GROSS_5000, clear=True),
            mock.patch.object(pick_waits, "read_waits", wraps=pick_waits.read_waits) as reads,
        ):
            self._drain(docs, placer.place)
            self._drain(docs, placer.place)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(len(_lines(pick_waits)), 3, "each pick journaled once")

    def test_the_drain_logs_once_when_many_picks_wait(self) -> None:
        placer = _Placer(self, _RecordingBroker())
        docs = [_doc(f"T{n}", notional=6_000.0) for n in range(capital_wait.MANY_WAITING + 1)]
        with (
            mock.patch.dict("os.environ", _GROSS_5000, clear=True),
            self.assertLogs(capital_wait.logger, level="WARNING") as logs,
        ):
            self._drain(docs, placer.place)
            self._drain(docs, placer.place)
        many = [r for r in logs.records if "picks are waiting for capital" in r.getMessage()]
        self.assertEqual(len(many), 1)


class TickAdmissionsTest(unittest.TestCase):
    def test_an_admission_nothing_shows_yet_counts_in_full(self) -> None:
        admissions = capital_wait.TickAdmissions()
        admissions.admit("AAA:2026-09-30", 3_000.0)
        self.assertEqual(admissions.outstanding_acct({}), 3_000.0)

    def test_only_the_part_its_own_watches_do_not_reserve_counts(self) -> None:
        # A now half placed as an order (not yet shown) plus a pullback watch
        # that the fold already values: only the now half is outstanding.
        admissions = capital_wait.TickAdmissions()
        admissions.admit("AAA:2026-09-30", 5_000.0)
        self.assertEqual(admissions.outstanding_acct({"AAA:2026-09-30": 2_000.0}), 3_000.0)

    def test_another_picks_watch_reduces_nothing(self) -> None:
        admissions = capital_wait.TickAdmissions()
        admissions.admit("AAA:2026-09-30", 3_000.0)
        self.assertEqual(admissions.outstanding_acct({"ZZZ:2026-09-29": 2_500.0}), 3_000.0)

    def test_a_new_tick_starts_empty(self) -> None:
        admissions = capital_wait.TickAdmissions()
        admissions.admit("AAA:2026-09-30", 3_000.0)
        admissions.begin_tick()
        self.assertEqual(admissions.outstanding_acct({}), 0.0)


# --------------------------------------------------------------------------
# A pick never waits on its own exposure
# --------------------------------------------------------------------------


class NoSelfDoubleCountTest(_CapitalWaitCase):
    def test_a_placed_now_half_is_not_valued_again_as_a_candidate(self) -> None:
        # Now half 300 x 10 = 3_000 rests WORKING; pullback 300 x 9 = 2_700.
        # Counted once: 3_000 + 2_700 = 5_700 fits a 6_000 limit. Counted twice
        # (the whole plan again) it is 8_700 and the pick waits on itself.
        intent = _doc(notional=6_000.0, immediate=True)
        record = {
            "ticker": "KO",
            "trade_date": _TRADE_DATE,
            "tranche": "now",
            "tranche_meta": {"armed_ts": intent.meta.armed_ts, "outcome": "placed"},
            "brackets": [{"client_request_id": "rid-now", "entry": 10.0, "qty": 300}],
        }
        working = _verdict(status="WORKING", details={"client_request_id": "rid-now"})
        broker = _RecordingBroker()
        placer = _Placer(self, broker, records=[record], verdicts=[working])
        with mock.patch.dict("os.environ", {PORTFOLIO_GROSS_FRAC_ENV: "0.06"}, clear=True):
            self.assertTrue(placer.place(intent))
        self.assertEqual(pick_waits.read_waits().latest, {})
        self.assertEqual(len(broker.placed), 1, "only the pullback half is placed")

    def test_the_re_drive_of_a_watching_pick_is_not_held_by_its_own_watches(self) -> None:
        # A crash between the journal-first watch_open and the retiring
        # submission record: the pick is drained again with its watches open.
        from tests.brokers.automanager.test_entry_watch_wiring import _ENV, _journal
        from tests.brokers.automanager.test_entry_watch_wiring import (
            _RecordingBroker as _TrailingBroker,
        )

        _journal(self)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": f"KO-{_TRADE_DATE}-entry-t0",
                "limit": 10.0,
                "qty": 300.0,
                "pick_key": f"KO:{_TRADE_DATE}",
            }
        )
        placer = _Placer(self, _TrailingBroker())
        env = {_ENV: "50", PORTFOLIO_GROSS_FRAC_ENV: "0.05"}  # 3_000 + 3_000 > 5_000
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(placer.place(_doc(notional=3_000.0)))
        self.assertEqual(pick_waits.read_waits().latest, {})
        self.assertEqual([r.get("note") for r in placer.submissions], ["entry-trail watch opened"])


class Day1GateIsUnchangedTest(_CapitalWaitCase):
    def test_a_day1_deferral_is_not_a_capital_wait(self) -> None:
        from alphalens_pipeline.brokers.automanager import day1_gap_gate as d1g

        placer = _Placer(self, _RecordingBroker())
        with (
            mock.patch.dict("os.environ", _GROSS_5000, clear=True),
            mock.patch.object(d1g, "_day1_gap_gate_defers", return_value=True),
        ):
            self.assertFalse(placer.place(_doc(notional=6_000.0)))
        self.assertEqual(pick_waits.read_waits().latest, {})
        self.assertEqual(placer.alerts, [])


def _lines(module: Any) -> list[str]:
    from alphalens_pipeline.brokers.automanager import state_paths

    del module
    path = state_paths.pick_waits_path()
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
