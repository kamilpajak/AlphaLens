"""Layer 2g LLM-researcher pilot (GuruScorer single-prompt, not TradingAgents multi-agent)."""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-25"
__closed_reason__ = "GuruAgent pilot failed validation (paradigm failure 5/5)"
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "UNTESTED: paradigm hits AP-14 LLM ceiling; re-val needs $150-300 Gemini budget",
    "sanity_checks_4gate": "N/A: not rotation overlay",
    "walk_forward_oos": "docs/research/5_paradigm_failures_postmortem.md",
    "multiple_testing_correction": "N/A: 4-year pre-committed regime test, no post-hoc correction",
    "cost_drag": "N/A: 1-year equal-weight hold, cost negligible",
    "bootstrap_ci": "UNTESTED: n=4 regime observations too small for bootstrap",
    "survivorship_pit": "N/A: random sample S&P 500 per year",
}
