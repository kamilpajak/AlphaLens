from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
from unittest import mock

import alphalens_pipeline.data.alt_data.saxo_price_stream as sps
from alphalens_pipeline.data.alt_data.saxo_price_stream import QuoteCache, SaxoPriceStream
from alphalens_pipeline.data.alt_data.session_reclaim import ReclaimLimiter

_T0 = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


def _row(**over) -> dict:
    row = {
        "Uic": 211,
        "LastUpdated": "2026-08-07T13:47:59Z",
        "Quote": {"Bid": 314.01, "Ask": 314.04, "DelayedByMinutes": 0},
    }
    row.update(over)
    return row


class TestQuoteCache(unittest.TestCase):
    def test_snapshot_row_is_stored(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        q = c.get(211)
        self.assertEqual((q.bid, q.ask), (314.01, 314.04))
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC))

    def test_delta_with_one_side_keeps_the_other(self):
        """THE delta rule. Saxo omits unchanged fields; treating an absent Ask as
        'no ask' would blank half the quote and produce a None mid."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": 314.10}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        q = c.get(211)
        self.assertEqual(q.bid, 314.10)
        self.assertEqual(q.ask, 314.04)  # preserved

    def test_delta_without_a_quote_block_still_advances_event_time(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:05Z"},
            received_at=_T0 + dt.timedelta(seconds=5),
        )
        q = c.get(211)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 48, 5, tzinfo=dt.UTC))
        self.assertEqual(q.bid, 314.01)

    def test_delayed_flag_is_carried_and_updatable(self):
        """Session demotion arrives as a flag change on an otherwise healthy
        quote - the ONLY signal that prices went 15 minutes stale."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": {"DelayedByMinutes": 15}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 15)

    def test_out_of_order_event_time_is_dropped(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:50Z", "Quote": {"Bid": 1.0}},
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        self.assertEqual(c.get(211).bid, 314.01)  # regression ignored

    def test_out_of_order_row_still_applies_a_carried_delayed_flag(self):
        """A regressive row is dropped for price/event_time (conservative and
        safe), but NOT for DelayedByMinutes: losing a newly-reported delay
        would leave a demoted session looking healthy, serving 15-minute-old
        prices to an order decision. The flag must apply even when the rest
        of the row is rejected."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:47:50Z",  # regressive
                "Quote": {"Bid": 1.0, "DelayedByMinutes": 15},
            },
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        q = c.get(211)
        self.assertEqual(q.delayed_by_minutes, 15)  # flag still applies
        self.assertEqual(q.bid, 314.01)  # price untouched
        self.assertEqual(q.ask, 314.04)
        self.assertEqual(
            q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC)
        )  # event_time untouched

    def test_out_of_order_row_without_the_delay_key_leaves_flag_untouched(self):
        """The OMITTED-key semantics still hold on the regression path: a
        regressive row that does not carry DelayedByMinutes must not disturb
        the cached flag, and must not resurrect any stale price either."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:50Z", "Quote": {"Bid": 1.0}},
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        q = c.get(211)
        self.assertEqual(q.delayed_by_minutes, 0)  # unchanged
        self.assertEqual(q.bid, 314.01)  # not resurrected to the regressive 1.0

    def test_naive_last_updated_yields_none_event_time(self):
        """A ``LastUpdated`` string carrying no offset (e.g. Saxo omitting the
        'Z'/offset suffix) must not silently become a naive datetime: that
        value would flow into ``PricePoint.event_time`` and later blow up
        ``is_fresh``'s ``(now - event_time)`` subtraction against an aware
        ``now`` with a ``TypeError`` that would crash the daemon tick. A
        timestamp whose timezone we cannot determine is a doubt, and a doubt
        is a veto: ``_parse_utc`` must return None instead."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:47:59",  # no trailing Z / offset
                "Quote": {"Bid": 314.01, "Ask": 314.04, "DelayedByMinutes": 0},
            },
            received_at=_T0,
        )
        q = c.get(211)
        self.assertIsNone(q.event_time)

    def test_unknown_uic_returns_none(self):
        self.assertIsNone(QuoteCache().get(999))

    def test_row_without_uic_is_ignored(self):
        c = QuoteCache()
        c.apply({"LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}}, received_at=_T0)
        self.assertIsNone(c.get(211))

    def test_non_numeric_uic_is_skipped_not_raised(self):
        """A malformed Uic must degrade to 'skip this row', not raise
        ValueError out of the WebSocket reader thread: a bare
        int(raw_uic) would count as a connection failure and, after six
        such frames, trip the reconnect circuit breaker and go permanently
        dark over a payload-shape change."""
        c = QuoteCache()
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
        ) as cm:
            c.apply(
                {
                    "Uic": "not-a-number",
                    "LastUpdated": "2026-08-07T13:48:00Z",
                    "Quote": {"Bid": 1.0},
                },
                received_at=_T0,
            )
        self.assertTrue(any("uic" in line.lower() for line in cm.output), cm.output)
        self.assertIsNone(c.get(211))

    def test_truthy_non_dict_quote_block_is_skipped_not_raised(self):
        """A row whose 'Quote' value is a truthy NON-dict slips past the
        falsy-only ``or {}`` guard and raises AttributeError inside apply.
        On the reader thread that is a counted connection failure — and when
        the row comes from the create-subscription REST snapshot the SAME body
        repeats on every reconnect, so six identical creates trip the breaker
        deterministically. Must skip the row (cached quote untouched) with a
        warning, never raise."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        for garbage in ("Delayed", ["Bid", 1.0], 5):
            with self.subTest(garbage=garbage):
                with self.assertLogs(
                    "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
                ) as cm:
                    c.apply(
                        {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": garbage},
                        received_at=_T0 + dt.timedelta(seconds=2),
                    )  # must not raise
                self.assertTrue(any("quote" in line.lower() for line in cm.output), cm.output)
        q = c.get(211)
        self.assertEqual((q.bid, q.ask), (314.01, 314.04))  # untouched
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC))

    def test_truthy_non_dict_quote_block_on_first_contact_creates_nothing(self):
        c = QuoteCache()
        with self.assertLogs("alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"):
            c.apply(
                {"Uic": 300, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": "Delayed"},
                received_at=_T0,
            )
        self.assertIsNone(c.get(300))

    def test_numeric_string_delayed_coerces_and_keeps_the_delay_signal(self):
        """apply stored DelayedByMinutes UNCOERCED, so ``any_delayed``'s
        ``> 0`` raised TypeError on a string payload — on the reader thread
        (via ``_maybe_reclaim``) a counted failure, and deterministic once the
        value comes from the create-subscription snapshot. A numeric string
        must coerce to int (keeping the demotion signal alive), never raise."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:48:00Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": "15"},
            },
            received_at=_T0,
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 15)
        self.assertTrue(c.any_delayed())  # must not raise

    def test_garbage_delayed_vetoes_to_none_without_raising(self):
        """A non-numeric DelayedByMinutes is 'unknown delay': stored as None
        (which every consumer already treats as not-confirmed-fresh), so
        ``any_delayed`` stays False and the latch vetoes — never a raise."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:48:00Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": "N/A"},
            },
            received_at=_T0,
        )
        self.assertIsNone(c.get(211).delayed_by_minutes)
        self.assertFalse(c.any_delayed())  # must not raise
        self.assertIsNone(c.drain_running_low(211))

    def test_infinite_delayed_vetoes_to_none_without_raising(self):
        """json.loads accepts non-standard Infinity as a float, and
        ``int(float("inf"))`` raises OverflowError — from the DETERMINISTIC
        create-subscription snapshot that raise would repeat every reconnect
        until the breaker trips. Must veto to None, never raise."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:48:00Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": float("inf")},
            },
            received_at=_T0,
        )
        self.assertIsNone(c.get(211).delayed_by_minutes)
        self.assertFalse(c.any_delayed())

    def test_infinite_uic_row_is_skipped_without_raising(self):
        """``int(float("inf"))`` on the Uic raises OverflowError — same
        deterministic-snapshot breaker-burn hazard as the delayed flag."""
        c = QuoteCache()
        c.apply(_row(Uic=float("inf")), received_at=_T0)  # must not raise
        self.assertIsNone(c.get(211))
        self.assertFalse(c.any_delayed())

    def test_out_of_order_row_coerces_a_carried_string_delayed_flag(self):
        """The regression branch applies a newly-reported delay even on a
        dropped row — that path must coerce too, or the string reaches
        ``any_delayed`` all the same."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:47:50Z",  # regressive
                "Quote": {"Bid": 1.0, "DelayedByMinutes": "15"},
            },
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 15)
        self.assertTrue(c.any_delayed())  # must not raise

    def test_prune_except_drops_quotes_and_running_lows_outside_the_keep_set(self):
        """Reader-thread companion to the caller-side ``forget``: after a
        (re)create, everything outside the desired set — quote AND planted
        running low — must go, so an in-flight snapshot cannot leave a frozen
        entry behind."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # uic 211, latchable -> low planted
        c.apply(_row(Uic=7), received_at=_T0)
        c.prune_except({7})
        self.assertIsNone(c.get(211))
        self.assertIsNone(c.drain_running_low(211))
        self.assertIsNotNone(c.get(7))
        self.assertEqual(c.drain_running_low(7), 314.01)

    def test_non_numeric_uic_does_not_block_a_valid_row_in_the_same_frame(self):
        c = QuoteCache()
        c.apply(
            {"Uic": "garbage", "LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}},
            received_at=_T0,
        )
        c.apply(_row(), received_at=_T0)
        q = c.get(211)
        self.assertEqual(q.bid, 314.01)

    def test_explicit_null_bid_propagates_as_unknown_not_preserved(self):
        """A PRESENT-but-null Bid (a plausible one-sided-market / halt signal)
        must BLANK the cached bid, unlike an OMITTED Bid key which preserves
        it. The two are distinguished by KEY PRESENCE, not truthiness: an
        explicit null means 'no bid right now' and must propagate so the
        downstream freshness gate vetoes, rather than silently reusing a
        stale price."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": None}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        q = c.get(211)
        self.assertIsNone(q.bid)
        self.assertEqual(q.ask, 314.04)  # untouched (omitted) key still preserved

    def test_identical_event_time_delta_is_still_applied(self):
        """Saxo timestamps carry only second resolution in the observed
        fixtures, so two updates sharing a LastUpdated during active trading
        are common, not a corner case. The regression guard is strict '<':
        an equal-timestamp update must still apply. Changing the guard to
        '<=' would freeze the price for the rest of every second - this test
        pins the current, correct, strict comparison."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:59Z", "Quote": {"Bid": 315.00}},
            received_at=_T0 + dt.timedelta(milliseconds=500),
        )
        self.assertEqual(c.get(211).bid, 315.00)

    def test_delta_before_any_snapshot_is_half_blank(self):
        """A delta for a uic never seen before (no prior snapshot - the same
        code path as a delta for an unknown uic) produces a Quote with the
        untouched side still None. That half-blank shape is acceptable ONLY
        because the downstream freshness gate must treat a missing side as
        no-price, never as a stale-but-valid one."""
        c = QuoteCache()
        c.apply(
            {"Uic": 900, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 100.0}},
            received_at=_T0,
        )
        q = c.get(900)
        self.assertEqual(q.bid, 100.0)
        self.assertIsNone(q.ask)

    def test_delayed_by_minutes_preserved_across_unrelated_bid_only_delta(self):
        """DelayedByMinutes preservation across an unrelated (Bid-only) delta
        is not covered by test_delta_with_one_side_keeps_the_other (which only
        checks bid/ask) - pinned independently so reverting just this default
        does not pass the whole suite."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": 314.10}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 0)

    def test_any_delayed_is_false_on_an_empty_cache(self):
        self.assertFalse(QuoteCache().any_delayed())

    def test_any_delayed_is_false_while_every_quote_is_fresh(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)  # DelayedByMinutes=0
        self.assertFalse(c.any_delayed())

    def test_any_delayed_is_true_once_any_quote_reports_a_delay(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": {"DelayedByMinutes": 15}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertTrue(c.any_delayed())

    # --- 1 Hz touch-latch running low (entry_trailing_design §5 mitigation) ---

    def _latch_row(self, second: int, *, bid: float, ask: float, delayed: int = 0) -> dict:
        """A latchable quote row at a strictly-advancing second (so the sequence
        guard never drops it) with an explicit bid/ask/delay."""
        return {
            "Uic": 211,
            "LastUpdated": f"2026-08-07T13:48:{second:02d}Z",
            "Quote": {"Bid": bid, "Ask": ask, "DelayedByMinutes": delayed},
        }

    def test_running_low_is_the_min_over_descending_then_rising_bids(self):
        """The latch ratchets DOWN only: the drained value is the minimum bid
        seen since the last drain, not the latest one."""
        c = QuoteCache()
        for i, bid in enumerate((314.05, 313.90, 313.70, 313.95)):
            c.apply(self._latch_row(i, bid=bid, ask=bid + 0.03), received_at=_T0)
        self.assertEqual(c.drain_running_low(211), 313.70)

    def test_drain_running_low_pops_and_resets(self):
        """Draining is read-and-reset: the accumulation window is inter-drain,
        so a second drain with no new quote returns None."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=313.80, ask=313.83), received_at=_T0)
        self.assertEqual(c.drain_running_low(211), 313.80)
        self.assertIsNone(c.drain_running_low(211))

    def test_drain_running_low_none_for_unknown_uic(self):
        self.assertIsNone(QuoteCache().drain_running_low(999))

    def test_forget_clears_the_running_low(self):
        """``forget`` (called on unsubscribe) must pop the latch too, so the
        dict stays bounded by the live subscription set."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=313.80, ask=313.83), received_at=_T0)
        c.forget(211)
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_rejects_a_delayed_quote(self):
        """A demoted (15-minute-old) quote must never plant a running low: it
        would drive a false touch off a stale price."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=313.80, ask=313.83, delayed=15), received_at=_T0)
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_rejects_an_absent_delayed_flag(self):
        """Strict ``== 0``: a quote whose DelayedByMinutes never arrived (None
        under delta-merge) is 'unknown delay', not 'undelayed', and is rejected."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:48:00Z",
                "Quote": {"Bid": 313.8, "Ask": 313.9},
            },
            received_at=_T0,
        )
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_rejects_a_crossed_market(self):
        """ask < bid is a broken quote — no running low."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=314.01, ask=313.99), received_at=_T0)
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_rejects_a_too_wide_spread(self):
        """A relative spread beyond the latch ceiling (2%) is a broken quote."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=100.0, ask=105.0), received_at=_T0)  # 5% spread
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_admits_a_spread_at_the_ceiling(self):
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=100.0, ask=102.0), received_at=_T0)  # exactly 2%
        self.assertEqual(c.drain_running_low(211), 100.0)

    def test_latch_rejects_a_missing_side(self):
        """An explicit-null bid (one-sided market / halt) plants no low."""
        c = QuoteCache()
        c.apply(
            {
                "Uic": 211,
                "LastUpdated": "2026-08-07T13:48:00Z",
                "Quote": {"Bid": None, "Ask": 314.04, "DelayedByMinutes": 0},
            },
            received_at=_T0,
        )
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_vetoes_not_raises_on_a_non_numeric_side(self):
        """CRITICAL: the latchable gate runs on the WS reader thread. A
        non-numeric bid/ask (a str, an explicit null) must VETO, never raise —
        an uncaught exception here is counted as a connection failure and, after
        six such frames, trips the reconnect breaker and darkens the whole live
        feed (entries AND live exits). No arithmetic touches a side until it is
        proven a finite number."""
        c = QuoteCache()
        for quote in (
            {"Bid": "N/A", "Ask": 314.04, "DelayedByMinutes": 0},
            {"Bid": 314.01, "Ask": "N/A", "DelayedByMinutes": 0},
            {"Bid": None, "Ask": None, "DelayedByMinutes": 0},
        ):
            c.apply(
                {"Uic": 211, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": quote},
                received_at=_T0,
            )  # must not raise
        self.assertIsNone(c.drain_running_low(211))

    def test_latch_updates_only_from_latchable_ticks_amid_garbage(self):
        """A single transient garbage tick (crossed) between clean ticks must
        not corrupt the running low; the clean minimum still wins."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=313.90, ask=313.93), received_at=_T0)
        c.apply(self._latch_row(1, bid=313.60, ask=313.50), received_at=_T0)  # crossed, ignored
        c.apply(self._latch_row(2, bid=313.85, ask=313.88), received_at=_T0)
        self.assertEqual(c.drain_running_low(211), 313.85)

    # --- reseed_running_low (2026-08-18 point-veto incident) -------------------

    def test_reseed_restores_a_drained_low_for_the_next_drain(self):
        """The 2026-08-18 incident shape: the caller drained a REAL touch's low
        (OLN bid 18.61 below the 18.6217 tier limit) but its concurrent
        point-sample was veto-stale, so no decision could act on it. Handing the
        low back must make the NEXT drain return it — the touch evidence
        survives instead of being destroyed by the unconditional pop."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=18.61, ask=18.62), received_at=_T0)
        drained = c.drain_running_low(211)
        self.assertEqual(drained, 18.61)
        c.reseed_running_low(211, drained)
        self.assertEqual(c.drain_running_low(211), 18.61)

    def test_drain_after_reseed_still_pops_and_resets(self):
        """The reseed restores the value, not the semantics: the next drain is
        still a pop, so the window after it starts empty again."""
        c = QuoteCache()
        c.reseed_running_low(211, 18.61)
        self.assertEqual(c.drain_running_low(211), 18.61)
        self.assertIsNone(c.drain_running_low(211))

    def test_reseed_min_merges_keeping_a_deeper_fresh_accrual(self):
        """A newer 1 Hz accrual (18.59) landed between the drain and the
        reseed: the reseeded 18.61 must not overwrite the deeper low."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=18.59, ask=18.60), received_at=_T0)
        c.reseed_running_low(211, 18.61)
        self.assertEqual(c.drain_running_low(211), 18.59)

    def test_reseed_min_merges_under_a_shallower_fresh_accrual(self):
        """The mirror case: the fresh accrual (18.65) sits ABOVE the reseeded
        low, so the reseeded 18.61 wins the min-merge."""
        c = QuoteCache()
        c.apply(self._latch_row(0, bid=18.65, ask=18.66), received_at=_T0)
        c.reseed_running_low(211, 18.61)
        self.assertEqual(c.drain_running_low(211), 18.61)

    def test_reseed_ignores_a_non_finite_or_non_positive_low(self):
        """Same veto-not-raise discipline as the latch gate: a doubtful value
        must never plant a phantom low (and must never raise into the tick)."""
        c = QuoteCache()
        for garbage in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            c.reseed_running_low(211, garbage)  # must not raise
        self.assertIsNone(c.drain_running_low(211))


class TestLatchSpreadCeilingMirrorsContract(unittest.TestCase):
    def test_latch_spread_ceiling_equals_broker_contract_value(self):
        """``_LATCH_MAX_RELATIVE_SPREAD`` is a VALUE copy of
        ``broker_contract.price_feed.DEFAULT_MAX_RELATIVE_SPREAD`` (the data
        package must not import the decision layer). Pin them equal so the copy
        cannot silently drift from the shared is_fresh ceiling."""
        from broker_contract.price_feed import DEFAULT_MAX_RELATIVE_SPREAD

        self.assertEqual(sps._LATCH_MAX_RELATIVE_SPREAD, DEFAULT_MAX_RELATIVE_SPREAD)


class _FakeMarketDataClient:
    """Stand-in for SaxoMarketDataClient - _apply_frame never touches it."""


class _FakeTokenProvider:
    """Stand-in for LiveTokenProvider - _apply_frame never touches it."""


class _ResolvingClient:
    """Stand-in for SaxoMarketDataClient exposing only resolve_uic, records
    every call so a test can assert on cache-hit vs cache-miss behavior.
    Upper-cases like the real client does (see its docstring) so the fixture
    matches production instead of asserting on a fake-only behavior."""

    def __init__(self, responses: dict[tuple[str, str], int | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
        self.calls.append((ticker, exchange_mic))
        return self._responses.get((ticker.upper(), exchange_mic.upper()))


class TestLiveUicFor(unittest.TestCase):
    """Fix round 2 (Task 7 review), finding 3: live_uic_for had zero hermetic
    coverage. Pins the cache-on-success-only contract its own docstring
    claims."""

    def test_a_successful_resolution_is_cached_not_re_resolved(self):
        client = _ResolvingClient({("AAPL", "XNYS"): 211})
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNYS"), 211)
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNYS"), 211)
        self.assertEqual(client.calls, [("AAPL", "XNYS")])  # ONE REST call, not two

    def test_a_failed_resolution_is_retried_not_cached(self):
        """A None (unknown venue, no match, ambiguous match, or a transient
        failure) must NOT be cached -- caching it would veto that ticker for
        the rest of the process instead of retrying on the next tick."""
        client = _ResolvingClient({})  # every lookup misses
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        self.assertIsNone(stream.live_uic_for("QUBT", exchange_mic="ZZZZ"))
        self.assertIsNone(stream.live_uic_for("QUBT", exchange_mic="ZZZZ"))
        self.assertEqual(client.calls, [("QUBT", "ZZZZ"), ("QUBT", "ZZZZ")])  # retried both times

    def test_the_cache_key_is_upper_cased_ticker_and_venue(self):
        client = _ResolvingClient({("AAPL", "XNAS"): 5})
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        self.assertEqual(stream.live_uic_for("aapl", exchange_mic="xnas"), 5)
        self.assertEqual(stream.live_uic_for("AAPL", exchange_mic="XNAS"), 5)
        self.assertEqual(client.calls, [("aapl", "xnas")])  # 2nd call hit the cache


class TestStreamRunningLowPassthroughs(unittest.TestCase):
    """``SaxoPriceStream`` exposes the cache's touch-latch accumulator to the
    feed adapter as thin passthroughs — ``drain_running_low`` (pop) and
    ``reseed_running_low`` (min-merge hand-back for the point-veto case)."""

    def test_reseed_running_low_delegates_to_the_cache(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        stream.reseed_running_low(211, 18.61)
        self.assertEqual(stream.drain_running_low(211), 18.61)


def _build_frame(message_id: int, reference_id: str, payload: bytes, *, fmt: int = 0) -> bytes:
    """Encode one Saxo streaming envelope message (same layout as the SIM
    frame builder in test_saxo_streaming.py) so _apply_frame can be exercised
    without a real WebSocket."""
    ref = reference_id.encode("ascii")
    return (
        message_id.to_bytes(8, "little")
        + b"\x00\x00"  # reserved
        + bytes([len(ref)])
        + ref
        + bytes([fmt])
        + len(payload).to_bytes(4, "little")
        + payload
    )


class TestDefaultContextId(unittest.TestCase):
    """A fixed default context_id means a rebuild after a dead reader thread
    (nothing calls stop()) re-POSTs the SAME ContextId+ReferenceId, and two
    processes on the same LIVE login would collide too. The default must be
    per-process unique, mirroring the SIM auto-manager's
    f"almgr-{os.getpid()}-{int(time.time())}" convention (control_loop.py),
    with a "px" marker distinguishing the two."""

    def test_default_context_id_is_per_process_unique_with_px_marker(self):
        with (
            mock.patch.object(sps.os, "getpid", return_value=4242),
            mock.patch.object(sps.time, "time", return_value=1754570000.0),
        ):
            stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        self.assertEqual(stream._context_id, "almgr-px-4242-1754570000")

    def test_two_instances_in_the_same_process_get_different_context_ids(self):
        """Constructed at different times (the getpid stays fixed, the clock
        moves), so a rebuilt stream never reuses the dead stream's ContextId."""
        with mock.patch.object(sps.os, "getpid", return_value=4242):
            with mock.patch.object(sps.time, "time", return_value=1754570000.0):
                first = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
            with mock.patch.object(sps.time, "time", return_value=1754570099.0):
                second = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        self.assertNotEqual(first._context_id, second._context_id)

    def test_context_id_stays_injectable_for_tests(self):
        stream = SaxoPriceStream(
            _FakeMarketDataClient(), _FakeTokenProvider(), context_id="custom-ctx"
        )
        self.assertEqual(stream._context_id, "custom-ctx")


class TestSaxoPriceStreamApplyFrame(unittest.TestCase):
    """_apply_frame is synchronous decode-and-apply glue - testable directly,
    without mocking a socket (only the async recv loop needs the live probe)."""

    def test_malformed_non_dict_row_is_dropped_with_a_debug_log(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        payload = json.dumps(
            [
                {"Uic": 5, "LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}},
                "garbage-not-a-row",
                None,
            ]
        ).encode("utf-8")
        frame = _build_frame(1, "px", payload)
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="DEBUG"
        ) as cm:
            stream._apply_frame(frame)
        self.assertTrue(
            any("garbage-not-a-row" in line or "non-dict" in line.lower() for line in cm.output),
            cm.output,
        )
        q = stream.get(5)
        self.assertEqual(q.bid, 1.0)


class _ReclaimTrackingClient:
    """Stand-in for SaxoMarketDataClient exposing only ``elevate_session``,
    used to assert the reclaim wiring without a real HTTP call."""

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    def elevate_session(self) -> bool:
        self.calls += 1
        return next(self._outcomes)


class TestApplyFrameDesiredSetFilter(unittest.TestCase):
    """A WS row for a uic OUTSIDE the desired set must not land in the cache:
    a late delta racing past _recreate_subscription's final prune could
    otherwise resurrect a forgotten uic (and, with a positive delay, pin
    ``any_delayed`` forever) with no dirty flag left to trigger another
    prune. Eventual invariant: cached uics ⊆ desired."""

    def test_row_for_an_undesired_uic_is_dropped(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        stream.ensure_subscribed({7}, scope="t")  # 5 is NOT desired
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))  # row uic=5
        self.assertIsNone(stream.get(5))
        self.assertFalse(stream.cache.any_delayed())

    def test_row_for_a_desired_uic_still_applies(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=0))
        self.assertEqual(stream.get(5).bid, 1.0)


