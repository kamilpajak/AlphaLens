"""Layer T: thematic event-driven decision-support tool.

Parallel track to factor-paradigm-search. Pulls news for S&P 100 + sector
leaders, runs LLM theme extraction (DeepSeek v4-flash) and second-order beneficiary
mapping (DeepSeek v4-pro), screens candidates against validated paradigm scorers
(Cohen-Malloy form-4, FCFF yield) plus SimFin valuation and technicals, then
emits a daily short-list digest to user. Design memo at
``docs/research/thematic_event_tool_v1_design_2026_05_15.md``.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
