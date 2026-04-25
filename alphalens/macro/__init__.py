"""Macro data clients + regime signals (FRED, scorer).

Reusable infrastructure powering rotation/ overlay (CLOSED) and any future
macro-aware research module.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
__closed_reason__ = "Infrastructure for research; no standalone strategy attached"
