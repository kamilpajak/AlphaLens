"""Shared Gemini configuration for TradingAgentsGraph.

Single source of truth for both `run_gemini.py` (ad-hoc runs) and
`cli.watchdog_main._build_worker` (launchd process-queue).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG


def build_gemini_config() -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["llm_provider"] = "google"
    cfg["deep_think_llm"] = "gemini-3.1-pro-preview"
    cfg["quick_think_llm"] = "gemini-3-flash-preview"
    cfg["google_thinking_level"] = "high"
    cfg["backend_url"] = None
    cfg["max_debate_rounds"] = 1
    cfg["max_risk_discuss_rounds"] = 1
    cfg["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "alpha_vantage",
        "news_data": "alpha_vantage",
    }
    return cfg
