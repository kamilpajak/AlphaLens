"""Stage-1 retro Phase-2/3 inference script — pure-stats tests.

Pins, per the pre-registration
(`docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md` §8, §10):

* the Stage-B labels-hash gate constant matches the committed value;
* winsorization, pair-cluster delta, and the two-way cluster bootstrap
  behave correctly on synthetic data with a known effect;
* the refusal-reason classifier buckets canonical decline phrasings;
* the power helpers are monotone and self-consistent.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage1_retro_outcome_inference.py"
_spec = importlib.util.spec_from_file_location("stage1_retro_outcome_inference", _SCRIPT)
assert _spec and _spec.loader
inference = importlib.util.module_from_spec(_spec)
sys.modules["stage1_retro_outcome_inference"] = inference
_spec.loader.exec_module(inference)

STAGE_B_SHA256 = "c8440d40a0c2751717b62f3a83ef2d245957c2a43cd55fbe3405ccdb25bfb95f"


def _synthetic_rows(effect: float, seed: int, n_pairs_per_leg: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    days = [f"2026-05-{d:02d}" for d in range(19, 29)]
    for leg, label in ((0, inference.REFUSED), (1, inference.KEPT)):
        for p in range(n_pairs_per_leg):
            pair_id = f"{label}_{p}"
            pair_shift = rng.normal(0.0, 0.02)
            for _ in range(rng.integers(1, 4)):
                rows.append(
                    {
                        "pair_id": pair_id,
                        "pair_label": label,
                        "brief_date": days[rng.integers(0, len(days))],
                        "y": leg * effect + pair_shift + rng.normal(0.0, 0.05),
                    }
                )
    return pd.DataFrame(rows)


class TestStageBHashGate(unittest.TestCase):
    def test_labels_sha_constant_matches_stage_b_commit(self):
        self.assertEqual(inference.LABELS_SHA256, STAGE_B_SHA256)


class TestWinsorize(unittest.TestCase):
    def test_clips_extremes_to_quantiles(self):
        values = np.concatenate([np.zeros(98), [10.0, -10.0]])
        out = inference.winsorize(values, pct=0.01)
        self.assertLess(out.max(), 10.0)
        self.assertGreater(out.min(), -10.0)

    def test_interior_values_unchanged(self):
        values = np.linspace(-1.0, 1.0, 101)
        out = inference.winsorize(values, pct=0.01)
        np.testing.assert_allclose(out[5:-5], values[5:-5])

    def test_empty_input_passes_through(self):
        self.assertEqual(inference.winsorize(np.array([])).size, 0)


class TestPairClusterDelta(unittest.TestCase):
    def test_delta_is_difference_of_unweighted_pair_mean_averages(self):
        rows = pd.DataFrame(
            {
                "pair_id": ["a", "a", "b", "c"],
                "pair_label": [
                    inference.KEPT,
                    inference.KEPT,
                    inference.KEPT,
                    inference.REFUSED,
                ],
                "brief_date": ["d1", "d2", "d1", "d1"],
                "y": [0.10, 0.30, 0.40, 0.10],
            }
        )
        delta, pair_means = inference.pair_cluster_delta(rows)
        # KEPT pair means: a=0.20, b=0.40 -> 0.30; REFUSED: c=0.10.
        self.assertAlmostEqual(delta, 0.20)
        self.assertEqual(len(pair_means), 3)


class TestTwoWayClusterBootstrap(unittest.TestCase):
    def test_seed_reproducibility(self):
        rows = _synthetic_rows(effect=0.0, seed=7)
        a = inference.two_way_cluster_bootstrap(rows, n_boot=200, seed=1)
        b = inference.two_way_cluster_bootstrap(rows, n_boot=200, seed=1)
        self.assertEqual(a["p_one_sided_gt0"], b["p_one_sided_gt0"])
        self.assertEqual(a["ci95"], b["ci95"])

    def test_large_positive_effect_gives_small_one_sided_p(self):
        rows = _synthetic_rows(effect=0.10, seed=11)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=2)
        self.assertLess(boot["p_one_sided_gt0"], 0.01)

    def test_null_effect_gives_moderate_p(self):
        rows = _synthetic_rows(effect=0.0, seed=13)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=3)
        self.assertGreater(boot["p_one_sided_gt0"], 0.05)
        self.assertLess(boot["p_one_sided_gt0"], 0.95)

    def test_negative_effect_gives_large_one_sided_p(self):
        rows = _synthetic_rows(effect=-0.10, seed=17)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=4)
        self.assertGreater(boot["p_one_sided_gt0"], 0.99)


class TestRefusalReasonClassifier(unittest.TestCase):
    def test_non_event(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "The headline is a stock market commentary without any concrete "
                "business development."
            ),
            inference.NON_EVENT,
        )

    def test_direction_filter(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "The event is adverse for the named company; declining to propose "
                "its victim as a long."
            ),
            inference.DIRECTION,
        )

    def test_no_channel(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "No transmission channel exists to any U.S.-listed company's revenue or costs."
            ),
            inference.NO_CHANNEL,
        )

    def test_empty_reason_is_other(self):
        self.assertEqual(inference.classify_refusal_reason(None), inference.OTHER)
        self.assertEqual(inference.classify_refusal_reason("  "), inference.OTHER)


class TestMajorityBucket(unittest.TestCase):
    def test_plain_majority_wins(self):
        self.assertEqual(
            inference.majority_bucket(["no_channel", "no_channel", "non_event"]),
            "no_channel",
        )

    def test_tie_breaks_deterministically_alphabetical(self):
        # A 1-1 tie must not depend on Python set iteration order.
        self.assertEqual(inference.majority_bucket(["non_event", "no_channel"]), "no_channel")
        self.assertEqual(inference.majority_bucket(["no_channel", "non_event"]), "no_channel")


class TestPowerHelpers(unittest.TestCase):
    def test_power_monotone_in_n(self):
        p_small = inference.power_one_sided(0.07, 0.15, 10, 15)
        p_large = inference.power_one_sided(0.07, 0.15, 60, 90)
        self.assertLess(p_small, p_large)

    def test_large_effect_power_near_one(self):
        self.assertGreater(inference.power_one_sided(1.0, 0.15, 30, 30), 0.999)

    def test_required_n_reaches_target_power(self):
        n = inference.required_n_per_leg(0.07, 0.15, power=0.80)
        self.assertGreaterEqual(inference.power_one_sided(0.07, 0.15, n, n), 0.80 - 1e-6)
        self.assertLess(inference.power_one_sided(0.07, 0.15, n - 2, n - 2), 0.80)

    def test_required_n_rejects_nonpositive_effect(self):
        with self.assertRaises(ValueError):
            inference.required_n_per_leg(-0.01, 0.15)


if __name__ == "__main__":
    unittest.main()
