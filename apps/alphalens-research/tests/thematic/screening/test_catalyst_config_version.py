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
        # Format only — read against the live schema constant so a LEGITIMATE
        # shape bump does not have to edit an assertion that is not about the
        # schema number. The number itself is pinned separately below.
        prefix = f"catalyst-v{catalyst_signals._STAMP_SCHEMA}-"
        token = catalyst_config_version()
        self.assertTrue(token.startswith(prefix), token)
        digest = token.removeprefix(prefix)
        self.assertEqual(len(digest), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest), digest)

    def test_stamp_schema_is_the_expected_generation(self):
        # A deliberate pin, because the docstring forbids the one shortcut this
        # constant invites: bumping the schema to FORCE a token change instead of
        # letting the changed constant produce one. A bump is legitimate only
        # when a key is added, removed or renamed — v2 added noise_event_types.
        # If this reddens, confirm the stamp SHAPE really changed before editing.
        self.assertEqual(
            catalyst_signals._STAMP_SCHEMA,
            2,
            msg=(
                "The stamp schema changed. Confirm the payload SHAPE really moved "
                "(a key added, removed or renamed) — a changed CONSTANT must show up "
                "as a different digest, never as a schema bump. See the _STAMP_SCHEMA "
                "docstring, and note the known gap: a code-level formula change does "
                "require a manual bump here."
            ),
        )

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

    def test_token_changes_when_the_noise_type_set_changes(self):
        # NOISE_EVENT_TYPES is a LIVE input to compute_catalyst_strength: a noise
        # type returns 0.0 outright, ahead of the tier lookup. Adding or removing
        # a member therefore re-scores every row of that type, which is exactly
        # what the poolability token exists to make non-comparable.
        #
        # It was missing from the payload, so a change to the noise set moved
        # real catalyst_strength values while the token stood still — the silent
        # pooling of two different formulas that ADR 0013 rule R3 forbids.
        base = catalyst_config_version()
        widened = (*catalyst_signals.NOISE_EVENT_TYPES, "analyst")
        with mock.patch.object(catalyst_signals, "NOISE_EVENT_TYPES", widened):
            self.assertNotEqual(catalyst_config_version(), base)

    def test_noise_set_is_ordered_canonically_in_the_token(self):
        # The payload must not depend on the tuple's declaration order, or a pure
        # reordering — which changes no behaviour at all — would invalidate a
        # cohort. Same discipline as the sorted() applied to EVENT_TYPE_TIER.
        base = catalyst_config_version()
        reordered = tuple(reversed(catalyst_signals.NOISE_EVENT_TYPES))
        with mock.patch.object(catalyst_signals, "NOISE_EVENT_TYPES", reordered):
            self.assertEqual(catalyst_config_version(), base)

    def test_token_changes_on_every_covered_constant(self):
        # Closes the drift-coverage gap: every scalar inside the token, not
        # just the three exemplars above. unittest subTest (NOT pytest
        # parametrize — CI's unittest discover would silently skip it).
        base = catalyst_config_version()
        for attr, new_value in [
            ("_W_CONFIDENCE", 0.99),
            ("_W_SOI_COUNT", 0.99),
            ("_SOI_SATURATION", 6),
            ("_FLOOR_STRONG_THRESHOLD", 0.85),
        ]:
            with self.subTest(constant=attr):
                with mock.patch.object(catalyst_signals, attr, new_value):
                    self.assertNotEqual(catalyst_config_version(), base)


if __name__ == "__main__":
    unittest.main()
