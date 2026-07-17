# Architecture Decision Records

Short, dated records of the load-bearing decisions in AlphaLens — why things
are the way they are. Each ADR is self-contained; new readers (and future-self
after a break) should be able to scan this index and drill into the few that
matter.

Format: lightweight MADR — Status, Context, Decision, Consequences, References.

## Index

- [ADR 0001 — Pivot to research infrastructure](0001-pivot-to-research-infrastructure.md) — 2026-04-25 — why AlphaLens is a research lab, not an active-alpha generator
- [ADR 0002 — Candidate / Queue / Runner core abstraction](0002-candidate-queue-runner.md) — 2025-09-01 — the contract every screener must honour
- [ADR 0003 — Screener-agnostic backtest with pluggable Scorer](0003-screener-agnostic-backtest.md) — 2025-12-15 — why `BacktestEngine` takes a callable, not a class
- [ADR 0004 — TradingAgents as vendored git subtree](0004-tradingagents-as-subtree.md) — 2025-08-10 — *Superseded by ADR 0008*
- [ADR 0005 — Closed layers retained as anti-pattern catalog](0005-closed-layers-as-anti-pattern-catalog.md) — 2026-04-25 — *Superseded by ADR 0010*
- [ADR 0006 — Phase-robust-backtesting OSS extraction](0006-phase-robust-backtesting-extraction.md) — 2026-04-29 — methodology bundle (preregistration ledger + multi-phase audit + Bonferroni helpers) lives standalone at `kamilpajak/phase-robust-backtesting` (MIT)
- [ADR 0007 — Layer architecture](0007-layer-architecture.md) — 2026-04-30 — explicit 5-layer separation (screener → selection-gate → engine → risk-overlay → attribution) so failures attribute cleanly
- [ADR 0008 — Sunset TradingAgents integration](0008-sunset-tradingagents-integration.md) — 2026-04-30 — remove vendored subtree + Layer 3 LLM runner; reimplement fundamentals fetcher + guru pilot LLM client standalone
- [ADR 0009 — Django + DRF replaces FastAPI + SQLite cache](0009-django-replaces-fastapi.md) — 2026-05-22 — briefs API moves from legacy FastAPI/SQLite cache to Django/Postgres; greenfield rip-out, no parallel deploy
- [ADR 0010 — Archive extracted and removed](0010-archive-extracted-and-removed.md) — 2026-05-22 — `alphalens/archive/` replaced by surgical extraction of reusable code into live packages; supersedes ADR 0005
- [ADR 0011 — Split into pipeline + research workspace members](0011-split-pipeline-and-research.md) — 2026-05-23 — live infrastructure and the research lab become separate workspace packages with a one-way dependency DAG
- [ADR 0012 — Decommission the paper-trading + broker chain](0012-decommission-paper-trading-and-broker-chain.md) — 2026-06-03 — Alpaca/Saxo chain removed; outcomes come from broker-free price-path replay over Polygon minute bars
- [ADR 0013 — Trade-side layer architecture (live tool)](0013-trade-side-layer-architecture.md) — 2026-07-14 — 8-layer decomposition of the live tool's trade side (signal → selection → ordering → display → setup → in-flight → exit → measurement) + 5 hard rules; sibling of ADR 0007
- [ADR 0014 — Broker-agnostic execution layer (Saxo first, SIM-only)](0014-broker-agnostic-execution-layer.md) — 2026-07-17 — `alphalens_pipeline/brokers/` Broker Protocol + registry + canonical SaxoClient with a SIM-only structural rail; the successor ADR 0012 anticipated, phased P1 reads → P2 placement → P3 reconciliation → P4 OAuth → (future ADR) LIVE
