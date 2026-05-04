# v7 Phase B verdict — DRAFT pending multi-phase audit completion

**Pre-registration:** `v7_smd_options_implied_2026_05_02` (signal class
`options_implied_search_2026_05_02`).
**Date:** 2026-05-02 PM.
**Universe:** Single-phase verdict on 1258 cache (77%); multi-phase audit on
1449 (89%); final sanity run on 3099-parquet cache (Tier 1 1591/1626 = 97.8%
+ Tier 2 1479/1482 survivorship pool, all Tier 1 entered universe iteration).
αt trajectory across runs: 0.53→1.64→2.16→2.04 (audit p0)→**2.09 (final)**.
L/S αt: -1.00→-2.19→-2.78→-2.97→**-3.25 (final, exceeds Bonferroni 2.86 in NEGATIVE direction)**.

## Single-phase headline (phase_offset=0)

**Verdict: FAIL — αt=+2.16 < 2.86 program-level Bonferroni (n=14).**

| Metric | Value |
| --- | ---: |
| n holdout rebalances | 101 |
| Sharpe (gross) | 0.80 |
| Sharpe (net 30bps RT) | 0.80 |
| Carhart-4F α (gross, ann) | +212.55% |
| Carhart-4F α t-stat (HAC=5) | **+2.16** |
| Excess vs MDY (net, ann) | +66.28% |
| Max drawdown (net) | -45.48% |

The annualised α magnitude (+212%) is non-physical for a long-only equity
strategy — driven by holdout outliers in 2024-2025 mid-cap rally. The αt-stat
is the metric pre-reg gates against; it correctly registers the noise via HAC
adjustment.

## Lasso fit on train (2018-04-30 → 2024-04-29)

| Feature | Coef (standardized) | Sign vs Xing prior |
|---|---:|:---|
| ivp30 | +0.00361 | **flipped** |
| ivx30 | +0.01168 | **flipped** |
| ivx180_minus_ivx30 | 0 | zero |
| ivx30_over_hv20 | -0.00605 | agrees |
| reversal_1m | +0.00067 | (positive — typical reversal) |
| momentum_6m | 0 | zero |
| rv_30d | +0.00492 | (positive — flipped from low-vol-anomaly) |

n_train = 167,583 obs across 1258 tickers × 6y train × 50 rebals/y.
CV-MSE = 0.0789 (residual std 0.28; raw 20d returns even after winsorization).
Lasso α (penalty) = 0.000888.

## L/S diagnostic — strongly NEGATIVE

| Metric | Value |
|---|---:|
| L/S Sharpe (gross) | -1.00 |
| L/S Carhart-4F α (gross, ann) | -921% |
| L/S α t-stat (HAC=5) | **-2.78** |

