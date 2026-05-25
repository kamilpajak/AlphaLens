"""Layer T: thematic event-driven decision-support tool.

Parallel track to factor-paradigm-search. Pulls news for S&P 100 + sector
leaders, runs LLM theme extraction (Gemini Flash) and second-order beneficiary
mapping (Gemini 3 Pro), screens candidates against validated paradigm scorers
(Cohen-Malloy form-4, FCFF yield) plus SimFin valuation and technicals, then
emits a daily short-list digest to user. Design memo at
``docs/research/thematic_event_tool_v1_design_2026_05_15.md``.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
