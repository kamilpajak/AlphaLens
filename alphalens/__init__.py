"""AlphaLens — stock analysis pipeline wrapping TradingAgents.

Layered architecture:
    alphalens.watchdog               — Layer 1: SEC EDGAR event monitor
    alphalens.screeners.prescreener  — Layer 2a: S&P 500 fundamental + technical filter
    alphalens.screeners.themed       — Layer 2b: curated themed YAML universe (pluggable scorer)
    alphalens.screeners.lean         — Layer 2c: archived (failed 5-year validation)

Layer 3 is the upstream `tradingagents` framework (imported as a dependency).
"""
