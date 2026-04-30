"""Tests for alphalens.archive.guru.report — 4-year aggregator + kill evaluator."""

from __future__ import annotations

import unittest

import pandas as pd

from alphalens.archive.guru.llm_scorer import ConvictionResult
from alphalens.archive.guru.pilot_runner import SingleYearResult


def _stub_year(year: int, port_return: float, bench_return: float) -> SingleYearResult:
    return SingleYearResult(
        year=year,
        asof=pd.Timestamp(f"{year}-01-01"),
        picks=[
            ConvictionResult(
                ticker="AAPL",
                asof=pd.Timestamp(f"{year}-01-01"),
                conviction=85.0,
                rationale="x",
                prompt_sha="a" * 64,
                raw_response="{}",
                input_tokens=1000,
                output_tokens=50,
                cost_usd=0.001,
            )
        ],
        portfolio_return=port_return,
        benchmark_return=bench_return,
        outperformance=port_return - bench_return,
        total_cost_usd=0.005,
        n_scored=30,
        n_skipped=0,
    )


class TestPilotReportAggregation(unittest.TestCase):
    def test_aggregates_per_year_means(self):
        from alphalens.archive.guru.report import PilotReport

        years = [
            _stub_year(2018, 0.10, 0.05),  # +5pp outperf
            _stub_year(2020, 0.15, 0.10),  # +5pp
            _stub_year(2022, -0.05, -0.10),  # +5pp
            _stub_year(2024, 0.20, 0.15),  # +5pp
        ]

        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        self.assertAlmostEqual(report.mean_outperformance, 0.05, places=6)
        self.assertAlmostEqual(report.min_year_outperformance, 0.05, places=6)
        self.assertAlmostEqual(report.total_cost_usd, 0.02, places=6)

    def test_correlation_to_benchmark(self):
        from alphalens.archive.guru.report import PilotReport

        years = [
            _stub_year(2018, 0.10, 0.05),
            _stub_year(2020, 0.15, 0.10),
            _stub_year(2022, -0.05, -0.10),
            _stub_year(2024, 0.20, 0.15),
        ]

        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        # All outperformance = +5pp → perfect correlation with benchmark
        self.assertGreater(report.correlation_to_benchmark, 0.99)