def _delayed_frame(message_id: int, *, delayed_by_minutes: int) -> bytes:
    payload = json.dumps(
        [
            {
                "Uic": 5,
                "LastUpdated": f"2026-08-07T13:48:{message_id:02d}Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": delayed_by_minutes},
            }
        ]
    ).encode("utf-8")
    return _build_frame(message_id, "px", payload)


class TestSaxoPriceStreamReclaim(unittest.TestCase):
    """The reclaim fires on a TRANSITION into the delayed state, not once per
    message - a 1 Hz stream would otherwise burn the whole hourly budget in
    seconds."""

    def test_delayed_transition_triggers_one_reclaim_attempt(self):
        client = _ReclaimTrackingClient([True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_reclaim_does_not_fire_again_while_still_delayed(self):
        client = _ReclaimTrackingClient([True, True, True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_reclaim_fires_again_after_recovering_then_delaying_once_more(self):
        client = _ReclaimTrackingClient([True, True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=0))
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))
        self.assertEqual(client.calls, 2)

    def test_healthy_stream_never_calls_elevate(self):
        client = _ReclaimTrackingClient([])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=0))
        self.assertEqual(client.calls, 0)

    def test_string_delayed_delta_still_triggers_the_reclaim(self):
        """End-to-end through ``_apply_frame``: a delta carrying
        DelayedByMinutes as a JSON string must neither raise out of
        ``_maybe_reclaim`` (a counted failure on the reader thread) nor lose
        the demotion signal — the coerced 15 still fires the reclaim."""
        client = _ReclaimTrackingClient([True])
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        payload = json.dumps(
            [
                {
                    "Uic": 5,
                    "LastUpdated": "2026-08-07T13:48:01Z",
                    "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": "15"},
                }
            ]
        ).encode("utf-8")
        stream._apply_frame(_build_frame(1, "px", payload))  # must not raise
        self.assertEqual(client.calls, 1)

    def test_budget_exhausted_logs_a_warning_and_leaves_the_quote_delayed(self):
        """No bypass: the freshness gate already vetoes delayed quotes, so the
        safe outcome on exhaustion is automatic - do nothing and wait for the
        budget to refill."""
        exhausted_limiter = ReclaimLimiter(
            max_per_hour=0, clock=lambda: dt.datetime(2026, 8, 7, 13, 48, tzinfo=dt.UTC)
        )
        client = _ReclaimTrackingClient([])
        stream = SaxoPriceStream(client, _FakeTokenProvider(), reclaim_limiter=exhausted_limiter)
        stream.ensure_subscribed({5}, scope="t")
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
        ) as cm:
            stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.assertTrue(any("budget" in line.lower() for line in cm.output), cm.output)
        self.assertEqual(client.calls, 0)
        self.assertEqual(stream.get(5).delayed_by_minutes, 15)


