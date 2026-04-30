"""Screener-agnostic regime-conditional deployment gate.

Wraps any AlphaLens ``Scorer`` (per ``alphalens.backtest.engine.Scorer``
type alias) with a regime classifier so the strategy is deployed only
when the classifier is ON / scaled by a graded score.

Pluggable by construction. Concrete classifier candidates (yield curve
slope, VIX threshold, NFCI, HY OAS, cross-sectional dispersion) live in
their own modules — this package supplies the wrapper + protocol only.
"""

from typing import Literal

from alphalens.regime_gate.wrapper import (
    RegimeClassifier,
    regime_gated_scorer,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = ["RegimeClassifier", "regime_gated_scorer"]
