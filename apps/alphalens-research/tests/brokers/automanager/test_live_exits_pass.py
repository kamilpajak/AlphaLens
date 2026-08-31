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


class TestFoldFiredSinceLatestPlan(unittest.TestCase):
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
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_a_new_tranche_plan_line_resets_the_uics_fired_set(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},  # re-entry: the OLD trade's fired tags reset
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_fired_tags_after_the_reset_still_accumulate(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_distinct_uics_reset_independently(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 1},
            {"kind": "tranche_fired", "uic": 1, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 2},
            {"kind": "tranche_fired", "uic": 2, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 1},  # only uic 1 resets
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertNotIn(1, out)
        self.assertEqual(out[2], frozenset({"tp1"}))

    def test_malformed_lines_are_skipped(self) -> None:
        lines = [
            {"kind": "tranche_fired", "tag": "tp1"},  # no uic
            {"kind": "tranche_fired", "uic": 307},  # no tag
            {"kind": "oco_placed", "uic": 307},  # unrelated kind
        ]
        out = cl._fold_fired_since_latest_plan(lines)
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
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertEqual(out[307], frozenset({"tp1"}))

    def test_a_different_pick_key_resets_the_fired_set(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-08-01"},  # a NEW trade
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_a_keyless_plan_always_resets(self) -> None:
        # Bracket-path lines carry no pick_key and keep today's semantics:
        # every keyless plan line is a new trade.
        lines = [
            {"kind": "tranche_plan", "uic": 307, "pick_key": "KO:2026-07-20"},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)

    def test_consecutive_keyless_plans_both_reset(self) -> None:
        lines = [
            {"kind": "tranche_plan", "uic": 307},
            {"kind": "tranche_fired", "uic": 307, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": 307},  # keyless == keyless must STILL reset
        ]
        out = cl._fold_fired_since_latest_plan(lines)
        self.assertNotIn(307, out)


class _JournalCase(unittest.TestCase):
    """Base case wiring a temp standalone-stop journal path per test."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        journal = Path(self._tmp.name) / "standalone_stops.jsonl"
        patch = mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal)
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
        cl._append_standalone_stop_journal(
            cl._build_tranche_plan_line(
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
            for line in cl._iter_standalone_stop_journal()
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
        self.assertIn("KO", msg)
        self.assertIn("TP1", msg)
        self.assertIn("50", msg)
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
            for line in cl._iter_standalone_stop_journal()
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
        cl._append_standalone_stop_journal(
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
        cl._append_standalone_stop_journal(
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
        cl._append_standalone_stop_journal({"kind": "tranche_fired", "uic": uic, "tag": "tp1"})
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
    positions' uics forever under a non-trailing exit policy (the peak
    updater, the only other "exits"-scope writer, runs only when the policy
    trails): the union never shrank, so the reader kept a WebSocket plus a
    server-side subscription streaming uics nobody reads."""

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
        # but the scope must stay on the open-position uics — the same set the
        # trailing peak updater writes. An empty write here would flip-flop
        # the shared subscription against the peak updater every tick,
        # reintroducing the churn the scope split exists to kill.
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


class TestLiveExitsFeedConstructionBoundary(unittest.TestCase):
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


class TestLiveExitsPassCapabilityGuard(unittest.TestCase):
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