class _SteppingClock:
    """Deterministic stand-in for the stream's injected ``clock`` — the same
    instant until ``advance`` moves it, so interval arithmetic is exact."""

    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


class TestSaxoPriceStreamReclaimRetry(unittest.TestCase):
    """Transition-only reclaim is not enough: when the elevation is immediately
    re-stolen (the operator logged into SaxoTraderGO), ``any_delayed`` stays
    True and the transition never re-fires — the session sat OrdersOnly/delayed
    for 20+ minutes live on 2026-08-18. While quotes STAY delayed the reclaim
    must retry every ``_RECLAIM_RETRY_INTERVAL_S``."""

    def _stream(self, outcomes: list[bool]) -> tuple[_ReclaimTrackingClient, SaxoPriceStream]:
        self.clock = _SteppingClock(_T0)
        client = _ReclaimTrackingClient(outcomes)
        stream = SaxoPriceStream(client, _FakeTokenProvider(), clock=self.clock)
        stream.ensure_subscribed({5}, scope="t")
        return client, stream

    def test_transition_attempt_still_fires_immediately(self):
        client, stream = self._stream([True])
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_no_retry_before_the_interval_elapses(self):
        client, stream = self._stream([True, True])
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.clock.advance(sps._RECLAIM_RETRY_INTERVAL_S - 1.0)
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=15))
        self.assertEqual(client.calls, 1)

    def test_retry_fires_once_the_interval_elapses_while_still_delayed(self):
        client, stream = self._stream([True, True])
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.clock.advance(sps._RECLAIM_RETRY_INTERVAL_S)
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=15))
        self.assertEqual(client.calls, 2)

    def test_retry_rearms_after_each_attempt_not_just_the_first(self):
        """The retry cadence is measured from the LAST attempt, not the
        transition: a stolen session must keep retrying every interval."""
        client, stream = self._stream([True, True, True])
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.clock.advance(sps._RECLAIM_RETRY_INTERVAL_S)
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=15))
        self.clock.advance(sps._RECLAIM_RETRY_INTERVAL_S - 1.0)
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))  # too early
        self.assertEqual(client.calls, 2)
        self.clock.advance(1.0)
        stream._apply_frame(_delayed_frame(4, delayed_by_minutes=15))
        self.assertEqual(client.calls, 3)

    def test_recovery_resets_so_the_next_transition_fires_immediately(self):
        client, stream = self._stream([True, True])
        stream._apply_frame(_delayed_frame(1, delayed_by_minutes=15))
        self.clock.advance(10.0)  # well inside the retry interval
        stream._apply_frame(_delayed_frame(2, delayed_by_minutes=0))  # session fresh again
        self.clock.advance(1.0)
        stream._apply_frame(_delayed_frame(3, delayed_by_minutes=15))  # new demotion
        self.assertEqual(client.calls, 2)


