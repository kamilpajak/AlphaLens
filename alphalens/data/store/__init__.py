"""PIT / as-of-t data store — single source of truth for historical reads.

Modules here answer the question "what did we know about X as of t?" for any
backtest or paper-trade replay. Consolidating these reads in one namespace
prevents research/live mismatch (Quant 2.0 feature-store discipline).

Modules:
- survivorship_pit   — survivorship-bias-free universe construction (delisted backfill)
- history            — OHLCV history store (Parquet + cache)
- fundamentals_pit   — point-in-time fundamentals store (powers Carhart-4F replay)
- simfin             — SimFin disk store backing fundamentals_pit

Layer 3 backtest engine and Layer 5 attribution both consume this layer; nothing
in `data/store/` imports back from any layer.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
