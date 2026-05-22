"""Distress × Credit-regime compound screener (Layer 2 × Layer 4).

Pre-registered hypothesis ``distress_credit_v1_2026_05_04`` (class
``distress_credit_search_2026_05_04``). Long-only equal-weighted bottom-
quintile Merton-PD portfolio drawn from S&P 1500 PIT (excluding top-50
megacap and excluding top-quintile distress always), with portfolio
dollar exposure modulated by HY OAS z-score gate (BAMLH0A0HYM2; floor
0.5, ceiling 1.0, linear interp between z=+1 and z=-1).

Compound hypothesis pays single Bonferroni per ADR 0007. Layer 2
modifies *which* tickers (bottom quintile by Merton PD); Layer 4
modifies *how much* exposure (HY OAS z-score gate, applied OUTSIDE
engine in the experiment driver).
"""

from typing import Literal

from alphalens_research.screeners.distress_credit.merton import (
    merton_d2,
    merton_pd,
    realised_vol_60d,
)
from alphalens_research.screeners.distress_credit.scorer import distress_credit_adapter

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = [
    "distress_credit_adapter",
    "merton_d2",
    "merton_pd",
    "realised_vol_60d",
]