class _FakeSharedInstance:
    """Stand-in for SaxoPriceStream at the get_shared_price_stream() level -
    the getter only ever calls start() and is_running() on what it holds."""

    def __init__(self, *, running: bool) -> None:
        self._running = running
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_running(self) -> bool:
        return self._running


class TestGetSharedPriceStream(unittest.TestCase):
    """Fix round 2 (Task 7 review), finding 4: after the reconnect circuit
    breaker trips, _supervise returns but self._thread stays non-None, so a
    dead stream would sit in the module singleton silently for the rest of
    the process. These patch the construction chain (LiveAuthConfig,
    LiveTokenProvider, SaxoMarketDataClient, SaxoPriceStream) so no real
    network/auth is touched."""

    def setUp(self) -> None:
        # The singleton is process-global state; reset it around each test.
        patcher = mock.patch.object(sps, "_shared_stream", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patched_construction(self, *stream_instances):
        return (
            mock.patch.object(sps, "LiveAuthConfig"),
            mock.patch.object(sps, "LiveTokenProvider"),
            mock.patch.object(sps, "SaxoMarketDataClient"),
            mock.patch.object(sps, "SaxoPriceStream", side_effect=list(stream_instances)),
        )

    def test_first_call_constructs_and_starts_the_stream(self):
        instance = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4:
            stream = sps.get_shared_price_stream()
        self.assertIs(stream, instance)
        self.assertTrue(instance.started)

    def test_a_running_stream_is_reused_not_rebuilt(self):
        instance = _FakeSharedInstance(running=True)
        # side_effect has exactly ONE instance -- a second construction
        # attempt raises StopIteration, failing the test loudly.
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4:
            first = sps.get_shared_price_stream()
            second = sps.get_shared_price_stream()
        self.assertIs(first, second)

    def test_a_dead_reader_thread_is_rebuilt_not_reused(self):
        """The regression this finding is about: a stream whose reader thread
        died (circuit breaker tripped) must be replaced, not handed back
        forever with only a log warning as the trace."""
        dead = _FakeSharedInstance(running=False)
        alive = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(dead, alive)
        with p1, p2, p3, p4:
            first = sps.get_shared_price_stream()
            self.assertIs(first, dead)
            second = sps.get_shared_price_stream()
        self.assertIs(second, alive)
        self.assertTrue(alive.started)

    def test_default_metrics_job_is_forwarded_to_construction(self) -> None:
        """ADR 0016 D5: with no override the singleton keeps the standalone/
        test default ``"live-price-stream"`` — unchanged behavior for a caller
        that never resolves a broker instance."""
        instance = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4 as mock_cls:
            sps.get_shared_price_stream()
        self.assertEqual(mock_cls.call_args.kwargs["metrics_job"], "live-price-stream")

    def test_default_session_window_is_none(self) -> None:
        """No gate wired -> None reaches the constructor -> today's behavior
        (the stream-side fail-open contract treats None as always-in-session)."""
        instance = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4 as mock_cls:
            sps.get_shared_price_stream()
        self.assertIsNone(mock_cls.call_args.kwargs["session_window"])

    def test_explicit_session_window_is_forwarded_to_construction(self) -> None:
        """The composition root's env-gated predicate must reach the
        SaxoPriceStream constructor unchanged on the constructing call."""
        instance = _FakeSharedInstance(running=True)
        predicate = lambda: True  # noqa: E731 — identity is what is asserted
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4 as mock_cls:
            sps.get_shared_price_stream(session_window=predicate)
        self.assertIs(mock_cls.call_args.kwargs["session_window"], predicate)

    def test_explicit_metrics_job_is_forwarded_to_construction(self) -> None:
        """The composition root (control_loop._default_live_exits_feed_factory)
        injects ``state_paths.price_stream_metrics_job()`` here every tick — it
        must reach the SaxoPriceStream constructor unchanged."""
        instance = _FakeSharedInstance(running=True)
        p1, p2, p3, p4 = self._patched_construction(instance)
        with p1, p2, p3, p4 as mock_cls:
            sps.get_shared_price_stream(metrics_job="live-price-stream-sim")
        self.assertEqual(mock_cls.call_args.kwargs["metrics_job"], "live-price-stream-sim")


class _ScriptedConn:
    """Fake WebSocket connection driven by a script of items: a bytes frame is
    delivered, an exception instance is raised, a callable is invoked (its
    return delivered — used to flip ``stream._stop`` mid-scenario). An
    exhausted script hangs forever, mirroring a healthy-but-quiet socket."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.closed = False

    async def recv(self):
        if not self._script:
            await asyncio.sleep(3600)
        item = self._script.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class _SubTrackingClient:
    """Stand-in for SaxoMarketDataClient recording subscription REST calls."""

    def __init__(self) -> None:
        self.creates: list[tuple[str, str, tuple[int, ...]]] = []
        self.deletes: list[tuple[str, str]] = []

    def create_price_subscription(self, *, context_id, reference_id, uics, refresh_rate_ms):
        self.creates.append((context_id, reference_id, tuple(uics)))
        return {}

    def delete_price_subscription(self, context_id, reference_id):
        self.deletes.append((context_id, reference_id))


class _SnapshotClient(_SubTrackingClient):
    """``_SubTrackingClient`` whose ``create_price_subscription`` returns a
    configured response body — the create-subscription snapshot is the ONLY
    carrier of ``DelayedByMinutes`` on a real-time session (2026-08-18 probe:
    WS deltas omit unchanged/zero fields, so a discarded snapshot leaves
    ``delayed_by_minutes`` None for the process lifetime)."""

    def __init__(self, response) -> None:
        super().__init__()
        self._response = response

    def create_price_subscription(self, **kwargs):
        super().create_price_subscription(**kwargs)
        return self._response


def _px_frame(message_id: int, uic: int = 5) -> bytes:
    payload = json.dumps(
        [
            {
                "Uic": uic,
                "LastUpdated": f"2026-08-10T14:48:{message_id % 60:02d}Z",
                "Quote": {"Bid": 1.0, "Ask": 1.1, "DelayedByMinutes": 0},
            }
        ]
    ).encode("utf-8")
    return _build_frame(message_id, "px", payload)


class _StaticTokenProvider:
    """Token provider for connection scenarios — ``_run_one_connection`` reads
    ``access_token()`` when building the WS auth header."""

    def access_token(self) -> str:
        return "tok"


class _SupervisedHarness:
    """Drive ``stream._supervise()`` hermetically: scripted connections, an
    instant fake ``async_sleep`` that can stop the loop after N idle sleeps,
    and a recorded ``ws_connect``."""

    def __init__(self, stream_kwargs=None, conns=None, stop_after_sleeps=None, client=None):
        self.client = client if client is not None else _SubTrackingClient()
        self.ws_calls: list[str] = []
        self.sleeps: list[float] = []
        self._conns = list(conns or [])
        self._stop_after_sleeps = stop_after_sleeps

        async def ws_connect(url: str, headers: dict) -> _ScriptedConn:
            self.ws_calls.append(url)
            if not self._conns:
                raise AssertionError("ws_connect called with no scripted connection left")
            return self._conns.pop(0)

        async def async_sleep(delay: float) -> None:
            self.sleeps.append(delay)
            if self._stop_after_sleeps is not None and len(self.sleeps) >= self._stop_after_sleeps:
                self.stream._stop = True

        self.stream = SaxoPriceStream(
            self.client,
            _StaticTokenProvider(),
            ws_connect=ws_connect,
            async_sleep=async_sleep,
            **(stream_kwargs or {}),
        )

    def run(self) -> None:
        asyncio.run(self.stream._supervise())


class TestEnsureSubscribedOwnership(unittest.TestCase):
    """R3 + the reworked ownership: ``ensure_subscribed`` only mutates the
    DESIRED set (caller thread) and forgets removed uics; ALL subscription
    REST traffic belongs to the reader thread, which recreates on every new
    connection. This kills both the caller/reader REST race and the
    stale-delayed-quote pin on ``any_delayed``."""

    def test_removed_uic_is_forgotten_from_the_cache(self):
        stream = SaxoPriceStream(_SubTrackingClient(), _FakeTokenProvider())
        stream.cache.apply(_row(Uic=7), received_at=_T0)
        stream.ensure_subscribed({7, 8})
        stream.ensure_subscribed({8})
        self.assertIsNone(stream.get(7), "an unsubscribed uic's quote must be forgotten")

    def test_caller_thread_never_talks_subscription_rest(self):
        client = _SubTrackingClient()
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream.ensure_subscribed({5})
        stream.ensure_subscribed({5, 6})
        stream.ensure_subscribed(set())
        self.assertEqual(client.creates, [])
        self.assertEqual(client.deletes, [])


class TestEnsureSubscribedScopes(unittest.TestCase):
    """Per-scope desired sets with union subscription (2026-08-18 incident):
    one daemon tick builds several feeds off the ONE shared stream (live
    exits, peak update, entry-watch), each calling ``ensure_subscribed`` with
    only ITS uics. Replace-the-whole-set semantics made those calls fight —
    zero open positions + one entry watch alternated the desired set
    {} <-> {watch} every tick, deleting and recreating the single server-side
    subscription and starving the reader into a ~90s recv-timeout reconnect
    loop. Each caller now owns a SCOPE; the wire-level set is the union."""

    def _stream(self) -> SaxoPriceStream:
        return SaxoPriceStream(_SubTrackingClient(), _FakeTokenProvider())

    def test_disjoint_scopes_union_into_the_desired_set(self):
        stream = self._stream()
        stream.ensure_subscribed({5}, scope="exits")
        stream.ensure_subscribed({7}, scope="entry-watch")
        self.assertEqual(stream._desired_uics(), {5, 7})

    def test_alternating_empty_exits_and_entry_watch_is_a_complete_no_op(self):
        """THE production bug: with zero open positions and one entry-trail
        watch, every tick called in exits={} then entry-watch={N}. That pair
        repeating must set the dirty flag ZERO additional times and never
        forget N — an unchanged union may not touch the wire."""
        stream = self._stream()
        stream.ensure_subscribed({5}, scope="entry-watch")
        stream.cache.apply(_row(Uic=5), received_at=_T0)
        stream._sub_dirty.clear()
        for _ in range(5):
            stream.ensure_subscribed(set(), scope="exits")
            stream.ensure_subscribed({5}, scope="entry-watch")
        self.assertFalse(stream._sub_dirty.is_set())
        self.assertIsNotNone(stream.get(5), "the watched uic's quote must survive")

    def test_uic_is_forgotten_only_when_it_leaves_the_last_scope_holding_it(self):
        stream = self._stream()
        stream.ensure_subscribed({7}, scope="exits")
        stream.ensure_subscribed({7}, scope="entry-watch")
        stream.cache.apply(_row(Uic=7), received_at=_T0)
        stream._sub_dirty.clear()
        stream.ensure_subscribed(set(), scope="exits")  # entry-watch still holds 7
        self.assertFalse(stream._sub_dirty.is_set())
        self.assertIsNotNone(stream.get(7))
        stream.ensure_subscribed(set(), scope="entry-watch")  # last holder gone
        self.assertTrue(stream._sub_dirty.is_set())
        self.assertIsNone(stream.get(7))
        self.assertEqual(stream._desired_uics(), set())

    def test_same_scope_replacement_still_unsubscribes_a_dropped_uic(self):
        stream = self._stream()
        stream.ensure_subscribed({7, 8}, scope="exits")
        stream.cache.apply(_row(Uic=7), received_at=_T0)
        stream._sub_dirty.clear()
        stream.ensure_subscribed({8}, scope="exits")
        self.assertTrue(stream._sub_dirty.is_set())
        self.assertIsNone(stream.get(7))
        self.assertEqual(stream._desired_uics(), {8})

    def test_growing_a_scope_arms_the_dirty_flag(self):
        stream = self._stream()
        stream.ensure_subscribed({5}, scope="exits")
        stream._sub_dirty.clear()
        stream.ensure_subscribed({5, 6}, scope="exits")
        self.assertTrue(stream._sub_dirty.is_set())
        self.assertEqual(stream._desired_uics(), {5, 6})

    def test_stop_best_effort_deletes_when_any_scope_holds_a_uic(self):
        """``stop()`` must read the union, not a dead attribute: a subscription
        desired by ANY scope still exists server-side and deserves the
        best-effort DELETE."""
        client = _SubTrackingClient()
        stream = SaxoPriceStream(client, _FakeTokenProvider())
        stream.ensure_subscribed({5}, scope="t")
        stream.ensure_subscribed({5}, scope="entry-watch")
        stream.stop(timeout=0.1)
        self.assertEqual(len(client.deletes), 1)


# The exact row shape a live create-subscription response carries in
# Snapshot.Data (probe evidence 2026-08-18) — the SAME shape as a WS delta
# row, so QuoteCache.apply consumes it unchanged.
_SNAPSHOT_ROW = {
    "AssetType": "Stock",
    "DisplayAndFormat": {"Symbol": "OLN:xnys"},
    "LastUpdated": "2026-08-07T13:47:58Z",
    "PriceInfo": {"High": 18.80, "Low": 18.50},
    "PriceSource": "NYSE",
    "Quote": {"Bid": 18.70, "Ask": 18.71, "DelayedByMinutes": 0, "MarketState": "Open"},
    "Uic": 211,
}


def _delta_frame_without_delay(
    message_id: int, *, bid: float, ask: float, second: int, uic: int = 211
) -> bytes:
    """A WS delta frame as a REAL-TIME session delivers it: Bid/Ask/LastUpdated
    but NO DelayedByMinutes key (Saxo omits unchanged/zero fields)."""
    payload = json.dumps(
        [
            {
                "Uic": uic,
                "LastUpdated": f"2026-08-07T13:48:{second:02d}Z",
                "Quote": {"Bid": bid, "Ask": ask},
            }
        ]
    ).encode("utf-8")
    return _build_frame(message_id, "px", payload)


class TestSubscriptionSnapshotSeedsTheCache(unittest.TestCase):
    """2026-08-18 LIVE incident root cause: ``_recreate_subscription``
    DISCARDED the create-subscription response, whose ``Snapshot.Data`` rows
    are the ONLY place Saxo reports ``DelayedByMinutes`` on a real-time
    session (WS deltas omit unchanged/zero fields). ``delayed_by_minutes``
    therefore stayed None for the process lifetime, the feed's strict
    ``!= 0`` check vetoed every point-sample, and the 1 Hz touch-latch never
    accrued — the entry-trail watch could never touch despite frames flowing
    with the bid below the watch limit."""

    def _stream(self, response) -> SaxoPriceStream:
        return SaxoPriceStream(_SnapshotClient(response), _FakeTokenProvider(), clock=lambda: _T0)

    @staticmethod
    def _subscribe_and_create(stream: SaxoPriceStream) -> None:
        stream.ensure_subscribed({211})
        stream._recreate_subscription()  # the reader-thread path under test

    def test_snapshot_seeds_delayed_zero_that_later_deltas_inherit(self):
        """THE regression: after the create + delay-less deltas, the cached
        quote's DelayedByMinutes must be the snapshot's confirmed 0 (inherited
        under apply()'s omitted-key semantics), not a permanent None."""
        stream = self._stream({"Snapshot": {"Data": [dict(_SNAPSHOT_ROW)]}})
        self._subscribe_and_create(stream)
        stream._apply_frame(_delta_frame_without_delay(1, bid=18.61, ask=18.62, second=1))
        q = stream.get(211)
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(q.bid, 18.61)

    def test_snapshot_received_at_comes_from_the_stream_clock(self):
        """``received_at`` must come from the SAME injected clock
        ``_apply_frame`` stamps deltas with — never a second clock source."""
        stream = self._stream({"Snapshot": {"Data": [dict(_SNAPSHOT_ROW)]}})
        self._subscribe_and_create(stream)
        self.assertEqual(stream.get(211).received_at, _T0)

    def test_touch_latch_accrues_from_delay_less_deltas_after_the_snapshot(self):
        """The live 30s reproduction shape: bids below the watch limit on
        delay-less deltas. With the snapshot applied the latch inherits the
        confirmed 0 and the drain returns the accrued low instead of None."""
        stream = self._stream({"Snapshot": {"Data": [dict(_SNAPSHOT_ROW)]}})
        self._subscribe_and_create(stream)
        stream._apply_frame(_delta_frame_without_delay(1, bid=18.61, ask=18.62, second=1))
        stream._apply_frame(_delta_frame_without_delay(2, bid=18.65, ask=18.66, second=2))
        self.assertEqual(stream.drain_running_low(211), 18.61)

    def test_feed_point_sample_is_not_vetoed_on_the_delayed_check(self):
        """The consumer-visible symptom: ``SaxoLivePriceFeed.latest`` returns
        None whenever ``delayed_by_minutes != 0`` — including the permanent
        None. With the snapshot applied, a fresh delay-less delta must yield a
        real PricePoint."""
        from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed

        stream = self._stream({"Snapshot": {"Data": [dict(_SNAPSHOT_ROW)]}})
        self._subscribe_and_create(stream)
        stream._apply_frame(_delta_frame_without_delay(1, bid=18.61, ask=18.62, second=1))
        feed = SaxoLivePriceFeed(
            stream=stream,
            resolve_live_uic=lambda _uic: 211,
            clock=lambda: dt.datetime(2026, 8, 7, 13, 48, 2, tzinfo=dt.UTC),
        )
        point = feed.latest(999)
        self.assertIsNotNone(point)
        self.assertEqual((point.bid, point.ask), (18.61, 18.62))

    def test_malformed_snapshot_response_shapes_never_raise(self):
        """Veto-not-raise: ``_recreate_subscription`` runs on the reader
        thread, where an uncaught exception is a counted connection failure —
        a payload-shape change must never burn the reconnect breaker budget."""
        for response in (
            None,
            [],
            "not-a-dict",
            {},
            {"Snapshot": None},
            {"Snapshot": []},
            {"Snapshot": {}},
            {"Snapshot": {"Data": None}},
            {"Snapshot": {"Data": "not-a-list"}},
            {"Snapshot": {"Data": {"Uic": 211}}},
        ):
            with self.subTest(response=response):
                stream = self._stream(response)
                self._subscribe_and_create(stream)  # must not raise
                self.assertIsNone(stream.get(211))

    def test_malformed_snapshot_rows_are_skipped_and_a_good_row_still_applies(self):
        """A non-dict row or a garbage-Uic row is dropped silently (same
        discipline as ``_apply_frame``'s row handling) while the good row in
        the same snapshot still seeds the cache."""
        response = {
            "Snapshot": {
                "Data": [None, "garbage-not-a-row", {"Uic": "not-a-number"}, dict(_SNAPSHOT_ROW)]
            }
        }
        stream = self._stream(response)
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
        ) as cm:
            self._subscribe_and_create(stream)  # must not raise
        self.assertTrue(any("uic" in line.lower() for line in cm.output), cm.output)
        self.assertEqual(stream.get(211).delayed_by_minutes, 0)

    def test_snapshot_row_with_non_dict_quote_block_never_raises(self):
        """The deterministic-breaker arm of the veto contract: unlike a
        one-off malformed WS frame, the create-subscription REST body repeats
        IDENTICALLY on every reconnect, so a raise on a truthy non-dict Quote
        would burn the whole breaker budget in six creates. The dict-row guard
        alone does not cover it — the row IS a dict; its Quote is not."""
        response = {
            "Snapshot": {
                "Data": [
                    {"Uic": 300, "LastUpdated": "2026-08-07T13:47:58Z", "Quote": "Delayed"},
                    dict(_SNAPSHOT_ROW),
                ]
            }
        }
        stream = self._stream(response)
        with self.assertLogs(
            "alphalens_pipeline.data.alt_data.saxo_price_stream", level="WARNING"
        ) as cm:
            self._subscribe_and_create(stream)  # must not raise
        self.assertTrue(any("quote" in line.lower() for line in cm.output), cm.output)
        self.assertIsNone(stream.get(300))
        self.assertEqual(stream.get(211).delayed_by_minutes, 0)

    def test_snapshot_string_delayed_seeds_a_coerced_int_and_any_delayed_survives(self):
        """The snapshot is what first populates delayed_by_minutes on a
        real-time session — if it seeds an uncoerced string, the NEXT frame's
        ``_maybe_reclaim -> any_delayed`` raises TypeError, the connection
        dies, and the deterministic REST body re-seeds the same string until
        the breaker trips. The seed must coerce: signal kept, no raise."""
        row = dict(_SNAPSHOT_ROW)
        row["Quote"] = {**_SNAPSHOT_ROW["Quote"], "DelayedByMinutes": "15"}
        stream = self._stream({"Snapshot": {"Data": [row]}})
        self._subscribe_and_create(stream)
        self.assertTrue(stream.cache.any_delayed())  # must not raise
        self.assertEqual(stream.get(211).delayed_by_minutes, 15)


class TestSuperviseAppliesTheCreateSnapshot(unittest.TestCase):
    """End-to-end through the reader loop: the connection's own
    ``_recreate_subscription`` (not a test-called one) must seed the cache
    before the first delta arrives."""

    def test_snapshot_applies_on_connection_before_the_first_delta(self):
        client = _SnapshotClient({"Snapshot": {"Data": [dict(_SNAPSHOT_ROW)]}})
        h = _SupervisedHarness(client=client)
        stream = h.stream
        conn = _ScriptedConn(
            [
                _delta_frame_without_delay(1, bid=18.61, ask=18.62, second=1),
                lambda: (
                    setattr(stream, "_stop", True),
                    _delta_frame_without_delay(2, bid=18.63, ask=18.64, second=2),
                )[1],
            ]
        )
        h._conns.append(conn)
        stream.ensure_subscribed({211})
        h.run()
        q = stream.get(211)
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(stream.drain_running_low(211), 18.61)


class _RacingRemovalClient(_SubTrackingClient):
    """The finding-3 race made deterministic: the caller's ``ensure_subscribed``
    removes a uic BETWEEN the reader's desired-set read and the create
    response. The fake's create reflects the REQUESTED uics in its snapshot
    (like the real venue) and, on the FIRST create only, performs the
    caller-side removal before returning — so the response resurrects the
    just-forgotten uic 212 with a delayed row."""

    def __init__(self) -> None:
        super().__init__()
        self.stream: SaxoPriceStream | None = None

    @staticmethod
    def _snapshot_row(uic: int) -> dict:
        return {
            "Uic": uic,
            "LastUpdated": "2026-08-07T13:47:58Z",
            "Quote": {"Bid": 18.70, "Ask": 18.71, "DelayedByMinutes": 15 if uic == 212 else 0},
        }

    def create_price_subscription(self, **kwargs):
        super().create_price_subscription(**kwargs)
        if self.stream is not None and len(self.creates) == 1:
            self.stream.ensure_subscribed({211})  # caller lands mid-create: 212 leaves
        return {"Snapshot": {"Data": [self._snapshot_row(u) for u in kwargs["uics"]]}}


class TestRecreatePrunesResurrectedQuotes(unittest.TestCase):
    """Finding-3 race: ``ensure_subscribed``'s forget-on-removal can be undone
    by an in-flight create's snapshot applied AFTER the forget — pre-snapshot
    this window was benign (WS deltas omit DelayedByMinutes), but a snapshot
    row reporting a delay would pin ``any_delayed`` True for the process
    lifetime, retrying ``elevate_session`` every 5 minutes forever. Every
    recreate must therefore prune the cache to a FRESH desired-set read; a
    removal that still slips through re-arms the dirty flag, so the loop's
    follow-up recreate prunes it (eventual invariant: cached uics ⊆ desired)."""

    def test_in_flight_snapshot_cannot_pin_a_removed_uic_forever(self):
        client = _RacingRemovalClient()
        stream = SaxoPriceStream(client, _FakeTokenProvider(), clock=lambda: _T0)
        client.stream = stream
        stream.ensure_subscribed({211, 212})
        stream._recreate_subscription()  # in-flight create resurrects 212 after the forget
        self.assertTrue(stream._sub_dirty.is_set(), "the caller's removal re-armed the flag")
        stream._recreate_subscription()  # the loop's guaranteed dirty follow-up
        self.assertIsNone(stream.get(212), "the resurrected quote must be pruned")
        self.assertIsNone(stream.cache.drain_running_low(212))
        self.assertFalse(stream.cache.any_delayed(), "a stale delayed row must not pin any_delayed")
        self.assertIsNotNone(stream.get(211), "the still-desired uic keeps its quote")


class TestSuperviseIdleWithoutSubscriptions(unittest.TestCase):
    """R4: a WS held open with ZERO subscriptions is exactly what an idle-kill
    turns into a failure storm — with nothing subscribed the reader must not
    connect at all."""

    def test_empty_desired_set_never_connects(self):
        h = _SupervisedHarness(stop_after_sleeps=3)
        h.run()
        self.assertEqual(h.ws_calls, [])
        self.assertEqual(h.stream._consecutive_failures, 0)


class TestReconnectRecreatesSubscription(unittest.TestCase):
    """R1: every connection attempt gets a FRESH contextId and re-creates the
    price subscription server-side — a reconnect can never resume into a
    context whose subscription Saxo already dropped."""

    def test_each_connection_rotates_context_and_recreates(self):
        h = _SupervisedHarness()
        stream = h.stream
        conn1 = _ScriptedConn([_px_frame(1), ConnectionError("dropped")])
        conn2 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(2))[1]])
        h._conns.extend([conn1, conn2])
        stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(len(h.ws_calls), 2)
        ctx1 = h.ws_calls[0].split("contextId=")[1]
        ctx2 = h.ws_calls[1].split("contextId=")[1]
        self.assertNotEqual(ctx1, ctx2, "each connection must use a fresh contextId")
        self.assertEqual(len(h.client.creates), 2, "subscription recreated per connection")
        self.assertEqual(h.client.creates[0][0], ctx1)
        self.assertEqual(h.client.creates[1][0], ctx2)
        self.assertEqual(h.client.creates[0][2], (5,))

    def test_a_delivered_frame_resets_the_failure_counter(self):
        h = _SupervisedHarness()
        stream = h.stream
        conn1 = _ScriptedConn([ConnectionError("dead on arrival")])
        conn2 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(2))[1]])
        h._conns.extend([conn1, conn2])
        stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(stream._consecutive_failures, 0)


