# Phase 1B: Multiple-testing correction audit — 2026-04 research program (issue #18)

**Date:** 2026-04-22
**Script:** `/tmp/alphalens_issue18/phase1b_bonferroni.py`
**Method:** Catalog 26 primary hypothesis tests (FF3/Carhart/CAPM α t-stats + IC t-stats) from all 2026-04 research docs. Apply 3 correction schemas: Bonferroni (family-wise error rate), Benjamini-Hochberg FDR (false discovery rate), Hommel (step-up).

## Setup

- N = 26 primary tests (exclude regime subsets, rolling stats, decile breakdowns, sensitivity scans)
- Family-wise α = 0.05
- Bonferroni α_adj = 0.05/26 = **0.0019** → t > **3.11** (2-sided, df ≈ n − 5)
- Raw 5% threshold: t > 1.96

## Results

### Top of rank — what survives correction

| Test | t-stat | p_raw | p_bonf | p_bh | p_hommel | Raw 5% | Bonf | BH | Hom |
|---|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|
| base_momentum_5d_**IC** | 3.97 | 0.0001 | 0.0020 | 0.0020 | 0.0020 | ✅ | **✅** | ✅ | **✅** |
| base_momentum_60d_**IC** | 3.51 | 0.0005 | 0.0122 | 0.0061 | 0.0117 | ✅ | **✅** | ✅ | **✅** |
| augmented_momentum_5d_FF3 | 2.99 | 0.0029 | 0.0744 | 0.0186 | 0.0601 | ✅ | ❌ | ✅ | ❌ |
| augmented_momentum_5d_Carhart | 2.99 | 0.0029 | 0.0744 | 0.0186 | 0.0601 | ✅ | ❌ | ✅ | ❌ |
| base_momentum_60d_FF3 | 2.66 | 0.0079 | 0.2066 | 0.0290 | 0.1510 | ✅ | ❌ | ✅ | ❌ |
| base_momentum_60d_Carhart | 2.66 | 0.0079 | 0.2066 | 0.0290 | 0.1510 | ✅ | ❌ | ✅ | ❌ |
| base_momentum_5d_FF3 | 2.62 | 0.0089 | 0.2322 | 0.0290 | 0.1697 | ✅ | ❌ | ✅ | ❌ |
| base_momentum_5d_Carhart | 2.62 | 0.0089 | 0.2322 | 0.0290 | 0.1697 | ✅ | ❌ | ✅ | ❌ |
| base_early_60d_Carhart | 1.96 | 0.0503 | 1.0 | 0.134 | 0.313 | ❌ | ❌ | ❌ | ❌ |
| (more tests, all below raw threshold) | | | | | | ❌ | ❌ | ❌ | ❌ |

### Summary by strategy category

| Strategy | N tests | Raw 5% | Bonferroni | BH FDR | Hommel |
|---|---:|---:|---:|---:|---:|
| **baseline-themed** | 12 | **6** | **2** | 6 | **2** |
| fundamental-gate (pre + post-fix) | 12 | **0** | **0** | **0** | **0** |
| augmented-universe (Test B) | 2 | 2 | 0 | 2 | 0 |

## Interpretacja

### 1. Gate family: ZERO tests pass correction (or even raw 5%)

Wszystkie 12 gate tests (pre-fix + post-fix, momentum + early-stage, 5d + 60d) **failują nawet raw 5% threshold** (max α t = 1.61 pre-fix, 1.54 post-fix). Close-family verdict z #14/#15/#17 pozostaje **ultra-robust** niezależnie od correction schema.

### 2. Baseline momentum: IC survives, α does NOT

- **Rank IC t-stat (3.97 / 3.51)**: passes Bonferroni i Hommel (strictest). Ranking quality jest real significant signal.
- **Portfolio α (FF3 i Carhart, 2.62-2.66)**: fails Bonferroni i Hommel. Pass only BH FDR (less strict).

**Co to znaczy?**
- Scorer ma real cross-sectional edge — potrafi ranking'ować correct'ly (IC)
- Ale top-5 portfolio α t-stat nie reaches strictest multiple-testing threshold — signal concentrates w małej liczbie names, α estimator ma szeroki CI
- **"Layer 2b validated alpha" claim w obecnej formie** (opartej na FF3 α t=2.62) **nie jest stabilny pod strict correction**

### 3. Augmented universe Test B α = 2.99 — fails Bonferroni

