# v5 design — Lasso L1 + cross-sectional rank-transformed target

**Status:** REVISED 2026-05-01 PM after zen + perplexity adversarial review.
Original draft proposed outer-loop CV-objective swap (argmax rank-IC); zen flagged
this as inner/outer mismatch (Lasso coord-descent inner solver still minimizes
MSE+L1 → still squashes to zero). Pivoted to **target transformation** approach.

**Class:** Same-class ablation of `alt_data_screener_search_2026_04_30` (n=3 → |t|≥2.39).

## Hypothesis (one-liner)

The 4-of-4 zero-coef pattern across this target+universe+holdout combo (prior class
v2/v3 + alt_data v2) reflects a **target-distribution failure**, not a feature-content
failure. Heavy-tailed forward-return targets cause MSE+L1 to find tail-magnification
penalties dominate variance reduction, driving λ to maximum regularization. By
transforming y to per-asof cross-sectional rank percentiles BEFORE training (range
centered at 0, std ≈ 0.29), the Lasso sees a bounded, near-uniform target where
modest cross-sectional ordering signal can survive L1 regularization.

## What changes vs v4 v2

| Variable | v4 v2 | v5 |
|---|---|---|
| Features | 10-feature alt-data whitelist | **same 10 features** (no post-hoc swap) |
| Target raw | 20d-forward excess return | **same 20d-forward excess return** |
| **Target transform** | none (raw return as y) | **per-asof percentile rank − 0.5** |
| Universe + ADV | AlphaLens PIT, ADV≥$5M | **same** |
| Train | 2018-01-01 → 2024-04-29 | **same** |
| Holdout | 2024-04-30 → 2026-04-30 | **same** |
| Stride / holding / overlap | 5d / 20d / 4-tranche | **same** |
| Cost / HAC / Sharpe | 30bps / maxlags=5 / Lo-2002 | **same** |
| Model class | Lasso L1 (sklearn) | **same Lasso L1** |
| CV | argmin mean fold MSE, 3-fold expanding, 60d embargo | **same** |
| **Selection rule** | top-30 long by predicted score | **same top-30** |

ONE variable changes: **the target is rank-transformed before fitting**. All
sklearn machinery, all CV machinery, all evaluation machinery is unchanged.

## Detailed specification

### Target transformation (the only change)

For each asof slice in the train pool:
1. Compute forward 20d excess return for each ticker (as in v4 v2).
2. Group by asof.
3. Within each asof slice, replace the raw return with `(percentile_rank - 0.5)` where
   `percentile_rank = scipy.stats.rankdata(y_asof, method='average') / n_asof`.
   This produces a target in `[-0.5 + 1/n, +0.5 - 1/n]` with mean 0, std ≈ 0.29.
4. Asof slices with fewer than 3 tickers are excluded (rank degenerate).

The Lasso then minimizes
```
mean_squared_error(rank_target, X @ coef) + lambda * |coef|_1
```
over the train pool. The resulting fit predicts cross-sectional rank, NOT return.

### Why this is mathematically aligned

Minimizing MSE between predictions and per-asof rank-percentile target is
equivalent (up to a constant) to maximizing the Pearson correlation between
predictions and ranks, which is the definition of Spearman rank correlation. So
the inner solver and the implicit outer objective are aligned: both push toward
rank-IC.

### Selection rule (unchanged from v4 v2)

At each holdout asof, score every ticker by Lasso(X). Take top-30 by score, equal-
weight, hold 20d. The score is in the rank-target's units, but ordering is what
matters.

## Pass / fail criteria (in-class n=3 → |t|≥2.39)

| Gate | Pass-rule | Source |
|---|---|---|
| Carhart-4F α t-stat (HAC=5) | ≥ 2.39 | Bonferroni-adjusted; same as v4 v2 |
| Lo-2002 Sharpe (net) | ≥ 0.5 | same as v4 v2 |
| α annualized (gross) | ≥ 3% | same as v4 v2 |
| MaxDD (net) | ≥ −35% | same as v4 v2 |
| ≥1 nonzero coef | ≥ 1 / 10 | same as v4 v2 (retained but no longer the primary diagnostic) |
| **In-CV IR (mean fold IC / std fold IC) ≥ 1.0** | NEW per zen Obj 3 | Catches "argmax picks noise-overfit λ" failure mode |
| **Holdout mean per-asof rank-IC > 0** | NEW per zen + perplexity | Out-of-CV-fold sanity check |
| MID rule | α t ≥ 1.5 AND Sharpe ≥ 0.3 AND ≥1 coef AND IR ≥ 0.5 AND holdout IC > 0 | refine + re-pre-register |
| FAIL rule | anything else | log to ledger, document |
| Phase C (multi-phase) | only on PASS or MID | per pre-reg `phase_robustness_followup` |

The **In-CV IR gate** addresses zen Objection 3 + perplexity's spurious-pick concern:
even if the chosen λ has positive mean IC across 3 folds, if fold-to-fold
dispersion is high (std ≥ mean), the pick is noise. IR ≥ 1.0 requires consistent
positive IC across all 3 folds.

The **Holdout rank-IC gate** addresses perplexity's HARKing concern: the holdout
period is not used for λ selection, so observing positive rank-IC on holdout is
true out-of-sample evidence.

## Adversarial review summary (locked)

