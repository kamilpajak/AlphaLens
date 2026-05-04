# v9 Sign-Constrained Lasso Design — Xing Prior Mechanically Enforced

**Date:** 2026-05-03
**Pre-reg:** `docs/research/preregistration/params_v9_sign_constrained_options_implied_2026_05_03.json`
**Class:** `options_implied_search_2026_05_02` (4th hypothesis in class)
**Status:** PRE-REG LOCKED 2026-05-03

## Why this exists

v7 (Lasso, αt +2.60 mean) and v8 (model-free −ivp30, αt +2.18 mean) both FAIL'd multi-phase audits on the burnt 2024-04-30 → 2026-04-30 holdout. Program-Bonferroni stands at n=16 → next test |t|≥3.04.

**v7 final canonical run** (`docs/research/v7_phase_b_holdout.json`) revealed a critical decomposition:
- Long-only top-decile-by-Lasso αt = +2.09 (sign-flipped HIGH-IV)
- L/S diagnostic αt = **−3.25** (clears Bonferroni in NEGATIVE direction)
- Implied bottom-decile (LOW-IV per Xing) long-only αt ≈ **+5.34** if direction had been correct

The bottom-decile of v7's Lasso was multi-dimensional: low ivp30 ∩ low ivx30 ∩ HIGH ivx30/hv20 ∩ recent winners (low reversal_1m magnitude) ∩ low rv_30d. v8's pure −ivp30 sort captured only +2.18 — the gap is the equity-control + IV/HV-ratio amplification.

**v9 thesis.** Combine v7's equity-control magnitude lift with v8's mechanically-enforced Xing direction by sign-constraining ONLY the 4 options-feature coefficients to ≤ 0, leaving the 3 equity controls free-sign.

## Adversarial review

Perplexity Sonar Reasoning Pro (2026-05-03) ranked v9 redesign axes:

| Rank | Axis | Likelihood | Bonferroni cost |
|---:|---|---:|---|
| **1** | **A. Sign-constrained Lasso (this design)** | **~25-35%** | n=17, \|t\|≥3.13 |
| 2 | D. Cross-sectional residualization (deterministic) | ~20-30% | n=18, \|t\|≥3.20 (pre-committed secondary) |
| 3 | E. ElasticNet sign-constrained | ~15-20% | n=17 |
| 4 | B. Deterministic composite z-score | ~10-15% | HARKing on weights |
| 5 | C. Rolling sign-constrained Lasso | ~5-10% | multiplicity cascade |
| 6 | F. Gate criterion change | <5% | p-hacking |

**Honest expectation per perplexity Q5:** v9 αt likely lands at +2.5 to +2.9 on the burnt holdout — *below* the |t|≥3.13 threshold. Underlying signal triangulated from v7+v8 at ~+2.2 αt; sign-constraining improves robustness, not magnitude. **v9 is the best-defensible attempt at clearing the bar; FAIL is the expected outcome and would be informative confirmation that the options-implied class is exhausted on burnt holdout.**

## Mathematical construction

**Goal.** Force `coef_options ≤ 0` (Xing prior) while leaving `coef_equity` unconstrained. Standard Lasso APIs (`sklearn.linear_model.LassoCV(positive=True)`) only support all-or-nothing positivity; we use the standard "free-sign-as-pair" trick to express partial sign constraints with a single positive-only Lasso solver.

**Algorithm.**
1. Standardize the 7 ORIGINAL features → mean μ ∈ ℝ⁷, std σ ∈ ℝ⁷, X_std ∈ ℝⁿˣ⁷
2. Build augmented matrix X_aug ∈ ℝⁿˣ¹⁰:
   - Columns 0..3: NEGATED options features `−X_std[:, options_idx]`
   - Columns 4..6: equity-positive `+X_std[:, equity_idx]`
   - Columns 7..9: equity-negative `−X_std[:, equity_idx]`
3. Fit `LassoCV(positive=True, eps=1e-6, n_alphas=25, cv=3)` on X_aug → β_aug ≥ 0 (10-vector)
4. Map β_aug back to original-feature standardized coordinates:
   - `coef_options[j] = −β_aug[j]` (≤ 0 by construction, Xing-correct)
   - `coef_equity[j] = β_aug[4+j] − β_aug[7+j]` (free sign)

