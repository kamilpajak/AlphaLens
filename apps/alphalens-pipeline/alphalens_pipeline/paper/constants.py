"""Locked operational constants for the paper-trade harness.

See ``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §3 for the
sizing math + reasoning. Re-derived numbers are pinned here so any code path
that allocates capacity references the same source of truth.
"""

from __future__ import annotations

# Little's Law: peak L = λ · W = 8 candidates/day · 30 avg-hold days = 240.
# Multiplied by 1.5 safety margin (paradigm-14 doctrine) → 360.
# Used to cap per-position weight via ``1 / N_FIXED``; binds for ~95% of
# candidates per the empirical suggested_size_pct distribution.
N_FIXED = 360

# Default paper equity used when no live AlpacaClient is provided (tests,
# dry-runs). Production planner reads live equity from
# ``AlpacaClient.get_account().equity`` — the live $1M paper account
# matches this value 1:1 at provisioning time.
DEFAULT_PAPER_EQUITY_USD = 1_000_000.0

# Gross safety guard: block new orders if planned cumulative notional would
# push the book past this fraction of equity. Paradigm-14 cost-model
# discipline ("no forced rebalancing" — overlap absorbed by pre-allocated
# capacity). Belt-and-suspenders given the ``1/N_FIXED`` per-position cap.
GROSS_SAFETY_FRAC = 1.0

# Time-stop applied to filled positions. Closed-form decision per memo §4
# (PEAD drift literature: bulk complete by day 60).
TIME_STOP_DAYS = 60

# Entry-order TTL fallback if a candidate's brief_trade_setup omits
# ``order_ttl_days`` (older parquet schema). Matches the trade_setup memo's
# documented default.
DEFAULT_ORDER_TTL_DAYS = 10

# Default location for the paper ledger SQLite file. Operator can override
# via CLI flag or env (analogous to ALPHALENS_BRIEFS_DIR for Django).
DEFAULT_LEDGER_RELPATH = ".alphalens/paper_ledger.db"

# Default location for thematic brief parquets (matches the daily pipeline's
# write target + Django bind mount source).
DEFAULT_BRIEFS_RELPATH = ".alphalens/thematic_briefs"
