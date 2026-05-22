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
    alphalens.literature_review — Perplexity periodic scan

Methodology bundle (preregistration ledger + multi-phase audit + Bonferroni)
is consumed via the external `phase-robust-backtesting` dep (ADR 0006).

Closed paradigms are documented as postmortem only — code retained for
reuse has been promoted to live packages (e.g. ParquetInsiderScorer →
`alphalens.screeners.insider_activity`). See ADR 0010 and
`docs/research/paradigm_failures_postmortem.md`.
"""
