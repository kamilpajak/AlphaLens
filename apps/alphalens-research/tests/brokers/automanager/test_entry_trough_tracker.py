"""T1a — the entry TROUGH tracker (entry-trailing design memo §5).

The exit side keeps a per-uic running HIGH (``control_loop._update_peaks``,
``max``, in-memory, restart reseeds DOWNWARD safely). The entry side needs the
MIRROR: a per-tier running LOW (``min``). Two properties are load-bearing and
DIFFER from the exit peak:

- it yields at most ONE journal-intent per TICK (memo §5: "<=1 line per tick,
  NOT per stream tick") — the daemon feeds it once per ~45s decision tick, so a
  new low emits exactly one ``trough`` line and a non-low emits none;
- restart resumes ``trough = min(journaled trough, first fresh price)`` — a
  downward reseed (what the exit peak does safely) is UNSAFE in the entry
  direction (it would forget the low the bounce is measured from), so the
  tracker is SEEDED with the journaled minimum and only ratchets down from it.

Pure in-memory unit: no I/O, no clock, no journal — it returns the value to
journal and the caller (T1b) wraps it into a ``trough`` intent.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_watcher import EntryTroughTracker


class TestEntryTroughTrackerRatchet(unittest.TestCase):
    def test_first_price_seeds_the_trough_and_journals_it(self) -> None:
        tracker = EntryTroughTracker()
        self.assertIsNone(tracker.trough)
        journaled = tracker.observe(10.0)
        self.assertEqual(journaled, 10.0)
        self.assertEqual(tracker.trough, 10.0)

    def test_a_new_low_ratchets_down_and_journals(self) -> None:
        tracker = EntryTroughTracker()
        tracker.observe(10.0)
        self.assertEqual(tracker.observe(9.5), 9.5)
        self.assertEqual(tracker.trough, 9.5)

    def test_a_higher_price_never_moves_the_trough_and_journals_nothing(self) -> None:
        # The trough is a MIN, not a last-price: a bounce must not raise it,
        # and must not emit a journal line this tick.
        tracker = EntryTroughTracker()
        tracker.observe(9.5)
        self.assertIsNone(tracker.observe(9.9))
        self.assertIsNone(tracker.observe(10.4))
        self.assertEqual(tracker.trough, 9.5)

    def test_an_equal_price_is_not_a_new_low(self) -> None:
        tracker = EntryTroughTracker()
        tracker.observe(9.5)
        self.assertIsNone(tracker.observe(9.5))
        self.assertEqual(tracker.trough, 9.5)

    def test_one_journal_intent_per_tick_even_across_a_long_decline(self) -> None:
        # Each observe() call is ONE tick; a monotone decline emits exactly one
        # line per tick (never a burst), and the running min is the last value.
        tracker = EntryTroughTracker()
        emitted = [tracker.observe(10.0 - i * 0.1) for i in range(5)]
        self.assertTrue(all(value is not None for value in emitted))
        self.assertAlmostEqual(tracker.trough, 9.6)


class TestEntryTroughTrackerRestartSeed(unittest.TestCase):
    """Restart resumes ``trough = min(journaled trough, first fresh price)``."""

    def test_seeded_trough_is_the_starting_minimum(self) -> None:
        tracker = EntryTroughTracker(seeded_trough=9.2)
        self.assertEqual(tracker.trough, 9.2)

    def test_first_fresh_price_below_seed_lowers_and_journals(self) -> None:
        tracker = EntryTroughTracker(seeded_trough=9.2)
        self.assertEqual(tracker.observe(9.0), 9.0)
        self.assertEqual(tracker.trough, 9.0)

    def test_first_fresh_price_above_seed_keeps_seed_and_journals_nothing(self) -> None:
        # The journaled minimum is already on disk — a higher fresh price must
        # NOT re-journal it (no redundant line) and must NOT raise the trough
        # (the unsafe upward reseed the exit peak is allowed but the entry is
        # not).
        tracker = EntryTroughTracker(seeded_trough=9.2)
        self.assertIsNone(tracker.observe(9.6))
        self.assertEqual(tracker.trough, 9.2)


class TestEntryTroughTrackerBadPrice(unittest.TestCase):
    """A non-finite / non-positive price is a doubt: no progress, no crash
    (mirrors ``_finite_positive_float`` — a NaN would freeze every later ``<``
    comparison, a negative would win the min)."""

    def test_nan_is_ignored(self) -> None:
        tracker = EntryTroughTracker()
        tracker.observe(9.5)
        self.assertIsNone(tracker.observe(float("nan")))
        self.assertEqual(tracker.trough, 9.5)

    def test_negative_and_zero_and_inf_are_ignored(self) -> None:
        tracker = EntryTroughTracker()
        tracker.observe(9.5)
        for bad in (-1.0, 0.0, float("-inf"), float("inf")):
            with self.subTest(bad=bad):
                self.assertIsNone(tracker.observe(bad))
        self.assertEqual(tracker.trough, 9.5)

    def test_a_bad_first_price_leaves_the_trough_unseeded(self) -> None:
        tracker = EntryTroughTracker()
        self.assertIsNone(tracker.observe(float("nan")))
        self.assertIsNone(tracker.trough)


if __name__ == "__main__":
    unittest.main()
