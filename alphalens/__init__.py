"""AlphaLens — stock analysis pipeline wrapping TradingAgents.

Layered architecture:
    alphalens.watchdog          — Layer 1: SEC EDGAR event monitor
    alphalens.prescreener       — Layer 2a: S&P 500 fundamental + technical filter
    alphalens.momentum_screener — Layer 2b: theme-based momentum scan

Layer 3 is the upstream `tradingagents` framework (imported as a dependency).
"""
