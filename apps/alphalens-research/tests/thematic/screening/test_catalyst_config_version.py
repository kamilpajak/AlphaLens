"""ADR 0013 rule R3: the catalyst-strength poolability key.

``catalyst_config_version()`` must drift when ANY constant shaping
catalyst_strength / catalyst_floor changes (dimension weights, SOI
saturation, floor thresholds, the per-event-type tier map) so rows scored
under different formulas can never pool silently in EDGE calibration.
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.thematic.screening import catalyst_signals
from alphalens_pipeline.thematic.screening.catalyst_signals import (
    catalyst_config_version,
)


class TestCatalystConfigVersion(unittest.TestCase):
    def test_token_is_nonempty_string_and_deterministic(self):
        t1, t2 = catalyst_config_version(), catalyst_config_version()
        self.assertIsInstance(t1, str)
        self.assertTrue(t1)
        self.assertEqual(t1, t2)

    def test_token_has_human_readable_prefix_and_short_hash(self):
        token = catalyst_config_version()
        self.assertTrue(token.startswith("catalyst-v1-"), token)
        digest = token.removeprefix("catalyst-v1-")
        self.assertEqual(len(digest), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest), digest)

    def test_token_changes_when_floor_threshold_changes(self):
        base = catalyst_config_version()
        with mock.patch.object(catalyst_signals, "_FLOOR_MODERATE_THRESHOLD", 0.25):
            self.assertNotEqual(catalyst_config_version(), base)

    def test_token_changes_when_a_tier_value_changes(self):
        base = catalyst_config_version()
        with mock.patch.dict(catalyst_signals.EVENT_TYPE_TIER, {"analyst": 0.99}):
            self.assertNotEqual(catalyst_config_version(), base)

    def test_token_changes_when_a_dimension_weight_changes(self):
        base = catalyst_config_version()
        with mock.patch.object(catalyst_signals, "_W_EVENT_TYPE", 0.50):
            self.assertNotEqual(catalyst_config_version(), base)


if __name__ == "__main__":
    unittest.main()
