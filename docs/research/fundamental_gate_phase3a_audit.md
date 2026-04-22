# Fundamental-gate Phase 3A + 3B audit (issue #17)

**Date:** 2026-04-21
**Window:** 2021-04-19 → 2026-04-17 (5y)
**Data:** daily CSVs z #14 (5d-hold) i #15 (60d-hold); SPY close do regime classification (60d trailing return, ±5% thresholds).
**Scripts:** `/tmp/alphalens_issue15/phase3a_audit.py`, `/tmp/alphalens_issue15/phase3a_overlap.py`, `/tmp/alphalens_issue15/phase3b_regime.py`.

## TL;DR

**Phase 3A (audit):**
1. **Tail risk hipoteza DISCONFIRMED.** Gate ma *mniejszy* max DD (+3.7pp dla momentum, +7.8pp dla early-stage). Rozbieżność IC↑/Sharpe↓ dla early-stage nie wynika z tail risk.
2. **Multiplicative "punishment function" hipoteza CZĘŚCIOWO DISCONFIRMED.** Penalties w `fundamental_gate_score` są *additive* (sum + clip + floor 0.3), nie multiplicative. Tylko finalny `gate × technical_composite` jest multiplicative z floor 0.3 (max 70% haircut).
3. **Constant Sharpe 0.745 across horizons — WYJAŚNIONE.** Rolling 20d Sharpe stability ratio (mean/std gate ÷ mean/std baseline) = **0.41-0.44** dla momentum, 0.85-0.92 dla early-stage. Sygnał momentum gate ma zniszczony signal-to-noise → constant ~0.75 to noise floor tej strategii w tym reżimie, nie artifact.
4. **Gate functionality: POPRAWNA.** Top-5 overlap baseline vs gate = **0.55 mean Jaccard** (2.2 replacements/day). Gate systematycznie usuwa speculation (biotech pre-profit: GERN, IMVT, IDYA, SRPT; quantum/crypto/space: IONQ, MARA, CIFR, RKLB) i zastępuje profitable semis/electronics (BELFB, RMBS, COHR, PLXS, ACMR). Gate robi dokładnie to, co miał robić.

**Phase 3B (regime split): GATE WYGRYWA W BEAR REGIME dla obu scorerów.**

## Phase 3A: tail risk + rolling Sharpe

### Headline metrics

| Run | Sharpe | Sortino | Calmar | Max DD | Rolling 20d Sharpe (mean / std) |
|---|---:|---:|---:|---:|---:|
| momentum_baseline_5d  | +1.488 | +1.71 | +3.51 | −50.1% | +1.32 / 3.43 |
| momentum_gate_5d      | +0.763 | +0.82 | +1.23 | −46.5% | +0.57 / 3.38 |
| momentum_baseline_60d | +1.567 | +1.81 | +3.81 | −50.1% | +1.40 / 3.46 |
| momentum_gate_60d     | +0.763 | +0.82 | +1.20 | −46.5% | +0.56 / 3.42 |
| early_baseline_5d     | +1.135 | +1.41 | +1.40 | −68.1% | +0.71 / 3.90 |
| early_gate_5d         | +0.981 | +1.11 | +1.16 | −60.4% | +0.75 / 4.51 |
| early_baseline_60d    | +1.201 | +1.51 | +1.51 | −68.1% | +0.80 / 3.97 |
| early_gate_60d        | +1.010 | +1.14 | +1.20 | −60.4% | +0.77 / 4.53 |

### Paired ratios (gate / baseline)

| Pair | Sharpe | Sortino | Calmar | Δ max_dd | Δ rolling20 mean | Rolling20 stability ratio |
|---|---:|---:|---:|---:|---:|---:|
| momentum_5d   | 0.51 | 0.48 | 0.35 | +0.037 | **−0.75** | **0.44** |
| momentum_60d  | 0.49 | 0.45 | 0.31 | +0.037 | **−0.83** | **0.41** |
| early_5d      | 0.86 | 0.79 | 0.83 | +0.078 | +0.05 | 0.92 |
| early_60d     | 0.84 | 0.76 | 0.80 | +0.078 | −0.02 | 0.85 |

