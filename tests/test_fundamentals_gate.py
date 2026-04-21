"""Unit tests for the fundamental-gate scoring and hard-reject logic.

`fundamental_gate_score(features, config) -> float in [floor, 1.0]` is a pure
multiplier applied to the technical composite. 1.0 = no concern, floor = all
red flags active. `should_hard_reject(features, config) -> (bool, reason)`
is used by the Guardrails layer to skip doomed tickers before scoring.
"""

from __future__ import annotations

import math
import unittest


DEFAULT_CONFIG = {
    "fundamental_gate_enabled": True,
    "cash_runway_months_hard_reject": 3,
    "cash_runway_months_penalty_full": 12,
    "ps_ceiling_preprofit_penalty_full": 100,
    "consecutive_neg_ocf_penalty": 4,
    "fundamental_gate_floor": 0.3,
}


class TestFundamentalGateScore(unittest.TestCase):
    def test_no_concerns_returns_one(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 36.0,
            "ps_ratio": 8.0,
            "net_income_ttm": 5_000_000.0,   # profitable
            "consecutive_neg_ocf_quarters": 0,
        }
        self.assertEqual(fundamental_gate_score(features, DEFAULT_CONFIG), 1.0)

    def test_missing_data_returns_neutral(self):
        """Empty features dict → no info → neutral gate = 1.0. Layer 3 will
        still see the pick; we don't want to penalize the unknown."""
        from alphalens.fundamentals.gate import fundamental_gate_score

        self.assertEqual(fundamental_gate_score({}, DEFAULT_CONFIG), 1.0)

    def test_runway_between_3_and_12_months_reduces_score(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 6.0,
            "ps_ratio": 8.0,
            "net_income_ttm": 5_000_000.0,
            "consecutive_neg_ocf_quarters": 0,
        }
        score = fundamental_gate_score(features, DEFAULT_CONFIG)
        self.assertLess(score, 1.0)
        self.assertGreaterEqual(score, DEFAULT_CONFIG["fundamental_gate_floor"])

    def test_runway_above_threshold_no_penalty(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 24.0,    # > 12 threshold
            "ps_ratio": 8.0,
            "net_income_ttm": 5_000_000.0,
            "consecutive_neg_ocf_quarters": 0,
        }
        self.assertAlmostEqual(fundamental_gate_score(features, DEFAULT_CONFIG), 1.0, places=6)

    def test_ps_ceiling_applies_only_preprofit(self):
        """Rule: a P/S of 150 is a red flag only for pre-profit names. Profitable
        companies routinely trade at P/S >100 (software cos), that's fine."""
        from alphalens.fundamentals.gate import fundamental_gate_score

        preprofit = {
            "cash_runway_months": 36.0,
            "ps_ratio": 150.0,
            "net_income_ttm": -10_000_000.0,
            "consecutive_neg_ocf_quarters": 0,
        }
        profitable = {
            "cash_runway_months": 36.0,
            "ps_ratio": 150.0,
            "net_income_ttm": 50_000_000.0,
            "consecutive_neg_ocf_quarters": 0,
        }
        self.assertLess(fundamental_gate_score(preprofit, DEFAULT_CONFIG), 1.0)
        self.assertAlmostEqual(
            fundamental_gate_score(profitable, DEFAULT_CONFIG), 1.0, places=6
        )

    def test_all_red_flags_clipped_at_floor(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 4.0,             # just above hard-reject, below penalty_full
            "ps_ratio": 500.0,
            "net_income_ttm": -50_000_000.0,       # pre-profit
            "consecutive_neg_ocf_quarters": 12,
        }
        score = fundamental_gate_score(features, DEFAULT_CONFIG)
        self.assertGreaterEqual(score, DEFAULT_CONFIG["fundamental_gate_floor"])
        self.assertLessEqual(score, 1.0)

    def test_consecutive_neg_ocf_triggers_penalty(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 36.0,
            "ps_ratio": 8.0,
            "net_income_ttm": 5_000_000.0,
            "consecutive_neg_ocf_quarters": 6,  # past penalty threshold 4
        }
        self.assertLess(fundamental_gate_score(features, DEFAULT_CONFIG), 1.0)

    def test_disabled_flag_returns_one(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {"cash_runway_months": 2.0}  # would otherwise be extreme
        cfg = {**DEFAULT_CONFIG, "fundamental_gate_enabled": False}
        self.assertEqual(fundamental_gate_score(features, cfg), 1.0)

    def test_floor_is_configurable(self):
        from alphalens.fundamentals.gate import fundamental_gate_score

        features = {
            "cash_runway_months": 4.0,
            "ps_ratio": 500.0,
            "net_income_ttm": -50_000_000.0,
            "consecutive_neg_ocf_quarters": 12,
        }
        cfg = {**DEFAULT_CONFIG, "fundamental_gate_floor": 0.5}
        score = fundamental_gate_score(features, cfg)
        self.assertGreaterEqual(score, 0.5)


class TestShouldHardReject(unittest.TestCase):
    def test_runway_below_hard_reject_threshold_rejects(self):
        from alphalens.fundamentals.gate import should_hard_reject

        features = {"cash_runway_months": 2.0}
        rejected, reason = should_hard_reject(features, DEFAULT_CONFIG)
        self.assertTrue(rejected)
        self.assertIn("runway", reason)

    def test_runway_at_exact_threshold_does_not_reject(self):
        """3mo is the boundary — we keep 3 and reject below."""
        from alphalens.fundamentals.gate import should_hard_reject

        features = {"cash_runway_months": 3.0}
        rejected, _ = should_hard_reject(features, DEFAULT_CONFIG)
        self.assertFalse(rejected)

    def test_missing_runway_data_does_not_reject(self):
        """Hard reject requires positive evidence of impending bankruptcy."""
        from alphalens.fundamentals.gate import should_hard_reject

        rejected, _ = should_hard_reject({}, DEFAULT_CONFIG)
        self.assertFalse(rejected)

    def test_nan_runway_does_not_reject(self):
        from alphalens.fundamentals.gate import should_hard_reject

        features = {"cash_runway_months": float("nan")}
        rejected, _ = should_hard_reject(features, DEFAULT_CONFIG)
        self.assertFalse(rejected)

    def test_disabled_flag_skips_hard_reject(self):
        from alphalens.fundamentals.gate import should_hard_reject

        features = {"cash_runway_months": 0.5}  # extreme
        cfg = {**DEFAULT_CONFIG, "fundamental_gate_enabled": False}
        rejected, _ = should_hard_reject(features, cfg)
        self.assertFalse(rejected)


if __name__ == "__main__":
    unittest.main()
