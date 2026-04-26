"""Quiver Quantitative congressional-trades screener.

Used for the Quiver Congress long/short validation (memory:
project_quiver_validation.md). Backtest produced alpha t=-2.14 HAC, robust to
drop-top-50 filter — signal so over-mined it inverted relative to the academic
publication. Verdict: KILL.

Code retained as anti-pattern record per ADR 0005 (closed-layers as
anti-pattern catalog) and to support `scripts/quiver_validate.py` /
`scripts/quiver_robustness.py` research replay.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-22"
__closed_reason__ = (
    "Quiver Congress L/S validation: alpha t=-2.14 HAC, robust to drop-top-50 "
    "filter — signal inverted from published academic edge, evidence of "
    "extreme alt-data crowding (AP-9)."
)