**Interpretacja:**
- **Tail risk DISCONFIRMED.** Max DD dla gate jest *mniejszy* (less negative) we wszystkich 4 parach. Gate NIE usuwa prawych outliers zostawiając lewe — usuwa i prawe i lewe, zmniejszając ogólny range.
- **Signal-to-noise collapse dla momentum gate.** Stability ratio 0.41-0.44 mówi: momentum gate's rolling mean/std spada o ~57% vs baseline. To jest signal decay, nie tail issue.
- **Early-stage gate preserves stability** (0.85-0.92) ale baseline Sharpe niższy, więc gap absolutny mniejszy.

## Phase 3A: top-5 overlap analysis

### Jaccard overlap

| Pair | Mean Jaccard | Median | % dni identical top-5 | Mean replacements/day |
|---|---:|---:|---:|---:|
| momentum_5d   | 0.557 | 0.43 | 21% | 2.21 |
| momentum_60d  | 0.548 | 0.43 | 20% | 2.26 |
| early_5d      | 0.508 | 0.43 | 9%  | 2.46 |
| early_60d     | 0.509 | 0.43 | 9%  | 2.46 |

Gate różni się od baseline na ~44% pozycji dziennie średnio — to aktywna re-selection, nie marginalny tweak.

### Który speculation gate usuwa, co dostaje w zamian?

**Top names usuwane przez gate (momentum 60d, baseline top-5 \ gate top-5):**

| Ticker | Dni usunięte | % | Kategoria |
|---|---:|---:|---|
| GERN  | 93  | 9.9% | biotech pre-profit (telomerase therapies) |
| IDYA  | 77  | 8.2% | biotech pre-profit (cancer) |
| IMVT  | 77  | 8.2% | biotech pre-profit (autoimmune) |
| SRPT  | 76  | 8.1% | gene therapy, high P/S |
| INSM  | 73  | 7.7% | respiratory biotech |
| CRNX  | 69  | 7.3% | endocrinology biotech |
| MDGL  | 67  | 7.1% | biotech pre-profit (NASH) |
| CIFR  | 65  | 6.9% | crypto miner |
| IONQ  | 65  | 6.9% | quantum computing pre-revenue |
| RKLB  | 61  | 6.5% | space, thin margins |

**Top names dodawane przez gate:**

| Ticker | Dni dodane | Kategoria |
|---|---:|---|
| ALKS  | 69 | profitable biotech (Vivitrol) |
| BELFB | 56 | profitable electronics |
| RMBS  | 48 | profitable semis |
| COHR  | 46 | profitable photonics |
| PLXS  | 45 | profitable electronics manufacturing |
| ACMR  | 38 | profitable semis equipment |
| AUPH  | 38 | biotech przy Lupkynis launch |
| INOD  | 36 | profitable AI data services |
| IONS  | 36 | profitable biotech (antisense) |
| NSIT  | 33 | profitable IT services |

**Wniosek:** gate robi dokładnie to, co miał robić — identyfikuje pre-profit speculation i zastępuje profitable mid-caps. W window 2021-2026, który był dominated przez speculation (pandemic biotech, quantum hype, crypto boom), speculation driverem zwrotów. Gate's correct identification of "risk" removed the return generators. **To jest regime-conditional signal, nie broken signal.**

## Phase 3B: per-regime Sharpe (KLUCZOWE)

### Regime distribution w oknie

| Regime | Dni 5d-hold | Dni 60d-hold | % |
|---|---:|---:|---:|
| bull | 397 | 397 | 40% |
| flat | 464 | 415 | ~45% |
| bear | 138 | 132 | ~14% |

### Per-regime Sharpe (pełne dane)

| Run | Total | Bull | Flat | Bear |
|---|---:|---:|---:|---:|
| momentum_baseline_5d   | +1.49 | +2.18 | +1.36 | −0.25 |
| momentum_gate_5d       | +0.76 | +0.53 | +1.19 | **−0.00** |
| momentum_baseline_60d  | +1.57 | +2.18 | +1.41 | +0.04 |
| momentum_gate_60d      | +0.76 | +0.53 | +1.10 | **+0.43** |
| early_baseline_5d      | +1.14 | +1.02 | +0.44 | +2.85 |
| early_gate_5d          | +0.98 | +0.92 | +0.06 | **+3.03** |
| early_baseline_60d     | +1.20 | +1.02 | +0.48 | +3.00 |
| early_gate_60d         | +1.01 | +0.92 | −0.07 | **+3.25** |

