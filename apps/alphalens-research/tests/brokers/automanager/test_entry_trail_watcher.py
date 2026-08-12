"""T1b — the entry-trailing WATCHER state machine (entry-trailing design memo
§5 state machine, §3 guards G2/G5/G6/G9).

PR-T1 is a DRY-RUN watcher: the ONE non-negotiable safety property is that the
engine NEVER constructs or returns an order. The "fire" is an ALERT intent
("would fire @ trigger X") plus a ``trail_armed`` journal marker — no bracket,
no order id, nothing placed. These tests assert the intent surface only; there
is no broker in this module and none is mocked, which is the point.

Per tier: ``WATCHING -> TOUCHED -> (WOULD_FIRE | SUSPENDED | EXPIRED |
CANCELLED)``. TOUCHED tracks the running LOW; a bounce off it to the trigger
``trough*(1+d)`` is the would-fire.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager import entry_trails as et
from alphalens_pipeline.brokers.automanager.entry_trail_watcher import (
    EntryTierWatcher,
    TickInput,
    TierWatchConfig,
    WatchState,
)

_T0 = dt.datetime(2026, 8, 12, 14, 0, 0, tzinfo=dt.UTC)
_FAR = _T0 + dt.timedelta(days=7)
_CRID = "KO:2026-08-12:T1-entry-0"


def _config(
    *,
    tier_limit: float = 10.0,
    d_bps: int = 50,
    window_end: dt.datetime = _FAR,
    qty: float = 100,
    fx_rate: float | None = None,
    next_tier_limit: float | None = 9.5,
) -> TierWatchConfig:
    return TierWatchConfig(
        crid=_CRID,
        tier_limit=tier_limit,
        d_bps=d_bps,
        window_end=window_end,
        qty=qty,
        fx_rate=fx_rate,
        next_tier_limit=next_tier_limit,
    )


def _tick(
    price: float | None,
    *,
    at: dt.datetime = _T0,
    session_boundary: bool = False,
) -> TickInput:
    return TickInput(now=at, price=price, session_boundary=session_boundary)


def _fresh_watch(config: TierWatchConfig | None = None) -> EntryTierWatcher:
    watcher, _intents = EntryTierWatcher.open_watch(config or _config(), day1_gap_clear=True)
    assert watcher is not None
    return watcher


def _kinds(intents: tuple) -> list[str]:
    return [intent.kind for intent in intents]


class TestOpenWatch(unittest.TestCase):
    def test_open_watch_emits_watch_open_with_reservation_fields(self) -> None:
        watcher, intents = EntryTierWatcher.open_watch(
            _config(tier_limit=10.0, qty=100, d_bps=50, fx_rate=0.25), day1_gap_clear=True
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertEqual(watcher.state, WatchState.WATCHING)
        self.assertEqual(_kinds(intents), [et.KIND_WATCH_OPEN])
        intent = intents[0]
        self.assertEqual(intent.crid, _CRID)
        # The G5 virtual reservation reads limit/qty/fx_rate off this record —
        # they MUST be present with those exact keys.
        self.assertEqual(intent.payload["limit"], 10.0)
        self.assertEqual(intent.payload["qty"], 100)
        self.assertEqual(intent.payload["d_bps"], 50)
        self.assertEqual(intent.payload["fx_rate"], 0.25)
        self.assertIn("window_end", intent.payload)

    def test_watch_open_record_values_through_the_reservation_fold(self) -> None:
        # End-to-end: the emitted watch_open must value correctly in the SAME
        # fold the gross cap / cash floor consume (memo G5).
        import json

        _watcher, intents = EntryTierWatcher.open_watch(
            _config(tier_limit=10.0, qty=100, fx_rate=0.25), day1_gap_clear=True
        )
        line = json.dumps({"crid": intents[0].crid, "kind": intents[0].kind, **intents[0].payload})
        total, bad = et.watching_virtual_gross_acct(et.fold_entry_trail_lines([line]))
        self.assertEqual(bad, 0)
        self.assertAlmostEqual(total, 4_000.0)  # 10*100 / 0.25

    def test_day1_gap_through_open_opens_no_watch(self) -> None:
        # memo §5: gap-through-open => no watch on day 1 (no trough tracked
        # through a falling-knife day). The engine HONORS the caller's verdict.
        watcher, intents = EntryTierWatcher.open_watch(_config(), day1_gap_clear=False)
        self.assertIsNone(watcher)
        self.assertEqual(intents, ())


class TestTouchDetection(unittest.TestCase):
    def test_price_above_limit_does_not_touch(self) -> None:
        watcher = _fresh_watch()
        result = watcher.process(_tick(10.5))
        self.assertEqual(result.state, WatchState.WATCHING)
        self.assertEqual(result.journal_intents, ())

    def test_price_at_limit_touches_and_seeds_the_trough(self) -> None:
        watcher = _fresh_watch()
        result = watcher.process(_tick(10.0))
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertEqual(_kinds(result.journal_intents), [et.KIND_TOUCHED, et.KIND_TROUGH])
        trough_intent = result.journal_intents[1]
        self.assertEqual(trough_intent.payload["trough"], 10.0)

    def test_price_below_limit_touches(self) -> None:
        watcher = _fresh_watch()
        result = watcher.process(_tick(9.8))
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertEqual(result.journal_intents[1].payload["trough"], 9.8)

    def test_no_would_fire_on_the_touch_tick(self) -> None:
        # At touch the trough equals the price and the trigger is above it by
        # d, so a fire cannot happen the same tick by construction.
        watcher = _fresh_watch()
        result = watcher.process(_tick(10.0))
        self.assertEqual(result.alerts, ())
        self.assertNotIn(et.KIND_TRAIL_ARMED, _kinds(result.journal_intents))


class TestTroughTrackingAndWouldFire(unittest.TestCase):
    def test_trough_ratchets_down_only_and_one_intent_per_tick(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))  # touch, trough 10.0
        low = watcher.process(_tick(9.7))  # new low
        self.assertEqual(_kinds(low.journal_intents), [et.KIND_TROUGH])
        self.assertEqual(low.journal_intents[0].payload["trough"], 9.7)
        bounce = watcher.process(_tick(9.72))  # a bounce below trigger — no new low
        self.assertEqual(bounce.journal_intents, ())  # trough must not rise, no line

    def test_would_fire_at_trigger_emits_alert_and_trail_armed_marker(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))  # touch, trough 10.0
        watcher.process(_tick(9.8))  # trough 9.8, trigger 9.8*1.005 = 9.849
        result = watcher.process(_tick(9.85))  # 9.85 >= 9.849 -> would fire
        self.assertEqual(result.state, WatchState.WOULD_FIRE)
        self.assertTrue(watcher.is_terminal)
        # DRY RUN: journal marker is trail_armed (NOT fired — no order), the
        # fire is an ALERT only.
        self.assertIn(et.KIND_TRAIL_ARMED, _kinds(result.journal_intents))
        self.assertNotIn(et.KIND_FIRED, _kinds(result.journal_intents))
        self.assertEqual(len(result.alerts), 1)
        alert = result.alerts[0]
        self.assertEqual(alert.kind, "would_fire")
        self.assertIn("would fire", alert.message)
        self.assertEqual(alert.crid, _CRID)

    def test_just_below_trigger_does_not_fire(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))
        watcher.process(_tick(9.8))  # trigger 9.849
        result = watcher.process(_tick(9.84))  # below trigger
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertEqual(result.alerts, ())

    def test_terminal_state_is_a_no_op(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))
        watcher.process(_tick(9.8))
        watcher.process(_tick(9.85))  # fires -> WOULD_FIRE
        after = watcher.process(_tick(9.9))  # would fire again if not terminal
        self.assertEqual(after.journal_intents, ())
        self.assertEqual(after.alerts, ())
        self.assertEqual(after.state, WatchState.WOULD_FIRE)

    def test_negative_concession_is_allowed_trigger_follows_trough_below_limit(self) -> None:
        # The trough can fall well below the tier limit; the trigger follows it
        # DOWN (trigger = trough*(1+d)), so a fire below the limit is a BETTER
        # entry (negative concession) and must NOT be clamped up to the limit.
        watcher = _fresh_watch(_config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0))
        watcher.process(_tick(10.0))  # touch
        watcher.process(_tick(9.6))  # trough 9.6, trigger 9.6*1.005 = 9.648 < limit 10.0
        result = watcher.process(_tick(9.65))  # 9.65 >= 9.648 -> fire below the limit
        self.assertEqual(result.state, WatchState.WOULD_FIRE)
        trail_armed = next(i for i in result.journal_intents if i.kind == et.KIND_TRAIL_ARMED)
        self.assertLess(trail_armed.payload["trigger"], 10.0)  # negative concession


class TestFreshnessVeto(unittest.TestCase):
    """A ``price=None`` reading is the freshness/trust veto (memo §5 Feed):
    ``is_fresh <=3s`` + ``delayed_by_minutes==0`` collapse to "no trustworthy
    price". On ANY doubt the engine makes NO state progress."""

    def test_veto_blocks_touch(self) -> None:
        watcher = _fresh_watch()
        result = watcher.process(_tick(None))
        self.assertEqual(result.state, WatchState.WATCHING)
        self.assertEqual(result.journal_intents, ())

    def test_veto_blocks_trough_and_would_fire(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))  # touch, trough 10.0
        watcher.process(_tick(9.8))  # trough 9.8, trigger 9.849
        # A None reading that, had it been a real 9.85, would have fired:
        result = watcher.process(_tick(None))
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertEqual(result.journal_intents, ())
        self.assertEqual(result.alerts, ())

    def test_non_finite_price_is_vetoed_like_none(self) -> None:
        watcher = _fresh_watch()
        for bad in (float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(bad=bad):
                result = watcher.process(_tick(bad))
                self.assertEqual(result.state, WatchState.WATCHING)
                self.assertEqual(result.journal_intents, ())


class TestG9DeepDeclineSuspend(unittest.TestCase):
    """G9 (memo §3, reworked): suspend a tier only when the trough falls below
    the NEXT tier's limit — the ladder says a deeper move is the next tier's
    job. SUSPENDED is terminal, emits a ``suspended`` journal-intent + alert."""

    def test_trough_below_next_tier_limit_suspends(self) -> None:
        watcher = _fresh_watch(_config(tier_limit=10.0, next_tier_limit=9.5))
        watcher.process(_tick(10.0))  # touch, trough 10.0
        result = watcher.process(_tick(9.4))  # 9.4 < next tier 9.5 -> suspend
        self.assertEqual(result.state, WatchState.SUSPENDED)
        self.assertTrue(watcher.is_terminal)
        self.assertIn(et.KIND_SUSPENDED, _kinds(result.journal_intents))
        self.assertEqual(len(result.alerts), 1)
        self.assertEqual(result.alerts[0].kind, "suspended")

    def test_gap_down_touch_through_next_tier_suspends_on_the_touch_tick(self) -> None:
        watcher = _fresh_watch(_config(tier_limit=10.0, next_tier_limit=9.5))
        result = watcher.process(_tick(9.3))  # gaps straight through E2
        self.assertEqual(result.state, WatchState.SUSPENDED)
        self.assertIn(et.KIND_SUSPENDED, _kinds(result.journal_intents))

    def test_deepest_tier_without_next_limit_never_suspends_on_depth(self) -> None:
        # E3 (the deepest tier) has no next tier; a deep decline is its own to
        # ride, so depth never suspends it (only expiry / fire / cancel do).
        watcher = _fresh_watch(_config(tier_limit=10.0, next_tier_limit=None))
        watcher.process(_tick(10.0))
        result = watcher.process(_tick(8.0))  # very deep
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertNotIn(et.KIND_SUSPENDED, _kinds(result.journal_intents))


class TestStalenessGate(unittest.TestCase):
    """G9 LULD/halt gate: NO would-fire on the FIRST fresh tick after a
    staleness gap > 5 min (a limit-up reopen looks like a violent bounce).
    Touch/trough still track — only the fire is suppressed."""

    def test_first_fresh_tick_after_a_gap_does_not_fire(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0, at=_T0))  # touch, trough 10.0
        watcher.process(_tick(9.8, at=_T0 + dt.timedelta(seconds=45)))  # trough 9.8
        # A > 5 min gap, then a price that WOULD clear the trigger 9.849:
        after_gap = watcher.process(_tick(9.9, at=_T0 + dt.timedelta(minutes=6)))
        self.assertEqual(after_gap.state, WatchState.TOUCHED)  # fire suppressed
        self.assertEqual(after_gap.alerts, ())
        # The very next fresh tick (within 5 min) is allowed to fire:
        ok = watcher.process(_tick(9.9, at=_T0 + dt.timedelta(minutes=6, seconds=45)))
        self.assertEqual(ok.state, WatchState.WOULD_FIRE)

    def test_gap_tick_still_updates_the_trough(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0, at=_T0))
        watcher.process(_tick(9.8, at=_T0 + dt.timedelta(seconds=45)))
        after_gap = watcher.process(_tick(9.5, at=_T0 + dt.timedelta(minutes=6)))
        # trough tracking is NOT gated by staleness — only the fire is.
        self.assertIn(et.KIND_TROUGH, _kinds(after_gap.journal_intents))


