"""AlphaLens — research/learning infrastructure for retail quant active alpha experimentation.

Layered architecture per ADR 0007 (5 layers):
    alphalens_research.watchdog        — Layer 1 SEC EDGAR event monitor (ACTIVE)
    alphalens_research.screeners       — Layer 2 selection (cross-sectional rank @ t)
    alphalens_research.gates           — Layer 2 selection-gate (RESEARCH_ONLY)
    alphalens_research.backtest        — Layer 3 strided rebalance engine
    alphalens_research.overlays        — Layer 4 time-series sizing overlays (RESEARCH_ONLY)
    alphalens_research.attribution     — Layer 5 cost / factor / metrics / verdict (ACTIVE)

Cross-cutting:
    alphalens_research.core            — plumbing (queue, candidates, runner, registry, config)
    alphalens_research.data            — clients + parsers + PIT store (data/store/ = as-of-t SoT)
    alphalens_research.literature_review — Perplexity periodic scan

Methodology bundle (preregistration ledger + multi-phase audit + Bonferroni)
is consumed via the external `phase-robust-backtesting` dep (ADR 0006).

Closed paradigms are documented as postmortem only — code retained for
reuse has been promoted to live packages (e.g. ParquetInsiderScorer →
`alphalens_research.screeners.insider_activity`). See ADR 0010 and
`docs/research/paradigm_failures_postmortem.md`.
"""
