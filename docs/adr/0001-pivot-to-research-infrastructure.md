# ADR 0001 — Pivot to research infrastructure

- **Status:** Accepted
- **Date:** 2026-04-25
- **Supersedes:** —

## Context

Between 2025-08 and 2026-04 the project shipped five candidate alpha paradigms:

1. Layer 2b — themed momentum (small/mid-cap)
2. Layer 2d — insider Form-4 cluster-buy
3. Layer 2e — tactical sector rotation (R12 macro overlay)
4. Layer 2f — 8-K event-driven screener
5. Layer 2g — LLM-researcher pilot (GuruScorer)

All five failed rigorous out-of-sample validation: in-sample t-stats collapsed
under bias-aware tests (FF3/Carhart, PIT universes, Bonferroni multiple-testing,
realistic execution cost). The recurring failure mode was identical — overfitting
to in-sample selection bias that did not survive OOS replay.

The user explicitly rejected pivoting to passive index investing as the exit
path; capital deployment based on AlphaLens-derived signals is off the table,
but research, periodic literature review, and Layer 1 SEC EDGAR watchdog
maintenance continue.

## Decision

AlphaLens is repositioned as **research/learning infrastructure**, not an
active-alpha generation product:

- All five failed paradigms remain in the codebase as a reusable framework and
  documented anti-pattern catalog (see ADR 0005).
- Reusable infrastructure (backtest engine, factor analysis, sanity checks,
  data clients, LLM scoring) is the asset of record.
- Layer 1 SEC EDGAR watchdog stays live in launchd as a near-zero-maintenance
  read-only event detector.
- New strategy work requires academic validation with proper OOS protocol
  before any further engineering investment.

## Consequences

- Lower cognitive load — no false sense that "the system is making money."
- + Codebase stays valuable as a learning artefact and reference implementation.
- + Future researchers (or future-self) inherit a kill-fast methodology and
  the postmortem rigor (`docs/research/paradigm_failures_postmortem.md`).
- − Some modules (`alphalens/screeners/{themed,lean,insider}`,
  `alphalens/{rotation,events,guru}`) carry "CLOSED" / "ARCHIVED" status that
  must be respected by future contributors.
- ⚠ Reactivation requires explicit trigger: new academic paper with proper
  OOS validation, regime change, broker switch acceptable, or data subscription
  budget growth.

## References

- `CLAUDE.md` — "Research lab posture (2026-04-25 →)" section
- Memory: `project_research_infrastructure_pivot.md`
- `docs/research/paradigm_failures_postmortem.md`
- ADR 0005 (closed layers retention policy)
