"""Lean-based batch universe screener (Layer 2c).

Runs QuantConnect Lean in Docker daily after US market close, ranks a curated
~500-ticker small/mid-cap universe by momentum/breakout/volume, emits top-N as
Candidates into the unified queue.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ARCHIVED"
__closed_date__ = "2026-04-19"
__closed_reason__ = "5y rigorous validation failed: Sharpe 0.25 net, FF3 alpha t-stat 0.14"

_REVAL_REPORT = "docs/backtest/layer2c_revalidation.md"

__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": _REVAL_REPORT,
    "sanity_checks_4gate": "N/A: rule-based screener, no passive overlay",
    "walk_forward_oos": _REVAL_REPORT,
    "multiple_testing_correction": _REVAL_REPORT,
    "cost_drag": _REVAL_REPORT,
    "bootstrap_ci": _REVAL_REPORT,
    "survivorship_pit": _REVAL_REPORT,
}
