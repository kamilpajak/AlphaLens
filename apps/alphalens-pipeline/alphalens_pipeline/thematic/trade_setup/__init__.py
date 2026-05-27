"""Deterministic Trade-Setup builder for thematic briefs (Layer 5).

Computes an entry ladder + take-profit ladder + structural disaster stop
from cached daily OHLCV — NO LLM-produced numbers (doctrine: levels come
from authoritative data, the LLM only narrates them). NOT a validated
alpha edge: technical levels carry no short-horizon predictive edge after
data-snooping correction (see design memo §2). The one evidence-based
component is volatility-normalized equal-risk sizing; everything else is
labelled as reference/coordination points, not forecasts.

Design memo: docs/research/thematic_trade_setup_v1_design_2026_05_27.md
"""

from __future__ import annotations

from alphalens_pipeline.thematic.trade_setup.model import (
    SCHEMA_VERSION,
    EntryTier,
    TpTranche,
    TradeSetup,
)

__all__ = [
    "SCHEMA_VERSION",
    "EntryTier",
    "TpTranche",
    "TradeSetup",
]
