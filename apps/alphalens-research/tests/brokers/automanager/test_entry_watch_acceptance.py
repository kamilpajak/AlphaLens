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
    _plan,
    _RecordingBroker,
)

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV


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
            (f"{pkg}.automanager.reconcile_bridge.verdicts", lambda _r, _b: []),
            (f"{pkg}.automanager.safety.check", lambda *_a, **_k: object()),
            (f"{pkg}.routing.resolve_us_instrument", lambda _b, _t: _instr()),
            (f"{pkg}.submission_log.iter_submission_records", lambda _p: []),
            (f"{pkg}.submission_log.build_submission_record", lambda **kw: dict(kw)),
            (f"{pkg}.submission_log.append_submission_record", lambda _r: None),
            ("broker_contract.sizing.compute_setup_plan", lambda _s, **_k: plan),
            (f"{pkg}.automanager.placement_planner.classify", lambda *_a, **_k: _placement()),
            (f"{pkg}.automanager.picks.mark_refused", lambda *_a, **_k: None),
        ):
            self.enterContext(mock.patch(target, fn))
        self.enterContext(mock.patch.object(cl, "_append_standalone_stop_journal", lambda _l: None))

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
            verdicts_fn=lambda _r, _b: [],
            build_position_view=lambda _b, _r: object(),
            build_protection_view=lambda _b, _r: _empty_pview(),
            execute_protection=lambda _a, _k, _r: None,
            sweep_orphans_fn=lambda _b: [],
            alert=lambda _m: None,
            alert_throttled=_throttled,
            live_exits_feed_factory=lambda _u2i: feed,
        )

    def test_flag_on_drives_would_fire_without_any_broker_order(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        prices: dict[int, float | None] = {}
        alerts: list[tuple[str, str]] = []
        # A recent brief_date so the real-calendar TTL window_end is in the
        # future (a stale date would expire the watch on tick 1).
        picks = [_pick(date=dt.date.today().isoformat())]
        deps = self._deps(broker, _plan((0, 10.0, 100)), picks, _FakeFeed(prices), alerts)

        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            prices[307] = 10.0
            cl.run_once(deps)  # tick 1: drain opens watch, pass touches
            picks.clear()  # pick retired from the drain
            prices[307] = 9.90
            cl.run_once(deps)  # tick 2: new low
            prices[307] = 9.95
            cl.run_once(deps)  # tick 3: bounce -> would fire

        self.assertEqual(broker.brackets, [], "DRY-RUN: no bracket order may be placed")
        self.assertEqual(broker.stops, [])
        self.assertEqual(broker.amends, [])
        self.assertEqual(broker.cancels, [])
        kinds = [line["kind"] for line in _lines(path)]
        self.assertEqual(kinds.count(entry_trails.KIND_WATCH_OPEN), 1)
        self.assertIn(entry_trails.KIND_TOUCHED, kinds)
        self.assertIn(entry_trails.KIND_TRAIL_ARMED, kinds)
        self.assertIn(entry_trails.KIND_FIRED, kinds)
        self.assertTrue(any("would fire" in m for m, _k in alerts))

    def test_flag_on_suspend_path_places_no_order(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        prices: dict[int, float | None] = {}
        picks = [_pick(date=dt.date.today().isoformat())]
        deps = self._deps(
            broker, _plan((0, 10.0, 100), (1, 9.5, 100)), picks, _FakeFeed(prices), []
        )
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            prices[307] = 10.0
            cl.run_once(deps)  # touches tier-0
            picks.clear()
            prices[307] = 9.40  # below tier-1 limit 9.5 -> tier-0 suspends
            cl.run_once(deps)
        self.assertEqual(broker.brackets, [])
        kinds = [line["kind"] for line in _lines(path)]
        self.assertIn(entry_trails.KIND_SUSPENDED, kinds)

    def test_flag_off_is_byte_identical_places_bracket_writes_no_watch(self) -> None:
        path = _journal(self)
        broker = _RecordingBroker()
        picks = [_pick()]
        deps = self._deps(broker, _plan((0, 10.0, 100)), picks, _FakeFeed({}), [])
        with mock.patch.dict("os.environ", {}, clear=True):
            cl.run_once(deps)
        self.assertEqual(len(broker.brackets), 1, "flag OFF: normal limit-entry placement runs")
        self.assertEqual(_lines(path), [], "flag OFF: nothing written to the entry-trails journal")


if __name__ == "__main__":
    unittest.main()
