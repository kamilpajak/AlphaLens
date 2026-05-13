"""EV/FCFF-Yield value screener (paradigm test #13, Layer 2 cross-sectional).

Pre-registered as ``ev_fcff_yield_2026_05_12_v1`` in signal class
``fundamental_value_dcf_2026_05_12`` — see
``docs/research/preregistration/ledger.json``.

Design memo: ``docs/research/ev_fcff_yield_v1_design_2026_05_12.md`` (LOCKED).

Honest framing: this is a value factor of the FCF-yield family applied to
the R2000 ex-financials universe with the AlphaLens cost model and
phase-robust audit framework. Per the 2026-05-12 adversarial review,
single-stage reverse Gordon with fixed WACC is mathematically a monotonic
transformation of FCF/EV at constant ``r``, so ranking by ``g_implied``
collapses to ranking by FCF/EV. The module is named for the metric it
actually computes (EV/FCFF yield), not the discovery narrative.

Data: SimFin Start tier ($25/mo, paid 2026-05-12). 10y depth from 2016,
PIT via SimFin ``Publish Date`` column. Universe excludes financials per
Option D decision (banks/insurance need residual-income / embedded-value
treatment, reserved for paradigm #14 conditional on #13 PASS).
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__: str = "2026-05-13"
__closed_reason__: str = (
    "Paradigm #13 FAIL on full 3-window audit 2026-05-12. EV/FCFF-yield on R2000 "
    "ex-financials produces real but modest positive signal (Carhart-4F αt mean "
    "across windows = 1.18, range per-window 0.96..1.34, every phase positive 15/15) "
    "below the project Bonferroni 3.5 threshold. FCF-yield-under-false-label finding "
    "from Perplexity 2026-05-12 adversarial review vindicated: factor produces "
    "documented-literature premium magnitudes (excess net 1.2%/12.4%/4.0% per window) "
    "but t-stat insufficient for the conservative gate. Class "
    "fundamental_value_dcf_2026_05_12 remains OPEN per project doctrine "
    "(feedback_never_close_the_door); this module is the v1 implementation only."
)
__closed_evidence__: dict[str, str] = {
    "walk_forward_oos": "docs/research/ev_fcff_yield_audit_verdict_2026_05_12.md",
    "carhart_4f_hac": "docs/research/ev_fcff_yield_audit_verdict_2026_05_12.md",
    "cost_drag": (
        "UNTESTED: G4 cost-stress in this orchestrator computes alpha as scalar "
        "(alpha_gross − drag_ann), leaving t-stat invariant to cost level — flagged "
        "in postmortem §4 as structural duplicate of G1. Verdict FAIL on G1/G2 "
        "unambiguously regardless; cost-stress as independent gate untested here."
    ),
    "bootstrap_ci": (
        "N/A: memo §8 success criteria do not require Romano-Wolf block-bootstrap "
        "for paradigm #13 (single-layer screener; bootstrap reserved for compound + "
        "overlay paradigms)"
    ),
    "multiple_testing_correction": "docs/research/ev_fcff_yield_v1_design_2026_05_12.md",
    "sanity_checks_4gate": "docs/research/ev_fcff_yield_audit_verdict_2026_05_12.md",
    "survivorship_pit": (
        "UNTESTED: universe is forward-looking — current IWM snapshot intersected "
        "with SimFin us-income-annual (memo §2 documented limitation). Survivorship "
        "bias on R2000 historical-composition is acknowledged but not quantified in "
        "this audit. Magnitude likely ~50-100 bps/y per Perplexity R3 guidance "
        "(comparable to layer 2d pit_universe known bias)."
    ),
}
