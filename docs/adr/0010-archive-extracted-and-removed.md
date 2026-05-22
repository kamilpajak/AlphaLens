# ADR 0010 — Archive extracted and removed

- **Status:** Accepted
- **Date:** 2026-05-22
- **Supersedes:** [ADR 0005](0005-closed-layers-as-anti-pattern-catalog.md)

## Context

ADR 0005 kept closed paradigms under `alphalens/archive/*` on the premise that
reusable infrastructure was interleaved with paradigm-specific code, and that
keeping the trail in-tree was zero-friction for postmortem analysis. Six months
of subsequent work clarified two things:

1. Only a small fraction of `archive/` is actually reused. The reuse pattern is
   one or two files (e.g. `ParquetInsiderScorer`, the `Rule` dataclass) — not
   entire packages.
2. The remaining bulk (themed, lean, rotation, insider, guru, quiver_screener,
   events) carries weight that no current paradigm reads. Tests for these
   packages account for ~50 files; CLI replay commands account for another 5.
   None of it is referenced by live infrastructure (launchd, systemd, Docker
   pipeline). Postmortem narrative already lives in
   `docs/research/paradigm_failures_postmortem.md` and the ADR set.

## Decision

Surgical extraction of reusable code, then removal of the rest:

1. **Promoted to live packages:**
   - `alphalens/archive/screeners/insider/parquet_scorer.py`
     → `alphalens/screeners/insider_activity/parquet_scorer.py`
   - `Rule` dataclass from `alphalens/archive/rotation/config.py`
     → inlined into `alphalens/data/macro/scorer.py`

2. **Removed:**
   - `alphalens/archive/` (entire tree)
   - `launchd/archived/` (plists + bin scripts, lifecycle record sufficed via
     ADR 0008)
   - CLI replay commands: `archive`, `rotation`, `themed`, `insider`,
     `research`, `backtest` (last one had no live scorer branch)
   - Pure-archive test suites (~52 files in `tests/test_{rotation,lean,themed,
     guru,insider,quiver,events,early_stage,momentum,macro,registry}*.py`)
   - 27 dead experiment scripts that depended on archive packages
   - `picks_from_history_store()` in `alphalens/backtest/historical_validation.py`
     (zero external callers, all archive imports)

3. **Updated:**
   - `alphalens/core/registry.py` — `SCREENERS` reduced to live `prescreener`
     entry; `SOURCE_PRIORITY` trimmed accordingly
   - `alphalens/preaudit/profiles.py` — removed exempted paradigm names that
     no longer have backing scripts
   - `alphalens_cli/commands/audit.py:_SCRIPTS` — 7 entries removed where the
     pointed-to script was deleted

Closed paradigms remain documented in `docs/research/paradigm_failures_postmortem.md`
and the ADR set. The source code itself is retrievable via git history.

## Consequences

**Positive**
- Repo carries only code that is depended on by some live execution path.
- Test runs no longer collect dead packages.
- Lint / type-check scope shrinks accordingly.
- Naming is honest: there is no longer a directory called `archive/` that the
  live screener silently depends on.

**Negative**
- Postmortem grep is no longer zero-friction: searching e.g. "why did Lean
  fail" now requires `git log -p -- alphalens/archive/` instead of a direct
  ripgrep over a present directory.
- If a future paradigm wants to re-use a third archive class beyond the two
  already promoted, the move happens via `git show` + apply rather than just
  rewriting an import path.

The trade-off favours operational clarity over postmortem ergonomics, which
matches the project's current phase (live thematic pipeline + paradigm #14
PEAD backfill) better than ADR 0005's anti-pattern-catalog framing did.

## References

- [`docs/research/paradigm_failures_postmortem.md`](../research/paradigm_failures_postmortem.md)
- [ADR 0001](0001-pivot-to-research-infrastructure.md) — research-infrastructure pivot
- [ADR 0005](0005-closed-layers-as-anti-pattern-catalog.md) — superseded
- [ADR 0008](0008-sunset-tradingagents-integration.md) — TradingAgents sunset (which had its own `launchd/archived/` reference now obsolete)