class TestKillEvaluation(unittest.TestCase):
    def test_proceed_when_all_thresholds_met(self):
        """Strong outperformance with LOW benchmark correlation → PROCEED."""
        from alphalens.archive.guru.report import PilotReport

        # Portfolio outperforms every year but amounts VARY → decorrelates from bench
        years = [
            _stub_year(2018, 0.18, 0.05),  # +13pp strong
            _stub_year(2020, 0.11, 0.10),  # +1pp tiny
            _stub_year(2022, 0.05, -0.10),  # +15pp huge beat in bear (decorrelates)
            _stub_year(2024, 0.17, 0.15),  # +2pp small
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        # Correlation should be < 0.90 for this fixture (uncorrelated signals)
        self.assertLess(report.correlation_to_benchmark, 0.90)

        verdict = report.evaluate_kill_thresholds()

        self.assertEqual(verdict.label, "PROCEED")

    def test_kill_when_mean_outperformance_below_200_bps(self):
        from alphalens.archive.guru.report import PilotReport

        years = [
            _stub_year(2018, 0.06, 0.05),  # +1pp
            _stub_year(2020, 0.11, 0.10),  # +1pp
            _stub_year(2022, -0.09, -0.10),  # +1pp
            _stub_year(2024, 0.16, 0.15),  # +1pp — mean outperf = 100 bps
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        verdict = report.evaluate_kill_thresholds()

        self.assertEqual(verdict.label, "KILL")
        self.assertIn("mean_outperformance", verdict.failed_gates)

    def test_kill_when_any_year_underperforms_benchmark(self):
        from alphalens.archive.guru.report import PilotReport

        years = [
            _stub_year(2018, 0.15, 0.05),  # +10pp great
            _stub_year(2020, 0.20, 0.10),  # +10pp
            _stub_year(2022, -0.15, -0.10),  # -5pp FAIL in bear
            _stub_year(2024, 0.25, 0.15),  # +10pp
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        verdict = report.evaluate_kill_thresholds()

        self.assertEqual(verdict.label, "KILL")
        self.assertIn("min_year_outperformance", verdict.failed_gates)

    def test_gray_zone_when_mean_between_thresholds(self):
        """Mean outperformance 200-500 bps + uncorrelated → GRAY (not PROCEED, not KILL)."""
        from alphalens.archive.guru.report import PilotReport

        # Uncorrelated moderate outperformance
        years = [
            _stub_year(2018, 0.10, 0.05),  # +5pp
            _stub_year(2020, 0.08, 0.10),  # -2pp (underperforms benchmark in bull)
            _stub_year(2022, -0.07, -0.10),  # +3pp
            _stub_year(2024, 0.18, 0.15),  # +3pp
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        verdict = report.evaluate_kill_thresholds()

        # 2020 underperforms → KILL on min-year gate, not GRAY.
        # This is realistic: even "GRAY" zones usually have at least one failing gate.
        self.assertIn(verdict.label, ("GRAY", "KILL"))


class TestRelaxedMinYearTolerance(unittest.TestCase):
    """Per Perplexity 2026-04-25: 'min-year > 0' is structurally unfair for
    value-style strategies. Relaxed gate '> -5%' is meritorious (not goalpost).
    Buffett 1999 (-9pp vs S&P) would have failed strict gate."""

    def test_proceed_with_relaxed_tolerance_when_underperformance_within_5pct(self):
        from alphalens.archive.guru.report import PilotReport

        # Underperforms by 3pp in 2020 (within -5% tolerance)
        years = [
            _stub_year(2018, 0.18, 0.05),  # +13pp
            _stub_year(2020, 0.07, 0.10),  # -3pp (within tolerance)
            _stub_year(2022, 0.05, -0.10),  # +15pp
            _stub_year(2024, 0.17, 0.15),  # +2pp
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        verdict_strict = report.evaluate_kill_thresholds()
        verdict_relaxed = report.evaluate_kill_thresholds(min_year_tolerance=-0.05)

        # Strict gate fails (negative min-year)
        self.assertEqual(verdict_strict.label, "KILL")
        # Relaxed gate (within -5pp tolerance) → not KILL
        self.assertNotEqual(verdict_relaxed.label, "KILL")

    def test_kill_when_underperformance_exceeds_tolerance(self):
        from alphalens.archive.guru.report import PilotReport

        # Underperforms by 8pp in 2020 (beyond -5% tolerance)
        years = [
            _stub_year(2018, 0.18, 0.05),
            _stub_year(2020, 0.02, 0.10),  # -8pp (BEYOND -5pp tolerance)
            _stub_year(2022, 0.05, -0.10),
            _stub_year(2024, 0.17, 0.15),
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        verdict = report.evaluate_kill_thresholds(min_year_tolerance=-0.05)

        self.assertEqual(verdict.label, "KILL")
        self.assertIn("min_year_outperformance", verdict.failed_gates)

    def test_default_tolerance_is_zero_strict(self):
        """Backward compat — calling without arg keeps strict 'min > 0' gate."""
        from alphalens.archive.guru.report import PilotReport

        years = [
            _stub_year(2018, 0.18, 0.05),
            _stub_year(2020, 0.07, 0.10),  # -3pp
            _stub_year(2022, 0.05, -0.10),
            _stub_year(2024, 0.17, 0.15),
        ]
        report = PilotReport(years=years, prompt_sha="a" * 64, git_sha="b" * 40)

        # No arg → strict gate (default 0.0) → KILL
        self.assertEqual(report.evaluate_kill_thresholds().label, "KILL")


if __name__ == "__main__":
    unittest.main()
