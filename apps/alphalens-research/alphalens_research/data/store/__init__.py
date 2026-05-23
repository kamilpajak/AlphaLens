"""PIT / as-of-t data store — single source of truth for historical reads.

Modules here answer the question "what did we know about X as of t?" for any
backtest or paper-trade replay. Consolidating these reads in one namespace
prevents research/live mismatch (Quant 2.0 feature-store discipline).

Modules:
- history            — OHLCV history store (Parquet + cache)
- fundamentals_pit   — point-in-time fundamentals store (powers Carhart-4F replay)
- simfin             — SimFin disk store backing fundamentals_pit
- form4_pit          — Form-4 PIT store (consumes ``DelistingEvent`` from diagnostics)

Survivorship-bias diagnostic battery (consumer of backtest engine + Carhart
attribution) lives in ``alphalens_research.diagnostics.survivorship_pit`` —
the ``DelistingEvent`` dataclass is sourced there and imported back here by
``form4_pit``.

Layer 3 backtest engine and Layer 5 attribution both consume this layer; nothing
in `data/store/` imports back from any layer.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
