"""Contract-tier operational constants shared across the client<->broker-manager
boundary.

Today this holds only ``DEFAULT_ORDER_TTL_DAYS``, the entry-TTL default the
Boundary-2 ``TradeSpec`` wire schema (``broker_contract.trade_intent.schema``)
depends on. The sizing constants (``STEADY_STATE_GROSS_FRAC``,
``EXPECTED_AVG_HOLD_DAYS``, ``N_FIXED``, ``DEFAULT_PAPER_EQUITY_USD``,
``GROSS_SAFETY_FRAC``), ``TIME_STOP_DAYS``, and the ledger/briefs relpath
defaults remain in ``alphalens_pipeline.paper.constants`` for now and join
this module in a later sub-PR of the broker-manager extraction arc (see
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1). Stdlib-only — this is the shared A-tier leaf, consumed by both
``alphalens_pipeline`` and ``alphalens_research``, never a consumer of
either.
"""

from __future__ import annotations

# Entry-order TTL fallback if a candidate's brief_trade_setup omits
# ``order_ttl_days`` (older parquet schema). Matches the trade_setup memo's
# documented default.
#
# Unit: **trading days** (XNYS) since PR-B. 7 trading days ≈ a clean
# calendar week-and-a-half of trading exposure. The prior 10-calendar-day
# value compressed to ~7 trading sessions in normal weeks and ~6 around
# Memorial Day / July 4 long weekends; pinning the unit to trading days
# removes the holiday drift.
DEFAULT_ORDER_TTL_DAYS = 7
