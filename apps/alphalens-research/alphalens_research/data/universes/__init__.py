"""PIT-clean curated index universes (S&P 500 / 400 / 600 / 1500).

ACTIVE namespace introduced 2026-05-03 for event_drift v4. Distinct from
the market-cap-band ``alphalens_research.data.alt_data.pit_universe`` (R2000-approx
mcap-band reconstruction); this namespace handles index-membership lists
maintained by S&P committee decisions, not derived from XBRL+OHLCV.

Modules:
- sp1500_pit          — S&P 500 + 400 + 600 union, latest-snapshot-≤-asof loader
- ishares_refresher   — generic iShares AJAX-CSV ETF holdings refresher
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
