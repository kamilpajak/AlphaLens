"""Layer 2d insider-transactions screener.

See docs/research/layer2d_alt_data_design.md for locked design.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-24"
__closed_reason__ = "Carhart t=2.14 in-sample collapses to 0.68 OOS; classic overfit"
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/backtest/layer2d_insider_oos.md",
    "sanity_checks_4gate": "N/A: weekly Form-4 scoring, not rotation overlay",
    "walk_forward_oos": "docs/research/layer2d_validation_final.md",
    "multiple_testing_correction": "docs/research/layer2d_validation_final.md",
    "cost_drag": "docs/backtest/layer2d_insider_oos.md",
    "bootstrap_ci": "docs/backtest/layer2d_insider_oos.md",
    "survivorship_pit": "docs/research/layer2d_pit_build_runbook.md",
}
