"""Tests for the load-bearing ladder-replay config stamp (PR-1).

The stamp lets a tuning analyst GROUP BY the token to detect when the replay
geometry changed, instead of silently blending two regimes into one mean.
"""

import datetime as dt
import json
import unittest
from unittest import mock

from alphalens_pipeline.feedback import bar_window, ladder_config
from alphalens_pipeline.paper import constants
from alphalens_pipeline.paper.calendar import session_on_or_after, session_open_utc


class TestLadderArrivalSession(unittest.TestCase):
    """A brief dated D is built after session D closes (T-1 dating), so the
    ladder can first trade on the first session strictly after D (#1416)."""

    def _assert_arrival(self, brief: str, expected: str, exchange: str = "XNYS") -> None:
        got = ladder_config.ladder_arrival_session(dt.date.fromisoformat(brief), exchange)
        self.assertEqual(got, dt.date.fromisoformat(expected))

    def test_session_day_brief_arrives_next_session(self):
        self._assert_arrival("2026-09-08", "2026-09-09")  # Tue -> Wed

    def test_friday_brief_arrives_monday(self):
        self._assert_arrival("2026-09-11", "2026-09-14")

    def test_saturday_brief_arrives_monday(self):
        self._assert_arrival("2026-09-12", "2026-09-14")

    def test_sunday_brief_arrives_monday(self):
        self._assert_arrival("2026-09-13", "2026-09-14")

    def test_holiday_brief_arrives_next_session(self):
        self._assert_arrival("2026-09-07", "2026-09-08")  # Labor Day Mon -> Tue

    def test_brief_before_a_monday_holiday_skips_it(self):
        self._assert_arrival("2026-09-04", "2026-09-08")  # Fri -> (Labor Day) -> Tue

    def test_half_day_brief_arrives_next_session(self):
        self._assert_arrival("2026-11-27", "2026-11-30")  # post-Thanksgiving half day

    def test_non_default_exchange(self):
        self._assert_arrival("2026-09-08", "2026-09-09", exchange="XWAR")

    def test_arrival_opens_after_the_brief_can_exist(self):
        # The brief for D is first generated on calendar day D+1, so the arrival
        # session must open after D+1 00:00 UTC for every date.
        day = dt.date(2026, 1, 1)
        while day < dt.date(2027, 1, 1):
            arrival = ladder_config.ladder_arrival_session(day)
            earliest = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)
            self.assertGreater(session_open_utc(arrival, "XNYS"), earliest, day)
            day += dt.timedelta(days=1)

    def test_guard_refutes_the_old_rule(self):
        # Positive control for the guard above: the old anchor opens BEFORE the
        # brief exists on a session-day brief.
        brief = dt.date(2026, 9, 8)
        earliest = dt.datetime.combine(brief + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)
        self.assertLess(session_open_utc(session_on_or_after(brief, "XNYS"), "XNYS"), earliest)


class TestLadderConfigVersion(unittest.TestCase):
    def test_token_is_canonical_json_with_load_bearing_keys(self):
        token = ladder_config.ladder_config_version(order_ttl_days=7)
        payload = json.loads(token)
        self.assertEqual(
            set(payload),
            {
                "schema",
                "time_stop_days",
                "order_ttl_days",
                "arrival_vwap_window_min",
                "arrival_rule",
                "ratchet_rule",
                "tiebreak_rule",
            },
        )

    def test_token_names_the_arrival_rule(self):
        payload = json.loads(ladder_config.ladder_config_version(order_ttl_days=7))
        self.assertEqual(payload["arrival_rule"], ladder_config.ARRIVAL_RULE)
        self.assertEqual(payload["schema"], 2)

    def test_values_are_read_from_the_live_constants(self):
        # The whole point: if a constant changes, the token changes. So the token
        # must reflect the imported constant, not a hardcoded copy.
        payload = json.loads(ladder_config.ladder_config_version(order_ttl_days=7))
        self.assertEqual(payload["time_stop_days"], constants.TIME_STOP_DAYS)
        self.assertEqual(payload["arrival_vwap_window_min"], bar_window.ARRIVAL_VWAP_WINDOW_MIN)
        self.assertEqual(payload["order_ttl_days"], 7)

    def test_distinct_order_ttl_yields_distinct_token(self):
        # Pins the 10-vs-7 divergence story: two rows replayed under a different
        # entry-TTL are genuinely different configs and must not compare equal.
        self.assertNotEqual(
            ladder_config.ladder_config_version(order_ttl_days=7),
            ladder_config.ladder_config_version(order_ttl_days=10),
        )

    def test_schema_bump_changes_token_with_identical_inputs(self):
        # Guard-rail: a stamp-SHAPE evolution (schema bump) must change the token
        # even when every value input is identical, so old rows stay separable
        # after a stamp redesign.
        base = ladder_config.ladder_config_version(order_ttl_days=7)
        with mock.patch.object(ladder_config, "_STAMP_SCHEMA", ladder_config._STAMP_SCHEMA + 1):
            bumped = ladder_config.ladder_config_version(order_ttl_days=7)
        self.assertNotEqual(base, bumped)

    def test_token_is_deterministic_and_stable(self):
        # Same inputs -> byte-identical token (sorted keys), so a plain string
        # GROUP BY partitions rows by geometry with no parsing.
        a = ladder_config.ladder_config_version(order_ttl_days=7)
        b = ladder_config.ladder_config_version(order_ttl_days=7)
        self.assertEqual(a, b)
        # Sorted-key canonical form: keys appear in alphabetical order.
        self.assertLess(a.index("arrival_vwap_window_min"), a.index("order_ttl_days"))
        self.assertLess(a.index("order_ttl_days"), a.index("ratchet_rule"))


if __name__ == "__main__":
    unittest.main()
