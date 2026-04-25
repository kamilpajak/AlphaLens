"""Aggregate multi-year GuruAgent pilot results + evaluate kill thresholds.

Per R12 pre-commit discipline, thresholds are locked BEFORE running pilot
and documented in the plan file (zippy-dancing-codd.md). This module just
evaluates them against the realised data.

Thresholds (locked 2026-04-24, plan v2):
  KILL when ANY:
    - mean outperformance across years < 200 bps
    - min-year outperformance < 0 (underperforms in any regime)
    - correlation to benchmark > 0.95 (no differentiation)
  PROCEED only when ALL:
    - mean outperformance >= 500 bps
    - min-year outperformance > 0
    - correlation to benchmark < 0.90
  GRAY otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from alphalens.guru.pilot_runner import SingleYearResult

KILL_MEAN_OUTPERF_BPS = 200.0
PROCEED_MEAN_OUTPERF_BPS = 500.0
KILL_CORRELATION_MAX = 0.95
PROCEED_CORRELATION_MAX = 0.90


@dataclass(frozen=True)
class KillVerdict:
    label: str  # "PROCEED" | "GRAY" | "KILL"
    failed_gates: tuple[str, ...]
    passed_gates: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class PilotReport:
    years: Sequence[SingleYearResult]
    prompt_sha: str
    git_sha: str

    @property
    def mean_outperformance(self) -> float:
        return float(np.mean([y.outperformance for y in self.years]))

    @property
    def min_year_outperformance(self) -> float:
        return float(min(y.outperformance for y in self.years))

    @property
    def max_year_outperformance(self) -> float:
        return float(max(y.outperformance for y in self.years))

    @property
    def total_cost_usd(self) -> float:
        return float(sum(y.total_cost_usd for y in self.years))

    @property
    def correlation_to_benchmark(self) -> float:
        port = [y.portfolio_return for y in self.years]
        bench = [y.benchmark_return for y in self.years]
        if len(port) < 2:
            return 0.0
        # np.corrcoef returns 2x2; we want the off-diagonal element.
        matrix = np.corrcoef(port, bench)
        corr = float(matrix[0, 1])
        if np.isnan(corr):
            return 0.0
        return corr

    def evaluate_kill_thresholds(self, *, min_year_tolerance: float = 0.0) -> KillVerdict:
        """Evaluate kill/proceed gates.

        ``min_year_tolerance`` (default 0.0 = strict) shifts the min-year
        underperformance threshold. Pass e.g. -0.05 to allow up to 5pp
        underperformance in worst year — meritorious for value-style
        strategies that historically lag in growth bulls (per Perplexity
        2026-04-25 follow-up: Buffett 1999 = −9pp vs S&P would fail strict
        gate but is *expected* value behaviour, not broken signal).
        """
        mean_bps = self.mean_outperformance * 10_000.0
        min_outperf = self.min_year_outperformance
        corr = self.correlation_to_benchmark

        failed: list[str] = []
        passed: list[str] = []

        # KILL gates (any failure kills)
        if mean_bps < KILL_MEAN_OUTPERF_BPS:
            failed.append("mean_outperformance")
        elif mean_bps >= PROCEED_MEAN_OUTPERF_BPS:
            passed.append("mean_outperformance")

        if min_outperf < min_year_tolerance:
            failed.append("min_year_outperformance")
        else:
            passed.append("min_year_outperformance")

        if corr > KILL_CORRELATION_MAX:
            failed.append("correlation_to_benchmark")
        elif corr < PROCEED_CORRELATION_MAX:
            passed.append("correlation_to_benchmark")

        if failed:
            label = "KILL"
        elif len(passed) == 3 and mean_bps >= PROCEED_MEAN_OUTPERF_BPS:
            label = "PROCEED"
        else:
            label = "GRAY"

        summary = (
            f"mean_outperf={mean_bps:+.0f} bps, "
            f"min_year={min_outperf:+.2%}, "
            f"corr={corr:+.2f}, "
            f"total_cost=${self.total_cost_usd:.2f}"
        )
        return KillVerdict(
            label=label,
            failed_gates=tuple(failed),
            passed_gates=tuple(passed),
            summary=summary,
        )

    def render_markdown(self) -> str:
        verdict = self.evaluate_kill_thresholds()
        lines = [
            "# GuruAgent Pilot v2 — report",
            "",
            f"**Prompt SHA:** `{self.prompt_sha}`",
            f"**Git SHA:** `{self.git_sha}`",
            f"**Total LLM cost:** ${self.total_cost_usd:.2f}",
            "",
            "## Verdict",
            "",
            f"**{verdict.label}** — {verdict.summary}",
            "",
            f"Passed gates: {', '.join(verdict.passed_gates) or '(none)'}",
            f"Failed gates: {', '.join(verdict.failed_gates) or '(none)'}",
            "",
            "## Per-year results",
            "",
            "| year | portfolio | benchmark | outperf | picks | skipped | cost |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for y in self.years:
            lines.append(
                f"| {y.year} | {y.portfolio_return:+.2%} | "
                f"{y.benchmark_return:+.2%} | {y.outperformance:+.2%} | "
                f"{len(y.picks)} | {y.n_skipped} | ${y.total_cost_usd:.3f} |"
            )
        lines += [
            "",
            "## Headline metrics",
            "",
            f"- Mean outperformance: {self.mean_outperformance:+.2%} "
            f"({self.mean_outperformance * 10_000:+.0f} bps)",
            f"- Min-year outperformance: {self.min_year_outperformance:+.2%}",
            f"- Max-year outperformance: {self.max_year_outperformance:+.2%}",
            f"- Correlation to benchmark: {self.correlation_to_benchmark:+.3f}",
            "",
            "## Picks per year",
            "",
        ]
        for y in self.years:
            lines.append(f"### {y.year}")
            lines.append("")
            lines.append("| ticker | conviction | rationale |")
            lines.append("|---|---:|---|")
            for p in y.picks:
                rationale_short = p.rationale[:120].replace("\n", " ")
                lines.append(f"| {p.ticker} | {p.conviction:.1f} | {rationale_short} |")
            lines.append("")
        return "\n".join(lines) + "\n"
