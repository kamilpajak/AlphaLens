"""Layer 2d insider-transactions screener.

See docs/research/layer2d_alt_data_design.md for locked design.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-24"
__closed_reason__ = "Carhart t=2.14 in-sample collapses to 0.68 OOS; classic overfit"
