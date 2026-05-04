# v9 verdict — FAIL multi-phase robust (PRIMARY + SECONDARY both)

**Pre-reg:** v9_sign_constrained_options_implied_2026_05_03 (PRIMARY) + v9_cross_sectional_residual_options_implied_2026_05_03 (PRE-COMMITTED SECONDARY)
**Class:** `options_implied_search_2026_05_02` (4th + 5th hypothesis tests in class)
**Date:** 2026-05-03
**Universe:** 1626 Tier 1 cache (108k holdout feature rows × 100% ivp30 coverage)

## PRIMARY (A) — Sign-constrained Lasso

| Phase | αt | Sharpe net | excess_net_ann |
|---:|---:|---:|---:|
| 0 | +1.24 | 0.79 | +121.4% |
| 1 | +1.58 | 1.12 | +157.2% |
| 2 | +2.95 | 2.14 | +51.6% |
| 3 | +1.57 | 1.32 | +64.6% |
| 4 | +1.28 | 1.03 | +380.5% |
| **Mean** | **+1.72** (±0.70) | 1.28 (±0.52) | +155.1% (±133.1pp) |

**Robust verdict: FAIL.**

Reasons:
1. Mean αt = +1.72 << 3.13 program-Bonferroni n=17
2. αt-range = 2.95 − 1.24 = **1.71** ≫ 0.5 amended dispersion gate
3. Phases 0, 4 below 1.96 unadjusted single-test threshold
4. excess_net_ann dispersion 328.9pp on legacy (descriptive) metric

**Lasso fit (single-phase 0):**

| Feature | Coef (standardized) |
|---|---:|
| ivp30 | 0 (sign-constraint binds; data wanted positive) |
| ivx30 | 0 (sign-constraint binds; data wanted positive) |
| ivx180_minus_ivx30 | -0.0012 |
| ivx30_over_hv20 | -0.0006 |
| reversal_1m | +0.0028 |
| momentum_6m | 0 |
| rv_30d | **+0.0127** (largest absolute coef) |

**Diagnostic:** sign constraint mechanically zeroed ivp30+ivx30 (where v7 fit positive), removing the magnitude lift that v7's bottom-decile demonstrated. The rv_30d free-sign coef +0.0127 drove top-decile selection toward HIGH-realized-vol stocks — the OPPOSITE of low-vol-anomaly direction in holdout regime. v9 primary captured almost nothing of the Xing signal.

## SECONDARY (D) — Cross-sectional residualization

Pre-committed in v9 pre-reg JSON. Run only after PRIMARY FAILed (per `secondary_pre_committed.execution_rule`).

| Phase | αt | Sharpe net | excess_net_ann |
|---:|---:|---:|---:|
| 0 | +2.03 | 1.86 | +617.5% |
| 1 | +2.47 | 1.57 | +957.1% |
| 2 | +1.86 | 1.45 | +876.9% |
| 3 | +2.18 | 1.87 | +457.1% |
| 4 | +2.92 | 2.03 | +284.8% |
| **Mean** | **+2.29** (±0.42) | 1.76 (±0.23) | +638.7% (±281.4pp) |

**Robust verdict: FAIL.**

Reasons:
1. Mean αt = +2.29 < 3.20 program-Bonferroni n=18
2. αt-range = 2.92 − 1.86 = **1.06** > 0.5 amended dispersion gate
3. Phase 2 αt = 1.86 below 1.96 unadjusted threshold
4. excess_net_ann dispersion 672pp on legacy descriptive metric

