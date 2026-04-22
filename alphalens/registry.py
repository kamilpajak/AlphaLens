"""Screener registry — one dict, add an entry when a new screener lands.

Every pipeline listed here is expected to expose `to_candidates(df)` so the CLI
can funnel results through the shared `CandidateQueue`.

Naming:
    `SCREENERS` key = pipeline identity (universe invariant).
    `SOURCE_PRIORITY` key = `Candidate.source` value = scorer identity.
These are decoupled: one pipeline (e.g. `themed`) can emit candidates tagged
with different source names depending on the scorer injected (e.g. `momentum`
vs `early-stage`).
"""

from __future__ import annotations

from alphalens.screeners.insider.pipeline import InsiderPipeline
from alphalens.screeners.lean.pipeline import LeanScreenerPipeline
from alphalens.screeners.prescreener.integration import PrescreenerPipeline
from alphalens.screeners.themed.pipeline import ThemedPipeline

SCREENERS = {
    "themed": ThemedPipeline,
    "prescreener": PrescreenerPipeline,
    "lean": LeanScreenerPipeline,
    "insider": InsiderPipeline,  # Layer 2d — Form 4 cluster-buy detection
}

SOURCE_PRIORITY = {
    "watchdog_sec": 0,
    "momentum": 10,
    "early-stage": 10,  # Layer 2b variant — base-breakout scorer, same priority as classic momentum
    "insider": 12,       # Layer 2d — Form 4 cluster buys; time-sensitive but less urgent than live SEC events
    "lean": 15,
    "prescreener": 20,
}
