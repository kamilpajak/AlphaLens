"""Locked operational constants for the paper-trade harness.

See ``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §3 for the
sizing math + reasoning. Re-derived numbers are pinned here so any code path
that allocates capacity references the same source of truth.
"""

from __future__ import annotations

# Historical cross-check, NOT the binding sizing constraint anymore.
# Equivalence with v2: STEADY_STATE_GROSS_FRAC / EXPECTED_AVG_HOLD_DAYS ≈
# 0.022 daily, integrated over W=30d hold ≈ 0.667 ≈ 240 / 360 = L / N_FIXED.
N_FIXED = 360

# Default paper equity used when no live AlpacaClient is provided (tests,
# dry-runs). Production planner reads live equity from
# ``AlpacaClient.get_account().equity`` — the live $1M paper account
# matches this value 1:1 at provisioning time.
DEFAULT_PAPER_EQUITY_USD = 1_000_000.0

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

# Default location for the paper ledger SQLite file. Operator can override
# via CLI flag or env (analogous to ALPHALENS_BRIEFS_DIR for Django).
DEFAULT_LEDGER_RELPATH = ".alphalens/paper_ledger.db"

# Default location for thematic brief parquets (matches the daily pipeline's
# write target + Django bind mount source).
DEFAULT_BRIEFS_RELPATH = ".alphalens/thematic_briefs"
