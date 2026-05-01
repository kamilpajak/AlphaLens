"""v4 alt_data_screener_search class — feature joiner + Lasso scorer.

10-feature whitelist locked in v4 v2 pre-reg per
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json.
Pre-Phase-A; no holdout reveal performed.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
