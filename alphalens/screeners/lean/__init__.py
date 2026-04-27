"""Lean-based batch universe screener (Layer 2c).

Runs QuantConnect Lean in Docker daily after US market close, ranks a curated
~500-ticker small/mid-cap universe by momentum/breakout/volume, emits top-N as
Candidates into the unified queue.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ARCHIVED"
__closed_date__ = "2026-04-19"
__closed_reason__ = "5y rigorous validation failed: Sharpe 0.25 net, FF3 alpha t-stat 0.14"
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/backtest/layer2c_revalidation.md",
    "sanity_checks_4gate": "N/A: rule-based screener, no passive overlay",
    "walk_forward_oos": "docs/backtest/layer2c_revalidation.md",
    "multiple_testing_correction": "docs/backtest/layer2c_revalidation.md",
    "cost_drag": "docs/backtest/layer2c_revalidation.md",
    "bootstrap_ci": "docs/backtest/layer2c_revalidation.md",
    "survivorship_pit": "docs/backtest/layer2c_revalidation.md",
}
