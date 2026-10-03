"""PIT / as-of-t data store — single source of truth for historical reads.

Modules here answer the question "what did we know about X as of t?" for any
backtest or retrospective replay. Consolidating these reads in one namespace
prevents research/live mismatch (Quant 2.0 feature-store discipline).

Modules:
- delisting          — ``DelistingEvent`` dataclass + parquet/yaml loaders
- edgar_fundamentals — EDGAR companyfacts reads
- form4_pit          — Form-4 PIT store (consumes ``DelistingEvent`` from delisting)
- history            — OHLCV history store (Parquet + cache)

Survivorship-bias diagnostic battery (consumer of backtest engine + Carhart
attribution) lives in ``alphalens_research.diagnostics.survivorship_pit`` and
reads ``DelistingEvent`` from this layer — preserving the forward-only DAG
``diagnostics → data/store`` instead of the previous reverse arrangement.

Layer 3 backtest engine and Layer 5 attribution both consume this layer; nothing
in `data/store/` imports back from any layer.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
