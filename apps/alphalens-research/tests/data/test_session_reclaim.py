from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.data.alt_data.session_reclaim import ReclaimLimiter


class _Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


class TestReclaimLimiter(unittest.TestCase):
    def test_reclaims_while_budget_remains(self):
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=4, clock=clock)
        for _ in range(4):
            self.assertEqual(lim.try_reclaim(lambda: True), "reclaimed")

    def test_stops_after_the_budget_is_spent(self):
        """The human wins if they keep pressing resume: the daemon must not
        ping-pong with the platform forever."""
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=2, clock=clock)
        lim.try_reclaim(lambda: True)
        lim.try_reclaim(lambda: True)
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")

    def test_budget_refills_on_a_rolling_hour(self):
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=1, clock=clock)
        lim.try_reclaim(lambda: True)
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")
        clock.advance(minutes=61)
        self.assertEqual(lim.try_reclaim(lambda: True), "reclaimed")

    def test_a_failed_elevation_still_spends_budget(self):
        """Otherwise a broken elevation retries without limit."""
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=1, clock=clock)
        self.assertEqual(lim.try_reclaim(lambda: False), "failed")
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")


if __name__ == "__main__":
    unittest.main()
