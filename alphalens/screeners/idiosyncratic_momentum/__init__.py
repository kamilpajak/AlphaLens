"""Idiosyncratic (residual) momentum screener — paradigm test #15.

Pre-registered as ``idiosyncratic_momentum_2026_05_14_v1`` in signal class
``price_factor_search_2026_04_29`` (n=4 prior tests, all FAIL → n=5 with IM).
Strict class-internal critical |t| = 2.57 at α=0.05; project doctrine 3.5
binds (k=14→15 with IM registration).

Design memo: ``docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md``
(LOCKED 2026-05-14, post zen continuation_id ``24c057f6...``).

Mechanism (Blitz-Huij-Martens 2011): residualize stock returns against FF3
over rolling 36-month window, then cumulate residual returns over t-12 to
t-2 (11-month formation, 1-month skip) standardised by 36-month σ. Cross-
sectional top-decile, equal-weight, long-only, monthly rebalance.

§5.1 mandatory diagnostics (per zen 2026-05-14): primary Carhart-4F α
does NOT control for BAB; the 1/σ_36 standardisation injects a low-vol
tilt. Audit logs ``β_market`` + FF5+UMD attenuation + Sharpe-vs-raw-
momentum to enable honest postmortem reading.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__: str = "2026-05-14"
__closed_reason__: str = (
    "Paradigm #15 FAIL on full 3-window audit 2026-05-14. Idiosyncratic momentum "
    "on S&P 1500 PIT union produces real but monotonically-strengthening positive "
    "signal across IS/OOS/FL (Carhart-4F alpha_t mean 0.02 / 0.71 / 1.58 — every "
    "phase positive in OOS+FL but IS effectively null) below the project Bonferroni "
    "3.5 threshold and class-internal n=5 critical |t|=2.57. Honest prior from memo "
    "section 9 predicted 'alpha_t mean 1.5-2.5, vindicated mechanism below Bonferroni "
    "bar' — outcome matched in OOS+FL but IS surprisingly null (consistent with the "
    "literature finding 'IM weakens after the early 2000s'; effective IS sample "
    "truncated to ~5.4y of the registered 8y window by MIN_BARS_REQUIRED warm-up). "
    "Material findings: (1) Blitz 1/sigma standardisation did NOT inject low-vol "
    "tilt on this universe (realised beta_market 0.97-1.17, memo flag <0.8 not "
    "triggered); (2) turnover 19-36%/mo, far below the feared 60-80% hyper-turnover "
    "range; (3) FL period (2022-2024 momentum crisis era) had the BEST IM "
    "performance, opposite to memo section 9 anticipated penalty. Class "
    "price_factor_search_2026_04_29 remains OPEN per project doctrine "
    "(feedback_never_close_the_door); this module is the v1 implementation only."
)
__closed_evidence__: dict[str, str] = {
    "walk_forward_oos": "docs/research/idiosyncratic_momentum_audit_verdict_2026_05_14.md",
    "carhart_4f_hac": "docs/research/idiosyncratic_momentum_audit_verdict_2026_05_14.md",
    "cost_drag": (
        "docs/research/idiosyncratic_momentum_audit_2026-05-14.json (G4 reads "
        "alpha_t_net from net-cost regression per H1 fix pattern; all 3 windows "
        "FAIL G4 at 15bps half-spread)"
    ),
    "bootstrap_ci": (
        "N/A: memo section 8 success criteria do not require Romano-Wolf "
        "block-bootstrap (single-layer screener; bootstrap reserved for compound + "
        "overlay paradigms)"
    ),
    "multiple_testing_correction": "docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md",
    "sanity_checks_4gate": "docs/research/idiosyncratic_momentum_audit_verdict_2026_05_14.md",
    "survivorship_pit": (
        "UNTESTED: S&P 1500 PIT union is survivorship-biased per memo section 3 "
        "documented limitation (single recent snapshot fallback per index; ~100-300 "
        "bps/y upward bias on alpha estimates). Bias magnitude not quantified in "
        "this audit; consistent with paradigm-13 ev_fcff_yield and paradigm-14 PEAD-v2 "
        "posture. Material finding only if a paradigm PASSED on survivorship-biased "
        "universe (n/a for this FAIL verdict)."
    ),
}
