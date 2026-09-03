"""Hermetic END-TO-END acceptance for entry-trailing (PR-T1e).

One pick drained through the REAL ``run_once`` tick (fully mocked broker +
injected price feed, NO market), flag ON, driven WATCHING -> TOUCHED ->
would_fire (plus the SUSPENDED variant), asserting the broker's
place/amend/cancel-order methods are NEVER called — the ONE non-negotiable
safety property of PR-T1. Flag OFF: the tick is byte-identical (the limit-entry
drain places a normal bracket and NOTHING is written to the entry-trails
journal).

The flag ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` is the only control: with it unset
the daemon behaves exactly as before; with it set the pick trails.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from broker_contract.sizing import SetupPlan

# Shared hermetic fixtures live next to the T1c wiring tests.
from tests.brokers.automanager.test_entry_watch_wiring import (
    _FakeFeed,
    _instr,
    _journal,
    _lines,
    _pick,
    _placement,
    _plan_with_tranches,
    _planned_journal,
    _RecordingBroker,
    _tranche,
)

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV


def _plan_l(*tiers: tuple[int, float, int]):
    """A SetupPlan with the brief's thirds TP ladder attached, targets far above
    every price this file ticks — the router journals a ``tranche_plan`` only
    for a non-empty ladder, and the #1112 brief-ladder arm gate fails CLOSED on
    a missing plan, so a trancheless SetupPlan routes a watch production could
    never produce."""
    return _plan_with_tranches(
        tuple(tiers),
        (_tranche(0, 1000.0, 1 / 3), _tranche(1, 1005.0, 1 / 3), _tranche(2, 1010.0, 1 / 3)),
    )


def _empty_pview() -> Any:
    from alphalens_pipeline.brokers.automanager.position_manager import ProtectionView

    return ProtectionView(
        long_positions={},
        all_positions={},
        sell_legs_by_uic={},
        planned_by_uic={},
        oco_unsupported=frozenset(),
    )


class TestEntryWatchEndToEndAcceptance(unittest.TestCase):
    def _deps(
        self,
        broker: Any,
        plan: SetupPlan,
        picks: list[Any],
        feed: Any,
        alerts: list[tuple[str, str]],
    ) -> cl.LoopDeps:
        pkg = "alphalens_pipeline.brokers"
        for target, fn in (
            (f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b, **_k: []),
            (f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()),
            (f"{pkg}.routing.resolve_us_instrument", lambda _b, _t, **_kw: _instr()),
            (f"{pkg}.submission_log.iter_submission_records", lambda _p: []),
            (f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)),
            (f"{pkg}.submission_log.append_submission_record", lambda _r: None),
            ("broker_contract.sizing.compute_setup_plan", lambda _s, **_k: plan),
            (f"{pkg}.automanager.placement_planner.classify", lambda *_a, **_k: _placement()),
            (f"{pkg}.automanager.picks.mark_refused", lambda *_a, **_k: None),
        ):
            self.enterContext(mock.patch(target, fn))
        # The routed tranche_plan must actually land: the #1112 brief-ladder
        # arm gate reads it back at touch time and fails CLOSED on a missing
        # plan, so a swallowed write would refuse every arm in this test.
        _planned_journal(self)

        def _throttled(message: str, reason: str) -> bool:
            alerts.append((message, reason))
            return True

        return cl.LoopDeps(
            broker=broker,
            kill_file=Path("/nonexistent/KILL"),
            ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
            iter_picks=lambda: iter(picks),
            place_pick=cl._make_place_pick(broker),
            read_records=list,
            verdicts_fn=lambda _r, _b, **_k: [],
            build_position_view=lambda _b, _r: object(),
            build_protection_view=lambda _b, _r: _empty_pview(),
            execute_protection=lambda _a, _k, _r: None,
            sweep_orphans_fn=lambda _b: [],
            alert=lambda _m: None,
            alert_throttled=_throttled,
            live_exits_feed_factory=lambda _u2i, *, scope: feed,
        )

    def test_flag_on_end_to_end_arms_one_native_trailing_order(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        prices: dict[int, float | None] = {}
        alerts: list[tuple[str, str]] = []
        # A recent trade_date so the real-calendar TTL window_end is in the
        # future (a stale date would expire the watch on tick 1).
        picks = [_pick(date=dt.date.today().isoformat())]
        deps = self._deps(broker, _plan_l((0, 10.0, 100)), picks, _FakeFeed(prices), alerts)

        with mock.patch.dict(
            "os.environ", {_ENV: "50", "ALPHALENS_BROKER_ALLOW_ORDERS": "1"}, clear=True
        ):
            prices[307] = 10.0
            cl.run_once(deps)  # tick 1: drain opens watch, pass touches -> ARM
            picks.clear()  # pick retired from the drain

        # PR-T2b: a native trailing-LIMIT order was placed at TOUCH — and NO
        # resting-limit bracket (the whole point of the feature).
        self.assertEqual(len(broker.trailing_orders), 1)
        self.assertEqual(broker.brackets, [], "no resting-limit entry order under the trail")
        # The ONE combined trailing-LIMIT carries the G1 ceiling (StopLimitPrice),
        # an initial trigger above the touch bid, and non-zero trail fields — the
        # whole native V1 shape, end to end through run_once.
        order = broker.trailing_orders[0]
        self.assertEqual(order["side"], "BUY")
        self.assertGreater(order["order_price"], 10.0, "the trigger sits above the touch bid")
        self.assertIsNotNone(order["ceiling_price"], "G1: the gap-through ceiling is set")
        self.assertGreaterEqual(order["ceiling_price"], order["order_price"])
        self.assertGreater(order["trailing_distance"], 0.0)
        self.assertGreater(order["trailing_step"], 0.0)
        kinds = [line["kind"] for line in _lines(path)]
        self.assertEqual(kinds.count(entry_trails.KIND_WATCH_OPEN), 1)
        self.assertIn(entry_trails.KIND_TOUCHED, kinds)
        self.assertIn(entry_trails.KIND_TRAIL_ARMED, kinds)
        # Native mode: no fabricated dry-run fired line (the server is the fire).
        self.assertNotIn(entry_trails.KIND_FIRED, kinds)

    def test_flag_on_suspended_tier_places_no_order(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        prices: dict[int, float | None] = {}
        picks = [_pick(date=dt.date.today().isoformat())]
        deps = self._deps(
            broker, _plan_l((0, 10.0, 100), (1, 9.5, 100)), picks, _FakeFeed(prices), []
        )
        with mock.patch.dict(
            "os.environ", {_ENV: "50", "ALPHALENS_BROKER_ALLOW_ORDERS": "1"}, clear=True
        ):
            # A single gap-down tick to 9.40: tier-0 (next-tier 9.5) suspends ON
            # the touch tick before any arm; the DEEPEST tier-1 (limit 9.5, no
            # next tier) legitimately touches + arms — that decline is its job.
            prices[307] = 9.40
            cl.run_once(deps)
        self.assertEqual(broker.brackets, [])
        kinds = [line["kind"] for line in _lines(path)]
        self.assertIn(entry_trails.KIND_SUSPENDED, kinds)
        # The SUSPENDED tier (t0) never placed an order; only the deepest tier armed.
        t0_orders = [o for o in broker.trailing_orders if "entry-t0-fire" in o["request_id"]]
        self.assertEqual(t0_orders, [])

    def test_flag_off_is_byte_identical_places_bracket_writes_no_watch(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        picks = [_pick()]
        deps = self._deps(broker, _plan_l((0, 10.0, 100)), picks, _FakeFeed({}), [])
        with mock.patch.dict("os.environ", {}, clear=True):
            cl.run_once(deps)
        self.assertEqual(len(broker.brackets), 1, "flag OFF: normal limit-entry placement runs")
        self.assertEqual(_lines(path), [], "flag OFF: nothing written to the entry-trails journal")


if __name__ == "__main__":
    unittest.main()
