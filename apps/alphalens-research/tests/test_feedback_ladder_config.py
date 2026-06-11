"""Tests for the load-bearing ladder-replay config stamp (PR-1).

The stamp lets a tuning analyst GROUP BY the token to detect when the replay
geometry changed, instead of silently blending two regimes into one mean.
"""

import json
import unittest

from alphalens_pipeline.feedback import bar_window, ladder_config
from alphalens_pipeline.paper import constants


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
                "ratchet_rule",
                "tiebreak_rule",
            },
        )

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