class TestRecvTimeoutReconnects(unittest.TestCase):
    """R2: a half-open socket must not wedge the reader forever — recv is
    bounded and a timeout counts as a connection failure (then reconnects)."""

    def test_hung_recv_times_out_and_reconnects(self):
        with mock.patch.object(sps, "_RECV_TIMEOUT_S", 0.01):
            h = _SupervisedHarness()
            stream = h.stream
            conn1 = _ScriptedConn([])  # hangs forever
            conn2 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(2))[1]])
            h._conns.extend([conn1, conn2])
            stream.ensure_subscribed({5})
            h.run()
        self.assertEqual(len(h.ws_calls), 2, "timeout must trigger a reconnect")
        self.assertTrue(conn1.closed, "the hung connection must be closed")


class TestResetSubscriptionsControlFrame(unittest.TestCase):
    """Saxo's ``_resetsubscriptions`` control frame means the server dropped
    the subscription — the reader must recreate it on the SAME connection,
    not wait for a 45s recv-timeout to force a full reconnect."""

    def test_reset_frame_recreates_on_the_same_connection(self):
        h = _SupervisedHarness()
        stream = h.stream
        conn1 = _ScriptedConn(
            [
                _px_frame(1),
                _build_frame(2, "_resetsubscriptions", b"{}"),
                lambda: (setattr(stream, "_stop", True), _px_frame(3))[1],
            ]
        )
        h._conns.append(conn1)
        stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(len(h.ws_calls), 1)
        self.assertEqual(
            len(h.client.creates), 2, "reset frame must recreate the subscription in-place"
        )
        self.assertEqual(h.client.creates[0][0], h.client.creates[1][0])


