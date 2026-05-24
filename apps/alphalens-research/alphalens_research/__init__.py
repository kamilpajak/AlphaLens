"""AlphaLens research — lab tier for retail quant active alpha experimentation.

Most sub-packages are RESEARCH_ONLY / CLOSED. This is the workshop, not
the deployment surface.

Layered architecture per ADR 0007 (5 layers, with infrastructure in the
sibling ``alphalens_pipeline`` package):

    alphalens_pipeline.edgar_detector        — Layer 1 SEC EDGAR event monitor (ACTIVE, pipeline)
    alphalens_research.screeners       — Layer 2 selection (cross-sectional rank @ t)
    alphalens_research.gates           — Layer 2 selection-gate (RESEARCH_ONLY)
    alphalens_research.backtest        — Layer 3 strided rebalance engine (ACTIVE)
    alphalens_research.overlays        — Layer 4 time-series sizing overlays (RESEARCH_ONLY)
    alphalens_research.attribution     — Layer 5 cost / factor / metrics / verdict (ACTIVE)

Cross-cutting (also research):
    alphalens_research.diagnostics     — survivorship + slippage stress diagnostics
    alphalens_research.preaudit        — fail-fast gates before long compute
    alphalens_research.paper_trade     — forward-observation paper portfolio (INCONCLUSIVE strategies)

Cross-cutting (pipeline, consumed by this package):
    alphalens_pipeline.data            — clients + parsers + PIT store (data/store/ = as-of-t SoT)
    alphalens_pipeline.core            — queue / candidate plumbing
    alphalens_pipeline.scorers         — reusable validated scorer library
    alphalens_pipeline.literature_scanner — Perplexity periodic scan

Methodology bundle (preregistration ledger + multi-phase audit + Bonferroni)
is consumed via the external ``phase-robust-backtesting`` dep (ADR 0006).

Closed paradigms are documented as postmortem only — code retained for
reuse has been promoted to live packages or moved to ``alphalens_pipeline.scorers``.
See ADR 0010 and ``docs/research/paradigm_failures_postmortem.md``.
"""