**At the L1 optimum,** at most one of {β_aug[4+j], β_aug[7+j]} per equity feature is nonzero, so the joint L1 penalty equals the standard Lasso L1 on the original equity coefficient. Mathematically equivalent to a custom solver with mixed sign constraints.

**The returned `GlobalLassoFit` has coefficients/scaler in ORIGINAL-feature standardized coordinates** so the existing `predict_scores(fit, original_features)` works unchanged. Same downstream pipeline as v7.

## Pre-commitments locked

| Choice | Value | Rationale |
|---|---|---|
| Model | sign-constrained LassoCV(positive=True) on augmented features | Combines v7 magnitude + v8 sign safety |
| Bonferroni | n=17 → \|t\|≥3.13 (primary), n=18 → \|t\|≥3.20 (secondary if primary FAILs) | One amendment per perplexity guidance |
| Dispersion gate | per-rebal αt-range ≤ 0.5 across 5 phases (PRIMARY) | Replaces 1d-fwd × 50.4 magnification-prone excess gate; legacy 50pp metric reported descriptively |
| Holdout | 2024-04-30 → 2026-04-30 (same burnt window) | Window-shopping = HARKing |
| Universe / cost / benchmark | 1626 Tier 1 cache, 30bps RT, MDY | Identical to v7/v8 (parity) |
| Selection-mechanism gate | ≥1 nonzero options coef required | Same as v7 — protects against degenerate alphabetical-first selection |

## Pre-committed SECONDARY (cross-sectional residualization)

Run ONLY IF primary (A) FAILs. Pre-committed in pre-reg JSON to avoid sequential p-hacking.

Per asof:
1. Cross-sectional OLS within asof: `−ivp30 ~ reversal_1m + momentum_6m + rv_30d + intercept`
2. Score = OLS residual (orthogonalized Xing signal)
3. Long top-decile EW by residual

**D is conceptually independent from A.** A is time-series (Lasso on pooled cross-sections); D is per-asof cross-sectional regression. Different statistical surfaces — independent test of the same Xing-direction hypothesis.

## Verdict rules

```
PRIMARY (A) PASS:   Holdout single-phase αt ≥ 3.13
                    AND multi-phase mean(αt) ≥ 3.13
                    AND alpha_t_range across 5 phases ≤ 0.5
                    AND ivp30 coverage ≥ 70%
                    AND ≥1 nonzero coef on options features

PRIMARY (A) FAIL:   Run pre-committed SECONDARY (D) — threshold |t|≥3.20

EITHER PASS:        Advance to prospective replication post-2026-04-30 at unadjusted p<0.05

BOTH FAIL:          options_implied class CLOSED on burnt holdout. Pivot OR wait for prospective.
```

## Citations

- Xing, Y., X. Zhang, and R. Zhao. 2010. "What Does Individual Option Volatility Smirk Tell Us About Future Equity Returns?" *Journal of Financial and Quantitative Analysis*.
- Bali, T. G., and A. Hovakimian. 2009. "Volatility Spreads and Expected Stock Returns." *Management Science*.
- Frazzini, A., and L. H. Pedersen. 2014. "Betting Against Beta." *Journal of Financial Economics*. (low-vol-anomaly equity-control direction)
- Jegadeesh, N. and S. Titman. 1993. "Returns to Buying Winners and Selling Losers." *Journal of Finance*. (momentum equity control)
- Tibshirani, R. 1996. "Regression Shrinkage and Selection via the Lasso." *Journal of the Royal Statistical Society Series B*. (sklearn LassoCV reference)
- Perplexity Sonar Reasoning Pro adversarial review, 2026-05-03 (v9 axis ranking + ceiling estimate).

## Engineering scope

- `alphalens/screeners/options_implied/sign_constrained.py` (NEW, ~110 LoC)
- `tests/test_options_implied_sign_constrained.py` (NEW, 10 tests, RED→GREEN)
- `scripts/experiment_v9_sign_constrained.py` (NEW, ~450 LoC; copy-modify of v7 with model swap)
- `scripts/audit_multi_phase.py` `_SCRIPTS` dict +1 entry
- 1644 baseline tests green throughout

## Reused infrastructure

All v7 modules unchanged: `features.py`, `target.py`, `model.py` (`GlobalLassoFit` shape preserved + `predict_scores` works on the returned fit), `ivolatility_smd_cache.py`, `cost_model`, `factor_analysis`, `multi_phase`. Only the model fit function is swapped.
