"""Unit tests for catalyst-strength + deep-drawdown-reversal scoring signals.

These augment the Layer 4 weighted_score composer with two complementary
levers:
- ``catalyst_strength``: scores the *news catalyst itself* (event_type
  significance × flash extraction confidence × number of second-order
  beneficiaries identified). Lifts the entire downstream cohort.
- ``is_deep_drawdown_reversal``: per-candidate "thematic momentum
  reversal" detector — fires when the candidate is in a deep drawdown,
  has a fresh catalyst, AND shows institutional volume surge.
  Discriminates within a cohort sharing a single catalyst.

Origin: 2026-05-18 NVDA→QUBT replay analysis. Current scoring gave all
4 quantum names conf 1/5 despite QUBT/QBTS/RGTI returning +44.6%/+30.4%/
+14.2% one month later. catalyst_strength recognises NVDA Ising launch
as a major catalyst (lifts cohort); is_deep_drawdown_reversal recognises
QUBT/QBTS/RGTI as the setup beneficiaries (discriminates vs FORM).
"""

from __future__ import annotations

import unittest

from alphalens.thematic.screening import catalyst_signals as cs


def _event(
    *,
    event_type="product_launch",
    confidence=0.95,
    second_order_implications=None,
):
    if second_order_implications is None:
        second_order_implications = ["beneficiary 1", "beneficiary 2"]
    return {
        "event_type": event_type,
        "confidence": confidence,
        "second_order_implications": second_order_implications,
    }


class TestEventTypeTier(unittest.TestCase):
    """The tier map encodes how market-moving each event_type is (hand-
    calibrated; operator-feedback ledger will tune over time)."""

    def test_high_tier_includes_canonical_catalysts(self):
        for et in ("m_and_a", "earnings", "guidance", "regulatory", "bankruptcy"):
            self.assertGreaterEqual(
                cs.EVENT_TYPE_TIER[et], 0.80, f"{et} should be high-tier catalyst"
            )

    def test_mid_tier_includes_product_and_partnership(self):
        for et in ("product_launch", "contract_award", "partnership"):
            self.assertGreaterEqual(cs.EVENT_TYPE_TIER[et], 0.50)
            self.assertLess(cs.EVENT_TYPE_TIER[et], 0.95)

    def test_low_tier_includes_analyst_and_macro(self):
        for et in ("analyst", "rating_change", "macro"):
            self.assertLess(cs.EVENT_TYPE_TIER[et], 0.65)

    def test_other_falls_back_to_low_default(self):
        # 'other' bucket = unmatched event, conservative low weight.
        self.assertLess(cs.EVENT_TYPE_TIER["other"], 0.5)

    def test_noise_types_absent_or_zero(self):
        # noise (opinion/promo/etc.) already filtered upstream; if any leak
        # through, tier weight must be 0 so they contribute nothing.
        for noise in ("opinion", "promo", "lifestyle", "listicle", "evergreen", "sponsored"):
            self.assertEqual(
                cs.EVENT_TYPE_TIER.get(noise, 0.0), 0.0, f"noise type {noise} must have zero weight"
            )


class TestComputeCatalystStrength(unittest.TestCase):
    def test_strong_catalyst_returns_high_score(self):
        # product_launch (0.85) + high conf + 3 SOIs → strong score
        cs_val = cs.compute_catalyst_strength(
            _event(
                event_type="product_launch",
                confidence=0.95,
                second_order_implications=["a", "b", "c"],
            )
        )
        self.assertGreaterEqual(cs_val, 0.65)

    def test_weak_catalyst_returns_low_score(self):
        # 'other' low-tier + low conf + 0 SOIs → weak score
        cs_val = cs.compute_catalyst_strength(
            _event(event_type="other", confidence=0.3, second_order_implications=[])
        )
        self.assertLess(cs_val, 0.30)

    def test_noise_event_type_returns_zero_contribution(self):
        # Even if confidence is high, an 'opinion' classification should
        # return a very low strength (only confidence + SOI dims contribute).
        cs_val = cs.compute_catalyst_strength(
            _event(event_type="opinion", confidence=0.95, second_order_implications=["a"])
        )
        # event_type contribution = 0; conf + SOI cap the total well below mid.
        self.assertLess(cs_val, 0.40)

    def test_returns_zero_when_event_is_none(self):
        self.assertEqual(cs.compute_catalyst_strength(None), 0.0)

    def test_returns_zero_when_event_is_empty(self):
        self.assertEqual(cs.compute_catalyst_strength({}), 0.0)

    def test_handles_missing_confidence(self):
        cs_val = cs.compute_catalyst_strength(
            {"event_type": "product_launch", "second_order_implications": ["a"]}
        )
        # No conf → conf dim contributes 0, other dims still count.
        self.assertGreaterEqual(cs_val, 0.30)

    def test_clamps_to_zero_one_range(self):
        # Pathological inputs (conf > 1 or huge SOI list) clamp.
        cs_val = cs.compute_catalyst_strength(
            _event(event_type="m_and_a", confidence=2.0, second_order_implications=["a"] * 50)
        )
        self.assertLessEqual(cs_val, 1.0)
        self.assertGreaterEqual(cs_val, 0.0)

    def test_unknown_event_type_falls_back_to_other_tier(self):
        # Future event_types not yet in the tier map shouldn't crash.
        cs_val = cs.compute_catalyst_strength(
            _event(event_type="alien_invasion", confidence=0.5, second_order_implications=["a"])
        )
        # Should not raise; uses 'other' tier (low).
        self.assertGreaterEqual(cs_val, 0.0)
        self.assertLessEqual(cs_val, 1.0)


