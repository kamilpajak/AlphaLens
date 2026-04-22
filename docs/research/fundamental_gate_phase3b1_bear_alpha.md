# Fundamental-gate Phase 3B.1 — bear-subset factor regression (issue #17)

**Date:** 2026-04-21
**Script:** `/tmp/alphalens_issue15/phase3b1_bear_alpha.py`, `/tmp/alphalens_issue15/phase3b2_diff_alpha.py`
**Setup:** Carhart-4F (Mkt-RF, SMB, HML, Mom) OLS with Newey-West HAC t-stats. Bear regime = 60d trailing SPY return ≤ −5% (per `alphalens.backtest.regime`). Bear sample: **132 days** z 944 total (60d-hold window).

## TL;DR — Phase 3B wyniki UNIEWAŻNIONE

**Phase 3B (per-regime Sharpe) PASS był statystycznym mirażem.** Przy formalnym teście (Carhart-4F HAC regression na bear-only subset oraz differenced regression gate − baseline), gate's bear-regime "edge" **nie jest statystycznie istotny**.

**Decyzja: Phase 3D NIE UZASADNIONA. Fundamental-gate family permanent close.**

## Dlaczego Sharpe ratio 11.76× był mirage'm

Phase 3B pokazał momentum gate bear Sharpe 0.43 vs baseline 0.04 — ratio 11.76×. Wyglądało imponująco, ale:

- **Mianownik near-zero** — baseline bear Sharpe +0.04 to statystycznie zero. Dzielenie cokolwiek / zero → arbitrarily large ratio.
- **Small sample** — 132 dni bear na 944 dni total (14% okna) z 5-letniego window. Single bear episode (2022 + 2024 Q1-Q2), nie generalizujące.
- **Sharpe nie filtruje factor exposure** — defensive low-beta stocks będą mieć positive Sharpe w bear nawet bez alphy, bo Mkt-RF jest negative.

Carhart-4F regression z HAC t-stats filtruje te wszystkie pułapki.

## Bear-subset Carhart-4F alpha (bezwarunkowo)

| Run | α (bear) ann | α t-stat (HAC) | n | Istotna? (próg 1.5) |
|---|---:|---:|---:|:---:|
| momentum_baseline_60d | +1.50% | **+0.02** | 132 | ❌ |
| momentum_gate_60d     | +26.26% | **+0.33** | 132 | ❌ |
| early_baseline_60d    | +256.69% | **+2.22** | 132 | ✅ |
| early_gate_60d        | +257.25% | **+2.28** | 132 | ✅ |

### Observacje

1. **Momentum scorer NIE MA istotnej bear alphy** ani w baseline (+0.02) ani w gate (+0.33). Sharpe ratio 11.76× odzwierciedla tylko szum w dwóch near-zero sygnałach.
2. **Early-stage scorer ma istotną bear alphy** (+2.22), ale gate dodaje marginalnie (+2.22 → +2.28). Praktycznie zero improvement.

## Differenced regression: (gate − baseline) na Carhart-4F

Bezpośredni test czy gate dodaje cokolwiek ponad baseline w każdym regime:

### Momentum 60d (gate − baseline)

| Regime | n | Δ α ann | Δ α t-stat (HAC) | Istotny? (próg \|t\| > 1.5) |
|---|---:|---:|---:|:---:|
| Full | 944 | **−62.59%** | **−2.73** | ✅ **gate destroys alpha** |
| Bull | 397 | **−129.03%** | **−3.16** | ✅ **gate kills bull strongly** |
| Flat | 415 | −32.49% | −1.00 | ❌ |
| **Bear** | 132 | +24.76% | **+0.74** | ❌ **NOT significant** |

### Early-stage 60d (gate − baseline)

| Regime | n | Δ α ann | Δ α t-stat (HAC) | Istotny? |
|---|---:|---:|---:|:---:|
| Full | 944 | −15.94% | −0.92 | ❌ |
| Bull | 397 | +0.85% | +0.03 | ❌ |
| Flat | 415 | −26.16% | −1.25 | ❌ |
| **Bear** | 132 | **+0.57%** | **+0.02** | ❌ **zero** |

### Co to znaczy

1. **Bear gate edge nie istnieje statystycznie.** Momentum: Δα t=+0.74 (0 jest w przedziale ufności); early-stage: Δα t=+0.02 (zero). Gate nie dodaje istotnie alphy w bear w żadnym scorerze.
2. **Full-sample effect dla momentum jest znacząco NEGATYWNY** (Δα t=−2.73). Gate statystycznie niszczy alphę.
3. **Bull-regime effect dla momentum jest katastrofalnie negatywny** (Δα t=−3.16, −129% ann). To dominant effect determining full-sample outcome.
4. **Early-stage gate jest statystycznie nieodróżnialny od baseline** we wszystkich regimach. Zero signal, ani pozytywny, ani negatywny.

## Wnioski

**Phase 3B Sharpe ratios były wprowadzające w błąd:**
- Momentum bear ratio 11.76× = near-zero / near-zero; statystyczne zero
- Early-stage bear ratio 1.08× = 8% różnica na małym samplu; szum

**Regime-conditional gate deployment (Phase 3D) nie jest uzasadniony:**
- Nie ma żadnego regime, w którym gate dodaje istotną alphę
- Bull regime wyraźnie pokazuje: gate niszczy momentum alphę (Δα t=−3.16)
- Bear regime, który miał być "saved by gate", pokazuje Δα t=+0.74 (momentum) / +0.02 (early-stage) — oba statystycznie zero

**Close fundamental-gate family permanently.**

## Rewizja poprzednich konkluzji

**Perplexity review miał rację w spirit, ale nie w wynikach:**
- "Regime bias" jako hipoteza warta testu — ✅ prawda, warto było przetestować
- "Gate works in bear market" jako przewidywanie — ❌ falsified by HAC regression
- "Multiplicative spec causes failure" — ✅ factually false (penalties są additive) ale też nie poprawia nic (bo gate signal sam w sobie nie działa)

**Lekcja metodologiczna:** Sharpe ratio jest underpowered dla regime-split analysis:
- Sample size 132 dni → wide CI na Sharpe → dramatic-looking ratios łatwo powstają
- Sharpe ignoruje factor exposure → defensive portfolios "wygrywają" w bear bez alphy
- Zawsze robić Carhart-4F HAC na subsetach PRZED promowaniem regime-conditional strategy

## Artifacts

- Bear-subset Carhart: `/tmp/alphalens_issue15/phase3b1_bear_alpha.py`
- Differenced regression: `/tmp/alphalens_issue15/phase3b2_diff_alpha.py`
- Ten raport

## Follow-up

- Zamknąć issue #17 z final verdict FAIL
- Update memory: fundamental-gate family permanently closed
- Następne kierunki: alt data sources (insider, short interest), ML ranker trenowany na historical-acceptance panel, Layer 3 rejection-prediction classifier