class TestExpiry(unittest.TestCase):
    """EXPIRED when ``now >= window_end`` (memo §5 TTL) — time-based, so it
    fires even without a fresh price and even before a touch."""

    def test_expires_before_touch_when_the_window_passes(self) -> None:
        watcher = _fresh_watch(_config(window_end=_T0 + dt.timedelta(minutes=1)))
        result = watcher.process(_tick(10.5, at=_T0 + dt.timedelta(minutes=2)))
        self.assertEqual(result.state, WatchState.EXPIRED)
        self.assertEqual(_kinds(result.journal_intents), [et.KIND_EXPIRED])

    def test_expires_even_on_a_vetoed_tick(self) -> None:
        watcher = _fresh_watch(_config(window_end=_T0 + dt.timedelta(minutes=1)))
        result = watcher.process(_tick(None, at=_T0 + dt.timedelta(minutes=2)))
        self.assertEqual(result.state, WatchState.EXPIRED)

    def test_expires_from_the_touched_state(self) -> None:
        watcher = _fresh_watch(_config(window_end=_T0 + dt.timedelta(minutes=5)))
        watcher.process(_tick(10.0, at=_T0))  # touch
        result = watcher.process(_tick(9.8, at=_T0 + dt.timedelta(minutes=6)))
        self.assertEqual(result.state, WatchState.EXPIRED)


