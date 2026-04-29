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

    def test_filters_phases_consistently_when_one_metric_is_nan(self):
        """When a phase has a valid alpha_t but NaN excess_net_ann (or vice
        versa), the row must be dropped from BOTH lists in the verdict
        computation — otherwise zip(strict=False) silently truncates and
        pairs phase-i of one list with phase-i of the other, mis-aligning
        the data. Single-pass filtering on (alpha_t valid AND excess valid)
        guarantees correct correspondence.

        Concrete case that exposes the bug: phase 2 has alpha_t=2.0 but
        excess_net_ann=NaN. With the buggy independent-filter approach,
        t_values keeps 5 entries (mean=1.0, AT the FAIL gate boundary),
        excess_values keeps 4 entries (mean=0.025, positive). The verdict
        falls through to MID. With single-pass filtering, phase 2 drops
        from BOTH; t_values is [2.0, 2.0, -0.5, -0.5] with mean 0.75 < 1.0
        → correct FAIL verdict.
        """
        from alphalens.backtest.multi_phase import robust_verdict

        rows = [
            {"alpha_t": 2.0, "excess_net_ann": 0.10},
            {"alpha_t": 2.0, "excess_net_ann": 0.10},
            {"alpha_t": 2.0, "excess_net_ann": float("nan")},  # half-valid
            {"alpha_t": -0.5, "excess_net_ann": -0.05},
            {"alpha_t": -0.5, "excess_net_ann": -0.05},
        ]
        self.assertEqual(robust_verdict(rows), "FAIL")

    def test_pass_threshold_at_minimum_with_all_phases_at_floor(self):
        """Boundary contract: all phases exactly at the PASS gate (t=1.5,
        excess=0) → PASS. Locks the >= behavior across the simplification
        that drops the redundant `mean_t >= 1.5` predicate."""
        from alphalens.backtest.multi_phase import robust_verdict

        rows = [
            {"alpha_t": 1.5, "excess_net_ann": 0.05},
            {"alpha_t": 1.5, "excess_net_ann": 0.05},
            {"alpha_t": 1.5, "excess_net_ann": 0.05},
            {"alpha_t": 1.5, "excess_net_ann": 0.05},
            {"alpha_t": 1.5, "excess_net_ann": 0.05},
        ]
        self.assertEqual(robust_verdict(rows), "PASS")

    def test_summarise_logs_when_metric_has_partial_coverage(self):
        """Per issue #38 item 2 (Option A): per-key independent NaN filtering
        is retained for the diagnostic summary, but partial coverage (any
        metric's `n` differing from total phase count) is surfaced via a
        single INFO log so silent column-misalignment doesn't pass review.
        """
        from alphalens.backtest.multi_phase import summarise_phase_results

        rows = [
            {
                "sharpe_gross": 0.4,
                "sharpe_net": 0.2,
                "excess_gross_ann": 0.18,
                "excess_net_ann": 0.15,
                "alpha_t": 1.5,
            },
            {
                "sharpe_gross": 0.6,
                "sharpe_net": 0.4,
                "excess_gross_ann": 0.22,
                "excess_net_ann": float("nan"),
                "alpha_t": 1.8,
            },
            {
                "sharpe_gross": 0.5,
                "sharpe_net": 0.3,
                "excess_gross_ann": 0.20,
                "excess_net_ann": 0.17,
                "alpha_t": 1.6,
            },
        ]

        with self.assertLogs("alphalens.backtest.multi_phase", level="WARNING") as cm:
            summary = summarise_phase_results(rows)

        # Logged the partial-coverage warning
        self.assertTrue(
            any("excess_net_ann" in m and "partial" in m.lower() for m in cm.output),
            f"expected partial-coverage log mentioning excess_net_ann; got {cm.output}",
        )
        # Per-metric counts unchanged (Option A is observability, not breaking change)
        self.assertEqual(summary["excess_net_ann"]["n"], 2)
        self.assertEqual(summary["alpha_t"]["n"], 3)

    def test_summarise_silent_when_all_metrics_complete(self):
        """No log when every metric has full coverage — must not spam normal runs."""
        import logging

        from alphalens.backtest.multi_phase import summarise_phase_results

        rows = [
            {
                "sharpe_gross": 0.4,
                "sharpe_net": 0.2,
                "excess_gross_ann": 0.18,
                "excess_net_ann": 0.15,
                "alpha_t": 1.5,
            },
            {
                "sharpe_gross": 0.6,
                "sharpe_net": 0.4,
                "excess_gross_ann": 0.22,
                "excess_net_ann": 0.18,
                "alpha_t": 1.8,
            },
        ]

        # `assertNoLogs` is 3.10+; emulate by capturing and asserting empty.
        logger = logging.getLogger("alphalens.backtest.multi_phase")
        with self.assertLogs(logger, level="WARNING") as cm:
            logger.warning("anchor")  # ensure the context produces at least one record
            summarise_phase_results(rows)

        partial_logs = [m for m in cm.output if "partial" in m.lower()]
        self.assertEqual(partial_logs, [])

    def test_single_pass_filter_does_not_promote_truncated_to_pass(self):
        """Regression: the old `zip(strict=False)` would truncate the tail of
        the longer list. If the LAST phase happened to be the only failing
        one and got truncated, a verdict could be wrongly upgraded. After
        the single-pass-filter fix, both metrics' tail entries are kept
        whenever both are valid — the failing phase pulls the verdict down."""
        from alphalens.backtest.multi_phase import robust_verdict

        # Four strong phases plus one deeply negative phase (NOT NaN — must
        # remain in the valid set). Mean t = (2.1+2.0+2.2+2.3-0.5)/5 = 1.62
        # so doesn't hit FAIL on the t<1.0 gate. Mean excess = (0.18+0.18+0.20+0.20-0.10)/5
        # = 0.132, > 0. With one phase materially negative but not majority,
        # contract says MID, not PASS.
        rows = [
            {"alpha_t": 2.1, "excess_net_ann": 0.18},
            {"alpha_t": 2.0, "excess_net_ann": 0.18},
            {"alpha_t": 2.2, "excess_net_ann": 0.20},
            {"alpha_t": 2.3, "excess_net_ann": 0.20},
            {"alpha_t": -0.5, "excess_net_ann": -0.10},
        ]
        self.assertEqual(robust_verdict(rows), "MID")


if __name__ == "__main__":
    unittest.main()
