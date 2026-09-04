"""Overnight DayOrder-cancel -> next-session RE-ARM (PR-T2b, Finding 2 / memo §5
CRITICAL-2).

A native trailing entry order is a DayOrder — it cancels at session close. Left
frozen, a 7-day-TTL trailing entry silently degrades to a single-session order.
This phase re-admits a DayOrder-cancelled tier through the normal watch-open path
with the trough CARRIED, and blocks the fresh arm until an OPEN-CHECK passes: if
the next session opens at/below the carried trigger (``trough*(1+d)``) no order is
placed until a FRESH bounce forms off a NEW post-open low — the stale trigger is
never handed to the broker into a gap.

Two seams cooperate:

- the reconcile pass (``_run_entry_trail_reconcile_pass``) detects a resting armed
  order that DISAPPEARED unfilled (resolve -> EXPIRED/CANCELLED = the DayOrder
  cancelled at close) and re-appends ``watch_open`` (arm state reset, trough
  carried, ``awaiting_fresh_low`` marker) — or, past the ORIGINAL ``window_end``,
  writes the terminal ``expired`` (no re-arm, reservation released);
- the watch pass reconstructs the re-armed tier WATCHING with the carried trough
  and the open-check ARMED, so ``_arm_native_trail`` places nothing until a fresh
  post-open low clears it.

A fill that raced the close-cancel is caught by the fill-reconcile (``fired``),
never re-armed (memo §3 G6). Re-arm never double-places (G3 dedup on the
``-entry-`` family through the unchanged arm path).
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from broker_contract.contract import BrokerError, OrderStatus

# Shared hermetic fixtures live in the T1c wiring tests.
from tests.brokers.automanager.test_entry_watch_reconcile import _os, _ResolvingBroker, _seed_armed
from tests.brokers.automanager.test_entry_watch_wiring import (
    _ALLOW,
    _ENV,
    _FakeFeed,
    _journal,
    _lines,
    _planned_journal,
    _RecordingBroker,
    _seed_healthy_plan,
    _seed_watch,
    _watch_deps,
)

_CRID = "KO-2026-07-20-entry-t0"
_UIC = 307
# A window_end far in the future (default seed) vs one already past — the TTL gate.
_FUTURE_WINDOW = "2099-01-01T21:00:00+00:00"
_PAST_WINDOW = "2000-01-01T21:00:00+00:00"


def _reconcile(deps: cl.LoopDeps, env: dict[str, str] | None = None) -> None:
    with mock.patch.dict("os.environ", {_ENV: "50"} if env is None else env, clear=True):
        cl._run_entry_trail_reconcile_pass(deps, cl.TickReport())


def _seed_carried_trough(crid: str, trough: float) -> None:
    entry_trails.append_entry_trail_line(
        {"kind": entry_trails.KIND_TROUGH, "crid": crid, "trough": trough}
    )


def _seed_armed_with_trough(path: Any, *, crid: str = _CRID, order_id: str, trough: float) -> None:
    """A resting armed tier that tracked a trough before it armed (the realistic
    order: watch_open -> trough -> trail_armed, so ``latest_kind`` is trail_armed
    and ``min_trough`` is carried)."""
    _seed_healthy_plan()
    _seed_watch(path, crid=crid, limit=10.0, next_tier_limit=None)
    _seed_carried_trough(crid, trough)
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_TRAIL_ARMED,
            "crid": crid,
            "order_id": order_id,
            "trigger": 10.05,
        }
    )


def _rearm_watch_open(
    path: Any,
    *,
    crid: str = _CRID,
    trough: float,
    window_end: str = _FUTURE_WINDOW,
    next_tier_limit: float | None = None,
) -> None:
    """Reproduce the fold state of a RE-ARMED tier directly: a carried trough
    (the running min from before the DayOrder-cancel) + a re-arm ``watch_open``
    (arm state reset, ``awaiting_fresh_low`` open-check marker) as the latest
    non-terminal line, exactly what ``_run_entry_trail_reconcile_pass`` writes."""
    _seed_healthy_plan()
    _seed_carried_trough(crid, trough)
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_WATCH_OPEN,
            "crid": crid,
            "limit": 10.0,
            "qty": 100.0,
            "d_bps": 50,
            "window_end": window_end,
            "fx_rate": None,
            "uic": _UIC,
            "ticker": "KO",
            "exchange_mic": "XNYS",
            "next_tier_limit": next_tier_limit,
            "pick_key": "KO:2026-07-20",
            "entry_mode": "entry-trail-native-d50-testcfg",
            "disaster_stop": 8.0,
            "tier_index": 0,
            "awaiting_fresh_low": True,
        }
    )


def _run_watch(deps: cl.LoopDeps, price: float | None, feed: dict[int, float | None]) -> None:
    feed[_UIC] = price
    with mock.patch.dict("os.environ", _ALLOW, clear=True):
        cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())


class TestReArmOpenCheckBlocksTheArm(unittest.TestCase):
    """memo §5 CRITICAL-2 open-check: a re-armed tier places NOTHING until a
    fresh post-open low re-anchors the carried trigger."""

    def test_touch_without_a_fresh_low_places_no_order_then_a_fresh_low_arms(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _rearm_watch_open(path, trough=9.70)  # carried trough 9.70, trigger 9.7485
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        # Session opens at/below the carried trigger but forms NO new low -> the
        # open-check blocks the arm.
        _run_watch(deps, 9.72, prices)
        self.assertEqual(broker.trailing_orders, [], "no arm until a fresh post-open low")

        # A FRESH post-open low re-anchors the trigger -> the arm proceeds.
        _run_watch(deps, 9.60, prices)
        self.assertEqual(len(broker.trailing_orders), 1, "the fresh low clears the open-check")
        self.assertEqual(broker.trailing_orders[0]["request_id"], f"{_CRID}-fire")

    def test_gap_up_open_never_arms_on_the_stale_trigger(self) -> None:
        # The session gaps UP above the carried trough and never returns below it:
        # no fresh low ever forms, so the stale trigger is never armed into the gap.
        path = _journal(self)
        _planned_journal(self)
        _rearm_watch_open(path, trough=9.70)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        for price in (9.90, 9.85, 9.95):  # all above the carried trough 9.70
            _run_watch(deps, price, prices)
        self.assertEqual(
            broker.trailing_orders, [], "a stale carried trigger is never handed to a gap"
        )


class TestReArmReAdmitsTheTier(unittest.TestCase):
    """A gone-unfilled DayOrder is re-armed through the watch-open path: the tier
    is WATCHING again, trough carried, arm state reset, re-admitted to the pass."""

    def test_expired_dayorder_before_window_end_re_arms_the_tier(self) -> None:
        path = _journal(self)
        _seed_armed_with_trough(path, crid=_CRID, order_id="TR-1", trough=9.70)
        broker = _ResolvingBroker()  # order gone from the book -> resolve EXPIRED
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.EXPIRED)
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        after = entry_trails.read_entry_trail_fold()
        state = after.tiers[_CRID]
        # No terminal — the tier is re-admissible, not killed.
        self.assertIsNone(state.terminal_kind, "a re-armed tier is never terminated")
        self.assertEqual(state.latest_kind, entry_trails.KIND_WATCH_OPEN, "back to a watching kind")
        self.assertIsNone(state.armed_order_id, "the stale resting-order id is cleared")
        self.assertEqual(state.min_trough, 9.70, "the carried trough is preserved")
        assert state.watch_open is not None
        self.assertTrue(state.watch_open.get("awaiting_fresh_low"), "the open-check is armed")
        # The watch pass RE-ADMITS it (the resting-order exclusion no longer bites).
        self.assertIn(_CRID, cl._active_entry_watches(after))
        self.assertNotIn(_CRID, cl._resting_armed_tiers(after))

    def test_cancelled_dayorder_before_window_end_re_arms_the_tier(self) -> None:
        path = _journal(self)
        _seed_armed(path, crid=_CRID, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.CANCELLED)
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        state = entry_trails.read_entry_trail_fold().tiers[_CRID]
        self.assertIsNone(state.terminal_kind)
        self.assertEqual(state.latest_kind, entry_trails.KIND_WATCH_OPEN)

    def test_re_arm_reservation_is_conserved_not_doubled(self) -> None:
        # The re-arm re-appends watch_open verbatim, so the virtual reservation
        # stays ONE tier limit x qty (never doubled by the re-append).
        path = _journal(self)
        _seed_armed(path, crid=_CRID, order_id="TR-1", limit=10.0)
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.EXPIRED)
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        total, bad = entry_trails.watching_virtual_gross_acct(entry_trails.read_entry_trail_fold())
        self.assertEqual((total, bad), (1_000.0, 0), "the re-armed tier still reserves once")


class TestRaceOfFillAndCloseCancelBecomesFired(unittest.TestCase):
    """memo §3 G6: a fill that raced the DayOrder close-cancel is a FILL, not an
    expiry — it becomes ``fired`` (the fill-reconcile path), never a re-arm."""

    def test_filled_order_writes_fired_and_no_rearm_watch_open(self) -> None:
        path = _journal(self)
        _seed_armed(path, crid=_CRID, order_id="TR-1", limit=10.0)
        watch_opens_before = len(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        )
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os(
            "TR-1", OrderStatus.FILLED, filled_quantity=100.0, avg_fill_price=10.05
        )
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        fired = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_FIRED]
        self.assertEqual(len(fired), 1, "the raced fill becomes fired (G6)")
        watch_opens_after = len(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        )
        self.assertEqual(watch_opens_after, watch_opens_before, "a filled tier is never re-armed")


class TestReArmRespectsOriginalTTL(unittest.TestCase):
    """memo §5 TTL (one rule): re-arm never extends the ORIGINAL window_end. A
    DayOrder gone unfilled PAST window_end is terminal ``expired`` — no re-arm,
    reservation released."""

    def test_expiry_past_window_end_terminates_and_releases_the_reservation(self) -> None:
        path = _journal(self)
        _seed_watch(path, crid=_CRID, limit=10.0, next_tier_limit=None, window_end=_PAST_WINDOW)
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_TRAIL_ARMED,
                "crid": _CRID,
                "order_id": "TR-1",
                "trigger": 10.05,
            }
        )
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.EXPIRED)
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        state = entry_trails.read_entry_trail_fold().tiers[_CRID]
        self.assertEqual(
            state.terminal_kind, entry_trails.KIND_EXPIRED, "past-TTL -> terminal expired"
        )
        total, bad = entry_trails.watching_virtual_gross_acct(entry_trails.read_entry_trail_fold())
        self.assertEqual((total, bad), (0.0, 0), "the terminal releases the reservation")
        # A terminal tier is NOT re-admitted and NOT re-owned by the reconcile pass.
        after = entry_trails.read_entry_trail_fold()
        self.assertNotIn(_CRID, cl._active_entry_watches(after))
        self.assertNotIn(_CRID, cl._resting_armed_tiers(after))

    def test_unknown_outcome_is_deferred_never_re_armed_or_terminated(self) -> None:
        # An UNKNOWN (audit not-in-retention / a fill still materializing) must
        # not presume expiry: no re-arm, no terminal — retry next tick.
        path = _journal(self)
        _seed_armed(path, crid=_CRID, order_id="TR-1", limit=10.0)
        watch_opens_before = len(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        )
        broker = _ResolvingBroker()
        broker.resolutions["TR-1"] = _os("TR-1", OrderStatus.UNKNOWN)
        deps = _watch_deps(None, [], broker=broker)

        _reconcile(deps)

        self.assertEqual(
            [ln for ln in _lines(path) if ln["kind"] in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS], []
        )
        watch_opens_after = len(
            [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        )
        self.assertEqual(watch_opens_after, watch_opens_before, "UNKNOWN is deferred, not re-armed")


class TestReArmFullCycle(unittest.TestCase):
    """End-to-end: the reconcile pass re-arms a gone DayOrder, then the watch pass
    reconstructs the tier from the marker it wrote — the open-check blocks until a
    fresh low, then a native order is placed. Proves the two seams share one wire."""

    def test_reconcile_re_arm_then_watch_pass_arms_only_after_a_fresh_low(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _seed_armed_with_trough(path, crid=_CRID, order_id="OLD-1", trough=9.70)
        broker = _ResolvingBroker()  # SupportsOrderResolution AND SupportsTrailingStop
        broker.resolutions["OLD-1"] = _os("OLD-1", OrderStatus.EXPIRED)  # gone at close
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        _reconcile(deps)  # DayOrder gone unfilled -> re-arm (marker on the watch_open)

        # The watch pass re-admits the tier; opening at/below the carried trigger
        # with no fresh low arms NOTHING.
        _run_watch(deps, 9.72, prices)
        self.assertEqual(broker.trailing_orders, [], "re-armed tier waits for a fresh low")
        # A fresh post-open low re-anchors the trigger -> a NEW native order rests.
        _run_watch(deps, 9.55, prices)
        self.assertEqual(len(broker.trailing_orders), 1, "the fresh low clears the open-check")
        armed = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED]
        self.assertEqual(armed[-1]["order_id"], "TR-1", "a fresh native order id is journaled")


class TestReArmDoesNotDoublePlace(unittest.TestCase):
    """memo §3 G3: the re-arm + re-place path dedups on the -entry- family, so a
    crash window never rests a second trail for the same tier."""

    def test_re_armed_tier_adopts_an_already_resting_order_no_second_post(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _rearm_watch_open(path, trough=9.70)
        broker = _RecordingBroker()
        # The prior session's native order is still visible on the book under the
        # deterministic -entry- ExternalReference (a crash-window straggler).
        broker.open_orders = [
            type(
                "OS",
                (),
                {"order_id": "TR-STALE", "external_reference": f"{_CRID}-fire"},
            )()
        ]
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        _run_watch(deps, 9.60, prices)  # a fresh low clears the open-check -> arm attempt

        self.assertEqual(
            broker.trailing_orders, [], "adopting the resting order must not POST again"
        )
        armed = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_TRAIL_ARMED]
        self.assertEqual(
            armed[-1]["order_id"], "TR-STALE", "the existing order is adopted by reference"
        )


class _RejectOnceBroker(_RecordingBroker):
    """``place_trailing_stop`` rejects the FIRST POST (a transient broker error),
    then records normally — the arm-failed-on-the-fresh-low-tick scenario."""

    def __init__(self) -> None:
        super().__init__()
        self._rejects_left = 1

    def place_trailing_stop(self, *args: Any, **kwargs: Any) -> Any:
        if self._rejects_left:
            self._rejects_left -= 1
            raise BrokerError("simulated transient reject")
        return super().place_trailing_stop(*args, **kwargs)


class TestOpenCheckClearanceSurvivesRestart(unittest.TestCase):
    """The fresh-low open-check clear must be DURABLE: the engine clears
    ``awaiting_fresh_low`` in memory, but the latest ``watch_open`` line keeps
    the re-arm marker — so an arm failure on the fresh-low tick followed by a
    daemon restart re-seeded the block from the fold and froze the tier until
    a SECOND fresh low formed (potentially a whole session's only dip)."""

    def test_arm_failure_then_restart_retries_instead_of_reblocking(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _rearm_watch_open(path, trough=9.70)
        prices: dict[int, float | None] = {}
        broker = _RejectOnceBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        # A fresh post-open low clears the open-check, but the POST fails.
        _run_watch(deps, 9.60, prices)
        self.assertEqual(broker.trailing_orders, [], "the rejected POST placed nothing")

        # Daemon restart: runtimes are rebuilt from the journal fold. No NEW
        # low forms (9.65 > 9.60) — the already-cleared open-check must not
        # re-block, so the arm retries and succeeds.
        restarted = _watch_deps(_FakeFeed(prices), [], broker=broker)
        _run_watch(restarted, 9.65, prices)
        self.assertEqual(len(broker.trailing_orders), 1, "restart must not re-block the arm")

    def test_clearance_is_journaled_once_on_the_clear_tick(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        _rearm_watch_open(path, trough=9.70)
        prices: dict[int, float | None] = {}
        broker = _RejectOnceBroker()
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        _run_watch(deps, 9.60, prices)  # the clear tick (arm fails, stays TOUCHED)
        fold = entry_trails.read_entry_trail_fold()
        watch_open = fold.tiers[_CRID].watch_open
        assert watch_open is not None
        self.assertFalse(
            watch_open.get("awaiting_fresh_low"),
            "the latest watch_open must no longer carry the re-arm marker",
        )

        _run_watch(deps, 9.58, prices)  # same lifetime: no second clearance line
        opens = [ln for ln in _lines(path) if ln["kind"] == entry_trails.KIND_WATCH_OPEN]
        self.assertEqual(len(opens), 2, "one re-arm watch_open + exactly one clearance re-append")

    def test_clearance_helper_is_a_noop_without_the_marker(self) -> None:
        # Defensive guard: a record with no marker (nothing to clear) appends
        # nothing — the clearance is transition-driven, never a per-tick echo.
        path = _journal(self)
        cl._persist_open_check_clearance(
            {"kind": entry_trails.KIND_WATCH_OPEN, "crid": _CRID, "limit": 10.0}
        )
        self.assertEqual(_lines(path), [])


class TestClearancePrecedesTheSameTickTerminal(unittest.TestCase):
    """#1106 ordering invariant: when the open-check clearance and a terminal
    land on the SAME tick (reachable via the G9 suspend — the fresh low that
    clears the check is the same low that dips below the next tier), the
    markerless ``watch_open`` clearance line must precede the terminal in the
    journal. The fold is terminal-sticky today so the reversed order re-admits
    nothing, but a journal that reads terminal-then-re-opened is a lie any
    future last-line-wins reader would believe."""

    def test_clearance_line_lands_before_the_same_tick_suspend(self) -> None:
        path = _journal(self)
        _planned_journal(self)
        # Carried trough 9.70, open-check armed, next tier at 9.5: one tick at
        # 9.40 both clears the open-check (fresh low) and trips the G9 suspend.
        _rearm_watch_open(path, trough=9.70, next_tier_limit=9.5)
        prices: dict[int, float | None] = {}
        broker = _RecordingBroker()  # empty book: G6 finalize is a no-op
        deps = _watch_deps(_FakeFeed(prices), [], broker=broker)

        _run_watch(deps, 9.40, prices)

        kinds = [ln["kind"] for ln in _lines(path)]
        self.assertIn(entry_trails.KIND_SUSPENDED, kinds, "the tick must suspend")
        last_open = max(i for i, k in enumerate(kinds) if k == entry_trails.KIND_WATCH_OPEN)
        self.assertEqual(
            kinds.count(entry_trails.KIND_WATCH_OPEN), 2, "re-arm + exactly one clearance"
        )
        self.assertLess(
            last_open,
            kinds.index(entry_trails.KIND_SUSPENDED),
            "the clearance watch_open must precede the same-tick terminal",
        )

        # The invariant the ordering protects: the tier stays terminal and is
        # not re-admitted by the fold.
        fold = entry_trails.read_entry_trail_fold()
        self.assertEqual(fold.tiers[_CRID].terminal_kind, entry_trails.KIND_SUSPENDED)
        self.assertNotIn(_CRID, cl._active_entry_watches(fold))


if __name__ == "__main__":
    unittest.main()
