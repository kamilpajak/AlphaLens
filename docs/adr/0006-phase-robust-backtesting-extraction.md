# ADR 0006 — Phase-robust-backtesting extracted as standalone OSS toolkit

- **Status:** Accepted
- **Date:** 2026-04-29
- **Supersedes:** —
- **Issue:** [#39](https://github.com/kamilpajak/AlphaLens/issues/39) Phase 1

## Context

Three modules built during the AlphaLens methodology audit had standalone value
beyond AlphaLens itself:

- `alphalens/preregistration/` — frozen-hypothesis ledger with class-conditional
  Bonferroni thresholds (Harvey-Liu-Zhu 2016 framework, enforced by code rather
  than by footnote).
- `alphalens/backtest/multi_phase.py` — multi-phase audit aggregator that
  collapses phase-aliasing in strided rebalance backtests; `robust_verdict`
  returns PASS/MID/FAIL using the full phase distribution.
- `alphalens/backtest/multiple_testing.py` — Bonferroni critical |t| + BH-FDR
  helpers.
- `scripts/audit_multi_phase.py` — subprocess driver that loops a backtest
  experiment script over every phase.

The bundle is genuinely self-contained (verified pre-flight: zero imports from
other AlphaLens modules outside the bundle). After 5/5 paradigm failures, this
infrastructure represents the most reusable artifact of the AlphaLens pivot to
research infrastructure (per ADR 0001).

## Decision

Extract the bundle to a public repository under the same owner:
[`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting),
MIT-licensed, pip-installable, with its own minimal CI (ruff + unittest discover).

### Mechanics

- `git filter-repo --path …` produced a history-preserving subset clone for the
  bundle paths.
- Restructured to a flat layout: `phase_robust_backtesting/{ledger.py,
  multi_phase.py, multiple_testing.py, audit_multi_phase.py, __init__.py}`.
  Subpackage `preregistration/` was dropped as it was an AlphaLens-internal
  layering convention; the OSS layout is intentionally smaller.
- Generalized the audit driver: dropped the AlphaLens-specific `_SCRIPTS`
  enum, accept `--script PATH` so any phase-offset-aware experiment can be
  audited.
- Replaced the AlphaLens `__status__` literal with a library docstring and
  `__version__ = "0.1.0"`. The status convention is project-local and not
  meaningful outside AlphaLens.
- Anti-pattern catalog (`docs/anti_patterns.md` in the new repo) summarises
  five mechanisms (phase-aliasing, single-phase point-estimate verdict,
  multiple-testing inflation, IS→OOS regime overfit, liquidity illusion) with
  concrete numbers from the AlphaLens postmortem.

### Vendoring policy (back into AlphaLens)

If future fixes flow back, use `git subtree pull` (same convention as
TradingAgents per [ADR 0004](0004-tradingagents-as-subtree.md)):

```bash
git subtree pull --prefix=alphalens/preregistration \
  https://github.com/kamilpajak/phase-robust-backtesting.git main --squash
```

Note: AlphaLens currently keeps its own copies of the bundle (because the
extraction happened from AlphaLens, not vice versa). If the OSS toolkit
diverges in a way AlphaLens wants to inherit, pull the subtree at that point;
otherwise the local copies stay authoritative for AlphaLens-specific use.

## Consequences

- **Positive.** The methodology infrastructure has a clean public face,
  reusable independent of AlphaLens's strategy outcomes. Anti-pattern catalog
  is citable for anyone hitting the same retail-quant traps. Closes #39
  Phase 1.
- **Positive.** The OSS repo's CI is minimal (no SonarCloud / bandit /
  TradingAgents subtree), so it's far cheaper to maintain than AlphaLens.
- **Negative (mild).** Two copies of the same code now exist. AlphaLens's
  `alphalens/preregistration/` and `alphalens/backtest/{multi_phase,
  multiple_testing}.py` are NOT replaced by a `pip install` dependency; we
  keep the in-repo copies because (a) AlphaLens is solo-research-only with
  no need to chase upstream OSS releases, (b) the extraction is one-way for
  Phase 1.
- **Negative (mild).** Future commits that touch both repos require two PRs.
  Acceptable cost for clean OSS surface.

## Alternatives considered

- **Replace in-repo copies with `pip install phase-robust-backtesting` as a
  hard dependency.** Rejected for now — adds a release cadence + version-pin
  burden disproportionate to the gain.
- **Keep the toolkit in AlphaLens, link OSS users at the AlphaLens repo.**
  Rejected — AlphaLens has too much paradigm-failure-specific surface to be
  a useful OSS entry point for a methodology toolkit.
- **Apache 2.0 license** (per Perplexity recommendation in #39 strategic
  consult). Rejected — MIT matches AlphaLens's existing license and is
  simpler. No patent considerations apply to a small validation library.

## References

- Issue: [#39](https://github.com/kamilpajak/AlphaLens/issues/39)
- New repo: <https://github.com/kamilpajak/phase-robust-backtesting>
- Anti-pattern catalog: <https://github.com/kamilpajak/phase-robust-backtesting/blob/main/docs/anti_patterns.md>
- Subtree precedent: [ADR 0004](0004-tradingagents-as-subtree.md)
- Closed-layer policy: [ADR 0005](0005-closed-layers-as-anti-pattern-catalog.md)
