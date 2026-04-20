"""Screener registry — one dict, add an entry when a new screener lands.

Every pipeline listed here is expected to expose `to_candidates(df)` so the CLI
can funnel results through the shared `CandidateQueue`.
"""

from __future__ import annotations

from alphalens.lean_screener.pipeline import LeanScreenerPipeline
from alphalens.momentum_screener.pipeline import MomentumPipeline
from alphalens.prescreener.integration import PrescreenerPipeline

SCREENERS = {
    "momentum": MomentumPipeline,
    "prescreener": PrescreenerPipeline,
    "lean": LeanScreenerPipeline,
}

SOURCE_PRIORITY = {
    "watchdog_sec": 0,
    "momentum": 10,
    "early-stage": 10,  # Layer 2b variant — base-breakout scorer, same priority as classic momentum
    "lean": 15,
    "prescreener": 20,
}