class TestStreamGauges(unittest.TestCase):
    """O1: the stream must be observable from Prometheus — reader up/down,
    last-frame timestamp, consecutive failures, subscribed-uic count. Without
    these a dark feed is indistinguishable from a quiet one (the exact
    blindness of the 2026-08-10 incident)."""

    def _run_and_capture(self, harness: _SupervisedHarness) -> list[dict]:
        emitted: list[dict] = []
        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics",
            side_effect=lambda job, metrics: emitted.append(dict(metrics)),
        ):
            harness.run()
        return emitted

    @staticmethod
    def _value(metrics: dict, name_fragment: str):
        for key, value in metrics.items():
            if name_fragment in key:
                return value
        raise AssertionError(f"{name_fragment} not in {sorted(metrics)}")

    def test_reader_up_transitions_and_last_frame_stamp(self):
        h = _SupervisedHarness()
        stream = h.stream
        conn1 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn1)
        stream.ensure_subscribed({5})
        emitted = self._run_and_capture(h)
        self.assertGreaterEqual(len(emitted), 2)
        self.assertEqual(self._value(emitted[0], "reader_up"), 1)
        self.assertEqual(self._value(emitted[-1], "reader_up"), 0)
        self.assertGreater(
            self._value(emitted[-1], "last_frame_timestamp_seconds"),
            0,
            "a delivered frame must stamp last_frame",
        )
        self.assertEqual(self._value(emitted[-1], "subscribed_uics"), 1)

    def test_breaker_trip_emits_reader_down_with_failure_count(self):
        h = _SupervisedHarness()
        stream = h.stream
        h._conns.extend(_ScriptedConn([ConnectionError(f"fail {i}")]) for i in range(6))
        stream.ensure_subscribed({5})
        emitted = self._run_and_capture(h)
        final = emitted[-1]
        self.assertEqual(self._value(final, "reader_up"), 0)
        self.assertEqual(self._value(final, "consecutive_failures"), 6)


