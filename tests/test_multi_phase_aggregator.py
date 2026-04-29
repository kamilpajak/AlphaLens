"""Tests for the multi-phase aggregator helper.

The aggregator runs an experiment at every phase 0..stride-1 and reports
phase-by-phase + aggregated mean ± stdev statistics. Closes the gap that
made today's tri-factor / mom+lowvol "FAIL" verdicts unreliable
(docs/research/methodology_audit_2026_04_29.md).
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass


@dataclass
class _StubResult:
    """A single phase's headline stats as the aggregator consumes them."""

    sharpe_gross: float
    sharpe_net: float
    excess_gross_ann: float
    excess_net_ann: float
    alpha_t: float


class MultiPhaseAggregatorTests(unittest.TestCase):
    def test_summary_collects_per_phase_results(self):
        from alphalens.backtest.multi_phase import summarise_phase_results

        results = [
            _StubResult(0.4, 0.2, 0.18, 0.15, 1.5).__dict__,
            _StubResult(0.6, 0.4, 0.22, 0.18, 1.8).__dict__,
            _StubResult(0.5, 0.3, 0.20, 0.17, 1.6).__dict__,
            _StubResult(-0.1, -0.3, -0.05, -0.08, -0.4).__dict__,
            _StubResult(0.3, 0.1, 0.10, 0.07, 1.0).__dict__,
        ]
        summary = summarise_phase_results(results)
        # Means
        self.assertAlmostEqual(summary["sharpe_gross"]["mean"], 0.34, places=2)
        self.assertAlmostEqual(summary["alpha_t"]["mean"], 1.10, places=2)
        # Stdev > 0 (these are 5 different phases)
        self.assertGreater(summary["sharpe_gross"]["std"], 0)
        # Counts
        self.assertEqual(summary["sharpe_gross"]["n"], 5)

    def test_robust_decision_helper_handles_phase_dispersion(self):
        """`robust_verdict` must require positive mean AND meaningfully
        positive lower confidence bound (mean − 1 sd) to recommend MID/PASS.
        High-dispersion data (any phase deeply negative) must downgrade."""
        from alphalens.backtest.multi_phase import robust_verdict

        # All 5 phases positive, low dispersion → PASS
        all_pos = [
            {"alpha_t": 2.1, "excess_net_ann": 0.20},
            {"alpha_t": 2.3, "excess_net_ann": 0.22},
            {"alpha_t": 2.0, "excess_net_ann": 0.19},
            {"alpha_t": 2.4, "excess_net_ann": 0.23},
            {"alpha_t": 2.2, "excess_net_ann": 0.21},
        ]
        self.assertEqual(robust_verdict(all_pos), "PASS")

        # Mean clearly positive but one phase deeply negative → MID
        mixed = [
            {"alpha_t": 2.0, "excess_net_ann": 0.18},
            {"alpha_t": 1.8, "excess_net_ann": 0.16},
            {"alpha_t": -0.4, "excess_net_ann": -0.10},
            {"alpha_t": 2.1, "excess_net_ann": 0.20},
            {"alpha_t": 1.5, "excess_net_ann": 0.12},
        ]
        self.assertEqual(robust_verdict(mixed), "MID")

        # Mean negative or near zero → FAIL
        negative = [
            {"alpha_t": -0.5, "excess_net_ann": -0.10},
            {"alpha_t": 0.2, "excess_net_ann": 0.02},
            {"alpha_t": -1.1, "excess_net_ann": -0.15},
            {"alpha_t": 0.3, "excess_net_ann": 0.05},
            {"alpha_t": -0.4, "excess_net_ann": -0.08},
        ]
        self.assertEqual(robust_verdict(negative), "FAIL")


if __name__ == "__main__":
    unittest.main()