### Gate/baseline Sharpe ratio by regime

| Pair | Bull | Flat | Bear |
|---|---:|---:|---:|
| momentum_5d   | 0.24 | 0.88 | 0.006 |
| momentum_60d  | 0.24 | 0.78 | **11.76×** |
| early_5d      | 0.90 | 0.13 | **1.06×** |
| early_60d     | 0.90 | −0.15 | **1.08×** |

### Annual return in bear regime

| Run | Bear ann return | Bear Sharpe |
|---|---:|---:|
| momentum_baseline_60d | **−17.1%** | +0.04 |
| momentum_gate_60d     | **+6.1%**  | +0.43 |
| early_baseline_60d    | +81.3% | +3.00 |
| early_gate_60d        | **+88.3%** | +3.25 |

**Interpretacja:**

1. **Bull regime: gate NISZCZY momentum edge** (Sharpe 2.18 → 0.53, ratio 0.24). Gate removes the high-beta speculation that drives bull returns.
2. **Flat regime: gate miesiany.** Momentum trochę gorzej (0.78 ratio), early-stage flipped sign.
3. **Bear regime: gate WYGRYWA w obu scorerach.**
   - Momentum: baseline Sharpe +0.04, gate Sharpe +0.43 — **ratio 11.76×**. Flip z −17% annual na +6% annual.
   - Early-stage: baseline Sharpe +3.00, gate Sharpe +3.25 — ratio 1.08× (8% lepiej). Oba scorery kwitną w bear dzięki defensive profitability filter.

**Acceptance criteria z issue #17:**
> Phase 3B PASS: gate Sharpe w bear regime ≥ baseline Sharpe w tym samym subset → regime-dependent edge confirmed.

**✅ PASS dla obu scorerów.**

Uwaga: sample size 132 dni bear = ~6 miesięcy. t-stat różnicy Sharpe'ów nie obliczony (limited sample), ale kierunek i magnitude ratio (zwłaszcza momentum 11.76×) są praktycznie znaczące. Należy potwierdzić w out-of-sample bear (następny bear event).

## Decyzja

**Phase 3A: DISCONFIRM "family dead".** Gate jest funkcjonalnie poprawny (systematic speculation → quality replacement). Tail risk hipoteza obalona. Sharpe collapse w momencie jest signal-to-noise collapse, ale to jest *regime-conditional*.

**Phase 3B: PASS.** Gate wygrywa w bear regime w obu scorerach. To oznacza że fundamental-gate **nie jest dead, jest regime-conditional**.

**Następne kroki (issue #17 Phase 3D):**

1. **Regime-conditional gate deploy.** Aktywuj gate tylko gdy market regime ≠ bull (VIX threshold, 60d trailing SPY return < +5%, lub equivalent). Pełny backtest tego wariantu.
2. **Walk-forward validation.** Bear sample size (132 dni) jest mały. Walk-forward z 2-letnim window train / 1-letnim test może pokazać czy bear edge survives across different bear episodes (2018, 2020, 2022, 2024 Q1).
3. **Bear alpha factor analysis.** Czy gate's bear edge jest FF3/Carhart-explicable (defensive beta), czy true alpha? Rerun factor regression na bear subset only.

**Non-action:**
- Phase 3C (additive gate spec) — w implementacji penalties JUŻ są additive. Finalny gate × technical multiplication ma sens architektonicznie (score multiplier design). Nie priorytet.
- Contrarian test (long rejected names) — niepotrzebne, 3B jasno pokazuje że gate ma positive signal w bear, nie zero/inverted.

**Zaktualizuj memory + zostaw #17 otwartą z Phase 3D jako ostatnią fazą.**

## Artifacts

- Tail risk + rolling Sharpe: `/tmp/alphalens_issue15/phase3a_audit.py`
- Top-5 overlap: `/tmp/alphalens_issue15/phase3a_overlap.py`
- Per-regime Sharpe: `/tmp/alphalens_issue15/phase3b_regime.py`
- Report: this file
