"""AlphaLens — research/learning infrastructure for retail quant active alpha experimentation.

Layered architecture per ADR 0007 (5 layers):
    alphalens.watchdog       — Layer 1 SEC EDGAR event monitor (ACTIVE)
    alphalens.screeners      — Layer 2 selection (cross-sectional rank @ t)
    alphalens.gates    — Layer 2 selection-gate (RESEARCH_ONLY)
    alphalens.backtest       — Layer 3 strided rebalance engine
    alphalens.risk_overlay   — Layer 4 time-series sizing overlays (RESEARCH_ONLY)
    alphalens.backtest       — Layer 5 attribution (cost, factor, metrics, verdict)

Closed strategies (10 paradigm failures) live under `alphalens.archive.*` per
ADR 0005 (anti-pattern catalog). See `docs/research/paradigm_failures_postmortem.md`.
"""
