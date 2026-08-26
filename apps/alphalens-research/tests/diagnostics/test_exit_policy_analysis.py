"""Tests for the §10.4/§10.5 analysis machinery (#1115).

Everything statistical is exercised on SYNTHETIC differences — never cohort
rows: the memo forbids any computation of the A-vs-B contrast on cohort data
before the floors are met, and these tests must be runnable throughout the
accrual window without constituting a look.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from alphalens_research.diagnostics import exit_policy_analysis as epa


def _clustered_frame(seed: int = 7, n_days: int = 130) -> pd.DataFrame:
    # 130 synthetic days x 3 rows: enough days for the 42-session moving
    # block to have many start positions (with fewer days than one block the
    # arm degenerates to a point — the very §6.3 phenomenon), and a strong
    # per-day shock so day clustering matters.
    rng = np.random.default_rng(seed)
    rows = []
    for day_index in range(n_days):
        shock = rng.normal(0.0, 50.0)
        for k in range(3):
            rows.append(
                {
                    "brief_date": f"d{day_index:03d}",
                    "ticker": f"T{(day_index * 3 + k) % 17}",
                    "d": shock + rng.normal(5.0, 5.0),
                }
            )
    return pd.DataFrame(rows)


class TestInferenceArms(unittest.TestCase):
    def test_all_five_arms_compute_and_are_seed_deterministic(self):
        frame = _clustered_frame()
        first = epa.inference_arms(frame, n_boot=500, seed=123)
        second = epa.inference_arms(frame, n_boot=500, seed=123)
        self.assertEqual(
            set(first),
            {"iid", "cluster_day", "cluster_ticker", "cluster_day_ticker", "moving_block"},
        )
        for name, arm in first.items():
            with self.subTest(arm=name):
                self.assertLess(arm.ci_low, arm.ci_high)
                self.assertEqual(
                    (arm.ci_low, arm.ci_high), (second[name].ci_low, second[name].ci_high)
                )
                self.assertGreaterEqual(arm.n_clusters, 1)

    def test_day_clustering_widens_the_iid_interval_on_day_shocked_data(self):
        # The whole point of arms 2-5: with a common per-day shock the iid
        # interval is anti-conservatively narrow.
        frame = _clustered_frame()
        arms = epa.inference_arms(frame, n_boot=800, seed=11)
        iid_width = arms["iid"].ci_high - arms["iid"].ci_low
        day_width = arms["cluster_day"].ci_high - arms["cluster_day"].ci_low
        self.assertGreater(day_width, iid_width)

    def test_verdict_reads_the_widest_of_arms_two_to_five(self):
        arms = {
            "iid": epa.InferenceArm(0.5, 1.5, 100),  # must be ignored
            "cluster_day": epa.InferenceArm(0.2, 1.2, 40),
            "cluster_ticker": epa.InferenceArm(0.3, 1.1, 17),
            "cluster_day_ticker": epa.InferenceArm(-0.1, 1.4, 40),  # widest, spans 0
            "moving_block": epa.InferenceArm(0.1, 1.3, 5),
        }
        name, verdict = epa.primary_verdict(arms)
        self.assertEqual(name, "cluster_day_ticker")
        self.assertEqual(verdict, "not_distinguishable")
        arms["cluster_day_ticker"] = epa.InferenceArm(0.05, 1.4, 40)
        _, verdict = epa.primary_verdict(arms)
        self.assertEqual(verdict, "arm_b_better")
        arms["cluster_day_ticker"] = epa.InferenceArm(-1.4, -0.05, 40)
        _, verdict = epa.primary_verdict(arms)
        self.assertEqual(verdict, "arm_a_better")


class TestFloors(unittest.TestCase):
    def test_non_overlapping_block_count(self):
        days = [f"d{i:03d}" for i in range(430)]
        self.assertEqual(epa.non_overlapping_blocks(days, block_len=42), 10)
        self.assertEqual(epa.non_overlapping_blocks(days[:100], block_len=42), 2)

    def test_pair_floor_formula(self):
        # n = (z_.975 + z_.80)^2 * (sd/delta_min)^2, rounded up.
        floor = epa.pair_floor(sd_d=200.0, delta_min=20.0)
        z = 1.959963984540054 + 0.8416212335729143
        self.assertEqual(floor, int(np.ceil(z**2 * 100.0)))


class TestReporting(unittest.TestCase):
    def test_tail_contribution_and_extremes_removed(self):
        d = np.array([0.0] * 18 + [100.0, -50.0])
        report = epa.tail_report(d)
        # Top 5% by |d| of 20 values = 1 value (the +100); its share of the
        # total sum (50) is 2.0.
        self.assertAlmostEqual(report["top5pct_abs_share_of_sum"], 2.0, places=12)
        # Delta with the single largest positive and negative pair removed.
        self.assertAlmostEqual(report["delta_without_extremes"], 0.0, places=12)
        self.assertAlmostEqual(report["delta"], 50.0 / 20.0, places=12)

    def test_distribution_report_histogram(self):
        # §8.1 item 1 mandates a histogram beside the deciles.
        d = np.arange(100, dtype=float)
        report = epa.distribution_report(d)
        hist = report["histogram"]
        self.assertEqual(len(hist["bin_edges"]), 21)
        self.assertEqual(len(hist["counts"]), 20)
        self.assertEqual(sum(hist["counts"]), 100)

    def test_distribution_report_has_the_mandated_shape(self):
        d = np.arange(100, dtype=float)
        report = epa.distribution_report(d)
        self.assertEqual(len(report["deciles"]), 11)  # 0..100 in steps of 10
        self.assertEqual(report["min"], 0.0)
        self.assertEqual(report["max"], 99.0)


class TestExtractHashProtocol(unittest.TestCase):
    def test_extract_columns_carry_no_outcome(self):
        # §10.5: the extract is committed BEFORE the outcome join — it must be
        # structurally incapable of containing one.
        self.assertNotIn("net", " ".join(epa.EXTRACT_COLUMNS))
        self.assertNotIn("realized", " ".join(epa.EXTRACT_COLUMNS))
        for required in ("brief_date", "ticker", "trade_setup_json"):
            self.assertIn(required, epa.EXTRACT_COLUMNS)

    def test_analyze_refuses_a_mismatching_hash(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            extract = Path(tmp) / "extract.parquet"
            pd.DataFrame({c: pd.Series(dtype=object) for c in epa.EXTRACT_COLUMNS}).to_parquet(
                extract, index=False
            )
            with self.assertRaises(SystemExit):
                epa.verify_extract_hash(extract, "0" * 64)
            # And the true hash passes.
            epa.verify_extract_hash(extract, epa.sha256_of(extract))


if __name__ == "__main__":
    unittest.main()
