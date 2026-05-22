"""Shared helpers + constants for the Phase E brief generator.

Mirrors the ``alphalens_research/thematic/screening/_common.py`` pattern: hoists
constants and pure functions used by both the renderer and the
orchestrator to a single import surface, so the "underscore = internal"
convention stays honest.
"""

from __future__ import annotations

import math
from typing import Any

# Memo §2 Layer 5: 8w default exit; 4w if catalyst-failure-triggered.
# Phase E emits both as columns so Phase F can apply the catalyst-failure
# downshift on runtime trigger detection.
TIME_EXIT_DEFAULT_WEEKS = 8
TIME_EXIT_ON_CATALYST_FAILURE_WEEKS = 4

# Memo §2 Layer 5 position-size ladder: 1.5%/2.0%/2.5% for conf 3/4/5.
# Conf 1-2 default to 1.0% (memo silent; conservative floor).
DISASTER_STOP_PCT = -25.0


def position_pct_from_conf(weighted_score: Any) -> float:
    """Map a Phase D weighted_score (1-5) to a position size %.

    None / NaN / non-numeric inputs collapse to the 1.0% floor — Phase D
    should always emit an int 1-5 but the orchestrator is defensive.
    """
    if weighted_score is None:
        return 1.0
    if isinstance(weighted_score, float) and math.isnan(weighted_score):
        return 1.0
    try:
        ws = int(weighted_score)
    except (TypeError, ValueError):
        return 1.0
    if ws >= 5:
        return 2.5
    if ws == 4:
        return 2.0
    if ws == 3:
        return 1.5
    return 1.0


__all__ = [
    "DISASTER_STOP_PCT",
    "TIME_EXIT_DEFAULT_WEEKS",
    "TIME_EXIT_ON_CATALYST_FAILURE_WEEKS",
    "position_pct_from_conf",
]
