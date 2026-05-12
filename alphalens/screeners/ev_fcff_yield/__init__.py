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

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
