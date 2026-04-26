"""Alt-data sources for Layer 2d insider-transactions screener.

See docs/research/layer2d_alt_data_design.md for locked design.

Layer 2d (insider cluster-buy) is CLOSED 2026-04-24 (Carhart t=2.14 IS → 0.68 OOS).
This package retained as RESEARCH_ONLY infrastructure: SEC EDGAR client, Form 4
parsing, ticker/CIK refreshers, Russell universe builder still useful for any
future event-driven research replay.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
