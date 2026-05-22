# Layer 2b themed screener — final audit closeout (issue #18)

**Date:** 2026-04-22
**Outcome:** **Layer 2b themed screener CLOSED dla capital deployment.** Gross OOS alpha obecna ale realistic transaction cost zjada całość.

## Timeline audytu (2026-04-21 do 2026-04-22, ~36h wall clock)

| Phase | Finding | Verdict |
|---|---|---|
| #14 (Phase 1) | Gate 5d FAIL — momentum Sharpe −49%, α t 2.62→1.20 | Close gate |
| #15 (Phase 2) | Gate 60d FAIL — α t 2.66→1.20 | Close gate |
| #17 Phase 3A audit | Tail risk disconfirmed; gate systematically replaces speculation with profitable mid-caps | Gate functionally correct, regime-dependent |
| #17 Phase 3B regime split | Gate Sharpe 11.76× in bear regime (mirage) | Looked like PASS |
| #17 Phase 3B.1 Carhart-4F HAC | Bear subset α t=0.02-0.33 (zero) — Sharpe ratio mirage | CLOSE gate family CONFIRMED |
| #18 Perplexity R1 (2026-04-21 PM) | Flagged regime bias + multiple-testing gaps | Triggered bias audit |
| #18 SimFin look-ahead bug | Filter used Report Date (fiscal end) instead of Publish Date (~45d filing lag) | FIXED |
| #18 Post-fix re-runs | Gate α drops −5% do −13% post-fix → close-family ultra-robust | CONFIRMED |
| #18 Perplexity R2 | Multiple testing, Bonferroni, delisted classification, universe PIT | Triggered Phase 1-4 |
| #18 Phase 1A delisted classification | ≥50% augmented universe = M&A selection effect, nie survivorship | Test B finding reinterpreted |
| #18 Phase 1B Bonferroni | Only IC t>3.5 survives; portfolio α t=2.62 fails | Baseline validation DOWNGRADED |
| #18 Phase 2 OOS walk-forward | PLOT TWIST: momentum overfit, early-stage survives | Pivot to early-stage |
| #18 Phase 3 PIT | Retrospective semis added +0.3 t; bankruptcy augment flips TRAIN sign | Smoking gun (scorer picks failures) |
| #18 Perplexity R3 | Sign flip = classification failure, recommend close | CONFIRMED close |
| #18 Bootstrap CI (10k iter) | 68% CI excludes zero across all 4 configs (per R3 criterion: keep open) | Mixed signal — proceed to Phase 4 |
| #18 Phase 4 liquidity audit | Realistic transaction cost ~100% ann (vs 100bps "moderate" scenario) | **CLOSE** — alpha consumed by spread |

## Finalne liczby

### Gate family (#14, #15, #17)

| Config | Full α t | OOS α t | Bonferroni | Verdict |
|---|---:|---:|:---:|:---:|
| Gate momentum 5d | 1.20 | 0.61 | fail | DEAD |
| Gate momentum 60d | 1.20 | 0.68 | fail | DEAD |
| Gate early 5d | 1.60 | 1.98 | fail | DEAD |
| Gate early 60d | 1.60 | 2.10 | fail | DEAD |

### Baseline (#18 Phase 1-4)

| Config | Train α t | OOS α t | PIT-95 OOS | Bootstrap 68% CI | Real cost | Economic α |
|---|---:|---:|---:|---|---:|---:|
| Baseline momentum 60d | +2.60 | **+0.82** | — | — | — | OVERFIT |
| Baseline early 60d | +1.21 | **+1.70** | +1.36 | [+27%, +200%] ann | ~100% ann drag | **≈ 0%** |
| Gate early 60d (post-fix) | +0.64 | **+2.10** | — | [+45%, +173%] ann | ~100% ann drag | **≈ +10% residual** |

## Kluczowe findings

### 1. Gate family permanently dead

Fundamental-gate (P/S + runway + OCF + NI penalties) nie dodaje istotnej alphy w żadnym reżimie, horizon, scorer, z look-ahead biasem ani bez. Phase 3B Sharpe 11.76× w bear był mirage (near-zero denominator). Bonferroni correction kills all 12 gate tests.

### 2. Momentum scorer overfit

Train α t=2.60 → OOS α t=0.82. Phase 1B Bonferroni IC claim valid (ranking quality), ale portfolio α collapses OOS. Classic small-sample overfit.

### 3. Early-stage scorer: real signal, but economic dead