Stage 1 (zen / gemini-3-pro-preview, thinking_mode=high) raised:
- **Fatal flaw — original outer-loop CV swap had inner/outer mismatch** → ACCEPTED, pivoted to target transformation. Resolves the issue.
- ≥1 nonzero coef gate vacuous under rank-IC argmax → ACCEPTED → IR gate added.
- 3 folds too few for stable IR estimate → KNOWN; mitigated by IR ≥ 1.0 gate
  requiring all 3 folds positive.
- Constant-prediction asof slices → NaN handling → IMPLEMENTATION DETAIL.
- Spearman over Kendall (O(n log n) vs O(n²)) → CONFIRMED, using Spearman for
  IR computation only (target is Pearson-equivalent rank).
- Mean vs t-stat across folds → IR gate uses both (mean and std).

Stage 2 (perplexity / sonar-reasoning-pro, search_context_size=high) added:
- HARKing concern — 4 failed experiments → new objective → expected success →
  PARTIAL PUSHBACK. Project framework requires pre-reg before run; class-
  conditional Bonferroni already conservative (n=3 → 2.39); explicit pre-reg
  + falsification gates locked here. The HARKing concern is structural to any
  iterative research program; Bonferroni + pre-reg + the new IR/holdout-IC gates
  collectively mitigate.
- Spurious in-fold IC overfit risk → ACCEPTED. IR gate addresses.
- Permutation null test recommended → DEFERRED to multi-phase Phase C if Phase
  B clears the in-CV gates. Adds substantial compute (100 refits × 25 λs = 2500
  fits) without changing Phase B verdict logic.

## Why this might break the zero-coef pattern

The proposed design ALIGNS the model's inner optimizer with the strategy's
selection rule:
- Inner optimizer (Lasso coord descent): minimizes MSE on rank-percentile target.
- Implicit outer optimization (CV-MSE on rank-target): equivalent to maximizing
  fold-mean Spearman rank correlation with returns.
- Strategy selection: top-30 by score, where score is exactly what the Lasso
  was trained to produce.

By compressing the heavy-tailed return distribution to a bounded [-0.5, +0.5]
rank-percentile distribution, the Lasso sees a target where modest cross-sectional
ordering signal contributes meaningfully to MSE reduction. This may flip the
λ-selection from λ_max (zero coefs) to a non-trivial λ where useful coefs survive.

## Why this might NOT break the pattern

If the underlying features genuinely carry zero cross-sectional rank-IC at this
universe + horizon, then the rank-transformed y will also have weak signal. Lasso
will pick low-λ in noise, IR gate will fail, holdout rank-IC will be ~0. The
verdict will be FAIL, but with a STRONGER diagnostic than v4 v2: the bottleneck
is definitively features, not target shape.

This is the value of v5: it isolates the variable. Either it surfaces signal
(unlikely but high-value if it does), or it provides clean evidence that
features at this combo carry no rank signal, narrowing the search to model class
(LightGBM) or fundamentally different feature space.

## Architectural variable status (post-v5 outcomes)

- Architecture: 4-regime vs single-global SETTLED (prior class v1+v2 same)
- Horizon: 5d vs 20d SETTLED (prior class v2+v3 same)
- Feature content: 21 v1-v3 vs 10 v4 v2 SETTLED (both fail)
- **Target shape: raw-return vs rank-percentile** — v5 settles this
- Model class: linear vs nonlinear UNTESTED (would be next class)
- Universe: ADV≥$5M vs sub-universes UNTESTED

## Implementation plan

### Code changes (small)

1. **`alphalens/screeners/multi_source_two_stage/model.py`**:
   - Add `target_transform: Literal['none', 'rank']` parameter to `fit_global`
     (backwards-compatible default 'none').
   - When transform='rank', apply per-asof rank-percentile transformation to y
     before fitting.
2. **`alphalens/screeners/multi_source_two_stage/target.py` or new module**:
   - Add `rank_transform_per_asof(y, asof_series)` helper.
3. **`scripts/experiment_alt_data_lasso_rankic_20d.py`** (new driver):
   - Clone of `experiment_alt_data_lasso_20d.py`.
   - Pass `target_transform='rank'` to `fit_global`.
   - Add IR gate computation + holdout rank-IC computation in `_assess`.
   - Update verdict gate logic.
4. **Tests:**
   - `tests/test_rank_transform.py` for the target transformation helper.
   - `tests/test_fit_global_rank_target.py` for the new fit_global parameter.

### Phase A smoke

Same approach as v4 v2: 21 R2000-mid-cap tickers + tighter window + verify IR
computation works end-to-end.

### Phase B production

Reuse cached PIT data (Polygon SI, companyfacts, prices). Just rerun with new
script. No additional bulk fetches needed.

### Phase C (multi-phase) — only if Phase B PASS or MID

5 phase offsets per pre-reg. Run on holdout-equivalent windows.

## Honest expectation

If zen's diagnostic is right (target shape is the bottleneck), v5 should produce
non-trivial nonzero coefs and SOME alpha t. Whether that t clears 2.39 is
uncertain — the prior 4-experiment evidence suggests features carry weak signal
even when properly aligned with selection rule, so MID is more likely than PASS.

If zen's diagnostic is wrong (features are the real bottleneck), v5 fails the
IR gate and/or holdout rank-IC gate, providing clean evidence that the next
class should explore model class (LightGBM) or different feature space, NOT
further target/objective tweaking.

Either way, v5 generates a definitive answer where v4 v2 was ambiguous.
