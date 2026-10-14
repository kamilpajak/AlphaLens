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
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager import trade_alerts as ta
from broker_contract.contract import BrokerError, InstrumentRef, Position
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

_ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
_LIVE_EXITS_ENV = "ALPHALENS_LIVE_MARKET_EXITS"


def _tr(index: int, target: float, pct: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_frac=pct,
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
            else PricePoint(
                uic=uic,
                bid=px,
                ask=px,
                event_time=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
                received_at=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
                source="test",
            )
        )


def _deps(
    broker: object,
    *,
    alerts: list[str],
    live_exits_feed_factory: object = None,
    alert_throttled: object = None,
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
        alert_throttled=(
            alert_throttled
            if alert_throttled is not None
            else (lambda msg, reason: alerts.append(msg) or True)
        ),
        live_exits_feed_factory=live_exits_feed_factory,  # type: ignore[arg-type]
    )


class TestLiveMarketExitsEnabledGate(IsolatedHomeTestCase):
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


class TestRenderExitDispositions(IsolatedHomeTestCase):
    """The one summary line that makes a skipped uic readable (#1392).

    Before this, the price veto in ``run_live_exits`` skipped a uic with no log
    at all, and a pass that fires nothing because the price is far from TP1
    logs nothing either — so silence meant either "managed, nothing to do" or
    "never evaluated". Overnight on LIVE every uic took the silent branch while
    the pre-engine line still said "N position(s) managed".
    """

    def test_a_fully_managed_pass_has_nothing_to_say(self) -> None:
        line = cl._render_exit_dispositions(
            fired=0, dispositions={641: "managed", 13697176: "managed"}
        )
        self.assertIsNone(line)

    def test_a_skipped_uic_is_named_with_its_reason(self) -> None:
        line = cl._render_exit_dispositions(
            fired=0, dispositions={641: "no_sole_sl", 13697176: "managed"}
        )
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("no_sole_sl=1", line)
        self.assertIn("641", line)
        self.assertIn("managed=1", line)

    def test_two_skipped_uics_still_produce_ONE_line(self) -> None:
        # The line budget is the point: today the engine logged one INFO per
        # skipped uic per tick. One line per pass, whatever the count.
        line = cl._render_exit_dispositions(
            fired=0, dispositions={641: "no_price", 13697176: "no_price"}
        )
        assert line is not None
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("no_price=2", line)
        self.assertIn("641", line)
        self.assertIn("13697176", line)

    def test_the_fired_count_travels_with_it(self) -> None:
        line = cl._render_exit_dispositions(fired=2, dispositions={641: "no_price"})
        assert line is not None
        self.assertIn("fired=2", line)

    def test_a_reason_the_renderer_does_not_know_is_still_named(self) -> None:
        # Found by reading my own diff: the renderer walked a FIXED tuple of
        # three reasons, so a fourth disposition added to the engine later
        # would produce a line that mentions neither it nor its uic — the same
        # silent-skip blind spot this whole change removes.
        line = cl._render_exit_dispositions(
            fired=0, dispositions={641: "some_future_reason", 13697176: "managed"}
        )
        assert line is not None
        self.assertIn("some_future_reason=1", line)
        self.assertIn("641", line)

    def test_an_empty_report_says_nothing(self) -> None:
        self.assertIsNone(cl._render_exit_dispositions(fired=0, dispositions={}))