- OOS PIT-corrected α t ≈ 1.35 (below 1.5 threshold but likely positive per bootstrap)
- IC t OOS 60d = 2.84 (ranking signal present)
- BUT: realistic transaction cost ~100% annual drag on daily-rebalance microcap biotech universe
- Gross α +110% → net α near zero
- Bankruptcy augment TRAIN sign flip (+0.80 → −0.86) = classification failure (scorer picks biotech losers with similar features as winners)

### 4. cost_model.py nierealistyczny

Flat 100bps/year "moderate" scenario underestimuje real cost by ~100×. Real daily-rebalance microcap biotech execution = ~100% ann drag through bid-ask spread alone.

### 5. Universe survivorship bias direction reversed

Memory `project_survivorship_probe` claim "bias w odwrotnym kierunku, curated universe konserwatywny" była nieprawidłowa. ≥50% Test B augmented = M&A selection, nie survivorship correction. True PIT dropped OOS α by 0.3 t-stat (retrospective semis) i flipped train sign (bankruptcy inclusion).

### 6. Methodological lessons

- Sharpe regime-split na low-sample = mirage generator → zawsze factor regression HAC
- Report Date vs Publish Date = baseline look-ahead w każdym fundamental store
- Multiple-testing bias → Bonferroni/FDR przed promowaniem strategii
- Delisted ≠ survivors missing — classify M&A vs bankruptcy (selection vs survivorship)
- cost_model musi scalować ze spread × turnover × frequency, nie flat bps

## Decyzje

### Immediate (this session)

- [x] Close #18 z final CLOSE verdict dla Layer 2b
- [x] Memory update: `project_themed_screener_design` → CLOSED
- [x] Memory update: `project_pipeline_bias_audit` → final status
- [x] MEMORY.md → reflect closure

### Near-term (user decision)

- [ ] **Disable daily launchd plist**: `launchctl unload ~/Library/LaunchAgents/com.alphalens.watchdog.themed.plist`
  - Alternatively: keep running for paper tracking, but NO capital deploy
- [ ] Layer 1 watchdog (SEC EDGAR) pozostaje aktywny — ortogonalny do Layer 2b
- [ ] Layer 3 paper trade 30d check-in (2026-05-21) — memory TODO nadal stoi

### Pivot direction (future session)

Option A: **Alt data screener** (insider transactions, short interest) — perplexity R3 ranking +20%:
- Higher signal-to-noise w biotech (Kelley & Tetlock 2017: insider buys +180bps/6mo w small caps)
- Lower multiple-testing risk (single data source, specific hypothesis)
- Polygon lacks these feeds; Finnhub insider API free tier exists

Option B: **Layer 3 rejection-prediction classifier** — memory hint:
- 6 miesięcy historical Layer 3 decisions (BUY/HOLD/SELL) jako panel data
- Train classifier na features znane przed decision
- Zamiast hand-crafted scorer, use Layer 3 as gold label
- Solo-dev feasible (~1-2 weeks implementation)

Option C: **Completely new paradigm** — drop thematic curated universe, pivot:
- Sector/region-rotation macro strategy
- Options-based volatility harvest
- Different asset class (crypto/commodities/FX)

**Recommend Option B** (Layer 3 classifier) — reuses existing Layer 3 infrastructure, avoids data costs, directly optimizes for what we actually want (Layer 3 approvals).

## Artifacts — full trail

- Research docs: `docs/research/fundamental_gate_*.md`, `pit_universe_backtest.md`, `multiple_testing_audit_2026-04.md`, `delisted_classification.md`, `walk_forward_oos_validation.md`, this file
- Backtest CSVs: `docs/backtest/postfix_gate_*`, `pit95_*`, `compare_*`, `baseline_*_hold60`
- Scripts: `/tmp/alphalens_issue15/phase*.py`, `/tmp/alphalens_issue18/phase*.py`
- Code fix: `alphalens/data/store/simfin.py::features_as_of` (Publish Date filter)
- Issue threads: #14, #15, #17, #18 all closed

## Post-mortem sentence

**Layer 2b zjadło 3 tygodnie solo-dev pracy i 3 rundy perplexity review aby odkryć że curated 113-ticker daily-rebalance microcap biotech strategia nie generuje ekonomicznie istotnego alpha post-execution — a każda faza walidacji odkrywała nowy bias niewidoczny w poprzednich raportach. Nauka dla następnej strategii: zacznij od execution realism + multiple testing discipline, nie na końcu.**