class TestStreamGaugeJobLabel(unittest.TestCase):
    """ADR 0016 D5: the gauge job label is a constructor parameter, injected
    by the composition root (``state_paths.price_stream_metrics_job()``) at
    ``get_shared_price_stream`` call time — never a fixed module constant, so
    a future LIVE instance's price stream never shares a Prometheus job (and
    thus textfile) with the SIM instance's."""

    def _run_and_capture(self, harness: _SupervisedHarness) -> list[tuple[str, dict]]:
        captured: list[tuple[str, dict]] = []
        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics",
            side_effect=lambda job, metrics: captured.append((job, dict(metrics))),
        ):
            harness.run()
        return captured

    def test_default_job_is_the_standalone_default(self) -> None:
        h = _SupervisedHarness()
        stream = h.stream
        conn1 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn1)
        stream.ensure_subscribed({5})
        captured = self._run_and_capture(h)
        self.assertTrue(captured)
        for job, metrics in captured:
            self.assertEqual(job, "live-price-stream")
            for name in metrics:
                self.assertIn('job="live-price-stream"', name)

    def test_injected_job_reaches_the_emitted_domain_and_labels(self) -> None:
        h = _SupervisedHarness(stream_kwargs={"metrics_job": "live-price-stream-sim"})
        stream = h.stream
        conn1 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn1)
        stream.ensure_subscribed({5})
        captured = self._run_and_capture(h)
        self.assertTrue(captured)
        for job, metrics in captured:
            self.assertEqual(job, "live-price-stream-sim")
            for name in metrics:
                self.assertIn('job="live-price-stream-sim"', name)


