"""Lean screener configuration — paths, scoring weights, guardrails, Docker."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
LEAN_PROJECT_DIR = PACKAGE_DIR / "lean_project"
UNIVERSE_PATH = LEAN_PROJECT_DIR / "universe.yaml"
DELISTED_UNIVERSE_PATH = LEAN_PROJECT_DIR / "delisted_universe.yaml"

# Runtime data lives outside the repo, survives git operations.
_RUNTIME_ROOT = Path.home() / ".alphalens" / "lean"
DATA_DIR = _RUNTIME_ROOT / "data"                # Lean CSV inputs
RESULTS_DIR = _RUNTIME_ROOT / "results"          # Lean JSON outputs
LOGS_DIR = _RUNTIME_ROOT / "logs"                # Lean execution logs
FACTORS_DIR = _RUNTIME_ROOT / "factors"          # Fama-French + other factor data
FF3_DAILY_PATH = FACTORS_DIR / "ff3_daily.csv"

LEAN_DOCKER_IMAGE = "quantconnect/lean:latest"

# Market benchmarks — used by backtest regime classifier + Fama-French regression.
# Stored in the same Lean CSV format as screener tickers, loaded separately.
BENCHMARKS = ("SPY", "QQQ", "IWM")

LEAN_DEFAULTS = {
    # Scoring weights (MVP1 rule-based — sum to 1.0)
    "weight_roc20": 0.20,         # 20-day return z-score
    "weight_roc60": 0.20,         # 60-day return z-score
    "weight_volume_surprise": 0.20,  # today volume vs 20d avg, z-score
    "weight_trend_strength": 0.20,   # close > SMA50 > SMA200 stack
    "weight_breakout": 0.10,      # close > 20d rolling high (binary)
    "weight_near_high": 0.10,     # distance to 60d high

    # Feature windows
    "roc_short": 5,
    "roc_medium": 20,
    "roc_long": 60,
    "sma_short": 20,
    "sma_medium": 50,
    "sma_long": 200,
    "volume_window": 20,
    "breakout_window": 20,
    "near_high_window": 60,

    # Breakout rule (binary flag in score)
    "breakout_volume_multiple": 1.5,   # today vol > 1.5x 20d avg

    # Guardrails (applied in Lean algo before scoring)
    "min_price": 5.0,
    "max_price": 200.0,
    "min_avg_dollar_volume": 2_000_000.0,  # USD, 20-day average

    # Output
    "top_n": 30,

    # Polygon sync (Starter tier: effectively unlimited calls + 5 years history)
    "polygon_base_url": "https://api.polygon.io",
    "polygon_rate_limit_per_min": 100,     # Starter: no hard cap published — 100/min is safe headroom
    "history_bootstrap_days": 1825,        # ~5 yrs calendar, ~1260 trading days (Starter max)
}


def polygon_api_key() -> str:
    """Read Polygon API key from env. Empty string if unset (tests tolerate this)."""
    return os.environ.get("POLYGON_API_KEY", "")
