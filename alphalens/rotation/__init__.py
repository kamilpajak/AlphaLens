"""Layer 2e tactical sector rotation overlay.

R12 long-only sector ETF rotation driven by macro regime classification.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-23"
__closed_reason__ = (
    "Failed IS sanity (2/4) + OOS sanity (3/4); OOS t=0.33 vs IS t=1.96. "
    "OverlayEngine, FREDClient, sanity_checks retained as reusable infra."
)
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/research/5_paradigm_failures_postmortem.md",
    "sanity_checks_4gate": "docs/research/5_paradigm_failures_postmortem.md",
    "walk_forward_oos": "docs/research/5_paradigm_failures_postmortem.md",
    "multiple_testing_correction": "UNTESTED: OOS t=0.33 fails any threshold; formal correction skipped",
    "cost_drag": "N/A: quarterly rebalance ETF overlay, drag <25 bps not material",
    "bootstrap_ci": "UNTESTED: re-val cost > marginal value (verdict already strong)",
    "survivorship_pit": "N/A: SPY/QQQ/IWM core ETFs, no survivorship risk",
}
