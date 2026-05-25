"""Layer 4 quantitative screen — enriches Phase C candidates with 4 signals.

Per design memo §12: insider Cohen-Malloy + FCFF yield + SimFin valuation +
technicals, all percentile-ranked within SimFin industry peers. Output is a
SCORING layer (adds columns), not a filter.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