L/S = (top-decile by Lasso prediction) − (bottom-decile). αt(L/S) = -2.78
near the -2.86 Bonferroni threshold means the spread is significantly
NEGATIVE: top decile (HIGH-IV per Lasso's positive coef) under-performs
bottom decile (LOW-IV) in holdout 2024-2026. **This empirically confirms
Xing 2010's negative-IV-return prior IN THE HOLDOUT** — but Lasso learned
the opposite direction from the train period (2018-2024).

## Sign-flip robustness across 4 sample sizes

| Run | Tier 1 | n_train | ivx30 | ivp30 | αt primary | αt L/S |
|---|---:|---:|---:|---:|---:|---:|
| Partial (243) | 243 | 36,276 | +0.0017 | 0 | n/a (buggy) | n/a |
| Marginal-1 (495 buggy 20d) | 495 | 70,581 | +0.0149 | 0 | +3.02 (bug) | +2.40 |
| Marginal-2 (552 fixed) | 552 | 77,611 | +0.0136 | 0 | +0.53 | -1.00 |
| Marginal-3 (1018) | ~1018 | 138,973 | +0.0128 | +0.0026 | +1.64 | -2.19 |
| **Real (1258)** | **1258** | **167,583** | **+0.0117** | **+0.0036** | **+2.16** | **-2.78** |

**ivx30 sign-flip is robust** across all sample sizes (always +0.011..0.015).
**ivp30 progressively becomes nonzero and flipped** as sample grows.
**L/S αt monotonically more negative** as sample grows.
**Primary αt rises with n_train** but cannot exceed L/S magnitude — the
positive primary is market-beta + size noise, not cross-sectional alpha.

## Auto_pivot diagnostic flag fired

Pre-reg `auto_pivot_triggers` includes:
> "Lasso flips sign vs literature prior across phases → diagnostic flag, no
> auto-pass; document in verdict memo."

`lasso_sign_alignment(fit)` returns:
```
{ivp30: flipped, ivx30: flipped, ivx180_minus_ivx30: zero,
 ivx30_over_hv20: agrees, any_options_flipped: True}
```

This is documented per pre-reg discipline. Do NOT pivot strategy direction
post-hoc to chase the holdout signal (LOW-IV winning) — that would be HARKing.

## Multi-phase audit (5 phases, offsets 0..4) — COMPLETE 2026-05-02 22:11

| Phase | αt | Sh_gross | Sh_net | Excess gross ann | Excess net ann |
|---:|---:|---:|---:|---:|---:|
| 0 | +2.04 | 0.75 | 0.74 | +62.4% | +62.1% |
| 1 | +2.93 | 2.07 | 2.07 | +123.5% | +123.2% |
| 2 | +2.95 | 2.22 | 2.21 | +56.3% | +56.0% |
| 3 | +1.90 | 1.66 | 1.65 | +115.1% | +114.8% |
| 4 | +3.20 | 2.21 | 2.20 | +84.8% | +84.5% |
| **MEAN** | **+2.60** | 1.78 | 1.77 | +88.4% | +88.1% |
| **±std** | ±0.59 | ±0.62 | ±0.62 | ±29.4pp | ±30.3pp |

**Phase dispersion (excess_net_ann): 123.2% − 56.0% = 67.2pp**
**EXCEEDS pre-reg gate <50pp → FAIL trigger.**

Robust verdict per `alphalens.backtest.multi_phase.robust_verdict()`:
**FAIL**.

Reasons:
1. Mean αt across 5 phases = +2.60 < 2.86 program-level Bonferroni (n=14)
2. Phase dispersion 67.2pp > 50pp gate on excess_net_ann
3. Phase 3 αt = +1.90 < 1.96 single-phase significance even at unadjusted level

The 67.2pp dispersion shows the strategy is HIGHLY phase-dependent:
shifting the rebalance calendar by 1-4 days changes annualised excess by
67 percentage points. This is economic-fragility in the sense pre-reg
guards against.

Sign-flip diagnostic (per-phase, from per-phase markdown reports):
- All 5 phases consistently show ivx30 + ivp30 with POSITIVE
  (flipped-vs-Xing) coefficients
- ivx30_over_hv20 consistently NEGATIVE (agrees with Xing)
- ivx180_minus_ivx30 mixed across phases

The flipped sign on raw vol-level features is robust across all 5 phases.
Pre-reg auto_pivot trigger "Lasso flips sign vs literature prior across
phases → diagnostic flag" FIRES.

Files:
- `docs/research/multi_phase_audit.json` — full audit aggregate
- `/tmp/audit_multi_phase_experiment_v7_options_implied_p{0..4}.md` — per-phase

## Pre-reg discipline summary

| Pre-reg gate | Status |
|---|:---|
| Phase A coverage ≥ 70% non-NaN | ✅ 100% |
| Phase A multicollinearity max \|corr\| <0.85 | ✅ 0.59 |
| Phase B Lasso non-zero options coefs | ✅ 3/4 |
| Phase B holdout αt ≥ 2.86 (program-level Bonferroni) | ❌ +2.16 |
| Phase B holdout αt ≥ 1.96 (within-class threshold) | ✅ +2.16 |
| Phase B Lasso sign-flip diagnostic | ⚠ FIRED (ivp30, ivx30 flipped, all 5 phases) |
| L/S diagnostic sign | ⚠ Strongly NEGATIVE αt=-2.78 |
| Phase dispersion <50pp on excess_net_ann | ❌ 67.2pp (exceeds gate) |
| Multi-phase robust mean αt ≥ 2.86 | ❌ +2.60 |

## Final verdict (multi-phase robust)

**FAIL — confirmed by 5-phase audit on frozen 1449 Tier 1 cache.**

Pre-reg gates that triggered FAIL:
1. Mean αt (multi-phase) = +2.60 < 2.86 program-level Bonferroni
2. Phase dispersion = 67.2pp > 50pp on excess_net_ann
3. Phase 3 αt = +1.90 below even single-test |t|≥1.96 significance
4. Sign-flip on ivx30 + ivp30 robust across 5 phases (auto_pivot diagnostic)
5. L/S spread αt = -2.78 strongly negative — Xing 2010 prior REPLICATES in
   holdout 2024-2026, but Lasso fitted on train (2018-2024) learned the
   OPPOSITE direction → the strategy is structurally on the wrong side of
   the cross-section.

## Provisional verdict (superseded by multi-phase above)

**Single-phase verdict: FAIL.**

Pre-reg `success_criteria.fail_rule`: "anything not clearing primary → FAIL,
log to ledger". αt=+2.16 < 2.86 → FAIL.

The empirical observation (Lasso sign-flip on train + Xing-direction in
holdout) is a SIGNIFICANT FINDING worth documenting:
1. Vol-risk-premium effects (high IV → low future return) vary by regime;
   2018-2024 train may have been a bull-trap regime where high-IV stocks
   recovered from drawdowns and outperformed.
2. Lasso CV without explicit regime-conditioning will overfit to train
   regime; holdout regime can be opposite.
3. Pre-reg discipline (commit direction ex-ante via "long top decile by
   Lasso prediction" without checking sign of fit) was tested and held —
   we report the FAIL honestly without HARKing the direction.
4. The sign-flip + opposite-direction-in-holdout is a MORE INTERESTING
   finding than a simple FAIL would be: it suggests that a regime-aware
   variant (e.g. fit Lasso separately per VIX-regime) might capture the
   actual Xing premium that exists in low-vol-following-stress periods like
   2024-2026.

## Ledger entry (when multi-phase confirms)

```
alphalens preregister complete v7_smd_options_implied_2026_05_02 \
    --verdict FAIL --alpha-t 2.16 \
    --notes "Single-phase FAIL αt=+2.16 < 2.86 Bonferroni; sign-flip diagnostic FIRED on ivp30+ivx30; L/S diagnostic strongly negative αt=-2.78 confirms Xing prior replicates in holdout but Lasso-on-train learned opposite (regime-shift overfitting); 1258 Tier 1 universe = 77% PIT coverage."
```

## Next program-level Bonferroni

After v7 completion: n=15, next critical |αt| = 2.95.

## Files

- **Final canonical Phase B**: `docs/research/v7_phase_b_holdout_FINAL_full_3099.json` (= `v7_phase_b_holdout.json` linked copy)
- **Multi-phase audit JSON**: `docs/research/multi_phase_audit.json` (5-phase robust verdict, frozen 1449 cache)
- **Verdict memo**: this file
- **Pre-reg JSON**: `docs/research/preregistration/params_v7_smd_options_implied_2026_05_02.json`
- **Design memo**: `docs/research/v7_options_implied_design_2026_05_01.md`
- **Archived intermediate runs**: `docs/research/v7_archive/` — 4 marginal trajectory JSONs + 1 buggy + 2 audit phase 0 archives, kept for trajectory reproducibility
