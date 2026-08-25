"""Contract-tier operational constants shared across the client<->broker-manager
boundary.

Holds ``DEFAULT_ORDER_TTL_DAYS``, the entry-TTL default the Boundary-2
``TradeSpec`` wire schema (``broker_contract.trade_intent.schema``) depends
on, plus the 3 money-math sizing constants (``STEADY_STATE_GROSS_FRAC``,
``EXPECTED_AVG_HOLD_DAYS``, ``GROSS_SAFETY_FRAC``) that
``broker_contract.sizing`` depends on (2A-4a, sub-PR of the broker-manager
extraction arc — see
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1/§2.3). ``N_FIXED``, ``DEFAULT_PAPER_EQUITY_USD``, ``TIME_STOP_DAYS``,
and the ledger/briefs relpath defaults remain in
``alphalens_pipeline.paper.constants`` — those are client-side (brief-parse /
operator-default) concerns, not consumed by the money-math leaf. Stdlib-only
— this is the shared A-tier leaf, consumed by both ``alphalens_pipeline`` and
``alphalens_research``, never a consumer of either.
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

# v2 sizing constants (memo §2.3, supersedes v1's per-candidate cap).
# The planner computes a daily global scale factor preserving
# inter-candidate ratios while bounding aggregate steady-state gross:
#
#   daily_target  = STEADY_STATE_GROSS_FRAC × equity / EXPECTED_AVG_HOLD_DAYS
#   aggregate     = Σ_i suggested_size_pct_i / 100 × equity
#   scale_factor  = min(1.0, daily_target / aggregate)
#   final_pct_i   = suggested_size_pct_i × scale_factor
#
# Average per-candidate notional matches v1 by construction (Little's Law
# equivalence at steady state); variance / inter-candidate ratios restored.
STEADY_STATE_GROSS_FRAC = 0.667
EXPECTED_AVG_HOLD_DAYS = 30

# Gross safety guard: block new orders if planned cumulative notional
# would push the day's book past this fraction of equity. v2's global
# scaling keeps the typical daily aggregate well below this (target
# 2.2% of equity per day for steady-state ~67%), so the guard is a
# belt-and-suspenders layer that catches realised-lambda spikes the
# scale factor under-projects for.
GROSS_SAFETY_FRAC = 1.0

# Broker share-quantity precision. Owned quantities are whole numbers on the
# wire but arrive as floats, so a bare ``>=`` on two of them can flicker (e.g.
# ``45.9999999`` vs ``46.0``). Every quantity comparison in the rail uses this
# instead of a bare operator, and every "is this quantity real" question is
# asked against it rather than a local float epsilon — one number, one meaning.
# Re-exported as ``broker_contract.contract._QTY_EPS`` for the protection
# comparisons that already read it under that name.
#
# "One number, one meaning" is ENFORCED since #1125, not merely asserted:
# ``live_exit_engine`` carried its own ``_QTY_EPS = 0.5`` under a comment
# claiming it mirrored this one, and that copy decided whether a filled
# position kept its standalone disaster stop.
# ``tests/brokers/test_one_share_quantity_precision.py`` now fails when any
# pipeline module binds a precision name to a numeric literal instead of
# importing it. Re-binding the NAME is fine; re-declaring the VALUE is not.
#
# WHAT THIS VALUE ACTUALLY ENCODES: half a share is "not a real quantity" only
# where the venue trades whole shares. Saxo does — live-probed on our own
# cohort: ``MinimumTradeSize 1``, ``IncrementSize 1``,
# ``FractionalOrderEnabled false`` (fractional cash equities are Singapore-only).
# Under fractional quantities this constant would classify a genuine 0.3-share
# tranche as not real, and every "is this quantity real" answer on the rail
# would be wrong without raising. The name says precision; the value says whole
# shares. The durable fix is an adapter-reported quantity increment (the
# capability seam sketched in #1122), deliberately NOT built for a single
# adapter — see #1125 for why that decision was left open rather than forced.
QTY_PRECISION = 0.5