Memory finding "Sharpe 1.49→1.75, α t 2.62→2.99" — po Bonferroni: p_bonf = 0.0744 > 0.05 → **nie survives**. Phase 1A pokazało że to ≥50% M&A selection, więc finding jest doubly suspect:
- Statystycznie niestable pod correction
- Mechanistycznie questionable (selection bias not survivorship correction)

### 4. Early-stage baseline: nie pass nawet raw 5%

- base_early_5d_FF3 t=1.86, p_raw=0.063
- base_early_60d_FF3 t=1.95, p_raw=0.052
- IC t=1.66, p_raw=0.097

Żadne nie przekracza uncorrected 5% threshold. Early-stage jako "validated" nie był.

## Implikacje

### Dla gate family

**Close-family verdict STRENGTHENED.** Już było FAIL w #14/#15/#17 na Sharpe i pre-fix α. Teraz formalnie confirmed: ani jedno pre- ani post-fix gate α test reaches raw 5% threshold, tym bardziej żadna correction. Rodzina martwa niezależnie od angle'a.

### Dla baseline momentum

**"Validated alpha" w obecnej formie (α t=2.62) nie survives strict multiple-testing correction.**

Co zostaje:
- ✅ **IC quality solidne**: mean Rank IC t-stat 3.5-4.0 survives all corrections
- ❌ **Portfolio α t-stat NIE survives Bonferroni/Hommel** (tylko BH)

**Rewizja claim'u:**
- OLD: "Layer 2b validated with Carhart α t=2.62 HAC, Sharpe 1.49-1.57"
- NEW: "Layer 2b has significant ranking quality (IC t ~3.5-4.0 survives Bonferroni); portfolio-level alpha claim requires OOS validation (Phase 2) because in-sample α t-stat does not survive strict multiple-testing correction"

### Dla early-stage

Formally confirmed: early-stage baseline **never was statistically validated**, even uncorrected (p_raw = 0.05-0.10). Live deployment `--scorer early-stage` scheduling (z memory: "production scheduled from 2026-04-21") requires **immediate status downgrade** to "paper/live experimental pending OOS".

### Dla augmented universe (Test B)

Finding unreliable on two grounds:
1. Phase 1A: ≥50% M&A selection effect
2. Phase 1B: t=2.99 fails Bonferroni correction

Memory `project_survivorship_probe` interpretation "survivorship bias jest w odwrotnym kierunku, curated universe ma NEGATYWNY bias" → **NOT JUSTIFIED**. Prawidłowa interpretacja: nieznany kierunek biasu, wymaga true PIT reconstruction (Phase 3).

## Decision dla Phase 1

### Phase 1 combined verdict

| Hypothesis | Phase 1A + 1B combined verdict |
|---|---|
| Fundamental-gate family dead | **CONFIRMED** (ultra-robust across all corrections) |
| Layer 2b "validated alpha" | **INVALIDATED** (only IC survives Bonferroni, α fails) |
| Augmented universe Sharpe boost = survivorship correction | **REJECTED** (M&A selection + Bonferroni fail) |
| Early-stage baseline validated | **NEVER WAS** (p_raw > 0.05 even uncorrected) |

### Paths forward

1. **Phase 2 (OOS validation) = CRITICAL, not confirmatory.**
   - If OOS baseline momentum α t > 1.5 (even without multiple-testing correction) AND OOS IC t > 2 → meaningful OOS evidence worth persisting strategy
   - If OOS α t ≤ 0 → close themed screener

2. **Early-stage scorer status downgrade** — don't await Phase 2, update memory + live config immediately

3. **Memory updates:**
   - `project_themed_screener_design` — downgrade "validated" status
   - `project_survivorship_probe` — reinterpret as M&A selection
   - `project_early_stage_paper_trade_1` — already cautious, fine

### Non-actions (deliberate restraint)

- Nie close'ować themed screener na Phase 1 evidence — IC survives, signal real
- Nie kończyć Layer 2b pipeline'u — paper/live deployment can continue bez capital
- Nie reopen'ować gate family — Phase 1B confirms dead unambiguously

## Artifacts

- `/tmp/alphalens_issue18/phase1b_bonferroni.py` — compute script
- This file: `docs/research/multiple_testing_audit_2026-04.md`
- `docs/research/delisted_classification.md` — Phase 1A
- Next: Phase 2 walk-forward OOS validation
