# ADR 0007 — Layer architecture for AlphaLens

- **Status:** Accepted
- **Date:** 2026-04-30
- **Supersedes:** —

## Context

Through nine phase-robust paradigm failures (Layer 2b/2c/2d/2e/2f/2g + tri-factor + mom+lowvol_combo + regime-gate rescue + quality+momentum), every "strategy" mixed three concerns into a single hypothesis and a single backtest:

1. **Selection** — which tickers to hold (the screener / scorer)
2. **Sizing** — how much exposure to carry on those holdings
3. **Cost / attribution** — what frictions and factor regressions apply to the realised returns

Mixing these concerns at the script level made failure attribution hard:
- Layer 2e rotation failed because R² ≈ 1.0 vs passive — a *sizing* property, but the experiment was framed as a selection failure.
- Mom+lowvol's 2017-2022 hole was structural mega-cap drift — a *universe* property; we correctly identified that adding a regime gate (a *selection-filter* layer) couldn't rescue it, but only after Phase 1 of the regime-gate experiment was already running.
- Quality+momentum's enormous OOS dispersion (167.8pp) was a *phase-aliasing* artifact in the backtest grid, not a property of the screener itself.

Without explicit layer boundaries, every new hypothesis tends to re-mix all three concerns. The user explicitly flagged the risk of getting lost ("user pogubi sie w warstwach") when introducing the next concern (vol-targeting).

The repo has already converged on layer-shaped abstractions in code:
- `alphalens/screeners/*`, `alphalens/rotation/`, etc. — selection (Layer 2*)
- `alphalens/regime_gate/` — selection-gating (RESEARCH_ONLY 2026-04-29)
- `alphalens/backtest/engine.py` — execution & return-series
- `alphalens/backtest/cost_model.py`, `factor_analysis.py` — attribution
- `alphalens/risk_overlay/` (NEW 2026-04-30) — time-series sizing

This ADR makes the implicit layering explicit.

## Decision

