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
- [ADR 0005 — Closed layers retained as anti-pattern catalog](0005-closed-layers-as-anti-pattern-catalog.md) — 2026-04-25 — why failed paradigms are not deleted
- [ADR 0006 — Phase-robust-backtesting OSS extraction](0006-phase-robust-backtesting-extraction.md) — 2026-04-29 — methodology bundle (preregistration ledger + multi-phase audit + Bonferroni helpers) lives standalone at `kamilpajak/phase-robust-backtesting` (MIT)
- [ADR 0007 — Layer architecture](0007-layer-architecture.md) — 2026-04-30 — explicit 5-layer separation (screener → selection-gate → engine → risk-overlay → attribution) so failures attribute cleanly
- [ADR 0008 — Sunset TradingAgents integration](0008-sunset-tradingagents-integration.md) — 2026-04-30 — remove vendored subtree + Layer 3 LLM runner; reimplement fundamentals fetcher + guru pilot LLM client standalone
- [ADR 0009 — Django + DRF replaces FastAPI + SQLite cache](0009-django-replaces-fastapi.md) — 2026-05-22 — briefs API moves from legacy FastAPI/SQLite cache to Django/Postgres; greenfield rip-out, no parallel deploy
