"""Locked operational constants left from the paper-trade harness.

See ``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §4 for the
time-stop reasoning. The harness itself was decommissioned (ADR 0012); the
feedback replay still reads the time stop.
"""

from __future__ import annotations

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
