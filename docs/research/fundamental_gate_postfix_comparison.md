# Fundamental-gate post-PIT-fix comparison (issue #18)

**Date:** 2026-04-22
**Window:** 2021-04-19 → 2026-04-17 (5y, 971 / 944 trading days)
**Fix:** `alphalens/fundamentals/simfin_store.py::features_as_of` now filters by `Publish Date` (actual 10-Q/10-K filing) instead of Report Date (fiscal quarter end). Eliminates ~40-60 day look-ahead bias.

## Headline: pre-fix vs post-fix

### Momentum 60d (flagship comparison)

| Metric | Pre-fix (Report Date) | Post-fix (Publish Date) | Δ |
|---|---:|---:|---:|
| Sharpe (gross) | +0.763 | **+0.668** | **−12.5%** |
| Sharpe (net 100bps) | +0.745 | +0.651 | −12.6% |
| FF3 α (ann.) | +35.27% | +29.97% | −15.0% |
| FF3 α t-stat (HAC) | +1.20 | **+1.03** | **−0.17** |
| Carhart-4F α t-stat | +1.23 | +1.04 | −0.19 |
| Mean Rank IC | +0.0154 | +0.0192 | +0.004 |
| IC t-stat | +2.56 | +3.24 | +0.68 |
| Turnover | 35.1% | 35.0% | ~unchanged |
| Max DD | −46.45% | −50.34% | −3.9pp worse |

### Momentum 5d

| Metric | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| Sharpe (gross) | +0.763 | **+0.676** | **−11.4%** |
| FF3 α t | +1.20 | **+1.01** | −0.19 |
| Carhart α t | +1.21 | +1.02 | −0.19 |
| IC t | +5.19 | +4.82 | −0.37 |

### Early-stage 60d

| Metric | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| Sharpe (gross) | +1.010 | **+0.959** | **−5.0%** |
| FF3 α t | +1.60 | **+1.53** | −0.07 |
| Carhart α t | +1.59 | +1.52 | −0.07 |
| IC t | +2.24 | +3.13 | +0.89 |

### Early-stage 5d

| Metric | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| Sharpe (gross) | +0.981 | **+0.932** | **−5.0%** |
| FF3 α t | +1.60 | **+1.54** | −0.06 |
| Carhart α t | +1.61 | +1.54 | −0.07 |
| IC t | +3.76 | +3.41 | −0.35 |

## Obserwacje

### 1. Wszystkie 4 runy pogorszyły się post-fix — look-ahead bias confirmed active

Pre-fix gate miał pomoc w postaci "widzenia" 10-Q danych ~40-60 dni przed ich publikacją. Po fix'ie:
- Sharpe spada wszędzie (−5% do −12.5%)
- FF3 α t spada wszędzie (−0.06 do −0.19)
- Max DD gorsze (gate nie może unikać firms które "wiedziały o nadchodzących złych earnings")

### 2. Momentum gate hit 2× bardziej niż early-stage gate

- Momentum: Sharpe −11 do −12% drop
- Early-stage: Sharpe −5% drop

**Dlaczego:** Momentum scorer na mega-cap names polega bardziej na fundamental-gate'u dla differentiation (pre-profit biotech vs profitable semis). Early-stage już jest "quality-aware" przez rev_growth/margin components, więc gate dodaje mniej. Large-cap momentum gate był najbardziej zależny od fundamental lookahead.

### 3. Close-family verdict z #17 STRENGTHENED

Pre-fix momentum 60d gate α t = 1.20; post-fix = 1.03. Obie poniżej 1.5 "significance" threshold, ale post-fix blizej zera. Gate nie dodaje alphy w żadnym scenariuszu nawet z look-ahead advantage — bez niego jest jeszcze gorzej.

### 4. Ciekawa anomalia: IC rośnie przy spadającej Sharpe (momentum 60d)

- IC t-stat: pre 2.56 → post 3.24 (+0.68)
- Sharpe: −12.5%
- α t: −14%

**Interpretacja:** IC mierzy cross-sectional ranking quality. Post-fix ranking tickers jest LEPSZE (gate nie dostaje wrong signals od pre-publication fundamentals). Ale top-5 portfolio Sharpe/α to gorsze. Mechanizm: IC correct across whole universe, ale concentrated top-5 selection traci top-tail winners bo gate widzi "bad fundamentals" które rynek jeszcze nie dostrzega.

Czyli: **look-ahead dawał gate illusion of signal pickup ahead of market**. Po fix'ie gate jest statistically sharper (IC better) ale portfolio effect jest slabszy bo gate nie handlować "ahead of the news" — tylko equal footing z market.

### 5. Early-stage gate na granicy istotności

Post-fix early-stage gate α t = 1.53-1.54 (oba horyzonty). To dokładnie threshold 1.5. Pre-fix 1.60 było marginally above; post-fix jest na pointcie. Statistically: gate early-stage "działa w teorii" ale borderline. Not significant przy strictest Bonferroni correction.

## Co to znaczy dla Layer 2b

### Baseline runs NIE DOTKNIĘTE

Baseline momentum + early-stage nie używają SimFin (brak `--fundamental-gate` flagi → scorer_config bez `fundamental_gate_enabled=True` → scorer gets `{}` fundamentals → `fundamental_gate_score` returns 1.0). Więc:
- MVP1 α t=2.62 HAC z `compare_momentum_2026-04-21.md` — **unaffected przez fix**
- Layer 2b live deployment wiki memory — **unaffected** (live uses MomentumScorer bez fundamentals)

### Gated code path fixed; baseline is untouched

Cała waga bias audit'u spada na gated backtests (#14, #15, #17, teraz #18). Baseline "validated alpha" status nie wymaga re-validation z tego powodu. Survivorship bias (inny problem) nadal wymaga osobnej reconstrukcji.

## Acceptance criteria z issue #18

- [x] Re-backtest gated momentum + early-stage post-fix — DONE
- [x] Compare pre-fix vs post-fix magnitudes — DONE (Sharpe −5 do −12.5%)
- [ ] Walk-forward chronological split (2021-04 → 2024-12 train, 2025-01 → 2026-04 test) — osobna faza
- [ ] PIT universe reconstruction z SimFin historical ticker lists — osobna faza (survivorship bias)

## Artifacts

- 4 post-fix reports: `docs/backtest/postfix_gate_{momentum,early_stage}_{5d,60d}.md`
- Ten raport: `docs/research/fundamental_gate_postfix_comparison.md`
- Code fix: `alphalens/fundamentals/simfin_store.py::features_as_of` (commit TBD)
