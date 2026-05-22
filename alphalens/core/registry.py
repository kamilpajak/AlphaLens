"""Screener registry — one dict, add an entry when a new screener lands.

Every pipeline listed here is expected to expose `to_candidates(df)` so the CLI
can funnel results through the shared `CandidateQueue`.

Naming:
    `SCREENERS` key = pipeline identity (universe invariant).
    `SOURCE_PRIORITY` key = `Candidate.source` value = scorer identity.
These are decoupled: one pipeline can emit candidates tagged with different
source names depending on the scorer injected.
"""

from __future__ import annotations

from alphalens.screeners.prescreener.integration import PrescreenerPipeline

SCREENERS = {
    "prescreener": PrescreenerPipeline,
}

SOURCE_PRIORITY = {
    "watchdog_sec": 0,
    "prescreener": 20,
}