class TestOvernightCarriedTroughOpenCheck(unittest.TestCase):
    """memo §5 CRITICAL-2: the trough CARRIES across a session boundary, but a
    would-fire is BLOCKED until a FRESH bounce forms off a NEW post-open low —
    the stale carried trigger is never handed to the market into a gap."""

    def test_bounce_off_the_carried_trough_does_not_fire_after_a_boundary(self) -> None:
        watcher = _fresh_watch(_config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0))
        watcher.process(_tick(10.0, at=_T0))  # touch day 1
        watcher.process(_tick(9.7, at=_T0 + dt.timedelta(seconds=45)))  # trough 9.7, trigger 9.7485
        nextday = _T0 + dt.timedelta(days=1)
        # Session opens ABOVE the carried trigger and would fire on the stale
        # trigger — but no NEW post-open low has formed yet, so it is blocked.
        opened = watcher.process(_tick(9.9, at=nextday, session_boundary=True))
        self.assertEqual(opened.state, WatchState.TOUCHED)
        self.assertEqual(opened.alerts, ())

    def test_fire_resumes_after_a_fresh_post_open_low_bounces(self) -> None:
        watcher = _fresh_watch(_config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0))
        watcher.process(_tick(10.0, at=_T0))
        watcher.process(_tick(9.7, at=_T0 + dt.timedelta(seconds=45)))  # carried trough 9.7
        nextday = _T0 + dt.timedelta(days=1)
        watcher.process(_tick(9.9, at=nextday, session_boundary=True))  # blocked, no new low
        watcher.process(_tick(9.6, at=nextday + dt.timedelta(seconds=45)))  # NEW post-open low
        result = watcher.process(
            _tick(9.65, at=nextday + dt.timedelta(seconds=90))
        )  # bounce off 9.6, trigger 9.648
        self.assertEqual(result.state, WatchState.WOULD_FIRE)
        self.assertEqual(result.alerts[0].kind, "would_fire")

    def test_a_watch_untouched_before_the_boundary_fires_normally_after_touch(self) -> None:
        # The open-check only bites a tier that was already tracking a trough
        # before the boundary; a first touch AFTER the boundary seeds a fresh
        # low and fires on the normal path.
        watcher = _fresh_watch(_config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0))
        nextday = _T0 + dt.timedelta(days=1)
        watcher.process(_tick(10.5, at=_T0, session_boundary=True))  # still WATCHING
        watcher.process(_tick(9.7, at=nextday))  # first touch, trough 9.7
        result = watcher.process(_tick(9.76, at=nextday + dt.timedelta(seconds=45)))  # 9.7485 trig
        self.assertEqual(result.state, WatchState.WOULD_FIRE)