**Diagnostic:** D produces the BEST mean αt across all four honest tests on this holdout (v7's +2.60 was a sign-flipped artifact, see v7 verdict memo). Cross-sectional residualization explicitly removes equity-control variance, so the signal is the cleaner orthogonalized Xing component. Result confirms underlying signal ~+2.2-2.3 αt.

## Comparison across all 4 holdout tests

| Run | Approach | Mean αt | αt-range | Sharpe net | Verdict |
|---|---|---:|---:|---:|---|
| v7 | Lasso (sign-flipped) | +2.60 | 1.30 | 1.77 | FAIL |
| v8 | Model-free −ivp30 | +2.18 | 0.42 | 1.76 | FAIL |
| v9 A | Sign-constrained Lasso | +1.72 | 1.71 | 1.28 | FAIL |
| **v9 D** | **Cross-sectional residual** | **+2.29** | 1.06 | 1.76 | FAIL |

## Underlying-signal triangulation

v8 (+2.18), v9 D (+2.29) — both honest, model-free / orthogonalized — converge on **~+2.2-2.3 αt** as the underlying low-IV-anomaly signal in this universe / 2024-2026 holdout regime. v7's +2.60 is the sign-flipped overfit upper bound, not the true signal.

**Threshold |t|≥3.20** for any further within-class test on this burnt holdout would require the next hypothesis to find +1pp of αt magnitude beyond what 4 independent specifications have triangulated. Per perplexity adversarial review (2026-05-03 Q5): *"You may be bumping into a real economic limit (the low-IV anomaly in your universe is real but modest), not a specification problem."*

## Pre-reg discipline summary

| v9 PRIMARY (A) gate | Status |
|---|:---|
| ivp30 coverage ≥ 70% | ✅ 100% |
| Single-phase αt ≥ 3.13 | ❌ +1.24 |
| Multi-phase mean αt ≥ 3.13 | ❌ +1.72 |
| αt-range ≤ 0.5 across 5 phases | ❌ 1.71 |
| ≥1 nonzero options coef | ✅ 2/4 |

| v9 SECONDARY (D) gate | Status |
|---|:---|
| ivp30 coverage ≥ 70% | ✅ 100% |
| Multi-phase mean αt ≥ 3.20 | ❌ +2.29 |
| αt-range ≤ 0.5 across 5 phases | ❌ 1.06 |

## Strategic verdict — options_implied class on burnt holdout

**4 hypotheses completed, 4 FAIL.** Class `options_implied_search_2026_05_02` is exhausted on the burnt 2024-2026 holdout. Per perplexity guidance: capital deploy off-table regardless of any verdict; further within-class refinement bumps program-Bonferroni higher without realistic prospect of crossing the bar.

Two paths forward (NOT decided in this verdict memo — for future planning):
1. **Pivot to fresh class** (analyst revisions / news sentiment / EDGAR text / intraday microstructure) — opens new feature space with potentially higher signal, new within-class multiplicity counter.
2. **Wait for prospective replication** (already-registered `v8_xing_frozen_direction_2026_05_03` track + new prospective tracks for v8/v9D) — accumulates fresh post-2026-04-30 data, ~2027-12 earliest n≥100. Most disciplined option per pre-reg policy.

## Program-level multiplicity

After v9 PRIMARY + SECONDARY completion: **n=18** in cumulative program-wide Bonferroni count. Next test in any class needs **|αt| ≥ 3.27** (naive Bonferroni at α=0.05).

Class `options_implied_search_2026_05_02`: 5 hypotheses registered, 4 completed FAIL (v7 + v8 + v9A + v9D), 1 prospective awaiting fresh data (`v8_xing_frozen_direction_2026_05_03`).

## Engineering artifacts (1653 tests green)

- `alphalens/screeners/options_implied/sign_constrained.py` (NEW, 110 LoC, 10 unit tests)
- `alphalens/screeners/options_implied/cross_sectional_residual.py` (NEW, 50 LoC, 9 unit tests)
- `scripts/experiment_v9_sign_constrained.py` (NEW, ~470 LoC)
- `scripts/experiment_v9_cross_sectional_residual.py` (NEW, ~330 LoC)
- `scripts/audit_multi_phase.py` `_SCRIPTS` dict +2 entries
- `docs/research/preregistration/params_v9_sign_constrained_options_implied_2026_05_03.json`
- `docs/research/v9_sign_constrained_design_2026_05_03.md`
- `docs/research/v9_multi_phase_audit.json` (PRIMARY)
- `docs/research/v9_cross_sectional_residual_audit.json` (SECONDARY)
- This verdict memo

## Take-aways

1. **Sign constraint zeroes v7-positive options coefs without recovering Xing magnitude.** The residual signal is dominated by equity controls (rv_30d), which fit a regime-mismatched HIGH-vol direction.
2. **Cross-sectional residualization (D) is the cleanest test of the orthogonalized Xing signal** — produces the best honest αt estimate (+2.29) of any v7/v8/v9 specification.
3. **Underlying signal triangulated at ~+2.2-2.3 αt** — convergent estimate from v8 (+2.18) and v9D (+2.29). Likely reflects real economic ceiling of low-IV anomaly in this universe / regime, not specification-induced bias.
4. **Pre-reg sequence discipline held.** PRIMARY locked FAIL before SECONDARY ran; both verdicts honest, no peek-and-pivot. Both contribute +1 to program-Bonferroni (n=16→17→18).
5. **Class options_implied_search_2026_05_02 is exhausted on burnt holdout** — 4/5 FAIL with one prospective still pending. Future Xing-direction validation requires fresh post-2026-04-30 data, not further refinement on the same window.
