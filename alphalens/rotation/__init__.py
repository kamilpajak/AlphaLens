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