Adopt a **5-layer architecture** for AlphaLens active-alpha experimentation. Each layer has a single responsibility, a defined input/output contract, and its own `__status__` declaration. New hypotheses compose layers; failures attribute to the responsible layer.

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. SCREENER (Layer 2*)         alphalens/screeners/*, rotation/ │
│    cross-sectional rank @ time t                                │
│    in:  histories of tickers @ t                                │
│    out: top-N tickers + scores                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │  Scorer = Callable[[Mapping[str,DF], Mapping], DF]
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│ 2. SELECTION-GATE              alphalens/regime_gate/           │
│    binary/graded gating @ time t                                │
│    in:  Scorer + RegimeClassifier (`is_on(asof)` or `score`)    │
│    out: wrapped Scorer (same signature; selection ON/OFF or     │
│        weighted by classifier value)                            │
└────────────────────────────┬────────────────────────────────────┘
                             │  wrapped Scorer
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│ 3. BACKTEST ENGINE             alphalens/backtest/engine.py     │
│    runs scorer over a strided rebalance calendar                │
│    in:  Scorer + universe + dates + holding_period + stride     │
│    out: BacktestReport (per-rebalance snapshots + portfolio_    │
│         returns timeseries)                                     │
└────────────────────────────┬────────────────────────────────────┘
                             │  pd.Series of portfolio returns
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│ 4. RISK OVERLAY                alphalens/risk_overlay/          │
│    time-series sizing on portfolio returns                      │
│    in:  portfolio-returns Series + sizing rule (e.g. vol-target)│
│    out: scaled Series (same index, scaled values; scale[t]      │
│        depends only on returns[<t])                             │
└────────────────────────────┬────────────────────────────────────┘
                             │  scaled portfolio returns
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│ 5. ATTRIBUTION                 alphalens/backtest/{cost_model,  │
│    cost-drag, Carhart-4F, FF5+UMD,             factor_analysis, │
│    Bonferroni, Sharpe, excess return                  metrics}  │
│    feeds the pre-registration ledger verdict                    │
└─────────────────────────────────────────────────────────────────┘
```

### Per-layer responsibility

| Layer | Operates on | Modifies | Causality contract |
|---|---|---|---|
| 1. Screener | cross-section @ t | which tickers + scores | reads PIT histories; no look-ahead by construction (engine truncates) |
| 2. Selection-gate | regime classifier @ t | whether selection deploys (and how strongly) | classifier reads only its own inputs as-of t |
| 3. Backtest engine | strided rebalance calendar | per-rebalance returns + IC + turnover | engine truncates histories before calling scorer |
| 4. Risk overlay | portfolio realised vol over [t-L, t-1] | gross exposure size | scale[t] uses returns[<t] strictly (enforced by tests) |
| 5. Attribution | scaled return series | risk-adjusted metrics + α t-stats + verdict | OLS regressions; primary metric for time-varying-beta strategies is Sharpe-improvement, not α t-stat (see "Limitation" below) |

### Composition rules

- **Layer 1 alone is a hypothesis** (e.g. mom+lowvol_combo). Pre-registered as a single-screener test.
- **Layer 1 + Layer 2** is a compound hypothesis (e.g. mom+lowvol gated by VIX>20). Pre-registered with both components frozen.
- **Layer 1 + Layer 4** is a different compound hypothesis (e.g. mom+lowvol with vol-target overlay). Pre-registered with both components frozen.
- **Layers 1+2+4 combined** would be a 3-way compound hypothesis. Possible but pays a higher Bonferroni cost.
- **Layer 3 is engine** — scorer-agnostic. Never tested in isolation; carries the calendar/stride/phase-offset semantics.
- **Layer 5 is verdict** — never tested in isolation; consumes everything upstream.

## Consequences

**Positive:**
- Failure attribution is sharper: a strategy that fails because of phase-aliasing fails at Layer 3 (engine grid), not Layer 1. A strategy that fails because the regime gate doesn't cover the failure window fails at Layer 2, not Layer 1. This is exactly how the regime-gate Phase 1 diagnostic was framed (2026-04-29) — a Layer 2 falsification independent of Layer 1.
- New layers compose explicitly. The `risk_overlay/` introduction in this ADR is a Layer 4 addition; it does not require touching any existing screener.
- Pre-registration ledger maps cleanly: each compound hypothesis names its layer choices; Bonferroni scope is limited to hypotheses sharing the same layer combination + signal class.
- `__status__` declarations on layer packages give cold readers a one-line lifecycle map.

**Negative:**
- More files / abstractions to navigate. Mitigated by the canonical diagram above (mirrored in `CLAUDE.md` and `README.md`).
- The boundary between Layer 2 (selection-gate) and Layer 4 (risk-overlay) is sometimes fuzzy. Rule of thumb: if it modifies *which* tickers are held, Layer 2; if it modifies *how much exposure* the basket carries, Layer 4. Edge case (binary "deploy / don't deploy" overlays) lives at Layer 2 by construction (the wrapper deletes the selection on OFF days).

**Limitation — time-varying betas:**
Risk overlays make the resulting portfolio's market beta time-varying. The OLS Carhart-4F regressions in `alphalens.attribution.factor_analysis` assume constant betas. For overlay-bearing strategies the reported α t-stat is therefore distorted. The convention (locked into the strategy validation playbook 2026-04-30):

- **Primary success metric for overlay-bearing strategies:** Sharpe-net-improvement vs ungated BASE, multi-phase mean (robust to time-varying beta). Implemented in `alphalens.risk_overlay.assess.compute_overlay_stats` — see the `sharpe_improvement_net` key.
- **Secondary metric:** Carhart-4F α t-stat (reported but flagged as upper-bound estimate).
- A conditional-beta regressor is on the deferred list. **Tracked as a follow-up:** revisit if and when an overlay-bearing strategy passes the Sharpe-improvement gate phase-robustly; pre-tracking it would be premature given the only overlay tested so far (`vol_target_mom_lowvol_2026_04_30`) FAIL'd on the primary gate. Until then, OLS attribution is acceptable as a coarse upper-bound.

## References

- Phase 1 diagnostic that motivated explicit layer separation: `docs/research/regime_gate_phase1_diagnostic.md` (2026-04-29).
- Postmortem of the 9 paradigm failures: `docs/research/paradigm_failures_postmortem.md` (latest title "Nine Paradigm Failures").
- Risk overlay introduction: `alphalens/risk_overlay/__init__.py` + `vol_target.py`, tests `tests/test_risk_overlay.py`. Pre-registered hypothesis: `vol_target_mom_lowvol_2026_04_30` in fresh signal class `risk_management_overlay_2026_04_30`.
- Methodology bundle (the OSS extraction of the discipline that surrounds these layers): https://github.com/kamilpajak/phase-robust-backtesting.
