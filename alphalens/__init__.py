"""AlphaLens — research/learning infrastructure for retail quant active alpha experimentation.

Layered architecture per ADR 0007 (5 layers):
    alphalens.watchdog        — Layer 1 SEC EDGAR event monitor (ACTIVE)
    alphalens.screeners       — Layer 2 selection (cross-sectional rank @ t)
    alphalens.gates           — Layer 2 selection-gate (RESEARCH_ONLY)
    alphalens.backtest        — Layer 3 strided rebalance engine
    alphalens.overlays        — Layer 4 time-series sizing overlays (RESEARCH_ONLY)
    alphalens.attribution     — Layer 5 cost / factor / metrics / verdict (ACTIVE)

Cross-cutting:
    alphalens.core            — plumbing (queue, candidates, runner, registry, config)
    alphalens.data            — clients + parsers + PIT store (data/store/ = as-of-t SoT)
    alphalens.preregistration — methodology bundle (mirror OSS phase-robust-backtesting)
    alphalens.literature_review — Perplexity periodic scan

Closed strategies (10 paradigm failures) live under `alphalens.archive.*` per
ADR 0005 (anti-pattern catalog). See `docs/research/paradigm_failures_postmortem.md`.
"""