class TestBuildManagedExits(IsolatedHomeTestCase):
    def test_a_uic_with_a_tranche_plan_and_a_live_long_becomes_one_managed_exit(self) -> None:
        pos = _mk_pos(uic=486, qty=100.0)
        tranches = (_tr(0, 16.0, 0.5),)
        tranche_plans = {486: (tranches, 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos], tranche_plans=tranche_plans, fired={}, trailed={}
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
        managed = cl._build_managed_exits(
            long_positions=[pos], tranche_plans={}, fired={}, trailed={}
        )
        self.assertEqual(managed, [])

    def test_a_uic_whose_plan_declares_no_tranches_is_skipped_too(self) -> None:
        # Issue #1511: the watch path now journals a plan for a document that
        # declared `tp_tranches: []`, so such a uic HAS a plan and no longer
        # falls into the `plan is None` skip above. It must still be skipped:
        # there is nothing to fire, and enrolling it would make run_live_exits
        # spend a positions read plus a working-orders read on it every tick
        # and stamp it MANAGED while planning nothing.
        pos = _mk_pos(uic=486, qty=100.0)
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans={486: ((), 100.0, 13.0)},
            fired={},
            trailed={},
        )
        self.assertEqual(managed, [])

    def test_a_reanchored_level_raises_the_managed_exit_stop(self) -> None:
        # Issue #1518. Without this the first tranche fire amends the resting
        # stop back to the placement-time plan stop: execute_tranche_exit frees
        # the tranche by amending to ManagedExit.stop_price, and a re-anchored
        # uic was never in the only map that could raise it.
        pos = _mk_pos(uic=486, qty=100.0)
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans={486: ((_tr(0, 16.0, 1.0),), 100.0, 13.0)},
            fired={},
            trailed={},
            reanchored={486: 14.5},
        )
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0].stop_price, 14.5)

    def test_the_higher_of_trailed_and_reanchored_wins(self) -> None:
        # The two floors are written by different arms of the same policy split
        # and are mutually exclusive per pick, but the fold is journal-lifetime,
        # so a uic can carry both. Never take the newer — take the higher; a
        # stop floor that can fall is not a floor.
        pos = _mk_pos(uic=486, qty=100.0)
        for label, trailed, reanchored, expected in (
            ("trailed higher", {486: 15.5}, {486: 14.5}, 15.5),
            ("reanchored higher", {486: 14.0}, {486: 14.5}, 14.5),
        ):
            with self.subTest(label):
                managed = cl._build_managed_exits(
                    long_positions=[pos],
                    tranche_plans={486: ((_tr(0, 16.0, 1.0),), 100.0, 13.0)},
                    fired={},
                    trailed=trailed,
                    reanchored=reanchored,
                )
                self.assertEqual(managed[0].stop_price, expected)

    def test_a_reanchored_level_below_the_plan_stop_never_lowers_it(self) -> None:
        # The clamp already refuses anything below the brief floor, so this
        # should be unreachable — pinned anyway, because a floor that can lower
        # the stop is the defect this issue is about, only inverted.
        pos = _mk_pos(uic=486, qty=100.0)
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans={486: ((_tr(0, 16.0, 1.0),), 100.0, 13.0)},
            fired={},
            trailed={},
            reanchored={486: 11.0},
        )
        self.assertEqual(managed[0].stop_price, 13.0)

    def test_fired_tags_flow_into_already_fired(self) -> None:
        pos = _mk_pos(uic=486, qty=50.0)
        tranche_plans = {486: ((_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)), 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans=tranche_plans,
            fired={486: frozenset({"tp1"})},
            trailed={},
        )
        self.assertEqual(managed[0].already_fired, frozenset({"tp1"}))

    def test_mixed_managed_and_skipped_positions(self) -> None:
        managed_pos = _mk_pos(uic=1, qty=10.0)
        skipped_pos = _mk_pos(uic=2, qty=20.0)
        tranche_plans = {1: ((_tr(0, 5.0, 1.0),), 10.0, 4.0)}
        managed = cl._build_managed_exits(
            long_positions=[managed_pos, skipped_pos],
            tranche_plans=tranche_plans,
            fired={},
            trailed={},
        )
        self.assertEqual([m.uic for m in managed], [1])

    def test_a_trailed_level_above_the_plan_stop_wins(self) -> None:
        # The partial-sale stop-reset hazard: execute_tranche_exit amends the
        # SL to ManagedExit.stop_price, so a tranche fire under a trailing
        # policy must carry the RATCHETED level, never the placement-time
        # disaster stop — otherwise TP1 firing would PATCH the stop back down.
        pos = _mk_pos(uic=486, qty=100.0)
        tranche_plans = {486: ((_tr(0, 16.0, 0.5),), 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans=tranche_plans,
            fired={},
            trailed={486: 14.5},
        )
        self.assertEqual(managed[0].stop_price, 14.5)

    def test_a_trailed_level_below_the_plan_stop_never_loosens(self) -> None:
        pos = _mk_pos(uic=486, qty=100.0)
        tranche_plans = {486: ((_tr(0, 16.0, 0.5),), 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans=tranche_plans,
            fired={},
            trailed={486: 12.0},
        )
        self.assertEqual(managed[0].stop_price, 13.0)

    def test_a_uic_absent_from_trailed_keeps_the_plan_stop(self) -> None:
        pos = _mk_pos(uic=486, qty=100.0)
        tranche_plans = {486: ((_tr(0, 16.0, 0.5),), 100.0, 13.0)}
        managed = cl._build_managed_exits(
            long_positions=[pos],
            tranche_plans=tranche_plans,
            fired={},
            trailed={999: 50.0},
        )
        self.assertEqual(managed[0].stop_price, 13.0)


class TestFoldFiredSinceLatestPlan(IsolatedHomeTestCase):
    """A uic is stable per instrument (Saxo nets by uic), and the standalone-stop
    journal is append-only and never cleared. A re-entered position (a fresh
    ``tranche_plan`` line for a uic that already fired tranches under a PRIOR
    trade) must NOT inherit the old trade's fired tags -- that would silently
    suppress the whole new ladder forever. ``_fold_fired_since_latest_plan``
    resets a uic's accumulator on every new ``tranche_plan`` line, processing
    the journal in write order."""

    def test_fired_tags_after_the_latest_plan_are_kept(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_a_new_tranche_plan_line_resets_the_uics_fired_set(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},  # re-entry: the OLD trade's fired tags reset
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_fired_tags_after_the_reset_still_accumulate(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_distinct_uics_reset_independently(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 1},
            {"kind": "tranche_fired", "uic": 1, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 2},
            {"kind": "tranche_fired", "uic": 2, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 1},  # only uic 1 resets
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertNotIn(1, out)
        self.assertEqual(out[2], frozenset({"tp1"}))

    def test_malformed_lines_are_skipped(self) -> None:
        lines = [
            {"kind": "tranche_fired", "tag": "tp1"},  # no uic
            {"kind": "tranche_fired", "uic": 307},  # no tag
            {"kind": "oco_placed", "uic": 307},  # unrelated kind
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertEqual(out, {})

    def test_a_same_pick_key_re_append_does_not_reset(self) -> None:
        # 2026-08-19 adjudication finding 4: the already_watching crash-recovery
        # re-drive re-journals the SAME pick's plan on every tick until the
        # retirement record lands — an identity-idempotent re-append must NOT
        # re-arm already-fired tranches (that would re-sell the remainder at
        # the tranche-0 target instead of laddering).
        lines = [
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_a_different_pick_key_resets_the_fired_set(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-08-01"},  # a NEW trade
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_a_keyless_plan_always_resets(self) -> None:
        # Bracket-path lines carry no pick_key and keep today's semantics:
        # every keyless plan line is a new trade.
        lines = [
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_consecutive_keyless_plans_both_reset(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},  # keyless == keyless must STILL reset
        ]
        out = sj._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)


class _JournalCase(IsolatedHomeTestCase):
    """Base case wiring a temp standalone-stop journal path per test."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        journal = Path(self._tmp.name) / "standalone_stops.jsonl"
        patch = mock.patch.object(sj, "_standalone_stop_journal_path", lambda: journal)
        patch.start()
        self.addCleanup(patch.stop)

    def _seed_tranche_plan(
        self,
        uic: int,
        *,
        tranches: tuple[TpTranchePlan, ...],
        reference_qty: float,
        stop_price: float,
        pick_key: str | None = None,
    ) -> None:
        sj._append_standalone_stop_journal(
            sj._build_tranche_plan_line(
                uic=uic,
                tp_tranches=tranches,
                reference_qty=reference_qty,
                stop_price=stop_price,
                pick_key=pick_key,
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
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_ALLOW_ORDERS_ENV: "1"}, clear=False):
            os.environ.pop(_LIVE_EXITS_ENV, None)
            with mock.patch.object(cl, "run_live_exits") as spy:
                report = cl.TickReport()
                cl._run_live_exits_pass(deps, report)
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
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "0"}):
            with mock.patch.object(cl, "run_live_exits") as spy:
                cl._run_live_exits_pass(deps, cl.TickReport())
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
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 50.0)
        sl = next(o for o in broker.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl.amount, 50.0)
        self.assertEqual(report.exits_placed, 1)
        fired = [
            line
            for line in sj._iter_standalone_stop_journal()
            if line.get("kind") == "tranche_fired"
        ]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["uic"], uic)
        self.assertEqual(fired[0]["tag"], "tp1")

    def test_a_full_close_retires_open_sibling_watches(self) -> None:
        # #1198 option B: the last tranche closes the position -> the pick's
        # still-open sibling entry watches are cancelled (journal terminal),
        # releasing their virtual gross reservation.
        import json as _json
        from pathlib import Path as _Path
        from tempfile import TemporaryDirectory

        from alphalens_pipeline.brokers.automanager import entry_trails

        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic,
            tranches=(_tr(0, 16.0, 1.0),),
            reference_qty=100.0,
            stop_price=13.0,
            pick_key="KO:2026-08-27",
        )
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        entry_path = _Path(tmp.name) / "entry_trails.jsonl"
        entry_path.write_text(
            _json.dumps(
                {
                    "kind": "watch_open",
                    "crid": "KO-2026-08-27-entry-t1",
                    "pick_key": "KO:2026-08-27",
                    "limit": 14.0,
                    "qty": 10.0,
                }
            )
            + "\n"
        )
        alerts: list[str] = []
        with mock.patch.object(entry_trails, "_entry_trail_journal_path", lambda: entry_path):
            deps = _deps(
                broker,
                alerts=alerts,
                live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            )
            with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
                cl._run_live_exits_pass(deps, cl.TickReport())
        cancelled = [
            _json.loads(ln)
            for ln in entry_path.read_text().splitlines()
            if _json.loads(ln).get("kind") == "cancelled"
        ]
        self.assertEqual([c["crid"] for c in cancelled], ["KO-2026-08-27-entry-t1"])
        self.assertTrue(any("retired 1 sibling" in a for a in alerts))
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 0.0)

    def test_a_partial_fire_leaves_sibling_watches_alone(self) -> None:
        import json as _json
        from pathlib import Path as _Path
        from tempfile import TemporaryDirectory

        from alphalens_pipeline.brokers.automanager import entry_trails

        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic,
            tranches=(_tr(0, 16.0, 0.5),),
            reference_qty=100.0,
            stop_price=13.0,
            pick_key="KO:2026-08-27",
        )
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        entry_path = _Path(tmp.name) / "entry_trails.jsonl"
        entry_path.write_text(
            _json.dumps(
                {
                    "kind": "watch_open",
                    "crid": "KO-2026-08-27-entry-t1",
                    "pick_key": "KO:2026-08-27",
                    "limit": 14.0,
                    "qty": 10.0,
                }
            )
            + "\n"
        )
        with mock.patch.object(entry_trails, "_entry_trail_journal_path", lambda: entry_path):
            deps = _deps(
                broker,
                alerts=[],
                live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            )
            with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
                cl._run_live_exits_pass(deps, cl.TickReport())
        kinds = [_json.loads(ln).get("kind") for ln in entry_path.read_text().splitlines()]
        self.assertNotIn("cancelled", kinds)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 50.0)

    def test_a_fired_tranche_sends_one_operator_alert(self) -> None:
        # #1219: exits must announce themselves — one throttled alert per fired
        # tranche, keyed per (uic, tag) so a repeat within the throttle window
        # cannot spam while a distinct tranche still notifies.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        seen: list[tuple[str, str]] = []
        deps = _deps(
            broker,
            alerts=[],
            alert_throttled=lambda msg, reason: seen.append((msg, reason)) or True,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
        self.assertEqual(len(seen), 1)
        msg, reason = seen[0]
        # #1621: the SELL was SENT; no fill was read back, so no price and no
        # "sold". Half of the position remains.
        self.assertRegex(
            msg,
            r"^SELL KO TP1 50 - take-profit, market sell sent - position still open "
            r"\(order \S+\)$",
        )
        self.assertEqual(reason, f"tranche-fired:{uic}:tp1")
        self.assertEqual(report.alerts, 1)

    def test_a_gap_through_price_fires_two_tranches_in_one_pass(self) -> None:
        # price crosses tp1 (16, 50%) AND tp2 (18, 30%) of a 100-share reference
        # in ONE pass. Guards the engine's cumulative-clamp + SL stepping on a
        # batch: the 2nd amend must land on LIVE owned, not a stale captured
        # sl_leg.amount (which would over-hedge the SL at 100-30=70).
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic,
            tranches=(_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)),
            reference_qty=100.0,
            stop_price=13.0,
        )
        alerts: list[str] = []
        deps = _deps(
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 18.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 20.0)  # sold 50 + 30
        sl = next(o for o in broker.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl.amount, 20.0)  # SL tracks the remaining owned, not a stale 70
        self.assertEqual(report.exits_placed, 2)
        fired_tags = {
            line["tag"]
            for line in sj._iter_standalone_stop_journal()
            if line.get("kind") == "tranche_fired"
        }
        self.assertEqual(fired_tags, {"tp1", "tp2"})

    def test_a_tranche_fire_amends_the_sl_at_the_trailed_level_not_the_plan_stop(self) -> None:
        # The partial-sale stop-reset hazard: with a trailing policy the SL has
        # been ratcheted ABOVE the placement-time stop; the tranche fire's SL
        # shrink must carry the ratcheted level, or TP1 firing would PATCH the
        # stop back down to the disaster level.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        sj._append_standalone_stop_journal(
            {"kind": "trailed", "uic": uic, "level": 14.5, "ts": 1.0}
        )
        seen_stop_prices: list[float] = []
        orig_amend = broker.amend_stop_amount

        def _recording_amend(*args: object, **kwargs: object):
            seen_stop_prices.append(float(kwargs["stop_price"]))  # type: ignore[arg-type]
            return orig_amend(*args, **kwargs)

        alerts: list[str] = []
        deps = _deps(
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            with mock.patch.object(broker, "amend_stop_amount", side_effect=_recording_amend):
                cl._run_live_exits_pass(deps, cl.TickReport())
        self.assertEqual(seen_stop_prices, [14.5])

    def test_a_prior_generation_trailed_level_does_not_reach_the_sl_amend(self) -> None:
        # A trailed marker journaled BEFORE the current position's tranche_plan
        # belongs to a PRIOR trade in the same uic — the amend must carry the
        # current plan's stop, never the stale (possibly absurdly high) level.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        sj._append_standalone_stop_journal(
            {"kind": "trailed", "uic": uic, "level": 44.0, "ts": 1.0}
        )
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        seen_stop_prices: list[float] = []
        orig_amend = broker.amend_stop_amount

        def _recording_amend(*args: object, **kwargs: object):
            seen_stop_prices.append(float(kwargs["stop_price"]))  # type: ignore[arg-type]
            return orig_amend(*args, **kwargs)

        alerts: list[str] = []
        deps = _deps(
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            with mock.patch.object(broker, "amend_stop_amount", side_effect=_recording_amend):
                cl._run_live_exits_pass(deps, cl.TickReport())
        self.assertEqual(seen_stop_prices, [13.0])

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
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: None}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
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
        sj._append_standalone_stop_journal({"kind": "tranche_fired", "uic": uic, "tag": "tp1"})
        alerts: list[str] = []
        deps = _deps(
            broker,
            alerts=alerts,
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 50.0)
        self.assertEqual(report.exits_placed, 0)

    def test_no_managed_positions_is_a_quiet_no_op(self) -> None:
        broker = FakeBroker()
        alerts: list[str] = []
        deps = _deps(
            broker, alerts=alerts, live_exits_feed_factory=lambda m, *, scope: _FakeFeed({})
        )
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            with mock.patch.object(cl, "run_live_exits") as spy:
                cl._run_live_exits_pass(deps, cl.TickReport())
                spy.assert_not_called()


class TestLiveExitsFeedFactoryReceivesTheVenue(_JournalCase):
    """Fix round 2 (Task 7 review), finding 2: the venue is the entire reason
    this task exists -- resolving a LIVE instrument by bare ticker is
    ambiguous for a cross-listed name. Every other test in this module injects
    a feed factory that ignores its argument; this is the only one that pins
    the SHAPE _run_live_exits_pass hands to the factory."""

    def test_the_factory_receives_uic_to_ticker_and_venue_tuples(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        received: list[dict[int, tuple[str, str]]] = []
        scopes: list[str] = []

        def capturing_factory(uic_to_instrument, *, scope):
            received.append(dict(uic_to_instrument))
            scopes.append(scope)
            return _FakeFeed({uic: 16.5})

        alerts: list[str] = []
        deps = _deps(broker, alerts=alerts, live_exits_feed_factory=capturing_factory)
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            cl._run_live_exits_pass(deps, cl.TickReport())
        # FakeBroker's _instrument fixture sets exchange_mic="XNYS".
        self.assertEqual(received, [{uic: ("KO", "XNYS")}])
        # And the exits pass must claim ITS scope of the shared subscription
        # (2026-08-18 churn fix), never the entry-watch one.
        self.assertEqual(scopes, ["exits"])


class TestLiveExitsScopeMaintenance(_JournalCase):
    """Follow-up to the 2026-08-18 churn fix: the pass must keep its "exits"
    slice of the shared price-stream subscription in step with the live long
    positions EVERY tick it runs — including the quiet ticks with no managed
    position. Returning before the feed build left the scope holding closed
    positions' uics forever (since #1587 the peak updater writes its own
    "peaks" scope, so this pass is the only "exits"-scope writer): the union
    never shrank, so the reader kept a WebSocket plus a server-side
    subscription streaming uics nobody reads."""

    def _capturing_factory(self, calls: list[tuple[dict[int, tuple[str, str]], str]]) -> object:
        def factory(uic_to_instrument: Mapping[int, tuple[str, str]], *, scope: str) -> _FakeFeed:
            calls.append((dict(uic_to_instrument), scope))
            return _FakeFeed({})

        return factory

    def test_no_positions_releases_the_exits_scope(self) -> None:
        broker = FakeBroker()  # zero positions -> zero managed exits
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = _deps(broker, alerts=[], live_exits_feed_factory=self._capturing_factory(calls))
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            cl._run_live_exits_pass(deps, cl.TickReport())
        self.assertEqual(calls, [({}, "exits")])

    def test_disabled_gate_releases_the_exits_scope(self) -> None:
        # Toggling the feature off must not freeze the scope on its last
        # uics: while disabled the gate is the only code that runs, so it
        # owns the release — mirroring the entry-watch pass's feature-off
        # release. Without it, disabling live exits under a non-trailing
        # policy leaves the last positions' uics subscribed forever.
        broker = FakeBroker()
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = _deps(broker, alerts=[], live_exits_feed_factory=self._capturing_factory(calls))
        with mock.patch.dict(os.environ, {_ALLOW_ORDERS_ENV: "1"}, clear=False):
            os.environ.pop(_LIVE_EXITS_ENV, None)
            cl._run_live_exits_pass(deps, cl.TickReport())
        self.assertEqual(calls, [({}, "exits")])

    def test_unmanaged_long_position_keeps_the_scope_on_the_position_uics(self) -> None:
        # A long position with NO tranche plan folds to zero managed exits,
        # but the scope must stay on the open-position uics. An empty write
        # here would drop its quote from the shared subscription, and every
        # later write that re-adds it re-creates the server-side subscription.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        calls: list[tuple[dict[int, tuple[str, str]], str]] = []
        deps = _deps(broker, alerts=[], live_exits_feed_factory=self._capturing_factory(calls))
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            with mock.patch.object(cl, "run_live_exits") as spy:
                cl._run_live_exits_pass(deps, cl.TickReport())
                spy.assert_not_called()
        self.assertEqual(calls, [({uic: ("KO", "XNYS")}, "exits")])


def _raising_factory(uic_to_instrument: object, *, scope: str) -> object:
    raise RuntimeError("boom: cannot reach Saxo LIVE auth")


class TestLiveExitsFeedConstructionBoundary(IsolatedHomeTestCase):
    """Fix round 1 (Task 7 review): the price-feed factory may reach out to
    real Saxo LIVE auth/REST/streaming machinery this pass has no control
    over -- a missing env var or an unbootstrapped token store is the single
    most likely rollout mistake. A construction failure must degrade to a
    vetoing feed, exactly like an OFF flag or a stale quote, never crash the
    tick and starve the never-naked protection pass that runs right after
    it."""

    def test_construction_failure_degrades_to_a_vetoing_feed(self) -> None:
        alerts: list[str] = []
        deps = _deps(broker=object(), alerts=alerts, live_exits_feed_factory=_raising_factory)
        report = cl.TickReport()
        feed = cl._build_live_exits_feed(deps, {211: ("AAPL", "XNYS")}, report)
        self.assertIsNone(feed.latest(211))
        self.assertEqual(len(alerts), 1)
        self.assertIn("live-exits", alerts[0])
        self.assertEqual(report.alerts, 1)

    def test_construction_failure_alerts_once_not_once_per_tick(self) -> None:
        """The two ticks raise DIFFERENT exception messages (a stale token
        one tick, a DNS failure the next -- both real, both possible from the
        same misconfiguration). If the throttle key were built from the
        exception message instead of a fixed reason string, this would still
        alert twice -- in production that would page every ~45s instead of
        once per re-alert interval."""
        alerts: list[str] = []
        seen_reasons: set[str] = set()

        def throttled(msg: str, reason: str) -> bool:
            if reason in seen_reasons:
                return False
            seen_reasons.add(reason)
            alerts.append(msg)
            return True

        def raising_factory_tick_1(uic_to_instrument: object, *, scope: str) -> object:
            raise RuntimeError("boom: cannot reach Saxo LIVE auth")

        def raising_factory_tick_2(uic_to_instrument: object, *, scope: str) -> object:
            raise RuntimeError("boom: token store corrupt")

        report = cl.TickReport()
        deps_1 = _deps(
            broker=object(),
            alerts=alerts,
            live_exits_feed_factory=raising_factory_tick_1,
            alert_throttled=throttled,
        )
        cl._build_live_exits_feed(deps_1, {211: ("AAPL", "XNYS")}, report)  # tick 1
        deps_2 = _deps(
            broker=object(),
            alerts=alerts,
            live_exits_feed_factory=raising_factory_tick_2,
            alert_throttled=throttled,
        )
        cl._build_live_exits_feed(deps_2, {211: ("AAPL", "XNYS")}, report)  # tick 2
        self.assertEqual(len(alerts), 1)
        self.assertEqual(report.alerts, 1)

    def test_flag_off_builds_nothing_and_alerts_nothing(self) -> None:
        """Guards the OFF path against this fix: no exception is ever raised
        when the flag is off (the default factory returns _NullPriceFeed
        directly), so the new boundary must stay silent -- not swallow a
        real construction failure, but not manufacture a phantom one either."""
        alerts: list[str] = []
        deps = _deps(broker=object(), alerts=alerts)  # live_exits_feed_factory=None -> default
        report = cl.TickReport()
        with mock.patch.dict(os.environ, {}, clear=True):
            feed = cl._build_live_exits_feed(deps, {211: ("AAPL", "XNYS")}, report)
        self.assertIsNone(feed.latest(211))
        self.assertEqual(alerts, [])
        self.assertEqual(report.alerts, 0)


class TestLiveExitsFeedConstructionFailureEndToEnd(_JournalCase):
    def test_the_tick_completes_normally_when_feed_construction_raises(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        alerts: list[str] = []
        deps = _deps(broker, alerts=alerts, live_exits_feed_factory=_raising_factory)
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)  # must not raise
        # No broker mutation: the degraded feed vetoed every uic, same as a stale quote.
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 100.0)
        sl = next(o for o in broker.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl.amount, 100.0)
        self.assertEqual(report.exits_placed, 0)
        self.assertEqual(len(alerts), 1)


class TestLiveExitsRunsBeforeProtection(IsolatedHomeTestCase):
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


class TestOpenPositionsStayManagedWhenTheTrailIsDisabled(_JournalCase):
    """#1116 round 2, point 4, the safety half.

    With the exit geometry active and ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` at 0,
    the daemon refuses to ARM a new entry (pinned in ``test_entry_watch_wiring``).
    It must NOT stop managing what is already open — a refusal that reached this
    pass would strand live positions with no take-profit path, which is worse
    than the un-gated entry the refusal exists to prevent.
    """

    _ENTRY_TRAIL_ENV = "ALPHALENS_BROKER_ENTRY_TRAIL_BPS"

    def test_a_touched_tranche_still_fires_with_the_entry_trail_off(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 16.0, 1.0),), reference_qty=100.0, stop_price=13.0
        )
        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
        )
        with mock.patch.dict(
            os.environ,
            {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1", self._ENTRY_TRAIL_ENV: "0"},
        ):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
        self.assertEqual(report.exits_placed, 1)
        self.assertEqual(broker.get_positions_by_uic(uic).quantity, 0.0)


class TestStopRaisePagesTheOperator(_JournalCase):
    """#1621 part 3 (A): a stop that RAISES must reach the operator.

    On 2026-09-29 an ASTS stop moved 50.64 -> 62.42, locking in +0.36R, and the
    first message the owner saw was the fill hours later. The raise was already
    computed and already de-noised once per level in ``_journaled_stop_floor``;
    its only sink was ``logger.info``, which on the VPS means journald and
    nobody.

    The emit is the CALLER's, not the pure build's: ``_build_managed_exits``
    holds no alert sink and knows no tickers (the ``_announce_fired_tranches``
    precedent), so the raise travels out on ``raised`` and is rendered here.
    """

    _PLAN_STOP = 13.0
    _TRAILED = 14.5

    def _pages(self) -> tuple[list[tuple[str, str]], cl.TickReport, int]:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        # A target far above the feed price, so the only thing this tick does is
        # the raise — the tranche-fired page would otherwise confound the count.
        self._seed_tranche_plan(
            uic,
            tranches=(_tr(0, 99.0, 0.5),),
            reference_qty=100.0,
            stop_price=self._PLAN_STOP,
            pick_key="KO:2026-09-29",
        )
        sj._journal_trailed(uic, self._TRAILED, peak=14.9, last_price=14.8)
        pages: list[tuple[str, str]] = []
        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            alert_throttled=lambda msg, reason: pages.append((msg, reason)) or True,
        )
        report = cl.TickReport()
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            cl._run_live_exits_pass(deps, report)
        # The pass QUEUES the page; ``run_once`` drains after the protection
        # pass, so nothing reaches the sink inside the pass itself. That
        # ordering has its own test below.
        cl._drain_pages(report)
        return pages, report, uic

    def test_the_page_carries_the_ticker_the_label_and_both_levels(self) -> None:
        pages, _report, _uic = self._pages()
        raised = [msg for msg, _key in pages if "raised" in msg]
        self.assertEqual(len(raised), 1, f"exactly one raise page; got {pages}")
        self.assertEqual(raised[0], "TRAIL KO SL - trailed stop resting 14.50 (raised from 13.00)")

    def test_the_page_is_counted_in_the_report(self) -> None:
        _pages, report, _uic = self._pages()
        self.assertEqual(report.alerts, 1)

    def test_a_restart_re_pages_the_same_raise_and_the_text_is_still_true(self) -> None:
        # Both suppressors are PROCESS-local: ``stop_raises_announced`` is a
        # field on the per-daemon deps and the throttle's last-sent map is built
        # per process. So a deploy or one `Restart=on-failure` bounce re-derives
        # the held raise from the journal and pages it AGAIN. The decision was to
        # word the page so that repeat is not a lie, rather than to persist the
        # announce map: the page states where the stop RESTS, which is true
        # whether it just moved or moved on Monday, and still carries the level
        # it came from.
        #
        # A past-tense event sentence here read as "the stop just jumped 1.50",
        # which is the #1621 point-7 defect class in the page part 3 adds to fix
        # point 6.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 99.0, 0.5),), reference_qty=100.0, stop_price=self._PLAN_STOP
        )
        sj._journal_trailed(uic, self._TRAILED, peak=14.9, last_price=14.8)
        pages: list[tuple[str, str]] = []

        def _a_fresh_daemon() -> cl.LoopDeps:
            """What a restart leaves: an empty announce map and a new throttle."""
            return _deps(
                broker,
                alerts=[],
                live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
                alert_throttled=lambda msg, reason: pages.append((msg, reason)) or True,
            )

        def _tick(deps: cl.LoopDeps) -> None:
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
            cl._drain_pages(report)

        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            _tick(_a_fresh_daemon())
            second = _a_fresh_daemon()  # the daemon restarted; nothing was persisted
            _tick(second)

            raised = [msg for msg, _key in pages if "resting" in msg]
            self.assertEqual(len(raised), 2, f"the restart re-pages; got {pages}")
            self.assertEqual(raised[0], raised[1], "the same fact must read the same way twice")
            self.assertIn("resting 14.50", raised[1], "a state line, true on a restart echo")

            # And the "from" level after a restart is the PLAN stop, not the
            # level the previous process announced -- there is nowhere to read
            # that from. Second phase: the SAME process sees a further raise to
            # 15.50 and reads "from" off its own announce map, then the daemon
            # restarts and re-derives. The echo says "resting 15.50 (raised from
            # 13.00)", which the state wording keeps TRUE -- the stop does rest
            # at 15.50, and over this position's life it did come from 13.00.
            # The event wording would have claimed a 2.50 jump this instant.
            sj._journal_trailed(uic, 15.5, peak=15.9, last_price=15.8)
            _tick(second)
            _tick(_a_fresh_daemon())

        resting = [msg for msg, _key in pages if "resting" in msg]
        self.assertEqual(
            resting[2:],
            [
                "TRAIL KO SL - trailed stop resting 15.50 (raised from 14.50)",
                "TRAIL KO SL - trailed stop resting 15.50 (raised from 13.00)",
            ],
            f"within a process it reads from the announced level, after a restart "
            f"from the plan stop; got {resting}",
        )

    def test_the_throttle_key_carries_the_uic_and_the_level(self) -> None:
        # The level is IN the key on purpose. Each distinct level is a distinct
        # fact (it says how much was locked in), and the orphan-key lesson of
        # #1766 is that a key built from less than the distinguishing facts
        # pages for the first event and HIDES the rest. The once-per-level
        # ``announced`` guard already suppresses the steady state, so the only
        # thing a level-less key could add is suppressing a SECOND, higher
        # raise — the page the operator most wants.
        pages, _report, uic = self._pages()
        keys = [key for msg, key in pages if "raised" in msg]
        self.assertEqual(keys, [f"stop-raised:{uic}:14.500000"])

    def test_a_re_anchored_floor_pages_its_OWN_cause(self) -> None:
        # Every other fixture in this class journals only a `trailed` level, so
        # the cause column is uniform and the page cannot tell a real read of
        # the winning floor from a constant: hard-coding
        # `reason=ExitReason.TRAILED_STOP` on the `StopRaise` left all 3588
        # broker tests green, as did having the re-anchored arm report the
        # trailed cause, as did `raises[0]` in place of `raises[-1]`.
        #
        # Here the only journaled floor is a RE-ANCHORED one, so the two arms
        # are distinguishable, and the second case drives BOTH arms in one tick
        # with the re-anchor higher -- which is what pins `raises[-1]`, the rule
        # that the LAST arm to fire owns the level actually placed.
        for label, trailed, expected in (
            (
                "re-anchor alone",
                None,
                "TRAIL KO SL - re-anchored stop resting 15.00 (raised from 13.00)",
            ),
            (
                "both arms, the re-anchor last",
                14.0,
                "TRAIL KO SL - re-anchored stop resting 15.00 (raised from 13.00)",
            ),
        ):
            with self.subTest(label):
                broker = FakeBroker()
                uic = broker.uic_of("KO")
                broker.set_position("KO", 100, avg_price=15.0)
                broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
                self._seed_tranche_plan(
                    uic,
                    tranches=(_tr(0, 99.0, 0.5),),
                    reference_qty=100.0,
                    stop_price=self._PLAN_STOP,
                )
                if trailed is not None:
                    sj._journal_trailed(uic, trailed, peak=16.0, last_price=15.9)
                sj._journal_reanchored(uic, avg_price=15.0, stop_price=15.0)
                pages: list[tuple[str, str]] = []
                deps = _deps(
                    broker,
                    alerts=[],
                    live_exits_feed_factory=lambda m, *, scope, uic=uic: _FakeFeed({uic: 16.5}),
                    alert_throttled=(
                        lambda msg, reason, pages=pages: pages.append((msg, reason)) or True
                    ),
                )
                report = cl.TickReport()
                with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
                    cl._run_live_exits_pass(deps, report)
                cl._drain_pages(report)
                self.assertEqual([msg for msg, _k in pages if "resting" in msg], [expected])

    def test_a_level_that_does_not_raise_pages_nothing(self) -> None:
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 99.0, 0.5),), reference_qty=100.0, stop_price=self._PLAN_STOP
        )
        sj._journal_trailed(uic, 1.0, peak=1.1, last_price=1.0)  # below the plan stop
        pages: list[tuple[str, str]] = []
        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            alert_throttled=lambda msg, reason: pages.append((msg, reason)) or True,
        )
        report = cl.TickReport()
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            cl._run_live_exits_pass(deps, report)
        cl._drain_pages(report)
        self.assertEqual([msg for msg, _key in pages if "raised" in msg], [])

    def test_a_held_raise_pages_once_not_on_every_tick(self) -> None:
        # The guard that already exists (`stop_raises_announced`) is the one
        # that must do this. Duplicating it here, or ignoring it, would page
        # once per tick for the whole session — the LIVE VST 2026-10-01 shape.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 99.0, 0.5),), reference_qty=100.0, stop_price=self._PLAN_STOP
        )
        sj._journal_trailed(uic, self._TRAILED, peak=14.9, last_price=14.8)
        pages: list[tuple[str, str]] = []
        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            alert_throttled=lambda msg, reason: pages.append((msg, reason)) or True,
        )

        def _tick() -> None:
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)
            cl._drain_pages(report)

        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            _tick()
            _tick()
            self.assertEqual(len([m for m, _k in pages if "raised" in m]), 1)
            # A NEW level is a new fact and pages again.
            sj._journal_trailed(uic, 15.5, peak=15.9, last_price=15.8)
            _tick()
        raised = [m for m, _k in pages if "raised" in m]
        self.assertEqual(len(raised), 2, f"the second level pages too; got {raised}")
        # The second page reads from the level the FIRST one announced, not from
        # the plan stop: that level is the one resting at the broker, so
        # "13.00 -> 15.50" would overstate the move the operator just missed.
        self.assertIn("resting 15.50 (raised from 14.50)", raised[1])

    def test_the_raise_is_queued_before_the_engine_so_an_engine_failure_cannot_eat_it(
        self,
    ) -> None:
        # The PR's headline ordering decision, which nothing pinned: moving the
        # announce call to the END of the pass -- behind `if not managed: return`
        # and behind `except BrokerError: return` -- left all 3588 broker tests
        # green.
        #
        # The raise is a JOURNAL fact and the once-per-level guard inside the
        # build has ALREADY consumed it, so a page the engine's failure skips is
        # lost for good rather than sent late. Here the engine raises, the pass
        # returns early, and the page must nonetheless be on the queue that
        # `run_once` drains in its `finally`.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 99.0, 0.5),), reference_qty=100.0, stop_price=self._PLAN_STOP
        )
        sj._journal_trailed(uic, self._TRAILED, peak=14.9, last_price=14.8)
        pages: list[tuple[str, str]] = []
        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            alert_throttled=lambda msg, reason: pages.append((msg, reason)) or True,
        )
        report = cl.TickReport()
        with mock.patch.object(cl, "run_live_exits", side_effect=BrokerError("engine down")):
            with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
                cl._run_live_exits_pass(deps, report)

        self.assertEqual(
            [what for what, _send in report.pending_pages],
            ["stop-raised"],
            "the raise was computed and queued before the engine ran",
        )
        self.assertEqual(
            [msg for msg, _key in pages if "resting" in msg],
            [],
            "and the page did not reach the sink inside the pass: the feed-scope "
            "write, the engine and the whole protection pass sit below that call, "
            "and the sink blocks for tens of seconds per page. (The pass's own "
            "engine-failure alert is pre-existing and must go out.)",
        )
        cl._drain_pages(report)
        self.assertEqual(
            [msg for msg, _key in pages if "resting" in msg],
            ["TRAIL KO SL - trailed stop resting 14.50 (raised from 13.00)"],
            "and the operator is told, even though the engine failed",
        )

    def test_an_alert_sink_that_raises_does_not_stop_the_drain_or_the_tick(self) -> None:
        # Observability may never endanger the money path. The drain runs after
        # the protection pass, so an uncontained sink failure there would abort
        # the tick between the pass and the rest of ``run_once`` and would also
        # cost the pages of the raises behind it. The tranche target stays out of
        # reach here so the only page is the raise: the tranche-fired page is
        # pre-existing and uncontained, and widening its containment is not this
        # change.
        broker = FakeBroker()
        uic = broker.uic_of("KO")
        broker.set_position("KO", 100, avg_price=15.0)
        broker.add_resting_sell("KO", 100, self._PLAN_STOP, order_type="StopIfTraded")
        self._seed_tranche_plan(
            uic, tranches=(_tr(0, 99.0, 0.5),), reference_qty=100.0, stop_price=self._PLAN_STOP
        )
        sj._journal_trailed(uic, self._TRAILED, peak=14.9, last_price=14.8)

        def _explode(message: str, reason: str) -> bool:
            raise RuntimeError("telegram down")

        deps = _deps(
            broker,
            alerts=[],
            live_exits_feed_factory=lambda m, *, scope: _FakeFeed({uic: 16.5}),
            alert_throttled=_explode,
        )
        report = cl.TickReport()
        with mock.patch.object(cl, "run_live_exits", return_value=[]) as engine:
            with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
                with self.assertLogs(cl.logger, level="WARNING") as captured:
                    cl._run_live_exits_pass(deps, report)  # must not raise
                    cl._drain_pages(report)  # nor must this
        engine.assert_called_once()
        self.assertEqual(report.alerts, 0, "a page that never went out is not counted")
        self.assertTrue(
            any("stop-raised page failed" in line for line in captured.output),
            f"the swallowed failure must still be visible; got {captured.output}",
        )


class TestTheFromLevelOnAStopRaise(unittest.TestCase):
    """Which level the page quotes as "from", straight off
    ``_journaled_stop_floor`` -- reachable with an ``announced`` dict and a
    ``raised`` list, no broker and no journal.

    The choice used to be one condition with two halves,
    ``plan_stop <= last_announced < stop_price``, and either half could be
    deleted on its own with the whole broker suite green: they masked each
    other, so neither was pinned. One comparison decides the level now, and the
    formatter decides the DIRECTION word, and each has its own case below."""

    _UIC = 40000

    def _raise(self, *, plan_stop: float, trailed: float, announced: float | None) -> cl.StopRaise:
        raised: list[cl.StopRaise] = []
        cl._journaled_stop_floor(
            self._UIC,
            plan_stop,
            trailed,
            None,
            {} if announced is None else {self._UIC: announced},
            raised,
        )
        self.assertEqual(len(raised), 1, "the floor did raise the stop")
        return raised[0]

    def test_the_level_resting_at_the_broker_beats_the_plan_stop(self) -> None:
        # The announced level is the one resting at the venue, so quoting the
        # plan stop would overstate the move the operator just missed.
        self.assertEqual(self._raise(plan_stop=13.0, trailed=15.5, announced=14.5).previous, 14.5)

    def test_the_plan_stop_wins_when_nothing_was_announced_yet(self) -> None:
        # The first raise of a position, and the first raise after a restart.
        self.assertEqual(self._raise(plan_stop=13.0, trailed=14.5, announced=None).previous, 13.0)

    def test_an_announced_level_BELOW_the_plan_stop_is_not_quoted(self) -> None:
        # A new-generation plan can carry a HIGHER disaster stop than the level
        # the previous generation announced. The resting stop is then the plan
        # stop, so quoting 12.00 would overstate what the move locked in.
        self.assertEqual(self._raise(plan_stop=13.0, trailed=14.5, announced=12.0).previous, 13.0)

    def test_a_stop_driven_DOWN_from_the_announced_level_says_so(self) -> None:
        # The case worth waking up for, and the one the old guard silently
        # relabelled: it fell back to the plan stop, so a stop moving 14.50 ->
        # 14.00 -- a ratchet floor LOST, the #1324 hazard this code warns about
        # -- was reported as a raise from 13.00. The level is carried honestly
        # and the formatter names the direction.
        raise_ = self._raise(plan_stop=13.0, trailed=14.0, announced=14.5)
        self.assertEqual(raise_.previous, 14.5)
        event = ta.TradeEvent(
            kind=ta.EventKind.STOP_RAISED,
            ticker="OLN",
            label="SL",
            reason=raise_.reason,
            level=raise_.level,
            previous_level=raise_.previous,
        )
        self.assertEqual(
            ta.render(event), "TRAIL OLN SL - trailed stop resting 14.00 (lowered from 14.50)"
        )


class TestLiveExitsPassCapabilityGuard(IsolatedHomeTestCase):
    """#1141: the pass narrows deps.broker to the engine's requirement set
    (LiveExitBroker) BEFORE any broker call. A non-conforming broker — possible
    only when build_default_deps and its boot gate were bypassed, i.e. in tests
    — skips the pass with a throttled alert. It must NEVER surface as an
    AttributeError: the pass's try catches only BrokerError, so an
    AttributeError would escape and starve the protection pass that follows in
    the same tick."""

    def test_broker_without_the_capability_set_skips_with_alert(self) -> None:
        class _NoReadsBroker:
            name = "noreads"

        alerts: list[str] = []
        deps = _deps(_NoReadsBroker(), alerts=alerts)
        with mock.patch.dict(os.environ, {_LIVE_EXITS_ENV: "1", _ALLOW_ORDERS_ENV: "1"}):
            report = cl.TickReport()
            cl._run_live_exits_pass(deps, report)  # must not raise
        self.assertTrue(
            any("live-exit capability" in msg for msg in alerts),
            f"expected the capability-skip alert, got: {alerts}",
        )
        self.assertEqual(report.exits_placed, 0)


if __name__ == "__main__":
    unittest.main()
