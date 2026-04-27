"""Layer 2f 8-K event-driven screener."""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-25"
__closed_reason__ = "8-K event screen failed validation (paradigm failure 4/5)"
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "UNTESTED: paradigm-level kill (event microstructure crowding); re-val needs CAR infra build (~2-4 weeks)",
    "sanity_checks_4gate": "N/A: event-driven, not rotation overlay",
    "walk_forward_oos": "UNTESTED: paradigm-level kill; OOS not run",
    "multiple_testing_correction": "UNTESTED: exploratory CAR by Item type, no formal Bonferroni",
    "cost_drag": "UNTESTED: event screen, no execution model built",
    "bootstrap_ci": "UNTESTED: winsorized mean only, no CI",
    "survivorship_pit": "N/A: S&P 500 universe, delisted treatment implicit",
}
