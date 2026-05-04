# v8 literature-direct verdict — FAIL multi-phase robust

**Pre-registration:** `v8_literature_direct_options_implied_2026_05_03`
**Class:** `options_implied_search_2026_05_02` (3 tests within class)
**Date:** 2026-05-03
**Universe:** Tier 1 cache (1626 tickers; 108k feature rows × 100% ivp30 coverage)

## Headline (LONG TOP decile EW by score = -ivp30, MDY benchmark, 30bps RT cost)

| Phase | αt | Sharpe net | excess_net_ann |
|---:|---:|---:|---:|
| 0 | +2.08 | 2.05 | +627.3% |
| 1 | +2.41 | 1.83 | +497.6% |
| 2 | +2.14 | 1.54 | +1373.4% |
| 3 | +2.28 | 1.93 | +531.0% |
| 4 | +1.99 | 1.47 | +866.4% |
| **MEAN** | **+2.18** (±0.17) | 1.76 (±0.25) | **+779.1%** (±362.2pp) |

**Robust verdict: FAIL.**

Reasons:
1. **Mean αt = +2.18 < 2.95** program-level Bonferroni n=15 (1-tailed α=0.05/15)
2. **Every phase falls below 2.95**; max single-phase αt = +2.41
3. **Phase dispersion on `excess_net_ann` = 875.8pp** ≫ 50pp gate (10× v7's 67.2pp dispersion — partly an artefact of high per-rebal annualization × 50.4 on outlier-heavy 1d-forward returns; underlying αt dispersion is actually tighter than v7's, range 0.42 vs v7's 1.30)
4. **Single-phase verdict** (phase 0 result memo): FAIL αt=+2.08 < 2.95 — locked the headline before audit completed

L/S diagnostic (top − bottom decile by score = -ivp30):

| Metric | Value |
|---|---:|
| Sharpe (net 60bps RT) | -0.78 (single-phase 0) |
| Carhart-4F α t-stat | -0.30 (single-phase 0) |

L/S essentially flat — much weaker than v7's αt = -2.78. Cross-decile spread on raw `ivp30` is small in this regime; v7's L/S decomposition implied a +4.94 long-only αt for "bottom-by-Lasso" but pure ivp30 sort recovers only +2.18 mean. The Lasso's equity-control weighting (positive `reversal_1m`, positive flipped `rv_30d`) was doing material lifting on the bottom-decile that the model-free scorer cannot replicate.

## Comparison to v7

| Metric | v7 (Lasso, sign-flip diag fired) | v8 (model-free, -ivp30) |
|---|---:|---:|
| Mean αt across 5 phases | +2.60 (±0.59) | +2.18 (±0.17) |
| αt range (max − min) | 1.30 | 0.42 |
| `excess_net_ann` dispersion | 67.2pp | 875.8pp |
| Sharpe net mean | 1.77 | 1.76 |
| Sign-flip diagnostic | FIRED on ivx30+ivp30 ALL 5 phases | N/A (no fit) |
| L/S spread αt | -2.78 (Xing direction in holdout) | -0.30 (effectively flat) |
| Verdict | FAIL multi-phase | FAIL multi-phase |

**αt dispersion is materially TIGHTER in v8** — removing the optimizer eliminated the per-phase fit-instability that v7 exhibited, which was the main argument for option E in the perplexity adversarial review. But the mean αt LEVEL is also lower (+2.18 vs +2.60), confirming perplexity's prediction that "a sign-flip from +2.60 to ≈2.50 shrinks mean alpha but does not mechanically reduce the standard deviation of per-phase alphas." We saw the αt-dispersion shrink, but the mean dropped further than that decomposition implied — Lasso's equity-control weighting was doing more work than just fixing the sign.

## Pre-reg discipline summary

| Pre-reg gate | Status |
|---|:---|
| ivp30 coverage ≥ 70% | ✅ 100% |
| Single-phase holdout αt ≥ 2.95 | ❌ +2.08 |
| Multi-phase mean αt ≥ 2.95 | ❌ +2.18 |
| Phase dispersion <50pp on excess_net_ann | ❌ 875.8pp |
| ALL phases αt ≥ 1.96 single-test | ✅ min +1.99 (vs v7 phase 3 +1.90) |

## Strategic narrative

Xing 2010 / Bali-Hovakimian 2009 NEGATIVE-IV-return effect IS empirically present in 2024-04-30 → 2026-04-30 — this v8 confirms it (mean αt +2.18 across 5 phases, all positive, low αt-dispersion ±0.17). But the magnitude is **insufficient** for program-level Bonferroni (n=15, |t|≥2.95) on this burnt holdout.

Pre-reg discipline held. v8 was committed before the audit ran (ledger `add` 2026-05-03 12:54). No HARKing — primary scoring rule was deterministic `-ivp30`, locked ex-ante, no choice among 4 options features post-v7-results.

Most informative finding: **αt dispersion collapsed from 1.30 to 0.42** when the optimizer was removed. v7's per-phase instability was real and traceable to Lasso's CV-α selection drifting under regime-shift. v8 confirms perplexity's diagnostic: "no model fit = no overfitting surface." But removing the optimizer also removed the equity-control weighting that materially lifted v7's bottom-decile alpha — clean signal in either direction is lower than v7's L/S decomposition implied.

## Pre-reg discipline (no auto-pivot)

Per pre-reg `auto_pivot_triggers`: dispersion >50pp on excess_net_ann → FAIL. The trigger fired (875.8pp). No re-running with relaxed gate. No swap to `ivx30` or other v7 options features (HARKing on within-class result space).

Per pre-reg `secondary_post_hoc.deferred = true`: sign-constrained Lasso (option A from plan) is moot — primary FAILed, secondary not run.

## Capital deploy

Per `capital_deploy_clause`: OFF-TABLE on this burnt holdout regardless of verdict. Even a hypothetical PASS would have required prospective walk-forward replication post-2026-04-30. v8 FAIL → standard FAIL ledger entry, no escalation.

## Program-level multiplicity

After v8 completion: **n=16** in cumulative program-wide Bonferroni count. Next test in any class needs **|αt| ≥ 3.04** (naive Bonferroni n=16 at α=0.05).

## Files

- **Multi-phase audit JSON**: `docs/research/v8_multi_phase_audit.json`
- **Single-phase headlines**: `/tmp/audit_multi_phase_experiment_v8_literature_direct_p{0..4}.md`
- **Pre-reg JSON**: `docs/research/preregistration/params_v8_literature_direct_options_implied_2026_05_03.json`
- **Design memo**: `docs/research/v8_literature_direct_design_2026_05_03.md`
- **Verdict memo**: this file

## Ledger entry

```
alphalens preregister complete v8_literature_direct_options_implied_2026_05_03 \
    --verdict FAIL --alpha-t 2.18 \
    --notes "5-phase robust mean αt=+2.18 (±0.17, all 5 phases 1.99-2.41) < 2.95 program-Bonferroni n=15. Dispersion 875.8pp on excess_net_ann >> 50pp gate. αt dispersion (0.42) is 3x tighter than v7's (1.30) — removing optimizer killed regime-shift overfit, but mean αt also dropped (+2.60 → +2.18) because Lasso's equity-control weighting was doing material lifting beyond just sign correction. Xing 2010 effect IS present in 2024-2026 holdout (all phases positive) but magnitude insufficient for n=15 Bonferroni. Class options_implied_search 2/3 FAIL (v7 + v8). Program n=16, next |t|≥3.04."
```