class TestSessionWindowGate(unittest.TestCase):
    """Outside-market-hours sleep gate: with the optional ``session_window``
    predicate wired and returning False, the reader holds NO WebSocket and
    idles exactly like the zero-desired-uics branch (the 24/7 connection used
    to recv-timeout every ~3min all night — reconnect churn + warning spam).

    HARD SAFETY RULE — fail-open everywhere: no predicate wired means today's
    behavior, and a RAISING predicate must be treated as in-session (connect)
    with exactly one warning per stream. The gate must never be able to
    silence the stream during trading hours."""

    _LOGGER = "alphalens_pipeline.data.alt_data.saxo_price_stream"

    def test_constructor_defaults_to_no_session_window(self):
        stream = SaxoPriceStream(_FakeMarketDataClient(), _FakeTokenProvider())
        self.assertIsNone(stream._session_window)

    def test_predicate_false_never_connects_and_idles(self):
        h = _SupervisedHarness(stream_kwargs={"session_window": lambda: False}, stop_after_sleeps=3)
        h.stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(h.ws_calls, [])
        self.assertEqual(h.sleeps, [sps._IDLE_POLL_S] * 3)
        self.assertEqual(h.stream._consecutive_failures, 0)

    def test_predicate_true_connects_via_the_existing_paths(self):
        h = _SupervisedHarness(stream_kwargs={"session_window": lambda: True})
        stream = h.stream
        conn = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn)
        stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(len(h.ws_calls), 1)
        self.assertEqual(stream.get(5).bid, 1.0)

    def test_no_predicate_behaves_like_today(self):
        """Spot-pin for the flag-off / un-wired case (byte-identical behavior
        is otherwise pinned by every pre-existing _SupervisedHarness test,
        none of which passes session_window)."""
        h = _SupervisedHarness()
        stream = h.stream
        conn = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn)
        stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(len(h.ws_calls), 1)
        self.assertEqual(stream.get(5).bid, 1.0)

    def test_raising_predicate_fails_open_and_warns_exactly_once(self):
        """A calendar bug must never darken the feed during trading hours: a
        raising predicate is treated as in-session (the reader connects), and
        the raise is logged ONCE per stream — not once per poll."""
        calls = {"count": 0}

        def raising_predicate() -> bool:
            calls["count"] += 1
            raise RuntimeError("calendar exploded")

        h = _SupervisedHarness(stream_kwargs={"session_window": raising_predicate})
        stream = h.stream
        # Two loop iterations (a dropped connection forces a second one), so
        # the predicate raises at least twice while the warning stays at one.
        conn1 = _ScriptedConn([ConnectionError("dropped")])
        conn2 = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(2))[1]])
        h._conns.extend([conn1, conn2])
        stream.ensure_subscribed({5})
        with self.assertLogs(self._LOGGER, level="WARNING") as cm:
            h.run()
        gate_warnings = [line for line in cm.output if "session window" in line.lower()]
        self.assertEqual(len(gate_warnings), 1, cm.output)
        self.assertGreaterEqual(calls["count"], 2)
        self.assertEqual(len(h.ws_calls), 2, "fail-open: the reader must still connect")

    def test_transitions_log_exactly_once_per_edge(self):
        """One INFO on awake->asleep, one INFO on asleep->awake — never one
        per idle poll (the whole point is killing the nightly log spam)."""
        scripted = [False, False, False]  # three asleep polls, then in-session

        def predicate() -> bool:
            return scripted.pop(0) if scripted else True

        h = _SupervisedHarness(stream_kwargs={"session_window": predicate})
        stream = h.stream
        conn = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn)
        stream.ensure_subscribed({5})
        with self.assertLogs(self._LOGGER, level="INFO") as cm:
            h.run()
        asleep_lines = [line for line in cm.output if "sleeping" in line.lower()]
        awake_lines = [line for line in cm.output if "resuming" in line.lower()]
        self.assertEqual(len(asleep_lines), 1, cm.output)
        self.assertEqual(len(awake_lines), 1, cm.output)
        self.assertEqual(len(h.ws_calls), 1)

    def test_entering_sleep_resets_the_failure_counter(self):
        """The post-close recv-timeout tail leaves 1-3 counted failures at
        sleep entry; carried over the night they would resume from that count
        at wake and could trip the circuit breaker mid-warmup. Entering sleep
        proves those failures were off-hours artifacts, not connection
        health — the counter must be zeroed on the awake->asleep edge."""
        scripted = [True]  # one in-session check (fails), then asleep

        def predicate() -> bool:
            return scripted.pop(0) if scripted else False

        h = _SupervisedHarness(stream_kwargs={"session_window": predicate}, stop_after_sleeps=3)
        h._conns.append(_ScriptedConn([ConnectionError("post-close timeout")]))
        h.stream.ensure_subscribed({5})
        h.run()
        self.assertEqual(len(h.ws_calls), 1, "the single in-session poll must have connected")
        self.assertEqual(
            h.stream._consecutive_failures,
            0,
            "the awake->asleep transition must zero the carried failure count",
        )

    def test_sleep_transitions_emit_the_session_asleep_gauge(self):
        """Prometheus must be able to tell INTENTIONAL overnight sleep from
        the dark-but-connected failure AlphalensLivePriceStreamStale pages
        on: each sleep/wake edge force-emits the gauges with
        ``session_asleep`` flipped (1 exactly once per sleep edge — the idle
        polls themselves stay emit-free, mirroring the zero-uics branch),
        and every emit carries the key so the series never goes absent."""
        scripted = [False, False]  # two asleep polls, then in-session

        def predicate() -> bool:
            return scripted.pop(0) if scripted else True

        h = _SupervisedHarness(stream_kwargs={"session_window": predicate})
        stream = h.stream
        conn = _ScriptedConn([lambda: (setattr(stream, "_stop", True), _px_frame(1))[1]])
        h._conns.append(conn)
        stream.ensure_subscribed({5})
        emitted: list[dict] = []
        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics",
            side_effect=lambda job, metrics: emitted.append(dict(metrics)),
        ):
            h.run()
        values = [TestStreamGauges._value(m, "session_asleep") for m in emitted]
        self.assertEqual(values[0], 0, "the startup emit must report awake")
        self.assertEqual(values.count(1), 1, f"one asleep emit per sleep edge, got {values}")
        self.assertEqual(values[-1], 0, "the final reader-down emit must report awake")


if __name__ == "__main__":
    unittest.main()