class TestCatalystFloor(unittest.TestCase):
    def test_strong_catalyst_lifts_cohort_by_2(self):
        # cs ≥ 0.75 → floor contribution = 2 (max lift)
        self.assertEqual(cs.catalyst_floor(0.80), 2)
        self.assertEqual(cs.catalyst_floor(1.0), 2)

    def test_moderate_catalyst_lifts_by_1(self):
        # Threshold raised to 0.45 (zen pre-push HIGH finding): prior 0.25
        # cutoff caused score inflation on weak-event high-confidence rows.
        self.assertEqual(cs.catalyst_floor(0.50), 1)
        self.assertEqual(cs.catalyst_floor(0.45), 1)

    def test_weak_catalyst_no_lift(self):
        self.assertEqual(cs.catalyst_floor(0.44), 0)
        self.assertEqual(cs.catalyst_floor(0.10), 0)
        self.assertEqual(cs.catalyst_floor(0.0), 0)

    def test_handles_nan(self):
        self.assertEqual(cs.catalyst_floor(float("nan")), 0)


class TestIsDeepDrawdownReversal(unittest.TestCase):
    """Per-candidate detector: deep_drawdown setup + fresh catalyst + volume surge.

    All three thresholds reuse already-justified values:
    - deep_drawdown: pct_off_52w_high ≤ -30% (renderer constant, NVDA→QUBT cohort)
    - catalyst: source_event_url present (Phase B output)
    - volume surge: technical_volume_zscore ≥ 2.0 (institutional accumulation rule)
    """

    def _row(self, **overrides):
        base = {
            "technical_pct_off_52w_high": -50.0,
            "technical_ma200_distance_pct": -20.0,
            "source_event_url": "https://example.com/catalyst",
            "technical_volume_zscore": 3.5,
        }
        base.update(overrides)
        return base

    def test_fires_when_all_three_conditions_met(self):
        self.assertTrue(cs.is_deep_drawdown_reversal(self._row()))

    def test_does_not_fire_when_setup_is_extended(self):
        # at 52w high → not deep_drawdown → no fire
        row = self._row(technical_pct_off_52w_high=0.0)
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_no_catalyst(self):
        row = self._row(source_event_url=None)
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_catalyst_url_empty_string(self):
        row = self._row(source_event_url="")
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_volume_zscore_below_threshold(self):
        row = self._row(technical_volume_zscore=1.5)
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_volume_zscore_missing(self):
        row = self._row(technical_volume_zscore=None)
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_handles_nan_pct_off_high(self):
        # Missing OHLCV → can't classify setup → no fire
        row = self._row(technical_pct_off_52w_high=float("nan"))
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_url_is_nan_float(self):
        # Pandas parquet round-trip can produce float('nan') in string column.
        # ``str(nan)`` returns "nan" (truthy) — naive .strip() check would
        # incorrectly fire. Zen pre-push HIGH finding fix.
        row = self._row(source_event_url=float("nan"))
        self.assertFalse(cs.is_deep_drawdown_reversal(row))

    def test_does_not_fire_when_url_is_string_nan(self):
        # Edge case: literal string "nan" / "<NA>" / "None" round-tripped.
        for sentinel in ("nan", "NaN", "<NA>", "None"):
            row = self._row(source_event_url=sentinel)
            self.assertFalse(
                cs.is_deep_drawdown_reversal(row),
                f"sentinel {sentinel!r} should not count as a valid URL",
            )


if __name__ == "__main__":
    unittest.main()
