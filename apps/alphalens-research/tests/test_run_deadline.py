import unittest

from alphalens_pipeline.feedback.population_ladder_monitor import (
    _BREAKER_CONSECUTIVE_FAILS,
    _FETCH_DEADLINE_S_DEFAULT,
    _RunDeadline,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestRunDeadline(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(_FETCH_DEADLINE_S_DEFAULT, 75 * 60)
        self.assertEqual(_BREAKER_CONSECUTIVE_FAILS, 6)

    def test_deadline_trips_on_wallclock_and_latches(self):
        c = _Clock()
        d = _RunDeadline(60.0, monotonic=c)
        self.assertFalse(d.should_stop())
        c.t += 59.0
        self.assertFalse(d.should_stop())
        c.t += 2.0  # now 61s past start, deadline 60s
        self.assertTrue(d.should_stop())
        self.assertEqual(d.stopped_reason, "deadline")
        c.t -= 100.0  # latched: stays stopped even if clock rewinds
        self.assertTrue(d.should_stop())

    def test_breaker_trips_after_consecutive_fails_only(self):
        c = _Clock()
        d = _RunDeadline(10_000.0, breaker_fails=3, monotonic=c)
        d.record_fetch_result(ok=False)
        d.record_fetch_result(ok=False)
        self.assertFalse(d.should_stop())
        d.record_fetch_result(ok=True)  # resets the streak
        d.record_fetch_result(ok=False)
        d.record_fetch_result(ok=False)
        self.assertFalse(d.should_stop())
        d.record_fetch_result(ok=False)  # 3 in a row
        self.assertTrue(d.should_stop())
        self.assertEqual(d.stopped_reason, "breaker")

    def test_healthy_run_never_stops(self):
        c = _Clock()
        d = _RunDeadline(10_000.0, monotonic=c)
        for _ in range(100):
            d.record_fetch_result(ok=True)
            self.assertFalse(d.should_stop())
        self.assertIsNone(d.stopped_reason)
