"""Locked operational constants for the paper-trade harness.

See ``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §3 for the
sizing math + reasoning. Re-derived numbers are pinned here so any code path
that allocates capacity references the same source of truth.
"""

from __future__ import annotations

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

# Historical cross-check, NOT the binding sizing constraint anymore.
# Equivalence with v2: STEADY_STATE_GROSS_FRAC / EXPECTED_AVG_HOLD_DAYS ≈
# 0.022 daily, integrated over W=30d hold ≈ 0.667 ≈ 240 / 360 = L / N_FIXED.
N_FIXED = 360

# Default paper equity used when no live AlpacaClient is provided (tests,
# dry-runs). Production planner reads live equity from
# ``AlpacaClient.get_account().equity`` — the live $1M paper account
# matches this value 1:1 at provisioning time.
DEFAULT_PAPER_EQUITY_USD = 1_000_000.0

# Gross safety guard: block new orders if planned cumulative notional
# would push the day's book past this fraction of equity. v2's global
# scaling keeps the typical daily aggregate well below this (target
# 2.2% of equity per day for steady-state ~67%), so the guard is a
# belt-and-suspenders layer that catches realised-lambda spikes the
# scale factor under-projects for.
GROSS_SAFETY_FRAC = 1.0

# Time-stop applied to filled positions. Memo §4: PEAD literature as
# analogy; primary anchor for thematic candidates is Moskowitz-Ooi-
# Pedersen 2012 time-series momentum (30-90d typical decay) +
# Chan-Jegadeesh-Lakonishok 1996 news-momentum.
#
# Unit: **trading days** (XNYS sessions, weekends and US public
# holidays skipped) since PR-B. The literature numbers above are
# expressed in trading days already (21d/month convention); the prior
# 60-calendar-day value was an under-estimate that also tightened
# erratically around long weekends and Q1 holiday clusters. 42 trading
# days ≈ 60 calendar days at long-run US holiday density (~10
# observances + ~104 weekend days per year).
TIME_STOP_DAYS = 42

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

# Default location for the paper ledger SQLite file. Operator can override
# via CLI flag or env (analogous to ALPHALENS_BRIEFS_DIR for Django).
DEFAULT_LEDGER_RELPATH = ".alphalens/paper_ledger.db"

# Default location for thematic brief parquets (matches the daily pipeline's
# write target + Django bind mount source).
DEFAULT_BRIEFS_RELPATH = ".alphalens/thematic_briefs"