class TestCancel(unittest.TestCase):
    def test_cancel_transitions_to_cancelled_and_emits_the_intent(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))
        result = watcher.cancel()
        self.assertEqual(result.state, WatchState.CANCELLED)
        self.assertTrue(watcher.is_terminal)
        self.assertEqual(_kinds(result.journal_intents), [et.KIND_CANCELLED])
        self.assertEqual(result.alerts, ())

    def test_cancel_is_a_no_op_after_a_terminal(self) -> None:
        watcher = _fresh_watch()
        watcher.process(_tick(10.0))
        watcher.process(_tick(9.8))
        watcher.process(_tick(9.85))  # WOULD_FIRE
        result = watcher.cancel()
        self.assertEqual(result.state, WatchState.WOULD_FIRE)
        self.assertEqual(result.journal_intents, ())


class TestRestartResume(unittest.TestCase):
    """The WIRE phase reconstructs a watcher from the fold on restart: the
    journaled state + the journaled ``min_trough`` seed. The trough must resume
    as ``min(journaled, first fresh price)`` (memo §5) — never reseed upward."""

    def test_resume_in_touched_state_with_a_seeded_trough_fires_off_the_low(self) -> None:
        watcher = EntryTierWatcher(
            _config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0),
            initial_state=WatchState.TOUCHED,
            seeded_trough=9.7,
        )
        self.assertEqual(watcher.state, WatchState.TOUCHED)
        # A higher first price must NOT raise the trough (no upward reseed):
        result = watcher.process(_tick(9.76))  # 9.7*1.005 = 9.7485, 9.76 >= it
        self.assertEqual(result.state, WatchState.WOULD_FIRE)

    def test_resume_does_not_forget_the_low_upward(self) -> None:
        watcher = EntryTierWatcher(
            _config(tier_limit=10.0, d_bps=50, next_tier_limit=9.0),
            initial_state=WatchState.TOUCHED,
            seeded_trough=9.7,
        )
        # A first fresh price of 9.9 must keep the trough at 9.7, not reseed to
        # 9.9 (which would put the trigger at 9.9*1.005 and lose the low).
        result = watcher.process(_tick(9.72))  # trigger stays 9.7485, 9.72 < it -> no fire
        self.assertEqual(result.state, WatchState.TOUCHED)
        self.assertEqual(result.alerts, ())


if __name__ == "__main__":
    unittest.main()
