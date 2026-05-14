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

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
