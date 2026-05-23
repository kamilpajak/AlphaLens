"""AlphaLens pipeline — infrastructure + live production services.

This package is the deployment tier: the code that runs in launchd
(macOS) + systemd (VPS) + Docker on a daily / event-driven cadence.

Sub-packages:
    alphalens_pipeline.edgar_detector          — Layer 1 SEC EDGAR detection (launchd)
    alphalens_pipeline.thematic          — Phase A-E daily pipeline (VPS systemd)
    alphalens_pipeline.literature_scanner — monthly + weekly Perplexity scan (launchd)
    alphalens_pipeline.data              — clients, PIT store, universes, fundamentals, macro
    alphalens_pipeline.core              — candidate queue + dataclass plumbing
    alphalens_pipeline.scorers           — reusable validated scorer library

Dependency direction (enforced by tests/test_module_dependencies.py):

    alphalens_research.* → alphalens_pipeline.{data, core, scorers}    OK
    alphalens_pipeline.* → alphalens_research.*                        FORBIDDEN

The single allowed exception is the CLI (``alphalens_cli``), which
orchestrates both tiers via lazy imports inside command bodies.

The research tier (screeners, backtest, attribution, overlays, gates,
preaudit, diagnostics, paper_trade) lives in the sibling
``alphalens_research`` package and is RESEARCH_ONLY / CLOSED for the
most part — see its README for status markers.
"""
