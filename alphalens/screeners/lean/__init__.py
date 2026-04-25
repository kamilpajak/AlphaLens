"""Lean-based batch universe screener (Layer 2c).

Runs QuantConnect Lean in Docker daily after US market close, ranks a curated
~500-ticker small/mid-cap universe by momentum/breakout/volume, emits top-N as
Candidates into the unified queue.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ARCHIVED"
__closed_date__ = "2026-04-19"
__closed_reason__ = "5y rigorous validation failed: Sharpe 0.25 net, FF3 alpha t-stat 0.14"
